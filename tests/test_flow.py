import json
from datetime import date, timedelta

import pytest

from zhiqiu import flow
from zhiqiu.clock import FixedClock
from zhiqiu.flow import IllegalTransition, TRANSITIONS
from zhiqiu.scoring import DimensionGrade
from zhiqiu.store import SCHEMA_VERSION, Store


def store_at(tmp_path, day=date(2026, 10, 2)):
    return Store(tmp_path / "state.sqlite", FixedClock(day))


def changes(store, entity=None):
    rows = [json.loads(r[0]) for r in store.db.execute(
        "SELECT payload_json FROM events WHERE type='StateChanged' ORDER BY id")]
    return [(r["entity"], r["from"], r["to"], r["cause"]) for r in rows if entity in (None, r["entity"])]


def answer_all(store, attempt):
    grades = []
    for dimension, score in (("concept", 4), ("mechanism", 3), ("application", 1)):
        question_id, _ = store.add_question(attempt, dimension, f"解释{dimension}")
        answer_id = store.add_answer(question_id, f"{dimension} 的原始回答")
        grades.append(DimensionGrade(dimension, score, question_id, answer_id,
                                     f"{dimension} 的原始回答", "依据已给出的回答"))
    return grades


def test_every_declared_table_has_known_states_only():
    for entity, table in TRANSITIONS.items():
        states = {state for state in table if state is not None}
        for targets in table.values():
            assert targets <= states, entity


