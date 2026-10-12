import json

import httpx
from typer.testing import CliRunner

from zhiqiu.cli import app
from zhiqiu.diagnostics import check_model
from zhiqiu.model import OpenAICompatibleModel


def test_check_model_uses_model_budget_and_provider_counts(monkeypatch):
    monkeypatch.setenv("ZHIQIU_MODEL_BUDGETS", json.dumps({
        "m": {"window": 1000, "output_reserve": 100, "margin": 50},
        "https://provider.example/v1|m": {"window": 1600, "output_reserve": 200,
                                          "margin": 100, "verified": True},
    }))
    model = OpenAICompatibleModel("https://provider.example/v1", "m", "secret")
    calls = []

    def probe(messages, max_tokens=16):
        calls.append(messages)
        count = 20 + len(messages[1]["content"]) // 4
        return {"usage": {"prompt_tokens": count, "completion_tokens": 2,
                          "total_tokens": count + 2}, "reported_model": "m"}

    monkeypatch.setattr(model, "probe", probe)
    result = check_model(model, calibrate=True)
    assert len(calls) == 2
    assert result["configured_budget"]["input_limit_tokens"] == 1300
    assert result["configured_budget"]["verified_claim_in_config"] is True
    assert result["provider_token_count_verified"] is True
    assert result["budget_sample_verified"] is True
    assert result["window_size_verified"] is False
    assert "secret" not in json.dumps(result)


def test_check_model_reports_missing_usage_without_claiming_budget(monkeypatch):
    monkeypatch.delenv("ZHIQIU_MODEL_BUDGETS", raising=False)
    for name in ("WINDOW", "OUTPUT_RESERVE", "MARGIN"):
        monkeypatch.delenv(f"ZHIQIU_LLM_{name}", raising=False)
    model = OpenAICompatibleModel("https://provider.example/v1", "m", "secret")
    monkeypatch.setattr(model, "probe", lambda messages: {"usage": {}, "reported_model": None})
    result = check_model(model, calibrate=True)
    assert result["connection_verified"] is True
    assert result["provider_token_count_verified"] is False
    assert result["budget_sample_verified"] is False
    assert result["configured_budget"]["input_limit_tokens"] is None


def test_calibration_caps_large_window_without_claiming_boundary(monkeypatch):
    monkeypatch.setenv("ZHIQIU_MODEL_BUDGETS", json.dumps({
        "m": {"window": 1048576, "output_reserve": 65536, "margin": 8192}}))
    model = OpenAICompatibleModel("https://provider.example/v1", "m", "secret")

    def probe(messages, max_tokens=64):
        count = 20 + len(messages[1]["content"]) // 4
        return {"usage": {"prompt_tokens": count, "completion_tokens": 2,
                          "total_tokens": count + 2}, "reported_model": "m"}

    monkeypatch.setattr(model, "probe", probe)
    result = check_model(model, calibrate=True)
    assert result["sample_capped"] is True
    assert result["sample_within_configured_budget"] is True
    assert result["budget_sample_verified"] is False
    assert result["window_size_verified"] is False


def test_boundary_probe_uses_reserved_output_parameter_without_claiming_full_output(monkeypatch):
    monkeypatch.setenv("ZHIQIU_MODEL_BUDGETS", json.dumps({
        "m": {"window": 10000, "output_reserve": 1000, "margin": 500}}))
    model = OpenAICompatibleModel("https://api.deepseek.com", "m", "secret")
    options = []

    def probe(messages, **kwargs):
        options.append(kwargs)
        count = 20 + len(messages[1]["content"]) // 4
        return {"usage": {"prompt_tokens": count, "completion_tokens": 2,
                          "total_tokens": count + 2}, "reported_model": "m"}

    monkeypatch.setattr(model, "probe", probe)
    result = check_model(model, boundary=True)
    assert len(options) == 3
    assert options[-1]["max_tokens"] == 1000
    assert options[-1]["thinking_mode"] == "enabled"
    assert result["budget_sample_verified"] is True
    assert result["output_reserve_parameter_accepted"] is True
    assert result["full_output_reserve_generated_verified"] is False
    assert result["window_size_verified"] is False


