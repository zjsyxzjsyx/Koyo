"""The workbench's file pane: read one text file, or list one folder, inside the readable or writable roots.

Only what the chat itself may read is shown, by the same rule (files.read_target): a readable type, no
symbolic link on the way, inside a root. Nothing is written, and nothing leaves the machine.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from . import texttypes
from .files import OutsideRoots, read_target
from .tools import ToolError

MAX_BYTES = 1_000_000
MAX_ENTRIES = 300
# Folders a listing leaves out: tool caches and version control, never notes.
SKIPPED_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
                ".ruff_cache", ".idea", ".vscode", "dist", "build", ".tox"}
LANGUAGES = {".md": "markdown", ".markdown": "markdown", ".py": "python", ".js": "javascript", ".ts": "typescript",
             ".json": "json", ".toml": "toml", ".yaml": "yaml", ".yml": "yaml", ".html": "html", ".css": "css",
             ".sql": "sql", ".sh": "shell", ".ps1": "powershell", ".txt": "text", ".rst": "text"}


class Refused(ValueError):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.extra = extra


def read(path_arg: str, roots: Sequence[Path]) -> dict[str, Any]:
    """One file's text (at most MAX_BYTES of it) with its size, time and language."""
    if not isinstance(path_arg, str) or not path_arg.strip():
        raise Refused(400, "path 必须是非空字符串")
    try:
        path = read_target(path_arg.strip(), list(roots))
    except OutsideRoots as exc:
        raise Refused(403, str(exc), outside=True, path=str(exc.path)) from None
    except ToolError as exc:
        status = {"not_found": 404, "invalid_args": 415}.get(exc.kind, 403)
        raise Refused(status, str(exc), outside=exc.kind == "denied") from None
    stat = path.stat()
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    truncated = len(raw) > MAX_BYTES
    raw = raw[:MAX_BYTES]
    if b"\x00" in raw:
        raise Refused(415, f"不是文本文件：{path.name}")
    text = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    return {"path": str(path), "name": path.name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "language": LANGUAGES.get(path.suffix.lower(), "text"), "truncated": truncated, "text": text}


def listing(dir_arg: str, roots: Sequence[Path]) -> dict[str, Any]:
    """One folder inside a root: its subfolders and files (readable ones marked), folders first.
    Without a folder, the roots themselves."""
    resolved_roots = [Path(root).resolve() for root in roots]
    if not dir_arg:
        return {"dir": "", "parent": None, "entries": [{"name": str(root), "path": str(root), "kind": "root"}
                                                        for root in resolved_roots], "truncated": False}
    folder = Path(dir_arg).expanduser()
    if ".." in folder.parts or not folder.is_absolute():
        raise Refused(400, "目录必须是绝对路径，且不能含 ..")
    for part in (folder, *folder.parents):
        if part.is_symlink():
            raise Refused(403, f"路径中含符号链接：{part}")
    folder = folder.resolve()
    root = next((root for root in resolved_roots if folder.is_relative_to(root)), None)
    if root is None:
        raise Refused(403, f"不在可读或可写目录内：{dir_arg}", outside=True)
    if not folder.is_dir():
        raise Refused(404, f"目录不存在：{dir_arg}")
    dirs, files = [], []
    for child in folder.iterdir():
        if child.name.startswith(".") or child.is_symlink():
            continue
        if child.is_dir():
            if child.name not in SKIPPED_DIRS:
                dirs.append({"name": child.name, "path": str(child), "kind": "dir"})
        elif child.is_file():
            files.append({"name": child.name, "path": str(child), "kind": "file", "size": child.stat().st_size,
                          "readable": texttypes.readable(child)})
    entries = sorted(dirs, key=lambda item: item["name"].lower()) + sorted(files, key=lambda item: item["name"].lower())
    return {"dir": str(folder), "parent": str(folder.parent) if folder != root else "",
            "entries": entries[:MAX_ENTRIES], "truncated": len(entries) > MAX_ENTRIES}
