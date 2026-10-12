from datetime import date
import sqlite3

import pytest
from typer.testing import CliRunner

from zhiqiu.clock import FixedClock
from zhiqiu.cli import app
from zhiqiu.store import SCHEMA_VERSION, Store


def test_v1_migration_keeps_business_state_and_creates_backup(tmp_path):
    path = tmp_path / "state.sqlite"
    # A minimal V1 database with real legacy score and task rows.
    db = sqlite3.connect(path)
    try:
        db.executescript("""
            CREATE TABLE knowledge_points (id TEXT PRIMARY KEY,title TEXT UNIQUE,state TEXT,
                difficulty_streak INTEGER,created_at TEXT,parent_id TEXT);
            CREATE TABLE attempts (id TEXT PRIMARY KEY,kp_id TEXT,kind TEXT,state TEXT,start_date TEXT,
                started_at TEXT,ended_at TEXT,finish_reason TEXT);
            CREATE TABLE questions (id TEXT PRIMARY KEY,attempt_id TEXT,seq INTEGER,dimension TEXT,
                text TEXT,asked_at TEXT,UNIQUE(attempt_id,seq));
            CREATE TABLE answers (id TEXT PRIMARY KEY,question_id TEXT UNIQUE,text TEXT,answered_at TEXT);
            CREATE TABLE reviews (id TEXT PRIMARY KEY,attempt_id TEXT UNIQUE,kind TEXT,total INTEGER,
                rating INTEGER,created_at TEXT,revised_at TEXT);
            CREATE TABLE dimension_scores (review_id TEXT,dimension TEXT,score INTEGER,question_id TEXT,
                answer_id TEXT,evidence TEXT,rationale TEXT,PRIMARY KEY(review_id,dimension));
            CREATE TABLE review_tasks (id TEXT PRIMARY KEY,kp_id TEXT,review_id TEXT UNIQUE,due_date TEXT,
                original_due_date TEXT,next_remind_date TEXT,state TEXT,deferrals INTEGER,rule_version TEXT,
                created_at TEXT,started_at TEXT,completed_at TEXT);
            INSERT INTO knowledge_points VALUES('kp','遍历','active',0,'2026-10-02',NULL);
            INSERT INTO attempts VALUES('attempt','kp','first','completed','2026-10-02','2026-10-02',
                '2026-10-02','natural');
            INSERT INTO questions VALUES('question','attempt',1,'concept','解释遍历','2026-10-02');
            INSERT INTO answers VALUES('answer','question','根左右','2026-10-02');
            INSERT INTO reviews VALUES('review','attempt','partial',75,NULL,'2026-10-02',NULL);
            INSERT INTO dimension_scores VALUES('review','concept',3,'question','answer','根左右','原答说明');
            INSERT INTO review_tasks VALUES('task','kp','review','2026-10-03','2026-10-03',
                '2026-10-03','pending',0,'partial-next-day-v1','2026-10-02',NULL,NULL);
        """)
    finally:
        db.close()

    reopened = Store(path, FixedClock(date(2026, 10, 2)))
    assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert reopened.trace("attempt")["review"][0]["total"] == 75
    assert reopened.db.execute("SELECT due_date FROM review_tasks").fetchone()[0] == "2026-10-03"
    assert reopened.db.execute("SELECT difficulty FROM questions").fetchone()[0] == "legacy_unset"
    assert reopened.trace("attempt")["review_evidence"][0]["quote"] == "根左右"
    backup = path.with_suffix(".sqlite.pre-v2.bak")
    assert backup.exists()
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_v9_upgrade_preserves_existing_v2_derived_records(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path, FixedClock(date(2026, 10, 2)))
    kp = store.ensure_knowledge_point("遍历")
    source = store.record_message(kp, "assistant", "lesson", "先访问根节点")
    store.db.execute("""INSERT INTO material_checks VALUES(?,?,?,?,?,?,?,?,?,?)""",
                     ("check", "遍历", "notes.md", "hash", 0, 5, "model", "context-v1", "{}", store._ts()))
    store.db.execute("""INSERT INTO clarification_chunks VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                     ("chunk", kp, source, "hash", "question-hash", 0, 5, "model",
                      "context-v1", '{"relevant":true,"finding":"根","uncertainty":""}', store._ts()))
    store.db.execute("DROP TABLE synthesis_batches")
    store.db.execute("PRAGMA user_version=9")
    store.close()
    reopened = Store(path, FixedClock(date(2026, 10, 2)))
    assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert reopened.db.execute("SELECT COUNT(*) FROM material_checks").fetchone()[0] == 1
    assert reopened.db.execute("SELECT COUNT(*) FROM clarification_chunks").fetchone()[0] == 1
    assert reopened.db.execute("SELECT content FROM messages WHERE id=?", (source,)).fetchone()[0] == "先访问根节点"
    assert path.with_suffix(".sqlite.pre-v10.bak").exists()


def test_session_log_survives_restart_and_new_day_links_to_same_attempt(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path, FixedClock(date(2026, 10, 2)))
    kp = store.ensure_knowledge_point("遍历")
    store.add_materials(kp, [{"kind": "local", "source": "notes.md", "content_hash": "hash",
                              "content": "遍历资料"}])
    lesson = store.record_message(kp, "assistant", "lesson", "先访问根节点")
    note = store.add_note(kp, "clarification", "为什么先访问根？")
    store.explain_note(note, "这是前序遍历")
    attempt = store.start_attempt(kp, "first")
    question, _ = store.add_question(attempt, "concept", "解释前序遍历")
    answer = store.add_answer(question, "根左右")
    first = store.trace(attempt)
    assert len(first["sessions"]) == 1
    assert [m["kind"] for m in first["messages"]] == [
        "lesson", "clarification", "clarification", "question", "answer"]
    assert first["message_materials"] == [
        {"message_id": lesson, "material_id": store.materials_for(kp)[0]["id"]},
        {"message_id": first["messages"][2]["id"], "material_id": store.materials_for(kp)[0]["id"]}]
    store.close()

    reopened = Store(path, FixedClock(date(2026, 10, 3)))
    reopened.pause(attempt)
    reopened.resume(attempt)
    reopened.record_message(kp, "user", "clarification", "继续问前序", attempt,
                            source_message_id=lesson)
    trace = reopened.trace(attempt)
    assert len(trace["sessions"]) == 2
    assert trace["messages"][-1]["source_message_id"] == lesson
    assert trace["questions"][0]["answer_id"] == answer
    assert len(reopened.export_json()["messages"]) == 6
    reopened.forget(kp)
    assert reopened.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert reopened.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert reopened.db.execute("SELECT COUNT(*) FROM message_materials").fetchone()[0] == 0


def test_answer_and_chat_log_commit_together(tmp_path):
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 2)))
    kp = store.ensure_knowledge_point("幂等")
    attempt = store.start_attempt(kp, "first")
    question, _ = store.add_question(attempt, "concept", "解释幂等")
    store.db.execute("""CREATE TRIGGER fail_message BEFORE INSERT ON messages
        BEGIN SELECT RAISE(ABORT, 'injected chat write failure'); END""")
    with pytest.raises(sqlite3.DatabaseError, match="injected chat write failure"):
        store.add_answer(question, "重复操作结果相同")
    assert store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 0
    store.db.execute("DROP TRIGGER fail_message")
    assert store.add_answer(question, "重复操作结果相同")
    assert len(store.messages_for(kp)) == 2


def test_session_references_cannot_cross_knowledge_points(tmp_path):
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 2)))
    first = store.ensure_knowledge_point("前序")
    second = store.ensure_knowledge_point("中序")
    message = store.record_message(first, "assistant", "lesson", "根左右")
    attempt = store.start_attempt(first, "first")
    with pytest.raises(ValueError, match="source message"):
        store.record_message(second, "user", "clarification", "继续", source_message_id=message)
    with pytest.raises(ValueError, match="attempt does not belong"):
        store.record_message(second, "user", "clarification", "继续", attempt_id=attempt)
    assert store.messages_for(second) == []


def test_failed_shadow_migration_preserves_original_database(tmp_path):
    path = tmp_path / "state.sqlite"
    db = sqlite3.connect(path)
    try:
        db.executescript("""
            CREATE TABLE knowledge_points(id TEXT PRIMARY KEY,title TEXT,parent_id TEXT,state TEXT,
                difficulty_streak INTEGER,created_at TEXT);
            CREATE TABLE attempts(id TEXT PRIMARY KEY,kp_id TEXT,kind TEXT,state TEXT,start_date TEXT,
                started_at TEXT,ended_at TEXT,finish_reason TEXT);
            CREATE TABLE questions(id TEXT PRIMARY KEY,attempt_id TEXT,seq INTEGER,dimension TEXT,text TEXT,
                asked_at TEXT,UNIQUE(attempt_id,seq));
            CREATE TABLE answers(id TEXT PRIMARY KEY,question_id TEXT,text TEXT,answered_at TEXT);
            CREATE TABLE reviews(id TEXT PRIMARY KEY,attempt_id TEXT,kind TEXT,total INTEGER,rating INTEGER,
                created_at TEXT,revised_at TEXT);
            CREATE TABLE dimension_scores(review_id TEXT,dimension TEXT,score INTEGER,question_id TEXT,
                answer_id TEXT,evidence TEXT,rationale TEXT,PRIMARY KEY(review_id,dimension));
            INSERT INTO knowledge_points VALUES('kp','遍历',NULL,'active',0,'2026-10-02');
            INSERT INTO attempts VALUES('a','kp','first','completed','2026-10-02','2026-10-02',
                '2026-10-02','natural');
            INSERT INTO questions VALUES('q','a',1,'concept','解释','2026-10-02');
            INSERT INTO answers VALUES('answer','q','根左右','2026-10-02');
            INSERT INTO reviews VALUES('r','a','partial',75,NULL,'2026-10-02',NULL);
            INSERT INTO dimension_scores VALUES('r','concept',3,'q','answer','虚构引文','理由');
        """)
    finally:
        db.close()
    with pytest.raises(sqlite3.IntegrityError, match="legacy score evidence"):
        Store(path, FixedClock(date(2026, 10, 2)))
    db = sqlite3.connect(path)
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT evidence FROM dimension_scores").fetchone()[0] == "虚构引文"
        assert db.execute("PRAGMA table_info(questions)").fetchall()[-1][1] == "asked_at"
    finally:
        db.close()
    assert path.with_suffix(".sqlite.pre-v2.bak").exists()


def test_unfinished_lesson_resumes_in_new_session_without_a_test_task(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path, FixedClock(date(2026, 10, 2)))
    kp = store.ensure_knowledge_point("遍历")
    store.add_materials(kp, [{"kind": "local", "source": "notes.md", "content_hash": "hash",
                              "content": "前序遍历"}])
    original = store.record_message(kp, "assistant", "lesson", "先访问根节点")
    pending = store.pending_lessons()[0]
    store.close()

    resumed = Store(path, FixedClock(date(2026, 10, 3)))
    current = resumed.resume_lesson(pending["session_id"])
    assert current["session_id"] != pending["session_id"]
    assert current["lesson"] == "先访问根节点"
    assert len(resumed.pending_lessons()) == 1
    assert resumed.db.execute("SELECT source_message_id FROM messages WHERE id=?",
                              (current["lesson_message_id"],)).fetchone()[0] == original
    assert resumed.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    assert resumed.db.execute("SELECT COUNT(*) FROM review_tasks").fetchone()[0] == 0
    attempt = resumed.start_attempt(kp, "first")
    assert resumed.trace(attempt)["sessions"][0]["id"] == current["session_id"]
    assert resumed.pending_lessons() == []


def test_lesson_resume_rejects_changed_sources(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path, FixedClock(date(2026, 10, 2)))
    kp = store.ensure_knowledge_point("遍历")
    store.add_materials(kp, [{"kind": "local", "source": "old.md", "content_hash": "old",
                              "content": "旧资料"}])
    store.record_message(kp, "assistant", "lesson", "旧讲解")
    session = store.pending_lessons()[0]["session_id"]
    store.add_materials(kp, [{"kind": "local", "source": "new.md", "content_hash": "new",
                              "content": "新资料"}])
    with pytest.raises(ValueError, match="materials changed"):
        store.resume_lesson(session)
    assert store.pending_lessons()[0]["session_id"] == session


def test_cli_can_reopen_saved_lesson_without_model_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr("zhiqiu.cli.load_local_env", lambda: None)
    path = tmp_path / "state.sqlite"
    store = Store(path, FixedClock(date(2026, 10, 2)))
    kp = store.ensure_knowledge_point("遍历")
    store.record_message(kp, "assistant", "lesson", "先访问根节点")
    session = store.pending_lessons()[0]["session_id"]
    store.close()
    monkeypatch.delenv("ZHIQIU_LLM_API_KEY", raising=False)
    result = CliRunner().invoke(app, ["--db", str(path), "resume", session], input="/pause\n")
    assert result.exit_code == 0
    assert "先访问根节点" in result.output
    assert "可用 resume 继续" in result.output


def test_resumed_lesson_reuses_runtime_across_clarifications(tmp_path, monkeypatch):
    monkeypatch.setattr("zhiqiu.cli.load_local_env", lambda: None)
    path = tmp_path / "state.sqlite"
    store = Store(path, FixedClock(date(2026, 10, 2)))
    kp = store.ensure_knowledge_point("遍历")
    store.record_message(kp, "assistant", "lesson", "前序是根左右")
    session = store.pending_lessons()[0]["session_id"]
    store.close()

    class FakeRuntime:
        def __init__(self):
            self.questions = []

        def clarify(self, kp_id, question):
            self.questions.append((kp_id, question))
            return {"explanation": "合成解释", "followup": ""}

    instances = []
    def make_runtime(_):
        instance = FakeRuntime()
        instances.append(instance)
        return instance

    monkeypatch.setattr("zhiqiu.cli.runtime", make_runtime)
    result = CliRunner().invoke(app, ["--db", str(path), "resume", session],
                                input="/question 第一问\n/question 第二问\n/pause\n")
    assert result.exit_code == 0
    assert len(instances) == 1
    assert instances[0].questions == [(kp, "第一问"), (kp, "第二问")]