def test_first_attempt_lifecycle_writes_one_event_per_change(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("二叉树遍历")
    store.record_message(kp, "assistant", "lesson", "讲解")
    attempt = store.start_attempt(kp, "first")
    store.pause(attempt)
    store.resume(attempt)
    store.resume(attempt)  # in_progress -> in_progress is a no-op
    result = store.finalize(attempt, answer_all(store, attempt))
    assert changes(store) == [
        ("knowledge_point", None, "active", "created"),
        ("lesson", None, "teaching", "lesson"),
        ("attempt", None, "in_progress", "start_first"),
        ("lesson", "teaching", "tested", "attempt_started"),
        ("attempt", "in_progress", "paused", "pause"),
        ("attempt", "paused", "in_progress", "resume"),
        ("review_task", None, "pending", "scheduled"),
        ("attempt", "in_progress", "completed", "finalize_natural"),
    ]
    assert store.pending_lessons() == []
    event = store.db.execute("""SELECT kp_id,attempt_id FROM events WHERE type='StateChanged'
        AND json_extract(payload_json,'$.to')='completed'""").fetchone()
    assert tuple(event) == (kp, attempt)
    task = store.db.execute("SELECT state FROM review_tasks WHERE id=?", (result["task_id"],)).fetchone()
    assert task[0] == "pending"


def test_review_and_zero_review_move_tasks_through_the_table(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("二叉树遍历")
    attempt = store.start_attempt(kp, "first")
    first = store.finalize(attempt, answer_all(store, attempt))
    later = store_at(tmp_path, date.fromisoformat(first["due_date"]))
    review = later.start_attempt(kp, "review", [first["task_id"]])
    later.finalize(review, [], "explicit")  # nothing answered: deferred to tomorrow
    assert changes(later, "review_task")[-2:] == [
        ("review_task", "pending", "started", "review_started"),
        ("review_task", "started", "pending", "zero_answer_deferred")]
    assert changes(later, "attempt")[-1] == ("attempt", "in_progress", "completed", "zero_answer")
    tomorrow = store_at(tmp_path, date.fromisoformat(first["due_date"]) + timedelta(days=1))
    second = tomorrow.start_attempt(kp, "review", [first["task_id"]])
    tomorrow.finalize(second, answer_all(tomorrow, second))
    assert changes(tomorrow, "review_task")[-3:] == [
        ("review_task", "pending", "started", "review_started"),
        ("review_task", None, "pending", "scheduled"),
        ("review_task", "started", "done", "reviewed")]


def test_zero_first_self_test_goes_through_needs_self_test(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("二叉树遍历")
    attempt = store.start_attempt(kp, "first")
    store.finalize(attempt, [], "explicit")
    store.resume(attempt)
    assert changes(store, "attempt")[-2:] == [
        ("attempt", "in_progress", "needs_self_test", "zero_answer"),
        ("attempt", "needs_self_test", "in_progress", "resume")]


@pytest.mark.parametrize("entity,start,target", [
    ("attempt", "completed", "in_progress"),
    ("attempt", "paused", "completed"),
    ("review_task", "done", "started"),
    ("lesson", "tested", "teaching"),
    ("lesson", "closed", "teaching"),
])
def test_illegal_transition_changes_nothing(tmp_path, entity, start, target):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("二叉树遍历")
    if entity == "attempt":
        row_id = store.start_attempt(kp, "first")
    elif entity == "review_task":
        attempt = store.start_attempt(kp, "first")
        row_id = store.finalize(attempt, answer_all(store, attempt))["task_id"]
    else:
        store.record_message(kp, "assistant", "lesson", "讲解")
        row_id = store.pending_lessons()[0]["session_id"]
    table, column = flow.COLUMNS[entity]
    store.db.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (start, row_id))
    before = store.db.execute("SELECT COUNT(*) FROM events WHERE type='StateChanged'").fetchone()[0]
    with pytest.raises(IllegalTransition) as error:
        with store.transaction():
            flow.transition(store, entity, row_id, target, "test")
    assert isinstance(error.value, ValueError)
    assert store.db.execute(f"SELECT {column} FROM {table} WHERE id=?", (row_id,)).fetchone()[0] == start
    assert store.db.execute("SELECT COUNT(*) FROM events WHERE type='StateChanged'").fetchone()[0] == before
    # V5: the refusal itself is kept as a guardrail event, outside the rolled-back transaction.
    guard = store.db.execute("SELECT payload_json FROM events WHERE type='GuardrailTriggered'").fetchall()
    assert [json.loads(row[0])["rule"] for row in guard] == ["illegal_transition"]


def test_closed_lesson_stays_closed_and_a_new_lesson_gets_its_own_session(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("二叉树遍历")
    store.record_message(kp, "assistant", "lesson", "第一次讲解")
    closed = store.pending_lessons()[0]["session_id"]
    store.close_lesson(closed)
    store.record_message(kp, "assistant", "lesson", "重新讲解")
    pending = store.pending_lessons()
    assert len(pending) == 1 and pending[0]["session_id"] != closed and pending[0]["lesson"] == "重新讲解"
    assert store.db.execute("SELECT lesson_state FROM sessions WHERE id=?", (closed,)).fetchone()[0] == "closed"
    attempt = store.start_attempt(kp, "first")
    assert store.db.execute("SELECT attempt_id FROM sessions WHERE id=?", (closed,)).fetchone()[0] is None
    assert store.db.execute("SELECT lesson_state FROM sessions WHERE attempt_id=?", (attempt,)).fetchone()[0] == "tested"


def test_cross_day_resume_closes_old_lesson_and_teaches_in_today_session(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("二叉树遍历")
    store.record_message(kp, "assistant", "lesson", "讲解")
    old = store.pending_lessons()[0]["session_id"]
    store.close()
    later = store_at(tmp_path, date(2026, 10, 3))
    resumed = later.resume_lesson(old)
    assert resumed["session_id"] != old
    assert later.db.execute("SELECT lesson_state FROM sessions WHERE id=?", (old,)).fetchone()[0] == "closed"
    assert changes(later, "lesson")[-2:] == [("lesson", "teaching", "closed", "continued"),
                                             ("lesson", None, "teaching", "lesson_resume")]


def test_v13_database_backfills_lesson_state_and_keeps_pending_lessons(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path, FixedClock(date(2026, 10, 2)))
    teaching = store.ensure_knowledge_point("讲解中")
    store.record_message(teaching, "assistant", "lesson", "还没学完")
    closed = store.ensure_knowledge_point("已关闭")
    store.record_message(closed, "assistant", "lesson", "不学了")
    store.close_lesson(next(r["session_id"] for r in store.pending_lessons() if r["kp_id"] == closed))
    tested = store.ensure_knowledge_point("已自测")
    store.record_message(tested, "assistant", "lesson", "学完了")
    store.start_attempt(tested, "first")
    store.ensure_knowledge_point("只有问答")
    before = [tuple(r) for r in store.pending_lessons()]
    store.db.execute("ALTER TABLE sessions DROP COLUMN lesson_state")
    store.db.execute("ALTER TABLE tool_calls DROP COLUMN conversation_id")
    store.db.execute("DROP TABLE conversation_messages")
    store.db.execute("DROP TABLE conversations")
    store.db.execute("PRAGMA user_version=13")
    store.close()
    reopened = Store(path, FixedClock(date(2026, 10, 2)))
    assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 25
    assert path.with_suffix(".sqlite.pre-v14.bak").exists()
    states = dict(reopened.db.execute("""SELECT k.title,s.lesson_state FROM sessions s
        JOIN knowledge_points k ON k.id=s.kp_id"""))
    assert states == {"讲解中": "teaching", "已关闭": "closed", "已自测": "tested"}
    assert [tuple(r) for r in reopened.pending_lessons()] == before
    assert reopened.db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    assert "conversation_id" in {r["name"] for r in reopened.db.execute("PRAGMA table_info(tool_calls)")}
    reopened.close()
