"""Lesson and quiz drivers shared by the CLI commands and the chat shell.

The drivers only talk to the user through FlowIO, so the same code runs in a
terminal, inside `zhiqiu chat`, or under a scripted test.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Protocol

from . import lessons, tracing
from .materials import Material
from .store import Store

LEARN_PROMPT = ("回答检查问题或直接提问；说“继续”看下一节，说“学完了”或 /done 开始自测"
                "（其他命令：/sources web、/sources local、/accept N、/skill、/pause、/end）：")
RESUME_PROMPT = "回答检查问题或直接提问；说“继续”看下一节，说“学完了”或 /done 开始自测（/skill、/pause、/end）："
QUIZ_PROMPT = "回答（/pause 暂停，/end 结束，/question 提问）："
CHOICE_PROMPT = "这是回答还是提问？输入 a 作为回答保存，输入 q 作为提问："
QUESTION_PROMPT = "识别为提问，不计入回答。回车确认，输入 a 改为回答保存："
QUIZ_SECONDS = 25 * 60
DIMENSION_NAMES = {"concept": "概念准确", "mechanism": "机制解释", "application": "应用辨析"}


class FlowInterrupted(Exception):
    """The user closed input (EOF or Ctrl+C) while a flow was waiting."""


class FlowIO(Protocol):
    def ask(self, prompt: str) -> str: ...
    def say(self, text: str) -> None: ...
    def confirm(self, text: str) -> bool: ...
    def confirm_tool(self, spec: Any, args: dict[str, Any]) -> bool: ...


# What a line of output is, for an IO that renders them differently (the web workbench):
# text for the user, tool call notes, system notes (greetings, switches), and grading summaries.
SAY_KINDS = ("text", "tool", "note", "outcome")


def say(io: Any, text: str, kind: str = "text", **meta: Any) -> None:
    """Say one line with its kind; an IO that does not declare `say_kinds` gets the plain text."""
    if kind not in SAY_KINDS:
        raise ValueError(f"unknown output kind: {kind}")
    if getattr(io, "say_kinds", False):
        io.say(text, kind=kind, **meta)
    else:
        # A diagram passed as data reaches a plain IO in its text form, so the picture is never lost.
        io.say(f"{text}\n{meta['diagram_text']}" if meta.get("diagram_text") else text)


def input_source(io: Any) -> str:
    """How the answer just read was entered: an IO that knows (the workbench) says voice for a confirmed
    transcript; everything else is typed."""
    source = getattr(io, "last_input_source", None)
    value = source() if callable(source) else "text"
    return value if value in ("text", "voice") else "text"


def _stage(io: FlowIO, mode: str, active_kind: str, active_id: str) -> None:
    """Tell an IO that tracks state (the chat shell) which flow is now running; others ignore it."""
    hook = getattr(io, "stage", None)
    if callable(hook):
        hook(mode, active_kind, active_id)


@dataclass(frozen=True)
class FlowResult:
    kind: str  # outcome | paused | ended | interrupted
    kp_id: str
    attempt_id: str | None = None
    session_id: str | None = None
    outcome: dict[str, Any] | None = None


def outbound_lines(materials: list[Material], endpoint: str, remote: list[str] | None = None) -> list[str]:
    """The consent text for one preparation: the endpoint, then each local file and remote item."""
    return ([f"将把以下资料发送到模型端点 {endpoint}："]
            + [f"  {material.source}" for material in materials if material.kind == "local"]
            + [f"  {item}" for item in remote or []])


def approver(io: FlowIO) -> Callable[..., bool]:
    """The send-before-confirm prompt that LearningRuntime.prepare calls."""
    def approve(materials: list[Material], endpoint: str, remote: list[str] | None = None) -> bool:
        for line in outbound_lines(materials, endpoint, remote):
            io.say(line)
        return io.confirm("允许本次学习发送这些内容吗？")
    return approve


def preapproved(io: FlowIO, lines: list[str]) -> Callable[..., bool]:
    """Consent already given with the learning proposal; asks again only if what would be sent changed."""
    ask = approver(io)

    def approve(materials: list[Material], endpoint: str, remote: list[str] | None = None) -> bool:
        if outbound_lines(materials, endpoint, remote) == lines:
            io.say("发送资料已在开始前确认。")
            return True
        io.say("要发送的资料与开始前确认的不同，需要重新确认。")
        return ask(materials, endpoint, remote)
    return approve


# A dimension's one line in the grading summary is cut here, and so is its "要点" line.
SUMMARY_LINE_CHARS = 40
SUMMARY_TIP_CHARS = 60
DETAIL_HINT = "输入 /detail 查看原题、原答和完整理由"


# A typed id prefix must be at least this long (the UI shows 8 characters).
MIN_PREFIX = 6


def short_id(value: str) -> str:
    """The 8-character prefix shown instead of a full id; commands accept a unique prefix of 6 or more."""
    return value[:8]


def resolve_prefix(prefix: str, ids: list[str], label: str) -> str:
    """The one id among `ids` that a typed full id or prefix names; short, unknown and ambiguous ones are refused."""
    if prefix in ids:
        return prefix
    if len(prefix) < MIN_PREFIX:
        raise ValueError(f"短号至少 {MIN_PREFIX} 位：{prefix}")
    matches = [item for item in ids if item.startswith(prefix)]
    if not matches:
        raise ValueError(f"没有以 {prefix} 开头的{label}")
    if len(matches) > 1:
        raise ValueError(f"有多个{label}以 {prefix} 开头，请多输入几位：" + "、".join(short_id(m) for m in matches[:5])
                         + ("……" if len(matches) > 5 else ""))
    return matches[0]


def resume_target(store: Store, target: str) -> tuple[str, str]:
    """("attempt", id) or ("lesson", session id) for a full id or unique prefix of something that can go on."""
    attempts = [row[0] for row in store.db.execute("SELECT id FROM attempts WHERE state!='completed'")]
    lessons = [row["session_id"] for row in store.pending_lessons()]
    if store.db.execute("SELECT 1 FROM attempts WHERE id=?", (target,)).fetchone():
        return "attempt", target
    found = resolve_prefix(target, attempts + lessons, "可继续的尝试或讲解")
    return ("attempt" if found in attempts else "lesson"), found


def _first_sentence(text: str, limit: int | None = None) -> str:
    text = (text or "").strip()
    for mark in ("。", "\n"):
        if mark in text:
            text = text.split(mark, 1)[0].strip()
            break
    if limit and len(text) > limit:
        text = text[:limit] + "…"
    return text


def _relative_day(store: Store, iso: str) -> str:
    days = (date.fromisoformat(iso) - store.clock.today()).days
    return {0: "今天", 1: "明天", 2: "后天"}.get(days, f"{days} 天后" if days > 0 else f"{-days} 天前")


def show_outcome(io: FlowIO, store: Store, outcome: dict[str, Any]) -> None:
    """The grading summary: the total and next review, then one line per dimension; /detail shows the rest."""
    if outcome["kind"] in ("awaiting_review", "zero"):
        show_outcome_detail(io, store, outcome)
        return
    label = "完整" if outcome["kind"] == "complete" else "阶段性"
    if outcome.get("task_id"):
        due = outcome["due_date"]
        head = f"{label}评阅 {outcome['total']} 分 · 下次复习 {due[5:]}（{_relative_day(store, due)}）"
    else:
        head = f"{label}评阅 {outcome['total']} 分 · 请先补学，暂不安排复习"
    lines = [head]
    grades = {row["dimension"]: row for row in store.trace(outcome["attempt_id"])["dimension_scores"]}
    for dimension, name in DIMENSION_NAMES.items():
        grade = grades.get(dimension)
        if not grade:
            lines.append(f"  {name}：未评")
            continue
        # The learner-facing feedback when the grader gave one, else the rationale's first sentence.
        line = (grade.get("feedback") or "").strip() or _first_sentence(grade["rationale"], SUMMARY_LINE_CHARS)
        lines.append(f"  {name} {grade['score']}/4：{line}")
        if grade["score"] <= 2 and grade.get("supplement"):
            lines.append(f"    要点：{_first_sentence(grade['supplement'], SUMMARY_TIP_CHARS)}")
    lines.append(DETAIL_HINT)
    say(io, "\n".join(lines), "outcome", attempt_id=outcome["attempt_id"])


def show_outcome_detail(io: FlowIO, store: Store, outcome: dict[str, Any]) -> None:
    """Everything about one grading: each dimension's quote, rationale and supplement, with the original
    questions and answers."""
    if outcome["kind"] == "awaiting_review":
        io.say(f"原题原答已保存，当前待评阅；未生成分数或复习任务。{outcome['detail']}")
        io.say(f"可调整模型窗口或用 submit-final 提交精简最终答案，再 resume {short_id(outcome['attempt_id'])}。")
        return
    if outcome["kind"] == "zero":
        if outcome.get("needs_relearn"):
            io.say("连续困难达到 3 次；请先补学。")
        elif outcome.get("due_date"):
            io.say(f"本次零回答；原任务未完成，下次提醒 {outcome['due_date']}。")
        else:
            io.say("证据不足；首次自测待补，没有分数或复习任务。")
        return
    label = "完整" if outcome["kind"] == "complete" else "阶段性"
    if outcome.get("task_id"):
        io.say(f"{label}评阅已保存：{outcome['total']} 分；下次复习 {outcome['due_date']}；任务 {outcome['task_id']}")
    else:
        io.say(f"{label}评阅已保存：{outcome['total']} 分；请先补学，当前不安排新任务。")
    trace = store.trace(outcome["attempt_id"])
    grades = {row["dimension"]: row for row in trace["dimension_scores"]}
    answers = {row["answer_id"]: row for row in trace["questions"] if row["answer_id"]}
    questions = {row["question_id"]: row for row in trace["questions"]}
    for dimension, name in DIMENSION_NAMES.items():
        grade = grades.get(dimension)
        if not grade:
            io.say(f"  {name}：未评")
            continue
        evidence = [e for e in trace["review_evidence"] if e["dimension"] == dimension]
        quote = evidence[0]["quote"] if evidence else "无逐字引文"
        io.say(f"  {name}：{grade['score']}/4；依据“{quote}”；{grade['rationale']}")
        if grade.get("supplement"):
            io.say(f"    补充：{grade['supplement']}")
        for assessed in (a for a in trace["question_assessments"] if a["dimension"] == dimension):
            original = questions[assessed["question_id"]]
            io.say(f"    原题：{original['question']}（{original['difficulty']}）")
            if assessed["answer_id"]:
                original_answer = answers[assessed["answer_id"]]
                io.say(f"    原答：{original_answer['answer']}")
                if original_answer["scoring_text"]:
                    io.say(f"    评分采用的最终答案：{original_answer['scoring_text']}")
            else:
                io.say("    原答：未回答")


def summarize_outcome(outcome: dict[str, Any]) -> str:
    """One line for the chat log after a flow ends."""
    if outcome["kind"] == "awaiting_review":
        return "原题原答已保存，待评阅"
    if outcome["kind"] == "zero":
        return "没有可评分的回答，未产生分数"
    label = "完整" if outcome["kind"] == "complete" else "阶段性"
    due = f"，下次复习 {outcome['due_date']}" if outcome.get("task_id") else ""
    return f"{label}评阅 {outcome['total']} 分{due}"


def _clarify(io: FlowIO, service: Any, kp_id: str, question: str,
             section: dict[str, str] | None = None, **kwargs: Any) -> None:
    if section:
        kwargs["section"] = section
    try:
        clarified = service.clarify(kp_id, question, **kwargs)
        io.say(clarified["explanation"])
        if clarified["followup"]:
            io.say(f"可继续思考：{clarified['followup']}")
    except ValueError as exc:
        io.say(f"问题已记录，但解释暂不可用：{exc}")


def _lesson_question(command: str) -> str | None:
    """The clarification text of a lesson-stage input, or None for a command."""
    if command.startswith("/question "):
        return command.split(" ", 1)[1]
    if command and not command.startswith("/"):
        return command
    return None


class _Sections:
    """Shows a lesson one section at a time; each shown section leaves a marker message for resume and
    for clarification (which then knows which check question the user is answering)."""

    def __init__(self, io: FlowIO, store: Store, kp_id: str, sections: list[dict[str, str]],
                 lesson_id: str | None, shown: int = 0, suggestions: Any = ()):
        self.io, self.store, self.kp_id = io, store, kp_id
        self.sections, self.lesson_id, self.shown = sections, lesson_id, shown
        self.suggestions = suggestions if isinstance(suggestions, list) else []

    @property
    def total(self) -> int:
        return len(self.sections)

    @property
    def remaining(self) -> int:
        return self.total - self.shown

    @property
    def current(self) -> dict[str, str] | None:
        if not self.shown or (self.total == 1 and not self.sections[0]["title"]):
            return None
        return {"number": f"{self.shown}/{self.total}", **self.sections[self.shown - 1]}

    def next(self) -> None:
        if self.shown >= self.total:
            self.io.say("已经是最后一节了。说“学完了”或输入 /done 开始自测，也可以继续提问。")
            return
        self.shown += 1
        section = self.sections[self.shown - 1]
        if self.total > 1:
            tracing.progress(step=f"讲解 {self.shown}/{self.total}")
        diagram = section.get("diagram")
        if diagram and getattr(self.io, "say_kinds", False):
            # The IO gets the picture as data (the workbench draws it, the terminal prints its text form).
            say(self.io, lessons.show(section, self.shown, self.total, with_diagram=False), "text",
                diagram=diagram, diagram_text=lessons.diagram_text(diagram))
        else:
            self.io.say(lessons.show(section, self.shown, self.total))
        if self.lesson_id and self.total > 1:
            self.store.record_message(self.kp_id, "assistant", "lesson_section",
                                      lessons.marker(section, self.shown, self.total),
                                      source_message_id=self.lesson_id)
        if self.shown == self.total:
            for number, suggestion in enumerate(self.suggestions, 1):
                self.io.say(f"建议 {number}: {suggestion}")
            if self.total > 1:
                self.io.say("讲解完了。说“学完了”或输入 /done 开始自测，也可以继续提问。")


SKILL_BY = {"model": "由知秋按资料选择", "user": "你指定的", "fallback": "通用兜底"}


def skills_of(service: Any) -> Any:
    """The runtime's Skill library; a service without one (scripted tests) gets the packaged Skills."""
    library = getattr(service, "skills", None)
    if library is None:
        from .context import builtin_library
        library = builtin_library()
    return library


