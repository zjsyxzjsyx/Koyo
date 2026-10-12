from __future__ import annotations

import json
import os
import hashlib
import time
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

import httpx

from .context import ModelRequest, _model_budget, _optional_int
from .search import WEB_SEARCH_DESCRIPTION, WEB_SEARCH_PARAMS
from .tools import function_schema


class ModelContextOverflowError(ValueError):
    """The provider rejected a request because it exceeded its context window."""


def _is_context_overflow(response: httpx.Response) -> bool:
    if response.status_code not in (400, 413, 422):
        return False
    body = response.text.lower()[:4000]
    return any(marker in body for marker in (
        "context_length_exceeded", "maximum context length", "context window",
        "too many tokens", "input tokens exceed", "prompt is too long"))


class Model(Protocol):
    """Per-stage interface for scripted models in tests and demos.

    The real adapter implements request(ModelRequest) instead; LearningRuntime sends
    every built request through it and only falls back to these methods when a model
    has no request().
    """

    endpoint: str

    def assess(self, point: str, sources: list[dict[str, str]]) -> dict[str, Any]: ...
    def teach(self, point: str, sources: list[dict[str, str]]) -> dict[str, Any]: ...
    def next_question(self, point: str, context: list[dict[str, Any]], sources: list[dict[str, str]],
                      focus: list[dict[str, Any]]) -> dict[str, Any]: ...
    def grade(self, point: str, context: list[dict[str, Any]], sources: list[dict[str, str]]) -> dict[str, Any]: ...
    def clarify(self, point: str, question: str, sources: list[dict[str, str]]) -> dict[str, Any]: ...
    def search_query(self, point: str, gap: str) -> str | None: ...


