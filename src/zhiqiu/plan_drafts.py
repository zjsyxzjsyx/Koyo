"""Turning a model's proposed study plan into a draft the program trusts.

The model sees only an outline of the user's material (paths and headings, never the text: PRD 10.0 §9
decision 3) and proposes units; `check` keeps what is well formed, repairs what can be repaired (order,
minutes, unknown sources) and refuses the rest (a cycle, too many units). Whether a unit was learned
before and which sources exist are decided here from the store and the scan, never by the model.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import planner
from .materials import Material

TITLE_CHARS, GOAL_CHARS, PLAN_TITLE_CHARS, SUMMARY_CHARS, NOTE_CHARS = 60, 120, 40, 200, 60
QUESTIONS = 4
# Headings and paths sent to the model, at most; deeper headings go first when there is more.
OUTLINE_CHARS = 20_000
# A unit title that is only one of these says nothing on its own and would clash across plans.
GENERIC_TITLES = frozenset({"概述", "简介", "总结", "介绍", "入门", "基础", "导论", "引言", "小结", "回顾", "练习",
                            "overview", "introduction", "intro", "summary", "basics", "review"})
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
EXISTING_POINTS = 60


class DraftInvalid(ValueError):
    """The model's plan cannot be used as it is; the message says why (and is shown to the model on retry)."""

    def __init__(self, message: str, *, limit: bool = False):
        super().__init__(message)
        self.limit = limit


