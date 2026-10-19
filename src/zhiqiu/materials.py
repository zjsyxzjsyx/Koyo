from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import texttypes


@dataclass(frozen=True)
class Material:
    kind: str
    source: str
    content_hash: str
    content: str
    reason: str = ""

    @property
    def lang(self) -> str:
        """The language of a local code or configuration file (empty for documents and remote text)."""
        return (texttypes.language(self.source) or "") if self.kind == "local" else ""

    def as_record(self) -> dict[str, str]:
        record = vars(self).copy()
        # Only code carries it, so a document's record (and every request built from it) is as before 9.0.
        if self.lang:
            record["lang"] = self.lang
        return record


@dataclass(frozen=True)
class Limits:
    max_chars: int = 500_000
    max_file_bytes: int = 2_000_000
    max_files: int = 500

    @classmethod
    def from_env(cls) -> "Limits":
        from .config import env_int
        return cls(env_int("ZHIQIU_MATERIALS_MAX_CHARS", cls.max_chars),
                   env_int("ZHIQIU_MATERIALS_MAX_FILE_BYTES", cls.max_file_bytes),
                   env_int("ZHIQIU_MATERIALS_MAX_FILES", cls.max_files))


@dataclass(frozen=True)
class Scan:
    materials: list[Material]
    # (path, why it was not read): binary, encoding, too large, matched by .gitignore.
    skipped: list[tuple[str, str]] = field(default_factory=list)


def check_total(materials: list[Material], what: str = "资料") -> None:
    limits = Limits.from_env()
    total = sum(len(item.content) for item in materials)
    if total > limits.max_chars:
        raise ValueError(f"{what}总文本约 {total:,} 字，超过上限 {limits.max_chars:,} 字"
                         f"（配置项 materials.max_chars）；请指定更小的范围或调高上限")


def _gitignore(top: Path):
    """A matcher for the .gitignore at the top of the material folder, or None."""
    if not texttypes._flag("ZHIQIU_MATERIALS_RESPECT_GITIGNORE", True):
        return None
    ignore = top / ".gitignore"
    if not ignore.is_file():
        return None
    import pathspec
    try:
        return pathspec.GitIgnoreSpec.from_lines(ignore.read_text(encoding="utf-8-sig", errors="replace").splitlines())
    except (OSError, ValueError):
        return None


def _walk(top: Path, skipped: list[tuple[str, str]]) -> list[Path]:
    """Readable files under a folder: no links, no hidden, dependency or build folders, nothing .gitignore
    excludes, no virtual environment (a folder holding pyvenv.cfg)."""
    ignore = _gitignore(top)
    found = []
    for folder, dirs, names in os.walk(top, followlinks=False):
        here = Path(folder)
        relative = here.relative_to(top)
        kept = []
        for name in sorted(dirs):
            child = here / name
            rel = (relative / name).as_posix() + "/"
            if (texttypes.excluded_dir(name) or child.is_symlink() or (child / "pyvenv.cfg").exists()
                    or (ignore is not None and ignore.match_file(rel))):
                continue
            kept.append(name)
        dirs[:] = kept
        for name in sorted(names):
            path = here / name
            if texttypes.excluded_file(name) or path.is_symlink() or not texttypes.readable(path):
                continue
            if ignore is not None and ignore.match_file((relative / name).as_posix()):
                skipped.append((str(path), "被 .gitignore 排除"))
                continue
            found.append(path)
    return sorted(found, key=lambda p: str(p).casefold())


def scan_local(path: Path, allow_empty: bool = False) -> Scan:
    """Read a file or a folder of text files, within the limits of the [materials] settings."""
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ValueError("不读取符号链接")
    path = path.resolve()
    if not path.exists():
        raise ValueError(f"资料不存在：{path}")
    limits = Limits.from_env()
    skipped: list[tuple[str, str]] = []
    if path.is_file():
        if not texttypes.readable(path):
            raise ValueError(f"不支持的文件类型：{path.name}（可读：{texttypes.describe_readable()}）")
        files = [path]
    elif path.is_dir():
        files = _walk(path, skipped)
    else:
        raise ValueError("资料路径必须是文件或目录")
    if len(files) > limits.max_files:
        raise ValueError(f"目录中可读的文件有 {len(files)} 个，超过上限 {limits.max_files} 个"
                         f"（配置项 materials.max_files）；请指定更小的范围或调高上限")
    result: list[Material] = []
    for file in files:
        size = file.stat().st_size
        if size > limits.max_file_bytes:
            if len(files) == 1:
                raise ValueError(f"文件超过 {limits.max_file_bytes / 1_000_000:g} MB（配置项 materials.max_file_bytes）：{file}")
            skipped.append((str(file), f"超过 {limits.max_file_bytes / 1_000_000:g} MB"))
            continue
        try:
            content = texttypes.read_text(file)
        except texttypes.NotText as exc:
            if len(files) == 1:
                raise ValueError(f"无法作为文本读取：{exc}") from exc
            skipped.append((str(file), str(exc).split("：", 1)[0]))
            continue
        if not content.strip():
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        result.append(Material("local", str(file), digest, content))
    if not result and not allow_empty:
        raise ValueError("指定范围没有可用的非空文本资料")
    check_total(result)
    return Scan(result, skipped)


def read_local(path: Path, allow_empty: bool = False) -> list[Material]:
    return scan_local(path, allow_empty).materials


# Sources the user chose; sending them to the model needs consent. "web" results are public supplements.
USER_SOURCE_KINDS = ("local", "url", "notion")


def consent_hash(materials: list[Material]) -> str:
    return combined_hash([item for item in materials if item.kind in USER_SOURCE_KINDS])


def combined_hash(materials: list[Material]) -> str:
    joined = "|".join(sorted(m.content_hash for m in materials))
    return hashlib.sha256(joined.encode("ascii")).hexdigest()