def skill_line(skill: dict[str, Any]) -> str:
    """The line above a lesson saying how it is taught."""
    line = f"讲法：{skill['title']}（/skill 可改）"
    if skill.get("missing"):
        line = f"你指定的 Skill {skill['missing']} 已不存在，本次用{skill['title']}。" + line
    return line


def describe_skills(library: Any, point: Any = None) -> str:
    library.refresh()
    lines = []
    if point is not None:
        current = library.get(point["skill"]) if point["skill"] else None
        if current is not None:
            lines.append(f"「{point['title']}」的讲法：{current.title}（{current.name}，"
                         f"{SKILL_BY.get(point['skill_source'], '')}）")
        elif point["skill"]:
            lines.append(f"「{point['title']}」指定的 {point['skill']} 已不存在，现在用通用讲解")
        else:
            lines.append(f"「{point['title']}」还没有选定讲法，用通用讲解")
    sources = {"builtin": "内置", "user": "用户"}
    lines.append("可用的 Skill：")
    lines += [f"  {skill.name} · {skill.title}（{sources[skill.source]}）：{skill.description}" for skill in library.all()]
    if library.errors:
        lines.append(f"另有 {len(library.errors)} 个用户 Skill 有错误，用 zhiqiu skills 查看。")
    lines.append("用法：/skill 名称 指定讲法；/skill auto 交给知秋按资料选。")
    return "\n".join(lines)


