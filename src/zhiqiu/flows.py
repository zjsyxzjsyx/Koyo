"""Lesson and quiz drivers shared by the CLI commands and the chat shell.

The drivers only talk to the user through FlowIO, so the same code runs in a
terminal, inside `zhiqiu chat`, or under a scripted test.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from . import lessons, tracing
from .materials import Material
from .store import Store

LEARN_PROMPT = ("回答检查问题或直接提问；说“继续”看下一节，说“学完了”或 /done 开始自测"
                "（其他命令：/sources web、/sources local、/accept N、/pause、/end）：")
RESUME_PROMPT = "回答检查问题或直接提问；说“继续”看下一节，说“学完了”或 /done 开始自测（/pause、/end）："
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


def show_outcome(io: FlowIO, store: Store, outcome: dict[str, Any]) -> None:
    if outcome["kind"] == "awaiting_review":
        io.say(f"原题原答已保存，当前待评阅；未生成分数或复习任务。{outcome['detail']}")
        io.say(f"可调整模型窗口或用 submit-final 提交精简最终答案，再 resume {outcome['attempt_id']}。")
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
            sections = _lesson_sections(io, store, kp_id, lesson)
            sections.next()
            continue
        if command.startswith("/accept "):
            try:
                number = int(command.split()[1])
                suggestion = sections.suggestions[number - 1]
            except (ValueError, IndexError, TypeError):
                io.say("无效建议序号。")
                continue
            child = store.ensure_knowledge_point(str(suggestion), parent_id=kp_id)
            io.say(f"已接受独立知识点：{suggestion}（{child}）。之后用 learn 指定它的资料并开始独立自测。")
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
    io.say(f"继续讲解：{lesson['title']}（会话 {lesson['session_id']}）")
    kp_id = lesson["kp_id"]
    found = store.lesson_sections(lesson["lesson_message_id"])
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
        # A lesson taught before sections existed is shown whole, as before.
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
        question = _lesson_question(command)
        if question is not None:
            service = service or service_factory()
            _clarify(io, service, kp_id, question, section=sections.current, confirm=io.confirm_tool)
            continue
        if command == "/pause":
            io.say(f"讲解已暂停；会话 {lesson['session_id']} 可用 resume 继续。")
            return FlowResult("paused", kp_id, session_id=lesson["session_id"])
        if command == "/end":
            store.close_lesson(lesson["session_id"])
            io.say("本次讲解已结束，未创建自测或复习任务。")
            return FlowResult("ended", kp_id, session_id=lesson["session_id"])
        io.say("未知命令。")


def _is_side_question(io: FlowIO, service: Any, attempt_id: str, question: str, text: str) -> bool:
    """Classify an unprefixed quiz input; the user confirms a question and decides when unsure."""
    classify = getattr(service, "classify_turn", None)
    kind = classify(attempt_id, question, text) if callable(classify) else None
    if kind is None or kind == "answer":
        return False
    if kind == "question":
        return io.ask(QUESTION_PROMPT).strip().lower() != "a"
    while True:
        choice = io.ask(CHOICE_PROMPT).strip().lower()
        if choice in ("a", "q"):
            return choice == "q"


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
            io.say(f"已暂停：{attempt_id}")
            return FlowResult("paused", kp_id, attempt_id=attempt_id)
        if answer in ("/end", "结束本轮学习", "结束这次复习"):
            return finish("explicit")
        if answer.startswith("/answer "):
            store.add_answer(proposed["question_id"], answer.split(" ", 1)[1].strip())
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
        if answer.startswith("/"):
            io.say("未知命令。可以用 /question 提问、/answer 回答、/pause 暂停、/end 结束。")
            continue
        try:
            is_question = _is_side_question(io, service, attempt_id, proposed["question"], answer)
        except FlowInterrupted:
            io.say("问答已中断；已保存的进度可用 resume 继续。")
            return FlowResult("interrupted", kp_id, attempt_id=attempt_id)
        if is_question:
            # A question asked mid-quiz is a clarification note, never an answer, so grading cannot see it.
            _clarify(io, service, kp_id, answer, attempt_id=attempt_id, via="classified")
        else:
            store.add_answer(proposed["question_id"], answer)
