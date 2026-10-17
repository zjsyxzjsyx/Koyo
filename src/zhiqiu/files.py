"""Local file tools: read already-approved material files, write notes under chosen roots."""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .materials import Material, read_local
from .tools import ToolError, ToolSpec

TEXT_SUFFIXES = (".md", ".txt")
ADD_ROOT_HINT = "请先在工作台“目录白名单”页（或 ZHIQIU_READ_ROOTS）中加入这个目录"
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
        raise ToolError("denied", f"还没有可读目录，对话中不能读文件；{ADD_ROOT_HINT}", rule="read_root")
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
        raise ToolError("denied", f"不在可读目录白名单内：{path_arg}；{ADD_ROOT_HINT}", rule="read_root")
    if not resolved.is_file():
        raise ToolError("not_found", f"文件不存在：{path_arg}")
    return resolved


Roots = Sequence[Path] | Callable[[], Sequence[Path]]


def _provider(roots: Roots) -> Callable[[], list[Path]]:
    """Roots given as a list stay fixed; given as a function they are read at every call."""
    if callable(roots):
        return lambda: list(roots())
    fixed = list(roots)
    return lambda: fixed


def read_root_file_spec(roots: Roots, on_read: Callable[[Material], None] | None = None) -> ToolSpec:
    """Chat-time read_file: any md/txt file under the configured read roots, no per-call prompt.

    Configuring a root is the user's consent to send its files to the model endpoint. The roots
    are listed in the chat context each turn, so the description does not repeat them.
    """
    current = _provider(roots)

    def handler(args: dict) -> str:
        try:
            material = read_local(read_target(args["path"], current()))[0]
        except ValueError as exc:
            if isinstance(exc, ToolError):
                raise
            raise ToolError("invalid_args", str(exc)) from exc
        if on_read:
            on_read(material)
        return material.content

    def precheck(args: dict) -> None:
        read_target(args["path"], current())

    return ToolSpec("read_file", "Read one .md or .txt file under the user's readable directories "
                    "(listed in the context as 可读目录). Relative paths are taken from the first directory.",
                    READ_FILE_PARAMS, "read", handler, source="zhiqiu.files", max_output_chars=20_000,
                    precheck=precheck)


def write_target(path_arg: str, roots: list[Path]) -> Path:
    """Resolve a requested note path; it must be a md/txt file inside one of the write roots."""
    if not roots:
        raise ToolError("denied", "还没有可写目录，不能写文件（在工作台“目录白名单”页或 ZHIQIU_WRITE_ROOTS 中添加）",
                        rule="write_root")
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