def change_skill(io: FlowIO, store: Store, library: Any, kp_id: str, wanted: str) -> None:
    """/skill 名称 or /skill auto for one knowledge point; no model call, no confirmation."""
    wanted = wanted.strip()
    if wanted.lower() == "auto":
        store.set_skill(kp_id, None, None)
        io.say("已清除指定的讲法；下次 /learn 时由知秋按资料选择。")
        return
    library.refresh()
    skill = library.find(wanted)
    if skill is None:
        names = "、".join(f"{item.name}（{item.title}）" for item in library.all())
        io.say(f"没有这个 Skill：{wanted}。可用：{names}")
        return
    store.set_skill(kp_id, skill.name, "user")
    io.say(f"已改用{skill.title}（{skill.name}），之后的提问按这种方式回答；想用新方式重讲，结束后再 /learn 一次。")


def skill_command(io: FlowIO, store: Store, library: Any, kp_id: str, command: str) -> bool:
    """Handle /skill inside a lesson, self-test or review; False when the input is something else."""
    head, _, rest = command.partition(" ")
    if head != "/skill":
        return False
    if rest.strip():
        change_skill(io, store, library, kp_id, rest)
    else:
        io.say(describe_skills(library, store.db.execute("SELECT * FROM knowledge_points WHERE id=?",
                                                         (kp_id,)).fetchone()))
    return True


