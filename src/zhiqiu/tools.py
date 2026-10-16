"""One tool layer: declaration, registration, validation, gate, execution and audit.

The program decides which step may use which tools; the model can at most propose
arguments. Every call goes through ToolExecutor.run, which never runs a handler
whose arguments failed validation or whose gate did not pass.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import httpx

from . import guardrails
from .materials import Material
from .redact import redact

# propose: the handler only records a suggestion (enter a flow, remember a preference); the program decides.
PERMISSIONS = ("read", "network", "write_file", "external_mcp", "propose")
ERROR_KINDS = ("invalid_args", "denied", "timeout", "network", "rate_limited", "upstream", "not_found", "internal")
RETRYABLE = frozenset({"network", "timeout", "rate_limited"})
# Who let a gated call through: the approval policy, the user now, or the user earlier in this turn.
APPROVAL_SOURCES = ("policy", "user", "turn")
_SECRET_MARKERS = ("key", "token", "secret", "password", "authorization", "cookie")
_SUMMARY_CHARS = 200


class ToolError(ValueError):
    """A classified tool failure; kind decides retry and the audit status."""

    def __init__(self, kind: str, message: str, status: int | None = None, *, rule: str | None = None):
        if kind not in ERROR_KINDS:
            raise ValueError(f"unknown tool error kind: {kind}")
        super().__init__(message)
        self.kind = kind
        # HTTP status when the failure came from a response, so adapters can explain it.
        self.status = status
        # The guardrail that refused the call (guardrails.RULES), recorded as an event with the audit.
        self.rule = rule


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: dict[str, Any]
    permission: str
    handler: Callable[[dict[str, Any]], Any]
    source: str
    timeout_s: float = 30
    max_retries: int = 0
    max_output_chars: int = 20_000
    requires_confirmation: bool = False
    # False keeps a tool out of every model tool loop; only program steps may call it.
    in_loops: bool = True
    # Tool-intrinsic argument rules beyond the schema (e.g. allowed paths); runs before the gate.
    precheck: Callable[[dict[str, Any]], None] | None = None
    # Text shown to the user when the call needs confirmation.
    describe: Callable[[dict[str, Any]], str] | None = None

    def __post_init__(self) -> None:
        if self.permission not in PERMISSIONS:
            raise ValueError(f"工具 {self.name} 的权限无效：{self.permission}")
        if self.params.get("type") != "object" or self.params.get("additionalProperties") is not False:
            raise ValueError(f"工具 {self.name} 的参数必须是 additionalProperties=false 的 object")


@dataclass(frozen=True)
class Approval:
    """A confirmation callback's answer with its source; a plain bool is read as the user's answer."""

    approved: bool
    source: str = "user"

    def __post_init__(self) -> None:
        if self.source not in APPROVAL_SOURCES:
            raise ValueError(f"unknown approval source: {self.source}")


@dataclass(frozen=True)
class ToolScope:
    """What one program step allows: permissions, the confirmation callback and trace links."""

    allowed: frozenset[str]
    confirm: Callable[[ToolSpec, dict[str, Any]], bool | Approval] | None = None
    # Step-specific argument rule run after schema validation, e.g. the query leak check.
    check: Callable[[ToolSpec, dict[str, Any]], None] | None = None
    # When set, only these tool names may run in the step.
    names: frozenset[str] | None = None
    kp_id: str | None = None
    attempt_id: str | None = None
    session_id: str | None = None
    point: str = ""
    loop_id: str | None = None
    round: int | None = None
    conversation_id: str | None = None


@dataclass(frozen=True)
class ToolResult:
    value: Any
    call_id: str
    attempts: int
    truncated: bool = False
    result_count: int | None = None
    output_chars: int = 0


def function_schema(name: str, description: str, params: dict[str, Any]) -> dict[str, Any]:
    """The Chat Completions function declaration for one tool."""
    return {"type": "function", "function": {"name": name, "description": description, "parameters": params}}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        existing = self._tools.get(spec.name)
        if existing:
            raise ValueError(f"工具名 {spec.name} 已由 {existing.source} 注册，{spec.source} 不能重复注册")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name] for name in self.names()]

    def openai_schema(self, names: Iterable[str]) -> list[dict[str, Any]]:
        result = []
        for name in names:
            spec = self._tools.get(name)
            if not spec:
                raise ValueError(f"未注册的工具：{name}")
            result.append(function_schema(spec.name, spec.description, spec.params))
        return result


def _type_ok(value: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_type_ok(value, item) for item in expected)
    if expected == "null":
        return value is None
    if expected == "number":
        return type(value) in (int, float)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return type(value) is int
    if expected == "boolean":
        return type(value) is bool
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    raise ValueError(f"不支持的参数类型：{expected}")


def validate_args(schema: dict[str, Any], value: Any, path: str = "参数") -> None:
    """Check the JSON Schema subset tools declare. Values are never coerced."""
    expected = schema.get("type")
    if expected and not _type_ok(value, expected):
        raise ToolError("invalid_args", f"{path} 应为 {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolError("invalid_args", f"{path} 不在允许值内")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ToolError("invalid_args", f"{path} 过短")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ToolError("invalid_args", f"{path} 超过 {schema['maxLength']} 字符")
    if type(value) is int:
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolError("invalid_args", f"{path} 小于 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolError("invalid_args", f"{path} 大于 {schema['maximum']}")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ToolError("invalid_args", f"{path} 超过 {schema['maxItems']} 项")
        for index, item in enumerate(value):
            validate_args(schema.get("items", {}), item, f"{path}[{index}]")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise ToolError("invalid_args", f"{path} 缺少 {key}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise ToolError("invalid_args", f"{path} 含未声明字段：{', '.join(extra)}")
        for key, item in value.items():
            if key in properties:
                validate_args(properties[key], item, f"{path}.{key}")


def summarize_args(value: Any, key: str = "") -> Any:
    """Audit-safe copy of arguments: secrets dropped, long strings cut."""
    if any(marker in key.lower() for marker in _SECRET_MARKERS):
        return "[redacted]"
    if isinstance(value, str):
        value = redact(value)
        return value if len(value) <= _SUMMARY_CHARS else value[:_SUMMARY_CHARS] + "…"
    if isinstance(value, dict):
        return {k: summarize_args(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [summarize_args(v) for v in value[:10]] + (["…"] if len(value) > 10 else [])
    return value


def _truncate(value: Any, cap: int) -> tuple[Any, bool, int | None, int]:
    """Cap tool output by characters; returns (value, truncated, result_count, chars)."""
    if isinstance(value, str):
        return (value[:cap], True, None, cap) if len(value) > cap else (value, False, None, len(value))
    if isinstance(value, list) and all(isinstance(item, Material) for item in value):
        kept: list[Material] = []
        used = 0
        for item in value:
            room = cap - used
            if room <= 0:
                break
            content = item.content[:room]
            kept.append(item if content == item.content else Material(item.kind, item.source, item.content_hash,
                                                                       content, item.reason))
            used += len(content)
        truncated = len(kept) < len(value) or used < sum(len(item.content) for item in value)
        return kept, truncated, len(kept), used
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= cap:
        return value, False, len(value) if isinstance(value, list) else None, len(text)
    return text[:cap], True, None, cap


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, audit: Callable[[dict[str, Any]], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep, backoff_s: float = 0.5):
        self.registry = registry
        self.audit = audit
        self.sleep = sleep
        self.backoff_s = backoff_s

    def _gate(self, spec: ToolSpec, args: dict[str, Any], scope: ToolScope) -> Approval | None:
        """None when the call needs no confirmation; otherwise the answer, approved or not."""
        if scope.names is not None and spec.name not in scope.names:
            raise ToolError("denied", f"本步骤不能使用工具 {spec.name}", rule="name_not_allowed")
        if spec.permission not in scope.allowed:
            raise ToolError("denied", f"本步骤未允许 {spec.permission} 类工具：{spec.name}", rule="permission_denied")
        if not spec.requires_confirmation:
            return None
        if scope.confirm is None:
            raise ToolError("denied", f"{spec.name} 需要用户确认，当前环境无法确认", rule="confirmation_unavailable")
        try:
            answer = scope.confirm(spec, args)
        except Exception as exc:
            # A broken confirmation path must never let the call through.
            raise ToolError("denied", f"{spec.name} 的确认失败：{type(exc).__name__}",
                            rule="confirmation_unavailable") from exc
        if isinstance(answer, Approval):
            return answer
        return Approval(answer is True)

    def run(self, name: str, args: Any, scope: ToolScope) -> ToolResult:
        call_id = str(uuid.uuid4())
        began = time.perf_counter()
        spec = self.registry.get(name)
        attempts = 0
        confirmed: bool | None = None
        approval_source: str | None = None
        record: dict[str, Any] = {"id": call_id, "tool": name, "source": spec.source if spec else None,
                                  "permission": spec.permission if spec else None,
                                  "kp_id": scope.kp_id, "attempt_id": scope.attempt_id,
                                  "session_id": scope.session_id, "point": scope.point,
                                  "loop_id": scope.loop_id, "round": scope.round,
                                  "conversation_id": scope.conversation_id, "started_at_ns": time.time_ns(),
                                  "args_summary": summarize_args(args) if isinstance(args, dict) else None}
        try:
            if not spec:
                raise ToolError("not_found", f"未注册的工具：{name}")
            validate_args(spec.params, args)
            if spec.precheck:
                spec.precheck(args)
            if scope.check:
                try:
                    scope.check(spec, args)
                except ToolError:
                    raise
                except Exception as exc:
                    raise ToolError("denied", f"{spec.name} 的参数检查失败：{type(exc).__name__}") from exc
            approval = self._gate(spec, args, scope)
            if approval is not None:
                confirmed, approval_source = approval.approved, approval.source
                if not approval.approved:
                    raise ToolError("denied", f"用户拒绝了 {spec.name}", rule="confirmation_denied")
            while True:
                attempts += 1
                try:
                    value = spec.handler(args)
                    break
                except ToolError as exc:
                    if exc.kind not in RETRYABLE or attempts > spec.max_retries:
                        raise
                    self.sleep(self.backoff_s * 2 ** (attempts - 1))
        except ToolError as exc:
            status = exc.kind if exc.kind in ("invalid_args", "denied", "timeout") else "error"
            if exc.rule:
                action = "denied_by_user" if exc.rule == "confirmation_denied" else "blocked"
                record["guard"] = guardrails.payload(exc.rule, action, str(exc), tool=name)
            record["approval_source"] = approval_source
            self._audit(record, status, exc.kind, attempts, confirmed, began, None, 0, False)
            raise
        except Exception as exc:
            record["approval_source"] = approval_source
            self._audit(record, "error", "internal", attempts, confirmed, began, None, 0, False)
            raise ToolError("internal", f"{name} 执行失败：{type(exc).__name__}") from exc
        record["approval_source"] = approval_source
        value, truncated, count, chars = _truncate(value, spec.max_output_chars)
        self._audit(record, "ok", None, attempts, confirmed, began, count, chars, truncated)
        return ToolResult(value, call_id, attempts, truncated, count, chars)

    def _audit(self, record: dict[str, Any], status: str, error_kind: str | None, attempts: int,
               confirmed: bool | None, began: float, count: int | None, chars: int, truncated: bool) -> None:
        if not self.audit:
            return
        self.audit({**record, "status": status, "error_kind": error_kind, "attempts": attempts,
                    "confirmed": confirmed, "latency_ms": int((time.perf_counter() - began) * 1000),
                    "result_count": count, "output_chars": chars, "truncated": truncated})


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout_s: float,
              max_bytes: int = 2_000_000, clock: Callable[[], float] = time.monotonic) -> Any:
    return request_json("POST", url, headers, timeout_s, payload=payload, max_bytes=max_bytes, clock=clock)


def request_json(method: str, url: str, headers: dict[str, str], timeout_s: float,
                 payload: dict[str, Any] | None = None, params: dict[str, Any] | None = None,
                 max_bytes: int = 2_000_000, clock: Callable[[], float] = time.monotonic) -> Any:
    """Send one JSON request with a hard total deadline.

    httpx timeouts apply per phase, so a server trickling bytes could outlast them; the
    body is streamed and the deadline is checked after every chunk.
    """
    deadline = clock() + timeout_s
    phase = httpx.Timeout(min(timeout_s, 10.0), read=timeout_s)
    try:
        with httpx.Client(timeout=phase) as client:
            with client.stream(method, url, json=payload, params=params, headers=headers) as response:
                status = response.status_code
                if status == 429:
                    raise ToolError("rate_limited", f"服务限流（HTTP 429）：{url}", status)
                if status >= 500:
                    raise ToolError("network", f"服务暂时不可用（HTTP {status}）：{url}", status)
                if status >= 400:
                    raise ToolError("upstream", f"服务拒绝请求（HTTP {status}）：{url}", status)
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if clock() > deadline:
                        raise ToolError("timeout", f"超过 {timeout_s:g} 秒总时限：{url}")
                    if len(body) > max_bytes:
                        raise ToolError("upstream", f"响应超过 {max_bytes} 字节：{url}")
    except httpx.TimeoutException as exc:
        raise ToolError("timeout", f"请求超时：{url}") from exc
    except httpx.TransportError as exc:
        raise ToolError("network", f"网络错误：{type(exc).__name__}") from exc
    try:
        return json.loads(bytes(body))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError("upstream", f"响应不是有效 JSON：{url}") from exc
