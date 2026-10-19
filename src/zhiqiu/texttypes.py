"""Which local files are text Zhiqiu reads, and how they are read.

Learning material, the chat's read_file and the root preview's file count all ask here, so the three
agree. Documents (.md, .txt...) are always read; code and configuration files too unless
materials.code_files is false; materials.extra_suffixes adds more. Writing stays limited to .md and
.txt (WRITE_SUFFIXES): reading code is not a reason to let the model write it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DOC_SUFFIXES = (".md", ".markdown", ".txt", ".rst")
WRITE_SUFFIXES = (".md", ".txt")
# Code and configuration: the suffix and the language name shown to the model.
CODE_LANGUAGES = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "jsx", ".java": "java", ".kt": "kotlin", ".go": "go", ".rs": "rust", ".c": "c",
    ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".cs": "csharp", ".rb": "ruby", ".php": "php",
    ".swift": "swift", ".scala": "scala", ".lua": "lua", ".sh": "shell", ".bash": "shell", ".ps1": "powershell",
    ".bat": "batch", ".sql": "sql", ".r": "r", ".m": "matlab",
    ".json": "json", ".jsonl": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini",
    ".cfg": "ini", ".xml": "xml", ".csv": "csv",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss", ".vue": "vue", ".svelte": "svelte",
    ".ipynb": "notebook",
}
# Files known by name alone.
NAMED = {"dockerfile": "dockerfile", "makefile": "makefile", "readme": "", "license": "", "changelog": ""}
# Never worth reading as material: dependencies, build output, caches. Hidden ones are skipped anyway.
EXCLUDED_DIRS = frozenset({"node_modules", "venv", "__pycache__", "dist", "build", "target", "site-packages",
                           "coverage", "htmlcov"})
EXCLUDED_FILES = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "uv.lock", "cargo.lock")
EXCLUDED_ENDINGS = (".min.js", ".min.css", ".map", ".lock")
BINARY_PROBE = 8192


class NotText(ValueError):
    """A file that is not read as text; the message says why (binary, encoding...)."""


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip()
    return default if not raw else raw == "1"


def code_files() -> bool:
    return _flag("ZHIQIU_MATERIALS_CODE_FILES", True)


def extra_suffixes() -> tuple[str, ...]:
    raw = os.getenv("ZHIQIU_MATERIALS_EXTRA_SUFFIXES", "")
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip().startswith("."))


def language(path: Path | str) -> str | None:
    """The language of a readable file ("" for a document), or None when the file is not read at all."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in DOC_SUFFIXES:
        return ""
    if not code_files():
        return None
    if suffix in CODE_LANGUAGES:
        return CODE_LANGUAGES[suffix]
    if suffix and suffix in extra_suffixes():
        return suffix[1:]
    if not suffix and path.name.lower() in NAMED:
        return NAMED[path.name.lower()]
    return None


def readable(path: Path | str) -> bool:
    return language(path) is not None


def writable(path: Path | str) -> bool:
    return Path(path).suffix.lower() in WRITE_SUFFIXES


def excluded_dir(name: str) -> bool:
    return name.startswith(".") or name.lower() in EXCLUDED_DIRS


def excluded_file(name: str) -> bool:
    lower = name.lower()
    return name.startswith(".") or lower in EXCLUDED_FILES or lower.endswith(EXCLUDED_ENDINGS)


def describe_readable() -> str:
    return ("文档（.md、.txt 等）" + ("与常见代码、配置文件" if code_files() else "")
            + "；可在 materials.extra_suffixes 中增加扩展名")


def decode(data: bytes, name: str) -> str:
    if b"\0" in data[:BINARY_PROBE]:
        raise NotText(f"二进制文件：{name}")
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise NotText(f"编码不是 UTF-8 或 GBK：{name}")


def notebook_text(text: str, name: str) -> str:
    """A Jupyter notebook as its cells' source, Markdown and code in order; outputs are dropped."""
    try:
        cells = json.loads(text).get("cells", [])
    except (json.JSONDecodeError, AttributeError) as exc:
        raise NotText(f"不是有效的 Jupyter 笔记本：{name}") from exc
    parts = []
    for cell in cells:
        if not isinstance(cell, dict) or cell.get("cell_type") not in ("markdown", "code"):
            continue
        source = cell.get("source", "")
        source = "".join(source) if isinstance(source, list) else str(source)
        if source.strip():
            parts.append(f"# [{cell['cell_type']}]\n{source.rstrip()}")
    return "\n\n".join(parts)


def read_text(path: Path) -> str:
    """The text of one readable file; NotText when it is binary or in an unknown encoding."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise NotText(f"无法读取：{path}") from exc
    # Universal newlines, as Path.read_text gave before 9.0: the same file keeps the same content hash.
    text = decode(data, str(path)).replace("\r\n", "\n").replace("\r", "\n")
    return notebook_text(text, str(path)) if path.suffix.lower() == ".ipynb" else text