def _lesson_sections(io: FlowIO, store: Store, kp_id: str, lesson: dict[str, Any]) -> _Sections:
    sections = lesson.get("sections") or lessons.normalize(lesson)
    return _Sections(io, store, kp_id, sections, lesson.get("message_id"),
                     suggestions=lesson.get("suggestions", []))


def _wants_quiz(io: FlowIO, command: str, sections: _Sections) -> bool | None:
    """True to start the quiz, False when the user backed out of it, None for any other input."""
    if command == "/done":
        return True
    if lessons.intent(command) != "done":
        return None
    left = f"还有 {sections.remaining} 节没看。" if sections.remaining else ""
    if io.confirm(f"{left}结束讲解，开始自测吗？"):
        return True
    io.say("好的，继续讲解。")
    return False


def _start_first(io: FlowIO, store: Store, kp_id: str) -> str | None:
    try:
        return store.start_attempt(kp_id, "first")
    except ValueError as exc:
        io.say(f"不能开始自测：{exc}")
        return None


def _show_sources(io: FlowIO, materials: list[Material]) -> None:
    io.say("实际采用来源：")
    for material in materials:
        io.say(f"  {material.kind}: {material.source}" + (f"（{material.reason}）" if material.reason else ""))


def _traced(name: str, result: FlowResult) -> FlowResult:
    span = tracing.current()
    if span is not None and span.name == name:
        span.set(end_reason=result.kind)
    return result