@dataclass
class Signals:
    """What the plan stage may know about the user's material: an outline, never the text."""
    files: list[dict[str, Any]] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    notion: list[str] = field(default_factory=list)
    web: list[dict[str, str]] = field(default_factory=list)
    note: str = ""

    @property
    def paths(self) -> set[str]:
        return {item["path"] for item in self.files}

    def as_input(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.files:
            data["local_files"] = self.files
        if self.urls:
            data["urls"] = self.urls
        if self.notion:
            data["notion_pages"] = self.notion
        if self.web:
            data["web_outline"] = self.web
        if self.note:
            data["outline_note"] = self.note
        return data

    def lines(self) -> list[str]:
        """What the confirmation says will be sent."""
        lines = []
        if self.files:
            lines.append(f"本地资料：{len(self.files)} 个文件的路径和标题（不发送正文）")
        if self.note:
            lines.append(self.note)
        if self.urls:
            lines.append(f"网页地址 {len(self.urls)} 个（只发送地址，不获取正文）")
        if self.notion:
            lines.append(f"Notion 页面 {len(self.notion)} 个（只发送链接，不读取内容）")
        return lines


def _headings(material: Material) -> list[str]:
    text = material.content
    if material.lang == "python":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        return [f"{'class' if isinstance(node, ast.ClassDef) else 'def'} {node.name}" for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    if material.lang:
        return []
    found = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        match = None if fenced else _HEADING.match(line)
        if match:
            found.append(f"{match.group(1)} {match.group(2)[:80]}")
    return found


def outline(materials: Sequence[Material], urls: Iterable[str] = (), notion: Iterable[str] = (),
            limit: int = OUTLINE_CHARS) -> Signals:
    """Paths and headings of local material (# to ###; for Python, top-level def and class names).

    Over `limit` characters, level-3 headings go first, then level 2, then all headings."""
    files = [{"path": item.source, "headings": _headings(item)} for item in materials if item.kind == "local"]
    signals = Signals(files, list(urls), list(notion))

    def size() -> int:
        return sum(len(item["path"]) + sum(len(h) for h in item["headings"]) for item in signals.files)

    for level in ("### ", "## "):
        if size() <= limit:
            break
        for item in signals.files:
            item["headings"] = [h for h in item["headings"] if not h.startswith(level)]
        signals.note = "资料目录较大，只发送了较高层级的标题"
    if size() > limit:
        for item in signals.files:
            item["headings"] = []
        signals.note = f"资料目录较大，只发送了 {len(signals.files)} 个文件的路径"
    while signals.files and size() > limit:
        signals.files.pop()
        signals.note = f"资料目录较大，只发送了前 {len(signals.files)} 个文件的路径"
    return signals


def existing_points(db: Any, limit: int = EXISTING_POINTS) -> list[dict[str, Any]]:
    """Points the user already has (title, state, latest total), so a plan can build on them."""
    rows = db.execute("""SELECT k.title,k.state,(SELECT r.total FROM attempts a JOIN reviews r ON r.attempt_id=a.id
        WHERE a.kp_id=k.id AND r.total IS NOT NULL ORDER BY r.created_at DESC LIMIT 1) total
        FROM knowledge_points k ORDER BY k.created_at DESC LIMIT ?""", (limit,)).fetchall()
    return [{"title": row["title"], "state": row["state"], "latest_total": row["total"]} for row in rows]


def _norm(title: str) -> str:
    return "".join(title.split()).casefold()


def _text(value: Any, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _topological(units: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Units ordered so every prerequisite comes first, keeping the model's order where it can."""
    by_key = {unit["key"]: unit for unit in units}
    done: list[str] = []
    state: dict[str, int] = {}

    def visit(key: str, path: tuple[str, ...]) -> None:
        if state.get(key) == 2:
            return
        if state.get(key) == 1:
            cycle = " → ".join(by_key[k]["title"] for k in path[path.index(key):] + (key,))
            raise DraftInvalid(f"前置关系成环：{cycle}")
        state[key] = 1
        for req in by_key[key]["requires"]:
            visit(req, path + (key,))
        state[key] = 2
        done.append(key)

    for unit in units:
        visit(unit["key"], ())
    ordered = [by_key[key] for key in done]
    return ordered, [u["key"] for u in ordered] != [u["key"] for u in units]


def check(raw: Any, *, signals: Signals, max_units: int, unit_minutes: int,
          web_urls: Iterable[str] = ()) -> dict[str, Any]:
    """The model's draft made trustworthy: {title, summary, basis, basis_note, suggest_web, suggest_web_reason,
    questions, units, notes}. Raises DraftInvalid when it cannot be used."""
    if not isinstance(raw, dict):
        raise DraftInvalid("计划必须是 JSON 对象")
    units_raw = raw.get("units")
    if not isinstance(units_raw, list) or not units_raw:
        raise DraftInvalid("units 必须是非空数组")
    if len(units_raw) > max_units:
        raise DraftInvalid(f"一个计划最多 {max_units} 个知识点，这份有 {len(units_raw)} 个；请合并相近的知识点，"
                           "或者只规划第一阶段", limit=True)
    known_urls = set(signals.urls) | set(web_urls)
    notes: list[str] = []
    downgraded = []
    units, keys, titles = [], set(), set()
    for index, item in enumerate(units_raw, 1):
        if not isinstance(item, dict):
            raise DraftInvalid(f"第 {index} 个单元不是对象")
        key = item.get("key")
        if not isinstance(key, str) or not _KEY.match(key) or key in keys:
            raise DraftInvalid(f"第 {index} 个单元的 key 无效或重复：{key!r}（只用小写字母、数字和下划线）")
        title = _text(item.get("title"), 10_000)
        if not title or len(title) > TITLE_CHARS:
            raise DraftInvalid(f"第 {index} 个单元的标题须为 1 到 {TITLE_CHARS} 字")
        if _norm(title) in GENERIC_TITLES:
            raise DraftInvalid(f"单元标题“{title}”太笼统，请写明主题，例如“Redis 持久化概述”")
        if _norm(title) in titles:
            raise DraftInvalid(f"单元标题重复：{title}")
        requires = item.get("requires") or []
        if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
            raise DraftInvalid(f"“{title}”的 requires 必须是 key 的数组")
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        kind, ref = source.get("kind"), source.get("ref") if isinstance(source.get("ref"), str) else ""
        valid = ((kind == "local" and ref in signals.paths) or (kind == "url" and ref in known_urls)
                 or (kind == "notion" and ref in signals.notion))
        if not valid:
            if kind not in (None, "lesson_web", "") or ref:
                downgraded.append(title)
            kind, ref = "lesson_web", ""
        keys.add(key)
        titles.add(_norm(title))
        units.append({"key": key, "title": title, "goal": _text(item.get("goal"), GOAL_CHARS),
                      "requires": list(dict.fromkeys(requires)), "minutes": planner.unit_minutes(item.get("minutes"),
                                                                                                 unit_minutes),
                      "source_kind": kind, "source_ref": ref,
                      "state": "skipped" if item.get("user_known") is True else "pending",
                      "skip_reason": "user_known" if item.get("user_known") is True else None,
                      "planned_date": None, "block_reason": None})
    for unit in units:
        unknown = [r for r in unit["requires"] if r not in keys]
        if unknown:
            raise DraftInvalid(f"“{unit['title']}”的前置单元不存在：{', '.join(unknown)}")
        if unit["key"] in unit["requires"]:
            raise DraftInvalid(f"“{unit['title']}”不能以自己为前置")
    units, reordered = _topological(units)
    if reordered:
        notes.append("已按前置关系调整顺序")
    if downgraded:
        notes.append(f"{len(downgraded)} 个单元引用的资料不存在，改为学习时联网找：{'、'.join(downgraded[:5])}")
    with_sources = sum(unit["source_kind"] != "lesson_web" for unit in units)
    user_sources = signals.files or signals.urls or signals.notion
    if web_urls and any(unit["source_kind"] == "url" and unit["source_ref"] in set(web_urls) for unit in units):
        basis = "web"
    elif user_sources and with_sources * 2 >= len(units):
        basis = "materials"
    else:
        basis = "model"
    questions = [_text(q, 120) for q in raw.get("questions") or [] if _text(q, 120)][:QUESTIONS]
    return {"title": _text(raw.get("title"), PLAN_TITLE_CHARS) or units[0]["title"][:PLAN_TITLE_CHARS],
            "summary": _text(raw.get("summary"), SUMMARY_CHARS),
            "basis": basis, "basis_note": _text(raw.get("basis_note"), NOTE_CHARS),
            "suggest_web": raw.get("suggest_web") is True and basis == "model",
            "suggest_web_reason": _text(raw.get("suggest_web_reason"), NOTE_CHARS),
            "questions": questions, "units": units, "notes": notes}


def mark_known(units: list[dict[str, Any]], db: Any) -> dict[str, dict[str, Any]]:
    """Units named like a point the user has: link them; skip those already learned (first self-test done).

    Returns key -> {title, total} for the units marked as learned and key -> {title} for those only linked."""
    found: dict[str, dict[str, Any]] = {}
    rows = {_norm(row["title"]): row for row in db.execute("SELECT id,title FROM knowledge_points")}
    for unit in units:
        row = rows.get(_norm(unit["title"]))
        if not row:
            continue
        unit["kp_id"] = row["id"]
        unit["title"] = row["title"]
        first = db.execute("""SELECT r.total FROM attempts a JOIN reviews r ON r.attempt_id=a.id
            WHERE a.kp_id=? AND a.kind='first' AND a.state='completed' AND r.total IS NOT NULL
            ORDER BY r.created_at DESC LIMIT 1""", (row["id"],)).fetchone()
        if first is not None:
            if unit["state"] == "pending":
                unit["state"], unit["skip_reason"] = "skipped", "known"
            found[unit["key"]] = {"title": row["title"], "total": first["total"], "known": True}
        else:
            found[unit["key"]] = {"title": row["title"], "known": False}
    return found


def snapshot(draft: dict[str, Any], *, pace: planner.Pace, today: date, goal: str = "",
             deadline: str | None = None, remind_at: str | None = None, signals: Signals | None = None,
             known: dict[str, Any] | None = None) -> dict[str, Any]:
    """A plan snapshot (plans.Plans.create) from a checked draft, undated."""
    meta = {"summary": draft["summary"], "suggest_web": draft["suggest_web"],
            "suggest_web_reason": draft["suggest_web_reason"], "questions": draft["questions"],
            "notes": draft["notes"], "known": known or {}}
    if signals is not None and signals.note:
        meta["outline_note"] = signals.note
    note = draft["basis_note"]
    if draft["basis"] == "model":
        note = "模型常识，未联网核对"
    elif draft["basis"] == "materials" and not note:
        note = "你的资料"
    return {"title": draft["title"], "goal": goal, "basis": draft["basis"], "basis_note": note,
            "new_per_day": pace.new_per_day, "daily_minutes": pace.daily_minutes, "rest_days": list(pace.rest_days),
            "remind_at": remind_at, "preset": pace.preset, "start_date": today.isoformat(), "deadline": deadline,
            "meta": meta, "units": draft["units"]}


def source_label(unit: dict[str, Any], root: str | None = None) -> str:
    kind, ref = unit["source_kind"], unit.get("source_ref") or ""
    if kind == "local":
        if root:
            try:
                return str(Path(ref).relative_to(root))
            except ValueError:
                pass
        return Path(ref).name
    if kind == "url":
        return re.sub(r"^https?://", "", ref)[:60]
    if kind == "notion":
        return "Notion 页面"
    return "学习时联网找"
