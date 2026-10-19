from __future__ import annotations

import json
import os
import hashlib
import inspect
import time
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

import httpx

from . import capabilities
from .context import ModelRequest, _model_budget, _optional_int
from .redact import redact
from .search import WEB_SEARCH_DESCRIPTION, WEB_SEARCH_PARAMS
from .tools import function_schema


class ModelContextOverflowError(ValueError):
    """The provider rejected a request because it exceeded its context window."""


class ModelServiceError(httpx.HTTPStatusError):
    """An HTTP error from the model service, with a Chinese explanation as its message and a kind
    (auth, forbidden, not_found, payment, rate_limited, server_error, bad_request) for the trace."""

    def __init__(self, kind: str, message: str, exc: httpx.HTTPStatusError):
        super().__init__(message, request=exc.request, response=exc.response)
        self.kind = kind


class ModelTimeoutError(httpx.TimeoutException):
    kind = "timeout"


class ModelConnectError(httpx.ConnectError):
    kind = "connect"


def _request_of(exc: httpx.HTTPError) -> httpx.Request | None:
    try:
        return exc.request
    except RuntimeError:
        return None


def _is_context_overflow(response: httpx.Response) -> bool:
    if response.status_code not in (400, 413, 422):
        return False
    body = response.text.lower()[:4000]
    return any(marker in body for marker in (
        "context_length_exceeded", "maximum context length", "context window",
        "too many tokens", "input tokens exceed", "prompt is too long"))


def _reply_text(choices: list[Any]) -> str:
    """The reply's text plus any tool calls, as one string for the optional trace content."""
    if not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message") or {}
    text = message.get("content") or ""
    calls = message.get("tool_calls")
    if calls:
        text += ("\n" if text else "") + json.dumps({"tool_calls": calls}, ensure_ascii=False, default=str)
    return text


