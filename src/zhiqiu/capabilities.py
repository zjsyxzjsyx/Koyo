"""What an endpoint will not take: request fields it refused, told apart from other failures.

OpenAI-compatible services differ in what they accept. When one refuses a field (streaming, tools,
thinking...), the adapter drops that field and asks once more instead of failing. A refusal that
names the field is remembered per endpoint and model in the database, so the next process does not
fail first; one that names nothing is only tried around for the rest of this process.
"""
from __future__ import annotations

import re
from typing import Any, Protocol

import httpx

# Checked in this order; the first field the error names and the request carried is the one refused.
FEATURES = ("stream_options", "tool_choice", "tools", "thinking", "max_tokens", "stream")
LABELS = {"stream_options": "流式用量统计（stream_options）", "tool_choice": "tool_choice 参数",
          "tools": "工具调用", "thinking": "thinking 参数", "max_tokens": "max_tokens 参数（改用 max_completion_tokens）",
          "stream": "流式输出"}
_NAMES = {
    "stream_options": r"stream_options",
    "tool_choice": r"tool_choice",
    "tools": r"\btools?\b|function[ _]?call(?:ing|s)?|tool[ _]calls?|tool use",
    "thinking": r"\bthinking\b",
    "max_tokens": r"\bmax_tokens\b",
    "stream": r"\bstream(?:ing)?\b",
}
_REFUSAL = re.compile(r"unsupported|not support|unknown|unrecognized|extra|not allowed|not permitted|invalid|"
                      r"not available|disabled|not enabled|does not accept|forbidden|不支持|未知|不允许", re.I)
# A refusal that names no field: these statuses mean the request was not acceptable as sent.
_FORMAT_STATUSES = (400, 415, 422, 501)
_FIELD_STATUSES = (400, 403, 404, 405, 415, 422, 501)


class Memory(Protocol):
    def refused_features(self, endpoint: str, model: str) -> set[str]: ...
    def record_refused_feature(self, endpoint: str, model: str, feature: str, evidence: str) -> None: ...


def sent_features(body: dict[str, Any]) -> list[str]:
    return [feature for feature in FEATURES if feature in body]


def classify_refusal(response: httpx.Response, body: dict[str, Any]) -> tuple[str, bool] | None:
    """(feature, named) for a refusal of one of the request's optional fields, else None.

    named: the error text names the field, so the refusal is worth remembering. Without a name only
    the streaming fields are tried around, as 8.0 did for any 400 while streaming."""
    status = response.status_code
    text = response.text[:4000]
    sent = sent_features(body)
    if status in _FIELD_STATUSES or status >= 500:
        if _REFUSAL.search(text):
            for feature in sent:
                if re.search(_NAMES[feature], text, re.I):
                    return feature, True
    if status in _FORMAT_STATUSES:
        for feature in ("stream_options", "stream"):
            if feature in sent:
                return feature, False
    return None


def without(body: dict[str, Any], feature: str) -> dict[str, Any]:
    """The request without one refused field (max_tokens is renamed rather than dropped)."""
    changed = dict(body)
    if feature == "tools":
        changed.pop("tools", None)
        changed.pop("tool_choice", None)
    elif feature == "max_tokens":
        value = changed.pop("max_tokens", None)
        if value is not None:
            changed["max_completion_tokens"] = value
    elif feature == "stream":
        changed.pop("stream", None)
        changed.pop("stream_options", None)
    else:
        changed.pop(feature, None)
    return changed


def evidence(response: httpx.Response) -> str:
    return f"HTTP {response.status_code}: {response.text[:300]}"


def notice(feature: str, remembered: bool) -> str:
    return (f"模型端点不支持{LABELS[feature]}，已去掉后重试"
            + ("；已记住，以后不再发送（zhiqiu check-model --capabilities --reset 可清除）。" if remembered else "。"))