def test_check_model_cli_does_not_echo_credential_or_create_store(monkeypatch, tmp_path):
    monkeypatch.setattr("zhiqiu.cli.load_local_env", lambda: None)
    for name in ("BASE_URL", "MODEL", "API_KEY"):
        monkeypatch.delenv(f"ZHIQIU_LLM_{name}", raising=False)
    path = tmp_path / "state.sqlite"
    result = CliRunner().invoke(app, ["--db", str(path), "check-model"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["connection_verified"] is False
    assert not path.exists()


def test_probe_uses_existing_chat_adapter_and_returns_provider_usage(monkeypatch):
    requests = []

    def post(self, url, **kwargs):
        requests.append((url, kwargs))
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "收到"}}],
                                         "usage": {"prompt_tokens": 31, "completion_tokens": 2}},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", post)
    model = OpenAICompatibleModel("https://api.deepseek.com", "m", "secret")
    result = model.probe([{"role": "user", "content": "公开测试"}])
    assert result == {"usage": {"prompt_tokens": 31, "completion_tokens": 2}, "reported_model": "m"}
    assert requests[0][1]["json"]["max_tokens"] == 64
    assert requests[0][1]["json"]["thinking"] == {"type": "disabled"}
    assert "secret" not in json.dumps(result)


def test_check_model_cli_redacts_credential_even_in_provider_metadata(monkeypatch):
    monkeypatch.setenv("ZHIQIU_LLM_BASE_URL", "https://provider.example/v1")
    monkeypatch.setenv("ZHIQIU_LLM_MODEL", "m")
    monkeypatch.setenv("ZHIQIU_LLM_API_KEY", "sentinel-secret")
    monkeypatch.setattr("zhiqiu.cli.check_model", lambda model, calibrate: {
        "connection_verified": True, "reported_model": "m/sentinel-secret"})
    result = CliRunner().invoke(app, ["check-model"])
    assert result.exit_code == 0
    assert "sentinel-secret" not in result.stdout
    assert json.loads(result.stdout)["reported_model"] == "m/[REDACTED]"


def test_check_model_cli_reports_status_without_provider_error_body(monkeypatch):
    monkeypatch.setenv("ZHIQIU_LLM_BASE_URL", "https://provider.example/v1")
    monkeypatch.setenv("ZHIQIU_LLM_MODEL", "m")
    monkeypatch.setenv("ZHIQIU_LLM_API_KEY", "sentinel-secret")

    def reject(model, calibrate):
        request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
        response = httpx.Response(405, text="sentinel-secret", request=request)
        raise httpx.HTTPStatusError("sentinel-secret", request=request, response=response)

    monkeypatch.setattr("zhiqiu.cli.check_model", reject)
    result = CliRunner().invoke(app, ["check-model"])
    assert result.exit_code == 1
    assert "sentinel-secret" not in result.stdout
    assert json.loads(result.stdout)["http_status"] == 405


def _capacity_model(monkeypatch, provider_window, generated):
    from zhiqiu.model import ModelContextOverflowError
    monkeypatch.setenv("ZHIQIU_MODEL_BUDGETS", json.dumps({
        "m": {"window": 1600, "output_reserve": 200, "margin": 100}}))
    model = OpenAICompatibleModel("https://provider.example/v1", "m", "secret")

    def probe(messages, max_tokens=64, timeout_seconds=60, thinking_mode="disabled"):
        prompt = 20 + len(messages[1]["content"]) // 4
        if prompt > provider_window:
            raise ModelContextOverflowError("overflow")
        completion = min(max_tokens, generated, provider_window - prompt) if max_tokens > 64 else 2
        return {"usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                          "total_tokens": prompt + completion}, "reported_model": "m"}

    monkeypatch.setattr(model, "probe", probe)
    return model


def test_capacity_verifies_window_only_when_full_output_fits_near_the_input_limit(monkeypatch):
    from zhiqiu.diagnostics import check_capacity
    result = check_capacity(_capacity_model(monkeypatch, provider_window=1600, generated=10_000))
    assert result["full_output_reserve_generated_verified"] is True
    assert result["window_size_verified"] is True
    assert result["capacity"]["over_window"]["rejected"] is True


def test_capacity_rejects_a_reserve_the_model_does_not_generate(monkeypatch):
    from zhiqiu.diagnostics import check_capacity
    result = check_capacity(_capacity_model(monkeypatch, provider_window=1600, generated=50))
    assert result["full_output_reserve_generated_verified"] is False
    assert result["window_size_verified"] is False


def test_capacity_accepts_a_smaller_configured_window_as_lower_bound(monkeypatch):
    from zhiqiu.diagnostics import check_capacity
    result = check_capacity(_capacity_model(monkeypatch, provider_window=100_000, generated=10_000))
    assert result["window_size_verified"] is True
    over = result["capacity"]["over_window"]
    assert over["rejected"] is False and over["configured_window_is_lower_bound"] is True
    assert over["completion_tokens"] <= 16