def run_lesson(io: FlowIO, store: Store, service: Any, point: str, *, source: Path | None = None,
               urls: list[str] | None = None, notion_pages: list[str] | None = None,
               offline: bool = False, approve: Callable[..., bool] | None = None) -> FlowResult:
    """Prepare and teach a point, then follow lesson commands until /done, /end or /pause.

    approve: consent already collected by the caller (see `preapproved`); otherwise the user is asked.
    A preparation failure raises ValueError before anything is taught.
    """
    with tracing.span(store, "flow", "learn", point=point):
        return _traced("learn", _run_lesson(io, store, service, point, source, urls or [],
                                                   notion_pages or [], offline, approve))


def _run_lesson(io: FlowIO, store: Store, service: Any, point: str, source: Path | None,
                urls: list[str], notion_pages: list[str], offline: bool,
                approve: Callable[..., bool] | None) -> FlowResult:
    tracing.progress(step="准备资料")
    kp_id, lesson, materials = service.prepare(point, source, approve or approver(io), offline=offline,
                                               urls=urls, notion_pages=notion_pages)
    tracing.link(kp_id=kp_id)
    tracing.progress(step="讲解")
    _stage(io, "lesson", "point", kp_id)
    _show_sources(io, materials)
    if lesson.get("skill"):
        say(io, skill_line(lesson["skill"]), "note", skill=lesson["skill"])
    sections = _lesson_sections(io, store, kp_id, lesson)
    sections.next()
    while True:
        try:
            command = io.ask(LEARN_PROMPT).strip()
        except FlowInterrupted:
            io.say("讲解已保存；可用 resume 继续，尚未创建自测或复习任务。")
            return FlowResult("interrupted", kp_id)
        quiz = _wants_quiz(io, command, sections)
        if quiz:
            attempt = _start_first(io, store, kp_id)
            if attempt:
                tracing.progress(step="自测")
                return run_quiz(io, store, service, attempt)
            continue
        if quiz is False:
            continue
        if lessons.intent(command) == "next":
            sections.next()
            continue
        if command in ("/sources web", "/sources local"):
            try:
                # Fetching other sources is a new send-out, so it is always confirmed again.
                kp_id, lesson, materials = service.prepare(
                    point, source, approver(io), offline=offline, force_web=command.endswith("web"),
                    force_local=command.endswith("local"), urls=urls, notion_pages=notion_pages)
            except ValueError as exc:
                io.say(f"未能重新准备资料，保留原讲解：{exc}")
                continue
            _show_sources(io, materials)
            if lesson.get("skill"):
                say(io, skill_line(lesson["skill"]), "note", skill=lesson["skill"])
            sections = _lesson_sections(io, store, kp_id, lesson)
            sections.next()
            continue
        if skill_command(io, store, skills_of(service), kp_id, command):
            continue
        if command.startswith("/accept "):
            try:
                number = int(command.split()[1])
                suggestion = sections.suggestions[number - 1]
            except (ValueError, IndexError, TypeError):
                io.say("无效建议序号。")
                continue
            store.ensure_knowledge_point(str(suggestion), parent_id=kp_id)
            io.say(f"已接受独立知识点：{suggestion}。之后用 learn 指定它的资料并开始独立自测。")
            continue
        question = _lesson_question(command)
        if question is not None:
            _clarify(io, service, kp_id, question, section=sections.current, confirm=io.confirm_tool)
            continue
        if command in ("/pause", "/end", ""):
            if command == "/end":
                pending = next((row for row in store.pending_lessons() if row["kp_id"] == kp_id), None)
                if pending:
                    store.close_lesson(pending["session_id"])
            io.say("学习尚未确认完成；未创建自测或复习任务。")
            return FlowResult("ended" if command == "/end" else "paused", kp_id)
        io.say("未知命令。")


