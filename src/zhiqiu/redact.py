"""Redact secrets by value before anything is written to an audit, a log or a viewer.

Two sources: the actual values of the configured credentials (so a key pasted into a
chat message or echoed in an error is caught whatever it looks like), and common token
formats. Key-name based redaction in summarize_args stays as a first line.
"""
from __future__ import annotations

import os
import re
from typing import Any

MARK = "[redacted]"
SECRET_ENV = ("ZHIQIU_LLM_API_KEY", "ZHIQIU_EXA_API_KEY", "EXA_API_KEY", "ZHIQIU_NOTION_TOKEN")
# Short values would match ordinary text; real credentials are far longer.
MIN_SECRET_CHARS = 8
PATTERNS = (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
            re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{12,}=*"),
            re.compile(r"\bntn_[A-Za-z0-9]{20,}"),
            re.compile(r"\bsecret_[A-Za-z0-9]{20,}"))


def _values() -> list[str]:
    values = []
    for name in SECRET_ENV:
        value = os.getenv(name, "").strip()
        if len(value) >= MIN_SECRET_CHARS:
            values.append(value)
    # Longest first, so a key that contains another is replaced whole.
    return sorted(set(values), key=len, reverse=True)


def redact(text: str) -> str:
    if not text:
        return text
    for value in _values():
        text = text.replace(value, MARK)
    for pattern in PATTERNS:
        text = pattern.sub(MARK, text)
    return text


def redact_obj(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_obj(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(item) for item in value]
    return value