def write_file_spec(roots: Roots) -> ToolSpec:
    current = _provider(roots)

    def describe(args: dict) -> str:
        target = write_target(args["path"], current())
        action = "覆盖（原文件先备份为 .bak）" if target.exists() else "新建"
        preview = args["content"][:PREVIEW_CHARS] + ("…" if len(args["content"]) > PREVIEW_CHARS else "")
        return f"写入文件：{target}\n操作：{action}，共 {len(args['content'])} 字\n内容预览：\n{preview}"

    def handler(args: dict) -> dict:
        target = write_target(args["path"], current())
        existed = target.exists()
        if existed:
            shutil.copy2(target, target.with_name(target.name + ".bak"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args["content"], encoding="utf-8")
        return {"path": str(target), "action": "overwritten" if existed else "created",
                "chars": len(args["content"])}

    def precheck(args: dict) -> None:
        write_target(args["path"], current())

    return ToolSpec("write_file", "Write a Markdown or text note (new or overwrite, never delete) under the "
                    "user's writable directories (listed in the context as 可写目录). Relative paths are placed "
                    "in the first directory. Every write needs the user's confirmation.",
                    WRITE_FILE_PARAMS, "write_file", handler, source="zhiqiu.files", requires_confirmation=True,
                    precheck=precheck, describe=describe)

# ----- roots added on the workbench page: checked here, stored by Store, merged by FileRoots -----

ROOTS_MAX = 20
SCAN_MAX_ENTRIES = 20_000
SCAN_SECONDS = 3.0
ROOT_SAMPLES = 5
BIG_ROOT_FILES = 1000
ROOT_PATH_CHARS = 1000
_KIND_NAMES = {"read": "可读", "write": "可写"}
_POSIX_SYSTEM = ("/etc", "/usr", "/bin", "/sbin", "/lib", "/var", "/proc", "/sys", "/dev", "/boot", "/private",
                 "/System", "/Library", "/Applications")
_NOTICES = {
    "read": "其中有 {files} 个 .md/.txt 文件；添加后，对话中知秋可以读取这些文件，内容会发送到模型端点。",
    "write": "添加后，知秋可以在这个目录下新建或覆盖 .md/.txt 文件（覆盖前先备份为 .bak）；每次写入都要你批准。",
}


class RootRefused(ValueError):
    """A directory that may not become a root; the message says why, for the page."""


@dataclass
class RootPreview:
    kind: str
    path: str
    input: str
    files: int
    truncated: bool
    samples: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notice: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def system_dirs() -> list[Path]:
    """Directories (with everything under them) that never become roots on this machine."""
    if os.name == "nt":
        found = [os.getenv(name) for name in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")]
        return [Path(item) for item in found if item] + [Path.home() / "AppData"]
    return [Path(item) for item in _POSIX_SYSTEM]


def _key(path: Path | str) -> Path:
    # Windows paths compare without case; the key is only for comparing, never stored or shown.
    return Path(os.path.normcase(os.path.normpath(str(path))))


def _within(path: Path | str, parent: Path | str) -> bool:
    return _key(path).is_relative_to(_key(parent))


def _real(path: Path | str) -> Path:
    try:
        return Path(path).resolve()
    except OSError:
        return Path(path)


def check_root(kind: str, raw: Any, existing: Sequence[Path | str], stored: int, *,
               home: Path | None = None, system: Sequence[Path] | None = None,
               skills: Path | None = None) -> RootPreview:
    """Check a pasted directory before it becomes a read or write root and count what it holds.

    `existing` are the current roots of this kind (environment and page), `stored` how many the page
    added. Read-only on the file system; raises RootRefused with the reason for the first rule broken."""
    if kind not in _KIND_NAMES:
        raise RootRefused(f"目录类别只能是 read 或 write：{kind}")
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        raise RootRefused("请输入目录的绝对路径")
    if len(text) > ROOT_PATH_CHARS:
        raise RootRefused(f"路径过长：超过 {ROOT_PATH_CHARS} 字")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise RootRefused("路径里有控制字符")
    if text.startswith(("\\\\", "//")):
        raise RootRefused("不支持网络路径（\\\\server\\share）或 \\\\?\\ 前缀，请用本机目录")
    if text.startswith("~") or "%" in text or "$" in text:
        raise RootRefused("请直接输入绝对路径，不要用 ~ 或环境变量")
    path = Path(text)
    if not path.is_absolute():
        raise RootRefused("请输入绝对路径，例如 D:\\notes\\算法")
    if not path.exists():
        raise RootRefused(f"目录不存在：{text}")
    if not path.is_dir():
        raise RootRefused(f"这是一个文件，不是目录：{text}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RootRefused(f"无法解析这个路径：{exc}") from exc
    if resolved.parent == resolved:
        raise RootRefused("不能把整个磁盘或根目录加入白名单，请选更具体的子目录")
    home = _real(home or Path.home())
    if _key(resolved) == _key(home):
        raise RootRefused("不能把用户主目录本身加入白名单，请选更具体的子目录")
    if _within(home, resolved):
        raise RootRefused("不能把用户主目录的上级目录加入白名单，请选更具体的子目录")
    if kind == "write":
        # The user's Skill directory is instructions for the teaching prompts; nothing the agent can call
        # may write there.
        from .skills import skills_dir
        guarded = _real(skills if skills is not None else skills_dir())
        if _within(guarded, resolved):
            raise RootRefused("这个目录包含 Skill 目录，知秋不能写入 Skill，请选别的目录")
        if _within(resolved, guarded):
            raise RootRefused("这个目录在 Skill 目录里，知秋不能写入 Skill，请选别的目录")
    for blocked in (system if system is not None else system_dirs()):
        if _within(resolved, _real(blocked)):
            raise RootRefused(f"不能把系统目录加入白名单：{blocked}")
    warnings = []
    for item in existing:
        current = _real(item)
        if _key(current) == _key(resolved):
            raise RootRefused(f"已在白名单中：{current}")
        if _within(resolved, current):
            raise RootRefused(f"已包含在 {current} 中，不需要再加")
        if _within(current, resolved):
            warnings.append(f"将覆盖已有的 {current}")
    if stored >= ROOTS_MAX:
        raise RootRefused(f"工作台最多添加 {ROOTS_MAX} 个{_KIND_NAMES[kind]}目录，请先移除不用的")
    files, truncated, samples = _count_texts(resolved)
    if truncated:
        warnings.append(f"目录太大，只数了前 {SCAN_MAX_ENTRIES} 项或 {SCAN_SECONDS:g} 秒，实际文件更多")
    elif files > BIG_ROOT_FILES:
        warnings.append("目录很大，建议选更具体的子目录")
    if kind == "read" and files == 0 and not truncated:
        warnings.append("这个目录里没有 .md/.txt 文件")
    notice = _NOTICES[kind].format(files=f"至少 {files}" if truncated else files)
    return RootPreview(kind, str(resolved), text, files, truncated, samples, warnings, notice)


def _count_texts(top: Path) -> tuple[int, bool, list[str]]:
    """md/txt files under a directory without following links, within an entry and a time budget."""
    deadline = time.monotonic() + SCAN_SECONDS
    files, entries, samples = 0, 0, []
    for folder, dirs, names in os.walk(top, followlinks=False):
        dirs.sort()
        entries += len(dirs)
        for name in sorted(names):
            entries += 1
            if entries > SCAN_MAX_ENTRIES or time.monotonic() > deadline:
                return files, True, samples
            if name.lower().endswith(TEXT_SUFFIXES):
                files += 1
                if len(samples) < ROOT_SAMPLES:
                    samples.append(str((Path(folder) / name).relative_to(top)))
        if entries > SCAN_MAX_ENTRIES or time.monotonic() > deadline:
            return files, True, samples
    return files, False, samples


def merge_roots(*groups: Iterable[Path | str]) -> list[Path]:
    """The roots in order, each once (Windows paths compare without case)."""
    merged, seen = [], set()
    for group in groups:
        for item in group:
            key = _key(_real(item))
            if key not in seen:
                seen.add(key)
                merged.append(Path(item))
    return merged


class FileRoots:
    """Read and write roots: the environment's first (relative paths land in the first one), then the
    ones added on the workbench page. Callers take a snapshot at the start of each turn."""

    def __init__(self, env_read: Iterable[Path] = (), env_write: Iterable[Path] = ()):
        self.env = {"read": list(env_read), "write": list(env_write)}

    @classmethod
    def from_env(cls) -> "FileRoots":
        return cls(read_roots_from_env(), write_roots_from_env())

    def current(self, store: Any, kind: str) -> list[Path]:
        return merge_roots(self.env[kind], (row["path"] for row in store.roots(kind)))

    def listing(self, store: Any) -> dict[str, list[dict[str, Any]]]:
        """Every root with where it came from, for the page; an environment root shadows the same stored one."""
        out: dict[str, list[dict[str, Any]]] = {}
        for kind in ("read", "write"):
            items, seen = [], set()
            for path in self.env[kind]:
                key = _key(_real(path))
                if key not in seen:
                    seen.add(key)
                    items.append({"path": str(path), "source": "env", "exists": Path(path).is_dir(),
                                  "files": None, "truncated": False, "added_at": None})
            for row in store.roots(kind):
                if _key(_real(row["path"])) not in seen:
                    items.append({"path": row["path"], "source": "workbench", "exists": Path(row["path"]).is_dir(),
                                  "files": row["files"], "truncated": bool(row["truncated"]),
                                  "added_at": row["added_at"]})
            out[kind] = items
        return out