def resume_lesson(io: FlowIO, store: Store, session_id: str,
                  service_factory: Callable[[], Any]) -> FlowResult:
    """Continue a saved lesson; the model runtime is created only when first needed."""
    with tracing.span(store, "flow", "resume_lesson", session_id=session_id):
        return _traced("resume_lesson", _resume_lesson(io, store, session_id, service_factory))


def _resume_lesson(io: FlowIO, store: Store, session_id: str,
                   service_factory: Callable[[], Any]) -> FlowResult:
    lesson = store.resume_lesson(session_id)
    tracing.link(kp_id=lesson["kp_id"], point=lesson["title"])
    tracing.progress(step="讲解")
    _stage(io, "lesson", "lesson", lesson["session_id"])
    service: Any = None
    io.say(f"继续讲解：{lesson['title']}")
    kp_id = lesson["kp_id"]
    found = store.lesson_sections(lesson["lesson_message_id"])
    if not found or len(found[1]) <= 1:
        # A lesson taught before sections existed (or taught as one piece) is cut at its numbered headings when it has them; the
        # markers point at the original lesson, so the next resume continues where this one stopped.
        legacy = lessons.split_legacy(lesson["lesson"])
        origin = store.lesson_origin(lesson["lesson_message_id"])
        found = (origin, legacy) if legacy and origin else found
    if found and len(found[1]) > 1:
        lesson_id, parts = found
        sections = _Sections(io, store, kp_id, parts, lesson_id,
                             shown=lessons.shown_count(store.lesson_markers(lesson_id)))
        io.say(f"本讲共 {sections.total} 节：" + "；".join(f"{number}. {part['title']}"
                                                         for number, part in enumerate(parts, 1)))
        if sections.remaining:
            if sections.shown:
                io.say(f"上次看到第 {sections.shown} 节。")
            sections.next()
        else:
            io.say("各节都已看过。说“学完了”或输入 /done 开始自测，也可以继续提问。")
    else:
        # Without numbered headings an old lesson is shown whole, as before.
        io.say(lesson["lesson"])
        sections = _Sections(io, store, kp_id, lessons.normalize({"lesson": lesson["lesson"]}), None, shown=1)
    while True:
        try:
            command = io.ask(RESUME_PROMPT).strip()
        except FlowInterrupted:
            io.say("讲解仍可用 resume 继续。")
            return FlowResult("interrupted", kp_id, session_id=lesson["session_id"])
        quiz = _wants_quiz(io, command, sections)
        if quiz:
            service = service or service_factory()
            attempt = _start_first(io, store, kp_id)
            if attempt:
                tracing.progress(step="自测")
                return run_quiz(io, store, service, attempt)
            continue
        if quiz is False:
            continue
        if lessons.intent(command) == "next":
            sections.next()
            continue
        if command == "/skill" or command.startswith("/skill "):
            service = service or service_factory()
            skill_command(io, store, skills_of(service), kp_id, command)
            continue
        question = _lesson_question(command)
        if question is not None:
            service = service or service_factory()
            _clarify(io, service, kp_id, question, section=sections.current, confirm=io.confirm_tool)
            continue
        if command == "/pause":
            io.say("讲解已暂停；可用 resume 继续。")
            return FlowResult("paused", kp_id, session_id=lesson["session_id"])
        if command == "/end":
            store.close_lesson(lesson["session_id"])
            io.say("本次讲解已结束，未创建自测或复习任务。")
            return FlowResult("ended", kp_id, session_id=lesson["session_id"])
        io.say("未知命令。")


