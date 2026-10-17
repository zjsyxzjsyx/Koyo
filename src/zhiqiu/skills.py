"""Teaching Skills: how to teach and how to answer, kept as versioned SKILL.md files.

The rules (data rule, no invented facts, no internal records, no promised program actions) and the output
structure stay in context.py; a Skill only says how to teach. The model sees the catalog (name, title and
description) while assessing the material and picks one; a Skill's body is read only when a teach or
clarify prompt is built (progressive disclosure). Built-in Skills ship with the package; the user may add
their own under ZHIQIU_SKILLS_DIR. Nothing the agent can call writes either directory.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

BUILTIN_DIR = Path(__file__).resolve().parent / "skills"
GENERAL = "general"
MAX_SKILLS = 20
STAGES = ("teach", "clarify")
SOURCES = ("builtin", "user")
_KEYS = ("name", "version", "title", "description", "stages", "sections", "section_chars", "answer_chars",
         "diagrams")
_REQUIRED = ("name", "version", "title", "description", "stages")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,29}$")
_HEADING = re.compile(r"^##\s+(\S+)\s*$")
TITLE_CHARS = 20
DESCRIPTION_CHARS = 120
BODY_CHARS = (30, 1500)
# Defaults are the v14 teach and clarify numbers.
DEFAULT_SECTIONS = (3, 5)
DEFAULT_SECTION_CHARS = 300
DEFAULT_ANSWER_CHARS = 200
LIMITS = {"sections": (1, 8), "section_chars": (100, 800), "answer_chars": (80, 500), "diagrams": (0, 3)}


class SkillError(ValueError):
    """A SKILL.md that cannot be used; the message names the file and the reason."""


@dataclass(frozen=True)
class Skill:
    name: str
    version: int
    title: str
    description: str
    stages: tuple[str, ...]
    sections: tuple[int, int]
    section_chars: int
    answer_chars: int
    diagrams: int
    source: str
    path: str
    sha: str
    bodies: tuple[tuple[str, str], ...]

    @property
    def label(self) -> str:
        return f"{self.name}@{self.version}"

    def body(self, stage: str) -> str | None:
        return dict(self.bodies).get(stage)

    def catalog_entry(self) -> dict[str, str]:
        return {"name": self.name, "title": self.title, "description": self.description}

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "title": self.title, "description": self.description,
                "stages": list(self.stages), "sections": list(self.sections), "section_chars": self.section_chars,
                "answer_chars": self.answer_chars, "diagrams": self.diagrams, "source": self.source,
                "sha": self.sha, "path": self.path}


def skill_stage(stage: str) -> str:
    """The SKILL.md section a context stage reads: the final answer after chunked checks is a clarify."""
    return "clarify" if stage == "clarify_combine" else stage


def skills_dir() -> Path:
    configured = os.getenv("ZHIQIU_SKILLS_DIR")
    if configured:
        return Path(configured)
    root = os.getenv("LOCALAPPDATA")
    return (Path(root) if root else Path.home() / ".local" / "share") / "zhiqiu" / "skills"


def _sha(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()[:12]


def _value(raw: str) -> Any:
    if re.fullmatch(r"\d+", raw):
        return int(raw)
    if match := re.fullmatch(r"(\d+)\s*-\s*(\d+)", raw):
        return (int(match.group(1)), int(match.group(2)))
    if raw.startswith("[") and raw.endswith("]"):
        return tuple(item.strip() for item in raw[1:-1].split(",") if item.strip())
    return raw


def parse_text(text: str, where: str, source: str, expected_name: str | None = None) -> Skill:
    """One SKILL.md: a small front matter (key: value lines) and one `## stage` section per stage."""
    def fail(reason: str) -> SkillError:
        return SkillError(f"{where}：{reason}")

    if source not in SOURCES:
        raise ValueError(f"unknown skill source: {source}")
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        raise fail("开头必须是 --- 包住的前置信息")
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        raise fail("前置信息缺少结尾的 ---") from None
    meta: dict[str, Any] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, colon, raw = line.partition(":")
        key = key.strip()
        if not colon or not key:
            raise fail(f"前置信息只认 key: value 的写法：{line.strip()}")
        if key not in _KEYS:
            raise fail(f"不认识的键：{key}")
        if key in meta:
            raise fail(f"键重复：{key}")
        meta[key] = _value(raw.strip())
    missing = [key for key in _REQUIRED if key not in meta]
    if missing:
        raise fail(f"缺少必填项：{'、'.join(missing)}")
    name = meta["name"]
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise fail("name 只能用小写字母、数字和连字符，2 到 30 个字符")
    if expected_name is not None and name != expected_name:
        raise fail(f"name（{name}）必须与目录名（{expected_name}）相同")
    version = meta["version"]
    if not isinstance(version, int) or version < 1:
        raise fail("version 必须是正整数")
    title, description = meta["title"], meta["description"]
    if not isinstance(title, str) or not title or len(title) > TITLE_CHARS:
        raise fail(f"title 必须是 1 到 {TITLE_CHARS} 字的一行文字")
    if not isinstance(description, str) or not description or len(description) > DESCRIPTION_CHARS:
        raise fail(f"description 必须是 1 到 {DESCRIPTION_CHARS} 字的一行文字")
    stages = meta["stages"]
    if isinstance(stages, str):
        stages = (stages,)
    if (not isinstance(stages, tuple) or not stages or len(set(stages)) != len(stages)
            or any(stage not in STAGES for stage in stages)):
        raise fail(f"stages 只能从 {', '.join(STAGES)} 中选，不能重复")
    if "teach" not in stages:
        raise fail("stages 必须包含 teach")
    sections = meta.get("sections", DEFAULT_SECTIONS)
    if isinstance(sections, int):
        sections = (sections, sections)
    low, high = LIMITS["sections"]
    if not isinstance(sections, tuple) or len(sections) != 2 or not all(isinstance(item, int) for item in sections) \
            or not low <= sections[0] <= sections[1] <= high:
        raise fail(f"sections 必须是 {low} 到 {high} 之间的范围，例如 3-5")
    numbers = {}
    for key, default in (("section_chars", DEFAULT_SECTION_CHARS), ("answer_chars", DEFAULT_ANSWER_CHARS),
                         ("diagrams", 0)):
        value = meta.get(key, default)
        low, high = LIMITS[key]
        if not isinstance(value, int) or not low <= value <= high:
            raise fail(f"{key} 必须是 {low} 到 {high} 之间的整数")
        numbers[key] = value
    bodies: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[end + 1:]:
        match = _HEADING.match(line)
        if match:
            current = match.group(1)
            if current in bodies:
                raise fail(f"阶段 {current} 写了两次")
            bodies[current] = []
        elif current is None:
            if line.strip():
                raise fail("正文必须以 ## teach 这样的阶段标题开头")
        else:
            bodies[current].append(line)
    extra = [stage for stage in bodies if stage not in stages]
    if extra:
        raise fail(f"正文里有 stages 没列出的阶段：{'、'.join(extra)}")
    texts: list[tuple[str, str]] = []
    for stage in stages:
        if stage not in bodies:
            raise fail(f"缺少 ## {stage} 一节")
        body = "\n".join(bodies[stage]).strip()
        low, high = BODY_CHARS
        if not low <= len(body) <= high:
            raise fail(f"## {stage} 一节要 {low} 到 {high} 字，现在 {len(body)} 字")
        if "{" in body or "}" in body:
            raise fail(f"## {stage} 一节不能有花括号（不做变量替换）")
        texts.append((stage, body))
    return Skill(name, version, title, description, tuple(stages), tuple(sections), numbers["section_chars"],
                 numbers["answer_chars"], numbers["diagrams"], source, where, _sha(text), tuple(texts))


def parse(path: Path, source: str) -> Skill:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillError(f"{path}：读不出来（{type(exc).__name__}）") from exc
    return parse_text(text, str(path), source, path.parent.name)


def _files(top: Path) -> list[Path]:
    if not top.is_dir():
        return []
    return sorted(child / "SKILL.md" for child in top.iterdir() if (child / "SKILL.md").is_file())


def _overlaps(a: Path, b: Path) -> bool:
    def key(path: Path) -> Path:
        try:
            path = path.resolve()
        except OSError:
            pass
        return Path(os.path.normcase(os.path.normpath(str(path))))
    first, second = key(a), key(b)
    return first.is_relative_to(second) or second.is_relative_to(first)


class Library:
    """Built-in Skills (a broken one is a bug and raises) and the user's (a broken one is skipped and
    reported). The user directory is scanned again whenever a SKILL.md under it changed."""

    def __init__(self, user_dir: Path | None = None, *, builtin_dir: Path = BUILTIN_DIR,
                 write_roots: Iterable[Path] = (), include_user: bool = True):
        self.builtin_dir = builtin_dir
        self.include_user = include_user
        self.user_dir = user_dir if user_dir is not None else skills_dir()
        self.write_roots = list(write_roots)
        self._builtin = self._load_builtin()
        self._signature: tuple[Any, ...] | None = None
        self._user: dict[str, Skill] = {}
        self.errors: list[str] = []
        self.refresh()

    def _load_builtin(self) -> dict[str, Skill]:
        found: dict[str, Skill] = {}
        for path in _files(self.builtin_dir):
            skill = parse(path, "builtin")
            found[skill.name] = skill
        general = found.get(GENERAL)
        if general is None or set(general.stages) != set(STAGES):
            raise SkillError(f"{self.builtin_dir}：缺少同时包含 teach 和 clarify 的内置 Skill {GENERAL}")
        if len(found) > MAX_SKILLS:
            raise SkillError(f"{self.builtin_dir}：内置 Skill 超过 {MAX_SKILLS} 个")
        return found

    def set_write_roots(self, roots: Sequence[Path]) -> None:
        if list(roots) != self.write_roots:
            self.write_roots = list(roots)
            self._signature = None

    def refresh(self) -> None:
        """Re-read the user directory if anything in it changed."""
        files = _files(self.user_dir) if self.include_user else []
        signature: tuple[Any, ...] = (tuple(str(root) for root in self.write_roots),) + tuple(
            (str(path), *self._stat(path)) for path in files)
        if signature == self._signature:
            return
        self._signature = signature
        user: dict[str, Skill] = {}
        errors: list[str] = []
        blocked = next((root for root in self.write_roots if _overlaps(root, self.user_dir)), None)
        if blocked is not None and files:
            errors.append(f"Skill 目录 {self.user_dir} 和可写目录 {blocked} 重叠，知秋可能写进 Skill，"
                          "已停用全部用户 Skill；请把它们分开")
            files = []
        for path in files:
            try:
                skill = parse(path, "user")
            except SkillError as exc:
                errors.append(str(exc))
                continue
            if skill.name in self._builtin:
                errors.append(f"{path}：和内置 Skill 同名（{skill.name}），已跳过；请换一个名字")
            elif len(self._builtin) + len(user) >= MAX_SKILLS:
                errors.append(f"{path}：Skill 总数超过 {MAX_SKILLS} 个，已跳过")
            else:
                user[skill.name] = skill
        self._user, self.errors = user, errors

    @staticmethod
    def _stat(path: Path) -> tuple[int, int]:
        try:
            info = path.stat()
        except OSError:
            return (0, 0)
        return (info.st_mtime_ns, info.st_size)

    def all(self) -> list[Skill]:
        builtin = sorted(self._builtin.values(), key=lambda skill: (skill.name != GENERAL, skill.name))
        return builtin + sorted(self._user.values(), key=lambda skill: skill.name)

    def get(self, name: str | None) -> Skill | None:
        if not name:
            return None
        return self._builtin.get(name) or self._user.get(name)

    def general(self) -> Skill:
        return self._builtin[GENERAL]

    def catalog(self) -> list[dict[str, str]]:
        """What the model sees while choosing: never a Skill's body."""
        return [skill.catalog_entry() for skill in self.all()]

    def find(self, text: str) -> Skill | None:
        """A Skill by name or by title, as a user types it after /skill."""
        wanted = text.strip()
        return self.get(wanted.lower()) or next((skill for skill in self.all() if skill.title == wanted), None)

    def body(self, skill: Skill, stage: str) -> tuple[Skill, str]:
        """The Skill whose text a stage uses, and that text: a Skill without a clarify section answers
        with the general one."""
        stage = skill_stage(stage)
        text = skill.body(stage)
        if text is not None:
            return skill, text
        general = self.general()
        return general, general.body(stage) or ""

    def status(self) -> dict[str, Any]:
        return {"count": len(self._builtin) + len(self._user), "errors": list(self.errors),
                "dir": str(self.user_dir)}