def cached_tokens(usage: dict[str, Any]) -> int | None:
    """Prompt tokens the provider served from its cache: DeepSeek or OpenAI style usage."""
    value = usage.get("prompt_cache_hit_tokens")
    if type(value) is not int:
        details = usage.get("prompt_tokens_details")
        value = details.get("cached_tokens") if isinstance(details, dict) else None
    return value if type(value) is int and value >= 0 else None


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
                 audit: Callable[[dict[str, Any]], None] | None = None,
                 capabilities: "capabilities.Memory | None" = None):
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
        self.last_output = ""
        self.kp_id: str | None = None
        self.attempt_id: str | None = None
        self.point = ""
        # Fields this endpoint refused: remembered in the database when the error named them
        # (`capabilities`), otherwise only for this process.
        self.capabilities = capabilities
        self._refused_here: set[str] = set()
        self._known: set[str] | None = None
        self._noticed: set[str] = set()
        # Told once per field when a refusal is first worked around (the chat shows it as a note).
        self.on_notice: Callable[[str], None] | None = None
        self.notices: list[str] = []

    def set_trace_context(self, kp_id: str | None, attempt_id: str | None) -> None:
        self.kp_id = kp_id
        self.attempt_id = attempt_id

    def _chat(self, instruction: str, payload: dict[str, Any], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return self._chat_messages([{"role": "system", "content": instruction},
                                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], tools)

    def refused_features(self) -> set[str]:
        """Request fields this endpoint and model do not take (see capabilities.FEATURES)."""
        if self._known is None:
            self._known = set()
            if self.capabilities is not None:
                try:
                    self._known = set(self.capabilities.refused_features(self.endpoint, self.model))
                except Exception:
                    self._known = set()
        return self._known | self._refused_here

    def forget_refusals(self) -> None:
        self._known = None
        self._refused_here.clear()
        self._noticed.clear()

    def streaming(self) -> bool:
        """Whether calls ask for a streamed reply: on unless ZHIQIU_LLM_STREAM=0, or the endpoint
        refused streaming (remembered, or earlier in this process)."""
        return os.getenv("ZHIQIU_LLM_STREAM", "1").strip() != "0" and "stream" not in self.refused_features()

    def _learn(self, feature: str, named: bool, evidence: str) -> None:
        """A refused field worked around with a successful retry: not sent again."""
        self._refused_here.add(feature)
        remembered = False
        if named and self.capabilities is not None:
            try:
                self.capabilities.record_refused_feature(self.endpoint, self.model, feature, evidence)
                remembered = True
            except Exception:
                remembered = False
            if remembered and hasattr(self.capabilities, "record_event"):
                from . import guardrails
                guardrails.record(self.capabilities, "endpoint_feature_disabled", "blocked",
                                  f"{self.model} 不支持 {feature}", feature=feature, model=self.model)
        if feature in self._noticed:
            return
        self._noticed.add(feature)
        text = capabilities.notice(feature, remembered)
        self.notices.append(text)
        if self.on_notice is not None:
            try:
                self.on_notice(text)
            except Exception:
                pass

    def _service_error(self, exc: httpx.HTTPStatusError) -> ModelServiceError:
        status = exc.response.status_code
        known = {
            401: ("auth", "API Key 无效或已过期（HTTP 401），请检查 model.api_key"),
            402: ("payment", "账户余额不足（HTTP 402）"),
            403: ("forbidden", "没有权限使用这个模型或端点（HTTP 403），请检查 Key 的权限和模型名"),
            404: ("not_found", f"端点地址或模型名不对（HTTP 404）：{self.endpoint}/chat/completions，模型 {self.model}"),
            429: ("rate_limited", "请求太频繁或额度用完（HTTP 429），请稍后再试或检查账户余额"),
        }
        if status in known:
            kind, message = known[status]
        elif status >= 500:
            kind, message = "server_error", f"模型服务出错（HTTP {status}），请稍后再试"
        else:
            kind, message = "bad_request", f"模型服务拒绝了请求（HTTP {status}）：{redact(exc.response.text[:200])}"
        return ModelServiceError(kind, message, exc)

    def _chat_messages(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                       request: ModelRequest | None = None, max_tokens: int | None = None,
                       thinking: str | None = None, timeout_seconds: float = 60,
                       tool_choice: str = "auto", on_delta: Callable[[str], None] | None = None,
                       stream: bool | None = None) -> dict[str, Any]:
        """One chat completion. Streamed unless turned off: on_delta then sees each piece of the reply's
        text as it arrives, and the returned message is the same as a non-streamed one."""
        body: dict[str, Any] = {"model": self.model, "temperature": 0,
            "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice
        output_cap = max_tokens if max_tokens is not None else self.output_reserve
        if output_cap is not None:
            body["max_tokens"] = output_cap
        if thinking is not None:
            body["thinking"] = {"type": thinking}
        # Taken before the stream fields: the same request has the same fingerprint streamed or not.
        fingerprint = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        stream = self.streaming() if stream is None else stream
        refused = self.refused_features()
        for feature in ("tools", "tool_choice", "thinking", "max_tokens"):
            if feature in refused and feature in body:
                body = capabilities.without(body, feature)
        started_at_ns = time.time_ns()
        began = time.perf_counter()
        reply = _Reply(began, on_delta)
        status = "error"
        error_kind: str | None = None
        self.last_usage = {}
        self.last_usage_request_build_id = None
        self.last_response_model = None
        try:
            # Streamed, the read timeout is the longest gap between two pieces, as it was the longest
            # wait for the whole reply before; the connection itself gets 10 seconds.
            try:
                with httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds))) as client:
                    self._send(client, body, stream, reply)
            except httpx.TimeoutException as exc:
                if isinstance(exc, ModelTimeoutError):
                    raise
                raise ModelTimeoutError(f"模型服务响应超时（连接 10 秒、两段之间 {timeout_seconds:g} 秒），"
                                        "请检查网络或稍后再试", request=_request_of(exc)) from exc
            except httpx.ConnectError as exc:
                if isinstance(exc, ModelConnectError):
                    raise
                raise ModelConnectError(f"连不上模型端点 {self.endpoint}：请检查网络、代理和 base_url",
                                        request=_request_of(exc)) from exc
            usage = reply.usage
            self.last_usage = usage
            if request and type(usage.get("prompt_tokens")) is int and usage["prompt_tokens"] > 0:
                self.last_usage_request_build_id = request.build_id
            self.last_response_model = reply.model
            status = "ok"
        except ModelContextOverflowError:
            status = error_kind = "context_overflow"
            raise
        except BaseException as exc:
            error_kind = getattr(exc, "kind", None) or type(exc).__name__
            raise
        finally:
            usage = reply.usage
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
                            "session_id": request.session_id if request else None,
                            "started_at_ns": started_at_ns, "tokens_cached": cached_tokens(usage),
                            "error_kind": error_kind,
                            "ttft_ms": reply.ttft_ms if status == "ok" else None, "streamed": reply.streamed,
                            # The store keeps these only when ZHIQIU_TRACE_CONTENT=1.
                            "request_messages": messages, "response_text": _reply_text(reply.choices)})
        if not reply.choices:
            raise ValueError("模型没有返回结果")
        return reply.choices[0]["message"]

    def _post(self, client: httpx.Client, body: dict[str, Any]) -> dict[str, Any]:
        response = client.post(f"{self.endpoint}/chat/completions", json=body,
                               headers={"Authorization": f"Bearer {self.api_key}"})
        _raise_for_status(response)
        return response.json()

    def _send(self, client: httpx.Client, body: dict[str, Any], stream: bool, reply: "_Reply") -> None:
        """Post the request; a refused optional field is dropped and the request sent once more, until it
        is accepted or nothing optional is left. A rate limit waits once for Retry-After (at most 10 s)."""
        request = {**body, "stream": True, "stream_options": {"include_usage": True}} if stream else dict(body)
        if "stream_options" in self.refused_features():
            request.pop("stream_options", None)
        pending: list[tuple[str, bool, str]] = []
        waited = False
        while True:
            try:
                if request.get("stream"):
                    self._post_streamed(client, request, reply)
                else:
                    reply.whole(self._post(client, request))
            except httpx.HTTPStatusError as exc:
                response = exc.response
                if response.status_code == 429 and not waited:
                    waited = True
                    time.sleep(_retry_after(response))
                    continue
                found = capabilities.classify_refusal(response, request)
                if found is None:
                    raise self._service_error(exc) from exc
                feature, named = found
                pending.append((feature, named, capabilities.evidence(response)))
                request = capabilities.without(request, feature)
                continue
            for feature, named, evidence in pending:
                self._learn(feature, named, evidence)
            return

    def _post_streamed(self, client: httpx.Client, body: dict[str, Any], reply: "_Reply") -> None:
        with client.stream("POST", f"{self.endpoint}/chat/completions", json=body,
                           headers={"Authorization": f"Bearer {self.api_key}"}) as response:
            _raise_for_status(response, read=True)
            if "text/event-stream" not in response.headers.get("content-type", ""):
                # The endpoint ignored `stream` and answered at once.
                response.read()
                reply.whole(response.json())
                return
            reply.read(response.iter_lines())

    def probe(self, messages: list[dict[str, str]], max_tokens: int = 64,
              timeout_seconds: float = 60, thinking_mode: str = "disabled") -> dict[str, Any]:
        """Send synthetic input through the normal adapter and return provider usage only."""
        thinking = thinking_mode if urlsplit(self.endpoint).hostname == "api.deepseek.com" else None
        message = self._chat_messages(messages, max_tokens=max_tokens, thinking=thinking, stream=False,
                                      timeout_seconds=timeout_seconds)
        if not isinstance(message.get("content"), str) or not message["content"].strip():
            raise ValueError("模型探测未返回文本")
        return {"usage": self.last_usage.copy(), "reported_model": self.last_response_model}

    def _json(self, instruction: str, payload: dict[str, Any]) -> dict[str, Any]:
        message = self._chat(instruction + " 仅返回一个 JSON 对象，不要 Markdown。", payload)
        self.last_output = message.get("content") or ""
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

    def request(self, request: ModelRequest, on_delta: Callable[[str], None] | None = None) -> dict[str, Any]:
        """on_delta: sees the JSON reply's text as it streams in (the runtime only counts it)."""
        reserve = request.budget.get("output_reserve_tokens")
        max_tokens = reserve if type(reserve) is int and reserve > 0 else None
        message = self._chat_messages(list(request.messages), request=request, max_tokens=max_tokens,
                                      on_delta=on_delta)
        # Kept so an answer that fails parsing or validation can be stored for a regression case.
        self.last_output = message.get("content") or ""
        return self._parse_json_message(message)

    def tool_round(self, request: ModelRequest, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]], tool_choice: str = "auto", timeout_s: float = 60,
                   on_delta: Callable[[str], None] | None = None) -> dict[str, Any]:
        """One round of a bounded tool loop; returns the raw assistant message."""
        reserve = request.budget.get("output_reserve_tokens")
        max_tokens = reserve if type(reserve) is int and reserve > 0 else None
        return self._chat_messages(messages, tools, request=request, max_tokens=max_tokens,
                                   tool_choice=tool_choice, timeout_seconds=timeout_s, on_delta=on_delta)


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


