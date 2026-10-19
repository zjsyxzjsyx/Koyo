"""Read-only checks of the configured Chat Completions endpoint and budget."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from .context import _model_budget, _optional_int
from .model import ModelContextOverflowError, OpenAICompatibleModel


def _positive_usage(usage: dict[str, Any], key: str) -> int | None:
    value = usage.get(key)
    return value if type(value) is int and value >= 0 else None


def check_model(model: OpenAICompatibleModel, calibrate: bool = False,
                boundary: bool = False) -> dict[str, Any]:
    """Probe with synthetic text; never include credentials, responses, or user data."""
    configured = _model_budget(model)
    budget = {key: configured[key] if key in configured else _optional_int(f"ZHIQIU_LLM_{env}")
              for key, env in (("window", "WINDOW"), ("output_reserve", "OUTPUT_RESERVE"),
                               ("margin", "MARGIN"))}
    present = all(value is not None for value in budget.values())
    limit = budget["window"] - budget["output_reserve"] - budget["margin"] if present else None
    if limit is not None and limit <= 0:
        raise ValueError("模型窗口预算无效：window 必须大于 output_reserve + margin")
    seed = "知秋上下文校准。" * 32
    system = {"role": "system", "content": "只回答：收到"}

    def send(content: str, *, max_tokens: int | None = None,
             timeout_seconds: float = 60, thinking_mode: str = "disabled") -> dict[str, Any]:
        messages = [system, {"role": "user", "content": content}]
        options = ({"max_tokens": max_tokens, "timeout_seconds": timeout_seconds,
                    "thinking_mode": thinking_mode} if max_tokens is not None else {})
        response = model.probe(messages, **options)
        usage = response["usage"]
        input_bytes = len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        prompt_tokens = _positive_usage(usage, "prompt_tokens")
        return {"input_bytes": input_bytes,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": _positive_usage(usage, "completion_tokens"),
                "total_tokens": _positive_usage(usage, "total_tokens"),
                "byte_proxy_covers_reported_prompt_tokens":
                    input_bytes >= prompt_tokens if prompt_tokens is not None else None,
                "reported_model": response["reported_model"]}

    first = send(seed)
    result: dict[str, Any] = {
        "connection_verified": True,
        "requested_model": model.model,
        "reported_model": first["reported_model"],
        "model_identity_verified": first["reported_model"] == model.model,
        "configured_budget": {**budget, "input_limit_tokens": limit,
                              "verified_claim_in_config": configured.get(
                                  "verified", os.getenv("ZHIQIU_LLM_WINDOW_VERIFIED") == "1")},
        "byte_estimate_method": "utf8_json_bytes; admission heuristic, not a token count",
        "samples": [first],
        "provider_token_count_verified": first["prompt_tokens"] is not None and first["prompt_tokens"] > 0,
        "sample_within_configured_budget": False,
        "budget_sample_verified": False,
        "window_size_verified": False,
        "verified_capacity_lower_bound_tokens": first["total_tokens"],
    }
    if not (calibrate or boundary) or limit is None or first["prompt_tokens"] is None or first["prompt_tokens"] <= 0:
        return result

    # Scale from the provider's observed token count. Cap input size so a bad
    # usage report or huge configured window cannot generate an unbounded call.
    target = int(limit * 0.75)
    if target <= first["prompt_tokens"]:
        result["calibration_status"] = "configured input limit is below the probe size"
        return result
    repeat_count = max(32, int(32 * (target / first["prompt_tokens"]) * 0.9))
    max_repeats = 250_000 // len("知秋上下文校准。".encode("utf-8"))
    capped = repeat_count > max_repeats
    repeat_count = min(repeat_count, max_repeats)
    result["sample_capped"] = capped
    content = "知秋上下文校准。" * repeat_count
    try:
        second = send(content)
    except ModelContextOverflowError:
        result["calibration_status"] = "provider rejected calibration sample as context overflow"
        return result
    result["samples"].append(second)
    result["reported_model"] = second["reported_model"]
    result["model_identity_verified"] = all(sample["reported_model"] == model.model for sample in result["samples"])
    actual = second["prompt_tokens"]
    if actual is not None:
        result["provider_token_count_verified"] = True
        result["verified_capacity_lower_bound_tokens"] = second["total_tokens"]
        result["sample_within_configured_budget"] = (
            actual <= limit and second["completion_tokens"] is not None
            and second["completion_tokens"] <= budget["output_reserve"]
            and second["total_tokens"] is not None
            and second["total_tokens"] <= budget["window"])
        result["budget_sample_verified"] = (result["sample_within_configured_budget"]
                                            and not capped and actual >= int(limit * 0.5))
        result["calibration_status"] = (
            "capped sample within budget; configured limit not tested" if capped and result["sample_within_configured_budget"]
            else "sample near configured limit passed" if result["budget_sample_verified"]
            else "sample did not establish budget near configured limit")
    else:
        result["calibration_status"] = "provider omitted prompt_tokens"
    if not boundary or actual is None or actual <= first["prompt_tokens"]:
        return result

    # Estimate the large request from the observed slope, then keep both the
    # request body and response wait bounded. This tests one practical budget
    # point; a short answer does not validate the entire output reservation.
    unit = "知秋上下文校准。"
    boundary_target = int(limit * 0.995)
    boundary_repeats = 32 + int((boundary_target - first["prompt_tokens"])
                                * (repeat_count - 32) / (actual - first["prompt_tokens"]))
    max_boundary_repeats = 6_000_000 // len(unit.encode("utf-8"))
    boundary_capped = boundary_repeats > max_boundary_repeats
    boundary_repeats = min(boundary_repeats, max_boundary_repeats)
    result["boundary_sample_capped"] = boundary_capped
    result["boundary_requested_output_reserve_tokens"] = budget["output_reserve"]
    result["output_reserve_parameter_accepted"] = False
    result["full_output_reserve_generated_verified"] = False
    if boundary_repeats <= repeat_count:
        result["boundary_status"] = "large sample would not exceed prior sample"
        return result
    try:
        boundary_sample = send(unit * boundary_repeats,
                               max_tokens=budget["output_reserve"], timeout_seconds=300,
                               thinking_mode="enabled")
    except ModelContextOverflowError:
        result["boundary_status"] = "provider rejected near-budget sample as context overflow"
        return result
    except (httpx.HTTPError, ValueError) as exc:
        result["boundary_status"] = "near-budget sample failed"
        result["boundary_error_type"] = type(exc).__name__
        if isinstance(exc, httpx.HTTPStatusError):
            result["boundary_http_status"] = exc.response.status_code
        return result
    result["samples"].append(boundary_sample)
    result["output_reserve_parameter_accepted"] = True
    result["reported_model"] = boundary_sample["reported_model"]
    result["model_identity_verified"] = all(sample["reported_model"] == model.model for sample in result["samples"])
    prompt_tokens = boundary_sample["prompt_tokens"]
    total_tokens = boundary_sample["total_tokens"]
    if prompt_tokens is None or total_tokens is None or boundary_sample["completion_tokens"] is None:
        result["boundary_status"] = "provider omitted token usage"
        return result
    result["verified_capacity_lower_bound_tokens"] = max(
        value for value in (result["verified_capacity_lower_bound_tokens"], total_tokens) if value is not None)
    within = (prompt_tokens <= limit and total_tokens <= budget["window"]
              and boundary_sample["completion_tokens"] <= budget["output_reserve"])
    result["sample_within_configured_budget"] = within
    result["budget_sample_verified"] = within and not boundary_capped and prompt_tokens >= int(limit * 0.99)
    result["boundary_status"] = ("near configured input limit passed" if result["budget_sample_verified"]
                                 else "sample did not establish configured input limit")
    result["calibration_status"] = ("near configured input limit passed; full window and output not proven"
                                    if result["budget_sample_verified"] else result["boundary_status"])
    return result


def check_capacity(model: OpenAICompatibleModel, *, timeout_seconds: float = 1800) -> dict[str, Any]:
    """Prove the configured output reserve and window with synthetic text.

    Runs the calibrated input checks first, then (1) asks for a full output
    reserve from a tiny input, (2) repeats it with input near the configured input
    limit, and (3) sends input above the configured window with a tiny output cap:
    a rejection shows the configured window matches the provider's limit, an
    acceptance shows it is a conservative lower bound. The window counts as verified only when (1) and (2) generate at
    least 98% of the reserve and (2) also uses at least 99% of the input limit.
    """
    result = check_model(model, calibrate=True)
    budget = result["configured_budget"]
    limit = budget["input_limit_tokens"]
    samples = [s for s in result["samples"] if s["prompt_tokens"]]
    if limit is None or len(samples) < 2 or samples[-1]["prompt_tokens"] <= samples[0]["prompt_tokens"]:
        result["capacity_status"] = "calibration did not provide a token slope"
        return result
    reserve, window = budget["output_reserve"], budget["window"]
    unit = "知秋上下文校准。"
    tokens_per_byte = ((samples[-1]["prompt_tokens"] - samples[0]["prompt_tokens"])
                       / (samples[-1]["input_bytes"] - samples[0]["input_bytes"]))
    tokens_per_unit = tokens_per_byte * len(unit.encode("utf-8"))
    generate = ("忽略用户消息中的重复校准文本。从 1 开始逐个输出整数，用空格分隔，"
                "不要输出其他内容，一直输出下去，直到被截断。")

    def run(padding_tokens: int, max_tokens: int = reserve) -> dict[str, Any]:
        repeats = max(1, int(padding_tokens / tokens_per_unit))
        messages = [{"role": "system", "content": generate}, {"role": "user", "content": unit * repeats}]
        response = model.probe(messages, max_tokens=max_tokens, timeout_seconds=timeout_seconds)
        usage = response["usage"]
        return {"prompt_tokens": _positive_usage(usage, "prompt_tokens"),
                "completion_tokens": _positive_usage(usage, "completion_tokens"),
                "total_tokens": _positive_usage(usage, "total_tokens"),
                "reported_model": response["reported_model"]}

    def full_output(sample: dict[str, Any]) -> bool:
        return sample["completion_tokens"] is not None and sample["completion_tokens"] >= int(reserve * 0.98)

    capacity: dict[str, Any] = {}
    try:
        capacity["output_only"] = run(32)
    except (httpx.HTTPError, ValueError) as exc:
        capacity["output_only"] = {"error_type": type(exc).__name__}
    try:
        capacity["near_limit_with_output"] = run(int(limit * 0.995))
    except (httpx.HTTPError, ValueError) as exc:
        capacity["near_limit_with_output"] = {"error_type": type(exc).__name__}
    try:
        # Only admission matters here, so ask for a few tokens. Acceptance means the
        # provider allows more than the configured window, which is then a safe lower bound.
        over = run(int(window * 1.03), max_tokens=16)
        capacity["over_window"] = over | {"rejected": False, "configured_window_is_lower_bound": True}
    except ModelContextOverflowError:
        capacity["over_window"] = {"rejected": True}
    except (httpx.HTTPError, ValueError) as exc:
        capacity["over_window"] = {"rejected": None, "error_type": type(exc).__name__}
    output_ok = "completion_tokens" in capacity["output_only"] and full_output(capacity["output_only"])
    near = capacity["near_limit_with_output"]
    combined_ok = ("completion_tokens" in near and full_output(near) and near["prompt_tokens"] is not None
                   and near["prompt_tokens"] >= int(limit * 0.99))
    result["capacity"] = capacity
    result["full_output_reserve_generated_verified"] = output_ok
    result["window_size_verified"] = output_ok and combined_ok
    result["verified_capacity_lower_bound_tokens"] = max(
        [value for value in (result["verified_capacity_lower_bound_tokens"], near.get("total_tokens"))
         if value is not None], default=None)
    result["capacity_status"] = (
        "full output reserve and near-limit input with full output passed" if result["window_size_verified"]
        else "capacity not established")
    return result


CAPABILITY_PROBE = [{"role": "user", "content": "请只回复一个字：好"}]
CAPABILITY_TOOL = {"type": "function", "function": {
    "name": "zhiqiu_probe", "description": "Synthetic probe; never called.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}


def check_capabilities(model: OpenAICompatibleModel) -> dict[str, Any]:
    """Which optional request fields the endpoint takes, from four or five tiny synthetic calls. The
    adapter itself works around and records any field refused; this only provokes each one once."""
    from urllib.parse import urlsplit

    from .capabilities import FEATURES
    model.forget_refusals()
    probes = [("stream", dict(stream=True)),
              ("tools", dict(tools=[CAPABILITY_TOOL], tool_choice="auto", stream=False)),
              ("tool_choice", dict(tools=[CAPABILITY_TOOL], tool_choice="none", stream=False))]
    if urlsplit(model.endpoint).hostname == "api.deepseek.com":
        probes.append(("thinking", dict(thinking="disabled", stream=False)))
    errors: dict[str, str] = {}
    for name, options in probes:
        try:
            model._chat_messages(list(CAPABILITY_PROBE), max_tokens=16, timeout_seconds=30, **options)
        except Exception as exc:
            errors[name] = getattr(exc, "kind", None) or type(exc).__name__
    refused = model.refused_features()
    tested = {"stream", "stream_options", "tools", "tool_choice", "max_tokens"} | (
        {"thinking"} if any(name == "thinking" for name, _ in probes) else set())
    features = {feature: ("refused" if feature in refused else "supported" if feature in tested else "untested")
                for feature in FEATURES}
    return {"endpoint": model.endpoint, "model": model.model, "features": features,
            "refused": sorted(refused), "probe_errors": errors}