# The reason the classifier gave is kept this long; the input itself is never copied into the event.
CLASSIFY_REASON_CHARS = 200


def _mixed_prompt(answer_text: str, question_text: str) -> str:
    return (f"这段话里既有回答也有提问：回答“{answer_text}”，提问“{question_text}”。"
            "回车＝分开处理（保存回答、再回答提问），a＝全部作为回答，q＝全部作为提问：")


def _classify_input(io: FlowIO, store: Store, service: Any, kp_id: str, attempt_id: str,
                    proposed: dict[str, Any], text: str) -> tuple[str, dict[str, Any] | None]:
    """How to handle an unprefixed quiz input: answer, question, or split (a mixed input taken apart).

    The user confirms a question, chooses for a mixed input and decides when the model is unsure.
    Each classification leaves a TurnClassified event with the decision but not the input."""
    classify = getattr(service, "classify_turn", None)
    result = classify(attempt_id, proposed["question"], text) if callable(classify) else None
    if result is None:
        return "answer", None
    kind, decided_by = result["kind"], "user"
    if kind == "answer":
        final, decided_by = "answer", "model"
    elif kind == "question":
        final = "answer" if io.ask(QUESTION_PROMPT).strip().lower() == "a" else "question"
    elif kind == "mixed":
        prompt = _mixed_prompt(result["answer_text"], result["question_text"])
        while (choice := io.ask(prompt).strip().lower()) not in ("", "a", "q"):
            pass
        final = {"": "split", "a": "answer", "q": "question"}[choice]
    else:
        while (choice := io.ask(CHOICE_PROMPT).strip().lower()) not in ("a", "q"):
            pass
        final = "answer" if choice == "a" else "question"
    store.record_event(kp_id, attempt_id, "TurnClassified", {
        "question_id": proposed["question_id"], "model_kind": result["model_kind"],
        "reason": (result.get("reason") or "")[:CLASSIFY_REASON_CHARS],
        "split_valid": result.get("split_valid"), "final": final, "decided_by": decided_by,
        "input_chars": len(text)})
    return final, result


def run_quiz(io: FlowIO, store: Store, service: Any, attempt_id: str,
             clock: Callable[[], float] = time.monotonic) -> FlowResult:
    """Ask questions of a self-test or review attempt until it is finished or paused."""
    attempt = store.db.execute("""SELECT a.kp_id,a.kind,k.title FROM attempts a
        JOIN knowledge_points k ON k.id=a.kp_id WHERE a.id=?""", (attempt_id,)).fetchone()
    name = "review" if attempt and attempt["kind"] == "review" else "quiz"
    with tracing.span(store, "flow", name, attempt_id=attempt_id,
                      kp_id=attempt["kp_id"] if attempt else None, point=attempt["title"] if attempt else None):
        return _traced(name, _run_quiz(io, store, service, attempt_id, clock))


