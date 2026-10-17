from datetime import date

from zhiqiu.clock import FixedClock
from zhiqiu.flows import FlowInterrupted, resume_lesson, run_lesson, run_quiz
from zhiqiu.runtime import LearningRuntime
from zhiqiu.store import Store

from test_runtime import FakeModel, complete_first, prepare_service


class ScriptedIO:
    """FlowIO that replays inputs and records everything said."""

    def __init__(self, *inputs, confirm=True):
        self.inputs = list(inputs)
        self.said = []
        self.prompts = []
        self.confirm_answer = confirm

    def ask(self, prompt):
        self.prompts.append(prompt)
        if not self.inputs:
            raise FlowInterrupted
        return self.inputs.pop(0)

    def say(self, text):
        self.said.append(text)

    def confirm(self, text):
        self.said.append(text)
        return self.confirm_answer

    def confirm_tool(self, spec, args):
        return self.confirm_answer

    @property
    def text(self):
        return "\n".join(self.said)


class RecordingModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.questions = []

    def clarify(self, point, question, sources):
        self.questions.append(question)
        return super().clarify(point, question, sources)


def lesson_state(store, kp):
    return [row[0] for row in store.db.execute(
        "SELECT lesson_state FROM sessions WHERE kp_id=? AND lesson_state IS NOT NULL ORDER BY rowid", (kp,))]


def test_free_text_during_lesson_is_a_question_not_an_unknown_command(tmp_path):
    model = RecordingModel()
    store, service, path = prepare_service(tmp_path, model)
    io = ScriptedIO("为什么先访问根？", "/question 中序呢？", "/oops", "/pause")
    result = run_lesson(io, store, service, "二叉树遍历", source=path)
    assert model.questions == ["为什么先访问根？", "中序呢？"]
    assert "根据资料解释：为什么先访问根？" in io.text
    assert io.text.count("未知命令。") == 1  # only for the unknown slash command
    assert result.kind == "paused" and lesson_state(store, result.kp_id) == ["teaching"]


def test_lesson_done_runs_quiz_to_a_saved_review(tmp_path):
    store, service, path = prepare_service(tmp_path)
    io = ScriptedIO("/done", "根左右", "递归栈", "表达式树")
    result = run_lesson(io, store, service, "二叉树遍历", source=path)
    assert result.kind == "outcome" and result.outcome["kind"] == "complete"
    assert result.outcome["task_id"] and "完整评阅 78 分 · 下次复习 10-04（后天）" in io.text
    assert lesson_state(store, result.kp_id) == ["tested"]
    assert "允许本次学习发送这些内容吗？" in io.text


def test_lesson_end_closes_and_refused_consent_raises(tmp_path):
    store, service, path = prepare_service(tmp_path)
    result = run_lesson(ScriptedIO("/end"), store, service, "二叉树遍历", source=path)
    assert result.kind == "ended" and lesson_state(store, result.kp_id) == ["closed"]
    try:
        run_lesson(ScriptedIO(confirm=False), store, service, "另一个点", source=path)
    except ValueError:
        pass
    else:
        raise AssertionError("refused consent must stop the lesson")


def test_lesson_eof_keeps_lesson_and_done_refusal_is_reported(tmp_path):
    store, service, path = prepare_service(tmp_path)
    first = run_lesson(ScriptedIO(), store, service, "二叉树遍历", source=path)
    assert first.kind == "interrupted" and len(store.pending_lessons()) == 1
    complete_first(store, service, first.kp_id)
    # A pending review task makes another first self-test illegal; the lesson stays open.
    io = ScriptedIO("/done", "/pause")
    second = run_lesson(io, store, service, "二叉树遍历", source=path)
    assert second.kind == "paused" and "不能开始自测" in io.text


def test_resume_lesson_creates_runtime_only_when_needed(tmp_path):
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 2)))
    kp = store.ensure_knowledge_point("遍历")
    store.record_message(kp, "assistant", "lesson", "前序是根左右")
    session = store.pending_lessons()[0]["session_id"]
    made = []

    def factory():
        made.append(LearningRuntime(store, RecordingModel()))
        return made[-1]

    result = resume_lesson(ScriptedIO("/pause"), store, session, factory)
    assert result.kind == "paused" and made == []
    result = resume_lesson(ScriptedIO("前序先访问谁？", "继续问", "/end"), store, session, factory)
    assert result.kind == "ended" and len(made) == 1
    # No approved materials here, so clarify records the questions and reports it cannot explain yet.
    notes = [r[0] for r in store.db.execute("SELECT text FROM notes WHERE kind='clarification' ORDER BY rowid")]
    assert notes == ["前序先访问谁？", "继续问"]
    assert store.pending_lessons() == []


