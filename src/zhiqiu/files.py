"""Local file tools: read already-approved material files, write notes under chosen roots."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable, Iterable

from .materials import Material, read_local
from .tools import ToolError, ToolSpec

TEXT_SUFFIXES = (".md", ".txt")
WRITE_MAX_CHARS = 100_000
PREVIEW_CHARS = 300

READ_FILE_PARAMS = {"type": "object", "additionalProperties": False, "required": ["path"],
                    "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 1000}}}
WRITE_FILE_PARAMS = {"type": "object", "additionalProperties": False, "required": ["path", "content"],
                     "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 1000},
                                    "content": {"type": "string", "minLength": 1, "maxLength": WRITE_MAX_CHARS}}}


def _roots_from_env(name: str) -> list[Path]:
    raw = os.getenv(name, "")
    return [Path(item).expanduser() for item in raw.split(os.pathsep) if item.strip()]


def write_roots_from_env() -> list[Path]:
    return _roots_from_env("ZHIQIU_WRITE_ROOTS")


def read_roots_from_env() -> list[Path]:
    return _roots_from_env("ZHIQIU_READ_ROOTS")


def read_file_spec(readable: Callable[[], Iterable[str]]) -> ToolSpec:
    """Read one file the user already approved for this step; readable() lists those paths."""

    def allowed(args: dict) -> Path:
        path = Path(args["path"]).expanduser()
        if path.is_symlink():
            raise ToolError("denied", "不读取符号链接", rule="read_root")
        resolved = path.resolve()
        if str(resolved) not in {str(Path(item).resolve()) for item in readable()}:
            raise ToolError("denied", f"只能读取本次已授权的本地资料：{args['path']}", rule="read_root")
        return resolved

    def handler(args: dict) -> str:
        return read_local(allowed(args))[0].content

    def precheck(args: dict) -> None:
        allowed(args)

    return ToolSpec("read_file", "Read the full text of one local material file listed as an approved source.",
                    READ_FILE_PARAMS, "read", handler, source="zhiqiu.files", max_output_chars=20_000,
                    precheck=precheck)


def read_target(path_arg: str, roots: list[Path]) -> Path:
    """Resolve a chat read request; it must be an existing md/txt file inside one of the read roots."""
    if not roots:
        raise ToolError("denied", "未配置 ZHIQIU_READ_ROOTS，对话中不能读文件；请先把目录加入白名单", rule="read_root")
    path = Path(path_arg).expanduser()
    if not path.is_absolute():
        path = roots[0] / path
    if ".." in path.parts:
        raise ToolError("denied", f"路径中不能含 ..：{path_arg}", rule="read_root")
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise ToolError("invalid_args", "只能读 .md 或 .txt 文件")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ToolError("denied", f"路径中含符号链接：{part}", rule="read_root")
    resolved = path.resolve()
    if not any(resolved.is_relative_to(root.resolve()) for root in roots):
        raise ToolError("denied", f"不在 ZHIQIU_READ_ROOTS 白名单目录内：{path_arg}；请先把目录加入白名单",
                        rule="read_root")
    if not resolved.is_file():
        raise ToolError("not_found", f"文件不存在：{path_arg}")
    return resolved


def read_root_file_spec(roots: list[Path], on_read: Callable[[Material], None] | None = None) -> ToolSpec:
    """Chat-time read_file: any md/txt file under the configured read roots, no per-call prompt.

    Configuring a root is the user's consent to send its files to the model endpoint.
    """
    roots = list(roots)

    def handler(args: dict) -> str:
        try:
            material = read_local(read_target(args["path"], roots))[0]
        except ValueError as exc:
            if isinstance(exc, ToolError):
                raise
            raise ToolError("invalid_args", str(exc)) from exc
        if on_read:
            on_read(material)
        return material.content

    def precheck(args: dict) -> None:
        read_target(args["path"], roots)

    where = "；".join(str(root) for root in roots) or "（未配置）"
    return ToolSpec("read_file", f"Read one .md or .txt file under the user's readable directories: {where}. "
                    "Relative paths are taken from the first directory.",
                    READ_FILE_PARAMS, "read", handler, source="zhiqiu.files", max_output_chars=20_000,
                    precheck=precheck)


def write_target(path_arg: str, roots: list[Path]) -> Path:
    """Resolve a requested note path; it must be a md/txt file inside one of the write roots."""
    if not roots:
        raise ToolError("denied", "未配置 ZHIQIU_WRITE_ROOTS，不能写文件", rule="write_root")
    path = Path(path_arg).expanduser()
    if not path.is_absolute():
        path = roots[0] / path
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise ToolError("invalid_args", "只能写 .md 或 .txt 文件")
    # Refuse links anywhere on the way, then compare the resolved path with the resolved roots.
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ToolError("denied", f"路径中含符号链接：{part}", rule="write_root")
    resolved = path.resolve()
    if not any(resolved.is_relative_to(root.resolve()) for root in roots):
        raise ToolError("denied", f"超出可写目录：{path_arg}", rule="write_root")
    if resolved.exists() and not resolved.is_file():
        raise ToolError("invalid_args", f"目标不是文件：{path_arg}")
    return resolved


def write_file_spec(roots: list[Path]) -> ToolSpec:
    roots = list(roots)

    def describe(args: dict) -> str:
        target = write_target(args["path"], roots)
        action = "覆盖（原文件先备份为 .bak）" if target.exists() else "新建"
        preview = args["content"][:PREVIEW_CHARS] + ("…" if len(args["content"]) > PREVIEW_CHARS else "")
        return f"写入文件：{target}\n操作：{action}，共 {len(args['content'])} 字\n内容预览：\n{preview}"

    def handler(args: dict) -> dict:
        target = write_target(args["path"], roots)
        existed = target.exists()
        if existed:
            shutil.copy2(target, target.with_name(target.name + ".bak"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args["content"], encoding="utf-8")
        return {"path": str(target), "action": "overwritten" if existed else "created",
                "chars": len(args["content"])}

    def precheck(args: dict) -> None:
        write_target(args["path"], roots)

    where = "；".join(str(root) for root in roots)
    return ToolSpec("write_file", f"Write a Markdown or text note (new or overwrite, never delete) under: {where}. "
                    "Relative paths are placed in the first directory. Every write needs the user's confirmation.",
                    WRITE_FILE_PARAMS, "write_file", handler, source="zhiqiu.files", requires_confirmation=True,
                    precheck=precheck, describe=describe)