def _run_quiz(io: FlowIO, store: Store, service: Any, attempt_id: str,
              clock: Callable[[], float]) -> FlowResult:
    attempt = store.db.execute("SELECT kp_id,kind,state FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    kp_id = attempt["kp_id"]
    if attempt["state"] in ("paused", "needs_self_test"):
        store.resume(attempt_id)
    _stage(io, "review" if attempt["kind"] == "review" else "self_test", "attempt", attempt_id)
    started = clock()

    def finish(reason: str) -> FlowResult:
        tracing.progress(step="评分")
        outcome = service.finish(attempt_id, reason)
        show_outcome(io, store, outcome)
        return FlowResult("outcome", kp_id, attempt_id=attempt_id, outcome=outcome)

    asked = store.db.execute("SELECT COUNT(*) FROM questions WHERE attempt_id=?", (attempt_id,)).fetchone()[0]
    span, mixed = tracing.current(), 0
    while True:
        if clock() - started >= QUIZ_SECONDS:
            return finish("limit")
        tracing.progress(step="出题", question=asked + 1, dimension=None, followup=None)
        proposed = service.next_question(attempt_id)
        if proposed["done"]:
            return finish(proposed["reason"])
        asked = proposed["seq"]
        tracing.progress(step="等待回答", question=proposed["seq"], dimension=proposed["dimension"],
                         followup=bool(proposed.get("followup")))
        prefix = "追问 · " if proposed.get("followup") else ""
        io.say(f"[{proposed['seq']}/8 · {prefix}{proposed['dimension']}] {proposed['question']}")
        try:
            answer = io.ask(QUIZ_PROMPT).strip()
        except FlowInterrupted:
            io.say("问答已中断；已保存的进度可用 resume 继续。")
            return FlowResult("interrupted", kp_id, attempt_id=attempt_id)
        if answer in ("/pause", "先停下"):
            store.pause(attempt_id)
            io.say(f"已暂停（短号 {short_id(attempt_id)}），可以用 resume 继续。")
            return FlowResult("paused", kp_id, attempt_id=attempt_id)
        if answer in ("/end", "结束本轮学习", "结束这次复习"):
            return finish("explicit")
        source = input_source(io)
        if answer.startswith("/answer "):
            store.add_answer(proposed["question_id"], answer.split(" ", 1)[1].strip(), source)
            continue
        if answer.startswith("/question ") or answer.startswith("/interest "):
            note_text = answer.split(" ", 1)[1]
            if answer.startswith("/question "):
                # LearningRuntime.sources_for checks consent for the active local files and endpoint.
                _clarify(io, service, kp_id, note_text, attempt_id=attempt_id)
            else:
                store.add_note(kp_id, "interest", note_text, attempt_id=attempt_id)
                io.say("延伸兴趣已记录。")
            continue
        if not answer:
            io.say("空输入不计为回答；可以输入“我不会”或 /end。")
            continue
        if answer.split()[0] == "/detail":
            io.say("自测结束后可以查看评阅详情。")
            continue
        if skill_command(io, store, skills_of(service), kp_id, answer):
            continue
        if answer.startswith("/"):
            io.say("未知命令。可以用 /question 提问、/answer 回答、/skill 换讲法、/pause 暂停、/end 结束。")
            continue
        try:
            final, classified = _classify_input(io, store, service, kp_id, attempt_id, proposed, answer)
        except FlowInterrupted:
            io.say("问答已中断；已保存的进度可用 resume 继续。")
            return FlowResult("interrupted", kp_id, attempt_id=attempt_id)
        if classified and classified["model_kind"] == "mixed" and span is not None:
            mixed += 1
            span.set(classified_mixed=mixed)
        if final == "split":
            # Only the answer part is graded; the question part becomes a clarification note.
            store.add_answer(proposed["question_id"], classified["answer_text"], source)
            _clarify(io, service, kp_id, classified["question_text"], attempt_id=attempt_id, via="classified")
        elif final == "question":
            # A question asked mid-quiz is a clarification note, never an answer, so grading cannot see it.
            _clarify(io, service, kp_id, answer, attempt_id=attempt_id, via="classified")
        else:
            store.add_answer(proposed["question_id"], answer, source)