class OpenAICompatibleModel:
    """Small Chat Completions adapter. Model output is always untrusted input."""

    def __init__(self, base_url: str | None = None, model: str | None = None, api_key: str | None = None,
                 audit: Callable[[dict[str, Any]], None] | None = None):
        self.endpoint = (base_url or os.getenv("ZHIQIU_LLM_BASE_URL", "")).rstrip("/")
        self.model = model or os.getenv("ZHIQIU_LLM_MODEL", "")
        self.api_key = api_key or os.getenv("ZHIQIU_LLM_API_KEY", "")
        if not all((self.endpoint, self.model, self.api_key)):
            raise ValueError("请配置 ZHIQIU_LLM_BASE_URL、ZHIQIU_LLM_MODEL、ZHIQIU_LLM_API_KEY")
        if not self.endpoint.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise ValueError("远程模型端点必须使用 HTTPS")
        configured = _model_budget(self)
        self.output_reserve = (configured["output_reserve"] if configured
                               else _optional_int("ZHIQIU_LLM_OUTPUT_RESERVE"))
        self.audit = audit
        self.last_usage: dict[str, Any] = {}
        self.last_usage_request_build_id: str | None = None
        self.last_response_model: str | None = None
        self.kp_id: str | None = None
        self.attempt_id: str | None = None
        self.point = ""

    def set_trace_context(self, kp_id: str | None, attempt_id: str | None) -> None:
        self.kp_id = kp_id
        self.attempt_id = attempt_id

    def _chat(self, instruction: str, payload: dict[str, Any], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return self._chat_messages([{"role": "system", "content": instruction},
                                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], tools)

    def _chat_messages(self, messages: list[dict[str, str]], tools: list[dict[str, Any]] | None = None,
                       request: ModelRequest | None = None, max_tokens: int | None = None,
                       thinking: str | None = None, timeout_seconds: float = 60) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "temperature": 0,
            "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        output_cap = max_tokens if max_tokens is not None else self.output_reserve
        if output_cap is not None:
            body["max_tokens"] = output_cap
        if thinking is not None:
            body["thinking"] = {"type": thinking}
        fingerprint = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        began = time.perf_counter()
        usage: dict[str, Any] = {}
        status = "error"
        self.last_usage = {}
        self.last_usage_request_build_id = None
        self.last_response_model = None
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.post(f"{self.endpoint}/chat/completions", json=body,
                                       headers={"Authorization": f"Bearer {self.api_key}"})
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    if _is_context_overflow(response):
                        status = "context_overflow"
                        raise ModelContextOverflowError("模型服务报告上下文窗口溢出") from exc
                    raise
                data = response.json()
                choices = data.get("choices", [])
                raw_usage = data.get("usage") or {}
                usage = raw_usage if isinstance(raw_usage, dict) else {}
                self.last_usage = usage
                if request and type(usage.get("prompt_tokens")) is int and usage["prompt_tokens"] > 0:
                    self.last_usage_request_build_id = request.build_id
                reported_model = data.get("model")
                self.last_response_model = reported_model if isinstance(reported_model, str) else None
            status = "ok"
        finally:
            if self.audit:
                self.audit({"kp_id": request.kp_id if request else self.kp_id,
                            "attempt_id": request.attempt_id if request else self.attempt_id,
                            "point": request.point if request else self.point,
                            "model": self.model, "prompt_hash": fingerprint,
                            "latency_ms": int((time.perf_counter() - began) * 1000),
                            "tokens_in": usage.get("prompt_tokens"), "tokens_out": usage.get("completion_tokens"),
                            "status": status,
                            "context_build_id": request.build_id if request else None,
                            "stage": request.stage if request else None,
                            "session_id": request.session_id if request else None})
        if not choices:
            raise ValueError("模型没有返回结果")
        return choices[0]["message"]

    def probe(self, messages: list[dict[str, str]], max_tokens: int = 64,
              timeout_seconds: float = 60, thinking_mode: str = "disabled") -> dict[str, Any]:
        """Send synthetic input through the normal adapter and return provider usage only."""
        thinking = thinking_mode if urlsplit(self.endpoint).hostname == "api.deepseek.com" else None
        message = self._chat_messages(messages, max_tokens=max_tokens, thinking=thinking,
                                      timeout_seconds=timeout_seconds)
        if not isinstance(message.get("content"), str) or not message["content"].strip():
            raise ValueError("模型探测未返回文本")
        return {"usage": self.last_usage.copy(), "reported_model": self.last_response_model}

    def _json(self, instruction: str, payload: dict[str, Any]) -> dict[str, Any]:
        message = self._chat(instruction + " 仅返回一个 JSON 对象，不要 Markdown。", payload)
        return self._parse_json_message(message)

    @staticmethod
    def _parse_json_message(message: dict[str, Any]) -> dict[str, Any]:
        content = message.get("content") or ""
        if content.startswith("```json"):
            content = content.removeprefix("```json").removesuffix("```").strip()
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("模型返回了无效 JSON，当前业务状态未结案") from exc
        if not isinstance(data, dict):
            raise ValueError("模型必须返回 JSON 对象")
        return data

    def request(self, request: ModelRequest) -> dict[str, Any]:
        reserve = request.budget.get("output_reserve_tokens")
        max_tokens = reserve if type(reserve) is int and reserve > 0 else None
        message = self._chat_messages(list(request.messages), request=request, max_tokens=max_tokens)
        return self._parse_json_message(message)

    def summarize(self, lesson: str, corrections: list[str]) -> str:
        response = self._json("为跨天追问生成有来源的简明讲解摘要。保留已确认要点、纠正和不确定项；不推断用户已理解。返回 summary 字符串。",
                              {"lesson": lesson, "corrections": corrections})
        summary = response.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("模型未返回有效讲解摘要")
        return summary.strip()

    def search_query(self, point: str, gap: str) -> str | None:
        self.point = point
        schema = function_schema("web_search", WEB_SEARCH_DESCRIPTION, WEB_SEARCH_PARAMS)
        message = self._chat("本地资料不足。请调用 web_search，查询字符串只包含知识点和缺口，不得包含本地资料原文。",
                             {"knowledge_point": point, "gap": gap}, [schema])
        for call in message.get("tool_calls") or []:
            if call.get("function", {}).get("name") == "web_search":
                try:
                    query = json.loads(call["function"]["arguments"])["query"]
                    if isinstance(query, str) and query.strip():
                        return query.strip()
                except (KeyError, TypeError, json.JSONDecodeError):
                    pass
        return None
