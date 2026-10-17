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


def test_v19_database_gains_title_and_feedback_columns_with_backup(tmp_path):
    from zhiqiu.conversations import Conversations
    from zhiqiu.store import SCHEMA_VERSION
    store = store_at(tmp_path)
    kp, attempt, grades = answered_attempt(store)
    store.finalize(attempt, grades)
    chats = Conversations(store)
    conversation = chats.open_today()
    chats.add_message(conversation["id"], "user", "chat", "你好")
    for table, column in (("conversations", "title"), ("conversations", "title_source"),
                          ("dimension_scores", "feedback")):
        store.db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    store.db.execute("PRAGMA user_version=19")
    before = store.db.execute("SELECT review_id,dimension,score,rationale FROM dimension_scores ORDER BY dimension").fetchall()
    before = [tuple(row) for row in before]
    store.close()

    reopened = store_at(tmp_path)
    assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 23
    assert (tmp_path / "state.sqlite.pre-v20.bak").exists()
    after = reopened.db.execute("""SELECT review_id,dimension,score,rationale FROM dimension_scores
        ORDER BY dimension""").fetchall()
    assert [tuple(row) for row in after] == before
    assert {row["feedback"] for row in reopened.db.execute("SELECT feedback FROM dimension_scores")} == {""}
    row = reopened.db.execute("SELECT title,title_source FROM conversations WHERE id=?", (conversation["id"],)).fetchone()
    assert tuple(row) == ("", "")
    exported = reopened.export_json()
    assert "title" in exported["conversations"][0] and "feedback" in exported["dimension_scores"][0]


def test_new_database_has_v20_columns_defaulting_to_empty(tmp_path):
    from zhiqiu.conversations import Conversations
    store = store_at(tmp_path)
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 23
    columns = {table: {row["name"]: row for row in store.db.execute(f"PRAGMA table_info({table})")}
               for table in ("conversations", "dimension_scores")}
    for table, column in (("conversations", "title"), ("conversations", "title_source"),
                          ("dimension_scores", "feedback")):
        assert columns[table][column]["notnull"] == 1 and columns[table][column]["dflt_value"] == "''"
    chats = Conversations(store)
    conversation = chats.open_today()
    store.db.execute("UPDATE conversations SET title='红黑树',title_source='user' WHERE id=?", (conversation["id"],))
    chats.forget(conversation["id"])
    assert store.db.execute("SELECT COUNT(*) FROM conversations WHERE title='红黑树'").fetchone()[0] == 0


def test_v20_database_gains_file_roots_with_backup(tmp_path):
    from zhiqiu.store import SCHEMA_VERSION
    store = store_at(tmp_path)
    kp, attempt, grades = answered_attempt(store)
    store.finalize(attempt, grades)
    store.db.execute("DROP TABLE file_roots")
    store.db.execute("PRAGMA user_version=20")
    store.close()

    reopened = store_at(tmp_path)
    assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 23
    assert (tmp_path / "state.sqlite.pre-v21.bak").exists()
    assert reopened.roots("read") == [] and "file_roots" in reopened.export_json()
    assert reopened.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1


def test_roots_are_added_and_removed_with_an_audit_event(tmp_path):
    import json
    store = store_at(tmp_path)
    store.add_root("read", "D:/notes/算法", 37, False)
    store.add_root("write", "D:/notes/out", 0, False)
    with pytest.raises(ValueError, match="已在白名单中"):
        store.add_root("read", "D:/notes/算法", 37, False)
    with pytest.raises(ValueError):
        store.add_root("exec", "D:/x", 0, False)
    assert [row["path"] for row in store.roots("read")] == ["D:/notes/算法"]
    store.remove_root("read", "D:/notes/算法")
    with pytest.raises(LookupError):
        store.remove_root("read", "D:/notes/算法")
    assert store.roots("read") == [] and len(store.roots("write")) == 1
    events = [json.loads(row[0]) for row in store.db.execute(
        "SELECT payload_json FROM events WHERE type='RootsChanged' ORDER BY id")]
    assert [(e["action"], e["kind"]) for e in events] == [("add", "read"), ("add", "write"), ("remove", "read")]
    assert set(events[0]) == {"action", "kind", "path", "files", "truncated", "via"} and events[2]["files"] == 37


def test_v21_database_gains_skill_and_input_source_columns_with_backup(tmp_path):
    from zhiqiu.store import SCHEMA_VERSION
    store = store_at(tmp_path)
    kp, attempt, grades = answered_attempt(store)
    store.finalize(attempt, grades)
    for table, column in (("knowledge_points", "skill"), ("knowledge_points", "skill_source"),
                          ("knowledge_points", "skill_at"), ("context_builds", "skill_name"),
                          ("context_builds", "skill_version"), ("context_builds", "skill_sha"),
                          ("context_builds", "skill_source"), ("answers", "input_source")):
        store.db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    store.db.execute("PRAGMA user_version=21")
    store.close()

    reopened = store_at(tmp_path)
    assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 23
    assert (tmp_path / "state.sqlite.pre-v22.bak").exists()
    assert {row["input_source"] for row in reopened.db.execute("SELECT input_source FROM answers")} == {"text"}
    row = reopened.db.execute("SELECT skill,skill_source,skill_at FROM knowledge_points WHERE id=?", (kp,)).fetchone()
    assert tuple(row) == (None, None, None)
    exported = reopened.export_json()
    assert "skill" in exported["knowledge_points"][0] and "input_source" in exported["answers"][0]


def test_set_skill_records_a_change_only_when_the_value_changes(tmp_path):
    import json
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("二叉树遍历")
    assert store.set_skill(kp, "feynman", "model", "适合先用白话讲清") is True
    assert store.set_skill(kp, "feynman", "model") is False
    assert store.set_skill(kp, "general", "user") is True
    with pytest.raises(ValueError):
        store.set_skill(kp, "general", "agent")
    with pytest.raises(LookupError):
        store.set_skill("missing", "general", "user")
    assert store.set_skill(kp, None, None) is True
    row = store.db.execute("SELECT skill,skill_source,skill_at FROM knowledge_points WHERE id=?", (kp,)).fetchone()
    assert tuple(row) == (None, None, None)
    events = [json.loads(r[0]) for r in store.db.execute(
        "SELECT payload_json FROM events WHERE type='SkillChanged' ORDER BY id")]
    assert events == [{"from": None, "to": "feynman", "source": "model", "reason": "适合先用白话讲清"},
                      {"from": "feynman", "to": "general", "source": "user"},
                      {"from": "general", "to": None, "source": "user"}]


def test_answers_record_how_they_were_entered(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("二叉树遍历")
    attempt = store.start_attempt(kp, "first")
    typed, _ = store.add_question(attempt, "concept", "解释前序")
    spoken, _ = store.add_question(attempt, "mechanism", "解释中序")
    store.add_answer(typed, "根左右")
    store.add_answer(spoken, "左根右", input_source="voice")
    other, _ = store.add_question(attempt, "application", "举例")
    with pytest.raises(ValueError):
        store.add_answer(other, "x", input_source="keyboard")
    rows = dict(store.db.execute("SELECT question_id,input_source FROM answers").fetchall())
    assert rows == {typed: "text", spoken: "voice"}