def _retry_after(response: httpx.Response) -> float:
    try:
        return min(10.0, max(0.0, float(response.headers.get("retry-after", "2"))))
    except ValueError:
        return 2.0


def _raise_for_status(response: httpx.Response, read: bool = False) -> None:
    """raise_for_status, telling a context overflow apart; read: load a streamed error body first."""
    if read and response.status_code >= 400:
        response.read()
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if _is_context_overflow(response):
            raise ModelContextOverflowError("模型服务报告上下文窗口溢出") from exc
        raise


class _Reply:
    """A reply being received: from one JSON body, or put together from the pieces of a stream."""

    def __init__(self, began: float, on_delta: Callable[[str], None] | None):
        self.began = began
        self.on_delta = on_delta
        self.choices: list[Any] = []
        self.usage: dict[str, Any] = {}
        self.model: str | None = None
        self.ttft_ms: int | None = None
        self.streamed = False

    def whole(self, data: dict[str, Any]) -> None:
        self.choices = data.get("choices") or []
        raw_usage = data.get("usage") or {}
        self.usage = raw_usage if isinstance(raw_usage, dict) else {}
        reported = data.get("model")
        self.model = reported if isinstance(reported, str) else None

    def _first(self) -> None:
        if self.ttft_ms is None:
            self.ttft_ms = int((time.perf_counter() - self.began) * 1000)

    def read(self, lines: Any) -> None:
        """Chat Completions chunks: text and reasoning pieces are joined, tool calls are put together
        by index (id, name and argument pieces), usage comes with the last chunk if at all. The first
        piece of any of them is the first token."""
        self.streamed = True
        text: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        role, chunks = "assistant", 0
        for line in lines:
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            if not payload:
                continue
            chunk = json.loads(payload)
            if not isinstance(chunk, dict):
                continue
            chunks += 1
            if isinstance(chunk.get("usage"), dict):
                self.usage = chunk["usage"]
            if isinstance(chunk.get("model"), str):
                self.model = chunk["model"]
            for choice in chunk.get("choices") or []:
                if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                    continue
                delta = choice.get("delta") or {}
                role = delta.get("role") or role
                if isinstance(delta.get("reasoning_content"), str) and delta["reasoning_content"]:
                    # A reasoning model's first tokens are its reasoning: they count for time to first token.
                    self._first()
                    reasoning.append(delta["reasoning_content"])
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    self._first()
                    text.append(piece)
                    if self.on_delta is not None:
                        try:
                            self.on_delta(piece)
                        except Exception:
                            # Showing the text is best effort; the reply itself must not depend on it.
                            pass
                for call in delta.get("tool_calls") or []:
                    self._first()
                    slot = calls.setdefault(int(call.get("index", 0)),
                                            {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                    if call.get("id"):
                        slot["id"] = call["id"]
                    if call.get("type"):
                        slot["type"] = call["type"]
                    function = call.get("function") or {}
                    slot["function"]["name"] += function.get("name") or ""
                    slot["function"]["arguments"] += function.get("arguments") or ""
        if not chunks:
            return
        message: dict[str, Any] = {"role": role, "content": "".join(text) if text or not calls else None}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if calls:
            message["tool_calls"] = [calls[index] for index in sorted(calls)]
        self.choices = [{"index": 0, "message": message}]


def accepts(function: Any, name: str) -> bool:
    """Whether a model method takes the keyword `name`; scripted test models often do not."""
    try:
        return name in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