def test_quiz_pause_and_interrupt_keep_the_attempt(tmp_path):
    store, service, path = prepare_service(tmp_path)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    attempt = store.start_attempt(kp, "first")
    paused = run_quiz(ScriptedIO("第一答", "/pause"), store, service, attempt)
    assert paused.kind == "paused"
    assert store.db.execute("SELECT state FROM attempts WHERE id=?", (attempt,)).fetchone()[0] == "paused"
    interrupted = run_quiz(ScriptedIO(), store, service, attempt)
    assert interrupted.kind == "interrupted"
    assert store.db.execute("SELECT state FROM attempts WHERE id=?", (attempt,)).fetchone()[0] == "in_progress"
    finished = run_quiz(ScriptedIO("第二答", "第三答"), store, service, attempt)
    assert finished.kind == "outcome" and finished.outcome["kind"] == "complete"


def test_quiz_time_limit_finishes_with_limit(tmp_path):
    store, service, path = prepare_service(tmp_path)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    attempt = store.start_attempt(kp, "first")
    ticks = iter([0.0, 0.0, 26 * 60])
    result = run_quiz(ScriptedIO("第一答"), store, service, attempt, clock=lambda: next(ticks))
    assert result.kind == "outcome"
    assert store.db.execute("SELECT finish_reason FROM attempts WHERE id=?", (attempt,)).fetchone()[0] == "limit"


UUID = __import__("re").compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def graded(tmp_path):
    from zhiqiu.scoring import DimensionGrade
    store = Store(tmp_path / "graded.sqlite", FixedClock(date(2026, 10, 16)))
    kp = store.ensure_knowledge_point("二叉树遍历")
    attempt = store.start_attempt(kp, "first")
    grades = []
    for dimension, score, rationale, supplement in (
            ("concept", 1, "只说了“链表只有 next”，没讲遍历是什么。其余部分没有涉及。", "遍历是按规则访问每个节点一次。再补一句。"),
            ("mechanism", 3, "说清了递归调用的顺序，但是没有说明记录当前节点相对于两次递归的位置，这一点很关键，需要补上", "")):
        question, _ = store.add_question(attempt, dimension, f"{dimension} 的题目？")
        answer = store.add_answer(question, f"{dimension} 的原答")
        grades.append(DimensionGrade(dimension, score, question, answer, f"{dimension} 的原答", rationale,
                                     supplement=supplement))
    return store, store.finalize(attempt, grades)


def test_grading_summary_is_one_line_per_dimension_without_ids(tmp_path):
    from zhiqiu.flows import show_outcome
    store, outcome = graded(tmp_path)
    io = ScriptedIO()
    show_outcome(io, store, outcome)
    lines = io.text.splitlines()
    assert lines[0] == f"阶段性评阅 {outcome['total']} 分 · 下次复习 {outcome['due_date'][5:]}（明天）"
    assert lines[1] == "  概念准确 1/4：只说了“链表只有 next”，没讲遍历是什么"
    assert lines[2] == "    要点：遍历是按规则访问每个节点一次"
    # The rationale has no full stop within 40 characters, so it is cut there.
    assert lines[3].startswith("  机制解释 3/4：说清了递归调用的顺序") and lines[3].endswith("…")
    assert len(lines[3].split("：", 1)[1]) == 41
    assert lines[4:] == ["  应用辨析：未评", "输入 /detail 查看原题、原答和完整理由"]
    assert not UUID.search(io.text)


def test_grading_detail_keeps_the_full_report(tmp_path):
    from zhiqiu.flows import show_outcome_detail
    store, outcome = graded(tmp_path)
    io = ScriptedIO()
    show_outcome_detail(io, store, outcome)
    assert io.said == [
        f"阶段性评阅已保存：{outcome['total']} 分；下次复习 {outcome['due_date']}；任务 {outcome['task_id']}",
        "  概念准确：1/4；依据“concept 的原答”；只说了“链表只有 next”，没讲遍历是什么。其余部分没有涉及。",
        "    补充：遍历是按规则访问每个节点一次。再补一句。",
        "    原题：concept 的题目？（basic）",
        "    原答：concept 的原答",
        "  机制解释：3/4；依据“mechanism 的原答”；说清了递归调用的顺序，但是没有说明记录当前节点相对于两次递归的位置，这一点很关键，需要补上",
        "    原题：mechanism 的题目？（basic）",
        "    原答：mechanism 的原答",
        "  应用辨析：未评"]


def test_detail_inside_a_quiz_says_to_wait(tmp_path):
    store, service, path = prepare_service(tmp_path)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    attempt = store.start_attempt(kp, "first")
    io = ScriptedIO("/detail", "/pause")
    run_quiz(io, store, service, attempt)
    assert "自测结束后可以查看评阅详情。" in io.text and "未知命令" not in io.text
