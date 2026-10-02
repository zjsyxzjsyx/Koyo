from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Material:
    kind: str
    source: str
    content_hash: str
    content: str
    reason: str = ""

    def as_record(self) -> dict[str, str]:
        return vars(self).copy()


def read_local(path: Path, allow_empty: bool = False) -> list[Material]:
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ValueError("不读取符号链接")
    path = path.resolve()
    if not path.exists():
        raise ValueError(f"资料不存在：{path}")
    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = sorted((p for p in path.rglob("*") if p.is_file() and
                        p.suffix.lower() in (".md", ".txt") and
                        not any(part.startswith(".") for part in p.relative_to(path).parts) and
                        not p.is_symlink()), key=lambda p: str(p).casefold())
    else:
        raise ValueError("资料路径必须是文件或目录")
    if path.is_file() and path.suffix.lower() not in (".md", ".txt"):
        raise ValueError("只支持 .md 和 .txt 文件")
    if len(files) > 200:
        raise ValueError("目录中 md/txt 文件超过 200 个")
    seen: set[str] = set()
    result: list[Material] = []
    for file in files:
        if file.stat().st_size > 1_000_000:
            raise ValueError(f"文件超过 1 MB：{file}")
        try:
            content = file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"无法读取 UTF-8 资料：{file}") from exc
        if not content.strip():
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if digest not in seen:
            result.append(Material("local", str(file), digest, content))
            seen.add(digest)
    if not result and not allow_empty:
        raise ValueError("指定范围没有可用的非空 md/txt 资料")
    if sum(len(item.content) for item in result) > 100_000:
        raise ValueError("资料总文本超过 100,000 字符；请指定更小的范围")
    return result


def combined_hash(materials: list[Material]) -> str:
    joined = "|".join(sorted(m.content_hash for m in materials))
    return hashlib.sha256(joined.encode("ascii")).hexdigest()
