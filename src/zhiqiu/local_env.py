"""Load project-local model settings without adding a third-party dependency."""

from __future__ import annotations

import os
from pathlib import Path

from .config import ENV_NAMES

PROJECT_ENV = Path(__file__).resolve().parents[2] / ".env"
# Every variable config.toml can set; ZHIQIU_LONG_ANSWER_SEGMENTS stays process-only on purpose.
ALLOWED_KEYS = frozenset(ENV_NAMES)


def load_local_env(path: Path = PROJECT_ENV) -> list[str]:
    """Read a simple KEY=VALUE file; process environment has precedence. Returns the names it set."""
    if not path.exists():
        return []
    parsed: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f".env 第 {number} 行缺少等号")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key not in ALLOWED_KEYS:
            raise ValueError(f".env 第 {number} 行变量名无效")
        if key in parsed:
            raise ValueError(f".env 第 {number} 行重复设置变量")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        parsed[key] = value
    loaded = []
    for key, value in parsed.items():
        if value and not os.environ.get(key):
            os.environ[key] = value
            loaded.append(key)
    return loaded
