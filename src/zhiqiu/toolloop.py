"""Bounded tool loop for the few program steps that allow one (restricted ReAct).

The program opens the loop, fixes which tools it may use and how many rounds it may
take, and decides afterwards what to do with the result. Saving, grading and
scheduling never happen here.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable

from . import guardrails, tracing
from .context import ContextManager, ModelRequest
from .materials import Material
from .model import accepts
from .tools import ToolError, ToolExecutor, ToolRegistry, ToolScope, ToolSpec

MAX_ROUNDS = 5
# Wall-clock budget for one whole loop. Rounds alone do not bound time: each model call
# may take up to its own timeout, plus every tool call in that round.
LOOP_SECONDS = 180.0
MODEL_CALL_SECONDS = 60.0
# The closing answer after the loop stops gets a short timeout of its own.
GRACE_SECONDS = 30.0
LEAK_RUN_CHARS = 15
QUERY_MAX_CHARS = 200
_URL = re.compile(r"https?://\S+")
STOP_REASONS = {"done": "模型结束", "max_rounds": "达到轮数上限", "budget": "上下文预算即将用完",
                "deadline": "超过循环总时限", "proposal": "模型提出了建议，等待用户确认"}


class Draft:
    """The text of the round being received, shown to the user as it streams in.

    A round may still end in tool calls, and then its text is not the answer (7.2 never showed it):
    the draft is withdrawn and the next round starts a new one. The answer that is finally said
    carries `stream_id`, so the page replaces the draft with it."""

    def __init__(self, io: Any):
        self.io = io
        self.stream_id = uuid.uuid4().hex
        self.shown = False

    def piece(self, text: str) -> None:
        self.shown = True
        self.io.stream(self.stream_id, text)

    def withdraw(self) -> None:
        if self.shown:
            self.io.stream_reset(self.stream_id)
        self.stream_id = uuid.uuid4().hex
        self.shown = False


@dataclass(frozen=True)
class LoopResult:
    status: str
    rounds: int
    loop_id: str
    materials: tuple[Material, ...]
    final: dict[str, Any]
    # The last answer's raw text, for steps that want prose rather than JSON.
    text: str = ""
    # With stop_on: the name and value of the call that ended the loop.
    stopped_by: tuple[str, Any] | None = None

    @property
    def reason(self) -> str:
        return STOP_REASONS[self.status]


def query_leak_check(private: Iterable[Material], point: str = "", run: int = LEAK_RUN_CHARS,
                     max_chars: int = QUERY_MAX_CHARS) -> Callable[[ToolSpec, dict[str, Any]], None]:
    """Reject a query that is too long or copies a run of private material text.

    The knowledge point is the user's own input, so its text never counts as a leak.
    """
    # Links in notes are public references, and different pages of one site share long URL
    # fragments; only the surrounding prose counts as private text.
    texts = [_URL.sub("\x00", item.content) for item in private]

    def check(spec: ToolSpec, args: dict[str, Any]) -> None:
        query = args.get("query")
        if isinstance(query, str) and len(query) > max_chars:
            raise ToolError("invalid_args", f"查询超过 {max_chars} 字符", rule="leak_check")
        # Every outgoing string counts: a URL can carry copied text as well as a query can.
        for value in args.values():
            if not isinstance(value, str):
                continue
            checked = value.replace(point, "\x00") if point else value
            for start in range(len(checked) - run + 1):
                fragment = checked[start:start + run]
                if "\x00" not in fragment and any(fragment in text for text in texts):
                    raise ToolError("invalid_args", f"参数包含本地资料原文（连续 {run} 字以上），请只写知识点和缺口",
                                    rule="leak_check")
    return check


def _tool_message(call_id: str, value: Any) -> dict[str, Any]:
    if isinstance(value, ToolError):
        payload: Any = {"error": value.kind, "message": str(value)}
    elif isinstance(value, list) and all(isinstance(item, Material) for item in value):
        payload = {"results": [{"source": item.source, "content": item.content} for item in value]}
    else:
        payload = {"result": value}
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(payload, ensure_ascii=False, default=str)}


def _parse_final(message: dict[str, Any]) -> dict[str, Any]:
    content = (message.get("content") or "").strip()
    if content.startswith("```"):
        content = content.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


class ToolLoop:
    def __init__(self, model: Any, context: ContextManager, registry: ToolRegistry, executor: ToolExecutor,
                 max_rounds: int = MAX_ROUNDS, deadline_s: float = LOOP_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.model = model
        self.context = context
        self.registry = registry
        self.executor = executor
        self.max_rounds = max_rounds
        self.deadline_s = deadline_s
        self.clock = clock

    def _observe(self, request: ModelRequest, sent: list[dict[str, Any]]) -> None:
        """Keep the provider's prompt token count for exactly what was sent, as _invoke does."""
        if getattr(self.model, "last_usage_request_build_id", None) == request.build_id:
            self.context.observe_messages(sent, (getattr(self.model, "last_usage", None) or {}).get("prompt_tokens"))

    def run(self, request: ModelRequest, tool_names: list[str], scope: ToolScope,
            require_final: bool = False, final_instruction: str | None = None,
            stop_on: frozenset[str] = frozenset(), draft: Draft | None = None) -> LoopResult:
        """require_final: when the rounds or the time run out, ask once more for the answer with tools off.

        stop_on: a successful call to one of these tools ends the loop at once (status "proposal");
        later calls of that round only get a "not run" reply. The deadline is checked before every
        round and every tool call, and no model call may wait longer than the time left, so one
        loop overruns its deadline by at most one tool call.
        """
        store = getattr(self.context, "store", None)
        if store is None:
            return self._run(request, tool_names, scope, require_final, final_instruction, stop_on, str(uuid.uuid4()),
                             draft)
        loop_id = str(uuid.uuid4())
        with tracing.span(store, "loop", request.stage or "tool_loop") as span:
            span.set(loop_id=loop_id, max_rounds=self.max_rounds)
            result = self._run(request, tool_names, scope, require_final, final_instruction, stop_on, loop_id, draft)
            span.set(end_reason=result.status, rounds=result.rounds)
            if result.status in ("max_rounds", "deadline", "budget"):
                guardrails.record(store, "loop_limit", "stopped", result.reason, stage=request.stage,
                                  end_reason=result.status, kp_id=request.kp_id, attempt_id=request.attempt_id)
            return result

    def _run(self, request: ModelRequest, tool_names: list[str], scope: ToolScope, require_final: bool,
             final_instruction: str | None, stop_on: frozenset[str], loop_id: str,
             draft: Draft | None = None) -> LoopResult:
        deadline = self.clock() + self.deadline_s
        # A model that cannot stream (scripted ones in tests) is called exactly as before.
        listen = ({"on_delta": draft.piece} if draft is not None and accepts(self.model.tool_round, "on_delta")
                  else {})
        tools = self.registry.openai_schema(tool_names)
        messages: list[dict[str, Any]] = [dict(message) for message in request.messages]
        materials: dict[tuple[str, str], Material] = {}
        status, final, rounds, text = "max_rounds", {}, 0, ""
        stopped_by: tuple[str, Any] | None = None
        for round_number in range(1, self.max_rounds + 1):
            estimate, limit = self.context.estimate(messages)
            if limit is not None and estimate > limit:
                status = "budget"
                break
            left = deadline - self.clock()
            if left <= 0:
                status = "deadline"
                break
            rounds = round_number
            tracing.progress(round=round_number, max_rounds=self.max_rounds, tool=None)
            sent = list(messages)
            message = self.model.tool_round(request, messages, tools, timeout_s=min(MODEL_CALL_SECONDS, left), **listen)
            self._observe(request, sent)
            calls = message.get("tool_calls") or []
            if calls and draft is not None:
                draft.withdraw()
            if not calls:
                status, final, text = "done", _parse_final(message), (message.get("content") or "").strip()
                break
            messages.append({key: value for key, value in message.items() if value is not None})
            round_scope = replace(scope, loop_id=loop_id, round=round_number, names=frozenset(tool_names))
            for call in calls:
                call_id = str(call.get("id") or uuid.uuid4())
                function = call.get("function") or {}
                name = function.get("name")
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except (TypeError, json.JSONDecodeError):
                    args = function.get("arguments")
                try:
                    if stopped_by is not None:
                        raise ToolError("denied", "已有待用户确认的建议，本次调用未执行")
                    if self.clock() >= deadline:
                        # Every call still needs a tool message, but none may start after the deadline.
                        status = "deadline"
                        raise ToolError("timeout", "已超过工具循环总时限，本次调用未执行")
                    tracing.progress(tool=str(name))
                    value = self.executor.run(str(name), args, round_scope).value
                    if name in stop_on:
                        stopped_by = (str(name), value)
                except ToolError as exc:
                    value = exc
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, Material):
                            materials.setdefault((item.source, item.content_hash), item)
                messages.append(_tool_message(call_id, value))
            if stopped_by is not None:
                status = "proposal"
                break
            if status == "deadline":
                break
        if require_final and status in ("max_rounds", "deadline"):
            used_up = "工具轮数已用完" if status == "max_rounds" else "工具循环已超过总时限"
            closing = final_instruction or "不要再调用工具，直接按本阶段要求返回最终 JSON。"
            messages.append({"role": "user", "content": f"{used_up}。{closing}"})
            estimate, limit = self.context.estimate(messages)
            if limit is None or estimate <= limit:
                tracing.progress(tool=None, closing=True)
                closing_message = self.model.tool_round(request, messages, tools, tool_choice="none",
                                                        timeout_s=GRACE_SECONDS, **listen)
                final, text = _parse_final(closing_message), (closing_message.get("content") or "").strip()
        return LoopResult(status, rounds, loop_id, tuple(materials.values()), final, text, stopped_by)
