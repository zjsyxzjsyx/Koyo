from datetime import date

import pytest

from zhiqiu.clock import FixedClock
from zhiqiu.scoring import DimensionGrade, calculate
from zhiqiu.store import Store


def store_at(tmp_path, day=date(2026, 10, 2)):
    return Store(tmp_path / "state.sqlite", FixedClock(day))


def answered_attempt(store):
    kp = store.ensure_knowledge_point("二叉树遍历")
    attempt = store.start_attempt(kp, "first")
    grades = []
    for dimension, score in (("concept", 4), ("mechanism", 3), ("application", 1)):
        question_id, _ = store.add_question(attempt, dimension, f"解释{dimension}")
        answer_id = store.add_answer(question_id, f"{dimension} 的原始回答")
        grades.append(DimensionGrade(dimension, score, question_id, answer_id,
                                     f"{dimension} 的原始回答", "依据已给出的回答"))
    return kp, attempt, grades


def test_complete_review_saves_one_task_and_read_only_startup(tmp_path):
    store = store_at(tmp_path)
    kp, attempt, grades = answered_attempt(store)
    first = store.finalize(attempt, grades)
    assert first["total"] == 78
    assert first["rating"] == 3
    assert first["due_date"] == "2026-10-04"
    assert store.finalize(attempt, grades) == first
    assert store.db.execute("SELECT COUNT(*) FROM review_tasks").fetchone()[0] == 1
    store.close()

    reopened = store_at(tmp_path, date(2026, 10, 4))
    before = reopened.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert [x["id"] for x in reopened.status()["due"]] == [first["task_id"]]
    assert [x["id"] for x in reopened.status()["due"]] == [first["task_id"]]
    assert reopened.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
    assert reopened.db.execute("SELECT state FROM review_tasks WHERE id=?", (first["task_id"],)).fetchone()[0] == "pending"
    assert reopened.get_knowledge_point(kp)["title"] == "二叉树遍历"


def test_pause_and_crash_keep_same_attempt(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("幂等")
    attempt = store.start_attempt(kp, "first")
    question, _ = store.add_question(attempt, "concept", "解释幂等")
    answer = store.add_answer(question, "重复操作结果相同")
    store.pause(attempt)
    store.close()

    reopened = store_at(tmp_path)
    assert reopened.active_attempt(kp)["id"] == attempt
    assert reopened.qa_for(attempt)[0]["answer_id"] == answer
    assert reopened.db.execute("SELECT COUNT(*) FROM review_tasks").fetchone()[0] == 0
    reopened.resume(attempt)
    assert reopened.start_attempt(kp, "first") == attempt


def test_task_write_failure_rolls_back_review_and_fsrs(tmp_path):
    store = store_at(tmp_path)
    _, attempt, grades = answered_attempt(store)
    store.db.execute("""CREATE TRIGGER fail_task BEFORE INSERT ON review_tasks
        BEGIN SELECT RAISE(ABORT, 'injected task write failure'); END""")
    with pytest.raises(Exception, match="injected task write failure"):
        store.finalize(attempt, grades)
    assert store.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM fsrs_cycles").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    assert store.db.execute("SELECT state FROM attempts WHERE id=?", (attempt,)).fetchone()[0] == "in_progress"
    store.db.execute("DROP TRIGGER fail_task")
    assert store.finalize(attempt, grades)["task_id"]


def test_missing_dimension_is_partial_and_zero_is_unscored(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("概念")
    attempt = store.start_attempt(kp, "first")
    question, _ = store.add_question(attempt, "concept", "是什么")
    answer = store.add_answer(question, "我不会")
    partial = store.finalize(attempt, [DimensionGrade("concept", 0, question, answer, "我不会", "明确不会")], "explicit")
    assert partial["kind"] == "partial" and partial["total"] == 0
    assert partial["due_date"] == "2026-10-03"
    assert store.db.execute("SELECT COUNT(*) FROM fsrs_logs").fetchone()[0] == 0

    other = store.ensure_knowledge_point("其他")
    empty_attempt = store.start_attempt(other, "first")
    zero = store.finalize(empty_attempt, [], "explicit")
    assert zero["kind"] == "zero" and zero["task_id"] is None
    assert store.active_attempt(other)["state"] == "needs_self_test"


def test_zero_first_self_test_can_resume_and_score_same_attempt(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("待补自测")
    attempt = store.start_attempt(kp, "first")
    question, _ = store.add_question(attempt, "concept", "请解释概念")
    zero = store.finalize(attempt, [], "explicit")
    assert zero["kind"] == "zero"
    store.close()
    store = store_at(tmp_path, date(2026, 10, 3))
    store.resume(attempt)
    answer = store.add_answer(question, "这是一个可评分的回答")
    result = store.finalize(attempt, [DimensionGrade("concept", 2, question, answer,
                                                      "可评分的回答", "有部分理解")], "explicit")
    assert result["kind"] == "partial" and result["due_date"] == "2026-10-04"
    assert store.finalize(attempt, []) == result
    assert store.db.execute("SELECT COUNT(*) FROM review_tasks").fetchone()[0] == 1


def test_grade_evidence_and_rounding(tmp_path):
    store = store_at(tmp_path)
    _, _, grades = answered_attempt(store)
    pairs = {g.answer_id: (g.question_id, g.evidence) for g in grades}
    assert calculate(grades, pairs).total == 78
    with pytest.raises(ValueError, match="evidence"):
        calculate([DimensionGrade("concept", 4, grades[0].question_id, grades[0].answer_id,
                                  "invented", "wrong")], pairs)
