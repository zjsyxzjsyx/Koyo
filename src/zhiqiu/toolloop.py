"""Bounded tool loop for the few program steps that allow one (restricted ReAct).

The program opens the loop, fixes which tools it may use and how many rounds it may
take, and decides afterwards what to do with the result. Saving, grading and
scheduling never happen here.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable

from .context import ContextManager, ModelRequest
from .materials import Material
from .tools import ToolError, ToolExecutor, ToolRegistry, ToolScope, ToolSpec

MAX_ROUNDS = 5
LEAK_RUN_CHARS = 15
QUERY_MAX_CHARS = 200
_URL = re.compile(r"https?://\S+")
STOP_REASONS = {"done": "模型结束", "max_rounds": "达到轮数上限", "budget": "上下文预算即将用完"}


@dataclass(frozen=True)
class LoopResult:
    status: str
    rounds: int
    loop_id: str
    materials: tuple[Material, ...]
    final: dict[str, Any]

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
            raise ToolError("invalid_args", f"查询超过 {max_chars} 字符")
        # Every outgoing string counts: a URL can carry copied text as well as a query can.
        for value in args.values():
            if not isinstance(value, str):
                continue
            checked = value.replace(point, "\x00") if point else value
            for start in range(len(checked) - run + 1):
                fragment = checked[start:start + run]
                if "\x00" not in fragment and any(fragment in text for text in texts):
                    raise ToolError("invalid_args", f"参数包含本地资料原文（连续 {run} 字以上），请只写知识点和缺口")
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
                 max_rounds: int = MAX_ROUNDS):
        self.model = model
        self.context = context
        self.registry = registry
        self.executor = executor
        self.max_rounds = max_rounds

    def run(self, request: ModelRequest, tool_names: list[str], scope: ToolScope,
            require_final: bool = False) -> LoopResult:
        """require_final: after the round limit, ask once more for the answer with tools switched off."""
        loop_id = str(uuid.uuid4())
        tools = self.registry.openai_schema(tool_names)
        messages: list[dict[str, Any]] = [dict(message) for message in request.messages]
        materials: dict[tuple[str, str], Material] = {}
        status, final, rounds = "max_rounds", {}, 0
        for round_number in range(1, self.max_rounds + 1):
            estimate, limit = self.context.estimate(messages)
            if limit is not None and estimate > limit:
                status = "budget"
                break
            rounds = round_number
            message = self.model.tool_round(request, messages, tools)
            calls = message.get("tool_calls") or []
            if not calls:
                status, final = "done", _parse_final(message)
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
                    value = self.executor.run(str(name), args, round_scope).value
                except ToolError as exc:
                    value = exc
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, Material):
                            materials.setdefault((item.source, item.content_hash), item)
                messages.append(_tool_message(call_id, value))
        if require_final and status == "max_rounds":
            messages.append({"role": "user", "content": "工具轮数已用完。不要再调用工具，直接按本阶段要求返回最终 JSON。"})
            estimate, limit = self.context.estimate(messages)
            if limit is None or estimate <= limit:
                final = _parse_final(self.model.tool_round(request, messages, tools, tool_choice="none"))
        return LoopResult(status, rounds, loop_id, tuple(materials.values()), final)
