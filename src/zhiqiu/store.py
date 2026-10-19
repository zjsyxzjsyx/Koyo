from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator

from fsrs import Card, Rating, Scheduler

from . import flow, guardrails, history, queries, tracing
from .redact import redact, redact_obj
from .clock import Clock
from .scoring import (RUBRIC_VERSION, DimensionGrade, QuestionAssessment, assessments_for, calculate,
                      rating_for)

# Opening an older file migrates a backed-up copy first, then swaps it in.
SCHEMA_VERSION = 24
# Review attempts a day may start; the schedule shows the days that would go over it.
DAILY_REVIEW_LIMIT = 3
LLM_FAILURE_CHARS = 20_000
# Caps for the optional prompt/reply text of one model call (ZHIQIU_TRACE_CONTENT=1).
TRACE_REQUEST_CHARS = 60_000
TRACE_RESPONSE_CHARS = 20_000
# An approval request keeps only the first line of what the user was shown (a write's preview is content).
APPROVAL_TITLE_CHARS = 200
# No prompt waits longer than this (the web workbench gives up after 30 minutes); older pending rows are stale.
APPROVAL_STALE_NS = 30 * 60 * 10**9
# Directories the user allowed on the workbench page: readable (sent to the model in chat) or writable.
ROOT_KINDS = ("read", "write")
# Who settled the Skill a knowledge point is taught with: the model while assessing, or the user (/skill).
SKILL_SOURCES = ("model", "user")
# How an answer was entered; voice means a transcript the user confirmed before sending.
INPUT_SOURCES = ("text", "voice")


def new_id() -> str:
    return str(uuid.uuid4())


class Store:
    """The authority for learning state. Every state change is one SQLite transaction."""

    def __init__(self, path: Path, clock: Clock | None = None, *, _shadow: bool = False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or Clock()
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=10000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        existing = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_points'"
        ).fetchone() is not None
        try:
            if existing and version < SCHEMA_VERSION and not _shadow:
                self._migrate_existing(version)
            else:
                self._migrate()
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _business_snapshot(db: sqlite3.Connection) -> dict[str, tuple[tuple[Any, ...], ...]]:
        snapshot_sql = {
            "knowledge_points": "SELECT id,title,parent_id,state,difficulty_streak,created_at FROM knowledge_points",
            "materials": "SELECT * FROM materials",
            "attempts": "SELECT * FROM attempts",
            "questions": "SELECT id,attempt_id,seq,dimension,text,asked_at FROM questions",
            "answers": "SELECT id,question_id,text,answered_at FROM answers",
            "reviews": "SELECT * FROM reviews",
            "dimension_scores": "SELECT review_id,dimension,score,rationale FROM dimension_scores",
            "review_revisions": "SELECT * FROM review_revisions",
            "fsrs_cycles": "SELECT * FROM fsrs_cycles",
            "fsrs_logs": "SELECT * FROM fsrs_logs",
            "review_tasks": "SELECT * FROM review_tasks",
            "attempt_tasks": "SELECT * FROM attempt_tasks",
            "notes": "SELECT * FROM notes",
            "consents": "SELECT * FROM consents",
            "receipts": "SELECT * FROM receipts",
            "events": "SELECT id,kp_id,attempt_id,type,payload_json,created_at FROM events",
            "llm_calls": """SELECT id,kp_id,attempt_id,point,model,prompt_hash,latency_ms,
                tokens_in,tokens_out,status,created_at FROM llm_calls""",
            "search_calls": "SELECT id,point,query,result_count,status,created_at FROM search_calls",
            "tool_calls": """SELECT id,tool,source,permission,kp_id,attempt_id,session_id,point,loop_id,round,
                args_summary_json,confirmed,status,error_kind,attempts,latency_ms,result_count,output_chars,
                truncated,created_at FROM tool_calls""",
            "metrics": "SELECT * FROM metrics",
            "sessions": "SELECT id,kp_id,attempt_id,session_date,created_at FROM sessions",
            "messages": "SELECT * FROM messages",
            "message_materials": "SELECT * FROM message_materials",
            "scoring_submissions": "SELECT * FROM scoring_submissions",
            "review_question_assessments": "SELECT * FROM review_question_assessments",
            "review_evidence": "SELECT * FROM review_evidence",
            "summaries": "SELECT * FROM summaries",
            "semantic_memories": "SELECT * FROM semantic_memories",
            "episodic_memories": "SELECT * FROM episodic_memories",
            "generation_jobs": "SELECT * FROM generation_jobs",
            "cross_day_queries": "SELECT * FROM cross_day_queries",
            "review_batches": "SELECT * FROM review_batches",
            "answer_segments": "SELECT * FROM answer_segments",
            "material_checks": "SELECT * FROM material_checks",
            "clarification_chunks": "SELECT * FROM clarification_chunks",
            "context_builds": """SELECT id,kp_id,session_id,attempt_id,stage,template_version,
                source_ids_json,excluded_json,budget_json,status,created_at FROM context_builds""",
        }
        existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {table: tuple(sorted((tuple(row) for row in db.execute(query)), key=repr))
                for table, query in snapshot_sql.items() if table in existing}

    def _migrate_existing(self, version: int) -> None:
        # SQLite backup makes a consistent snapshot even when a previous process
        # did not exit cleanly. Migrate the copy and swap only after comparison.
        mode = self.db.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if mode != "delete":
            raise RuntimeError("database journal must be closed before migration")
        before = self._business_snapshot(self.db)
        backup = self.path.with_suffix(self.path.suffix +
                                       (f".pre-v{max(2, version + 1)}.bak"))
        if backup.exists():
            backup = backup.with_name(f"{backup.name}.{new_id()}")
        temporary_backup = backup.with_name(f"{backup.name}.{new_id()}.tmp")
        working = self.path.with_name(f"{self.path.name}.{new_id()}.migrating")
        try:
            for target_path in (temporary_backup, working):
                target = sqlite3.connect(target_path)
                try:
                    self.db.backup(target)
                finally:
                    target.close()
            os.replace(temporary_backup, backup)
            shadow = Store(working, self.clock, _shadow=True)
            try:
                if shadow.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise sqlite3.IntegrityError("migrated copy failed integrity check")
                if shadow.db.execute("PRAGMA foreign_key_check").fetchone():
                    raise sqlite3.IntegrityError("migrated copy failed foreign key check")
                after = self._business_snapshot(shadow.db)
                if any(after.get(table) != rows for table, rows in before.items()):
                    raise sqlite3.IntegrityError("migration changed V1 scores or business state")
            finally:
                shadow.close()
            self.db.close()
            os.replace(working, self.path)
            self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA busy_timeout=10000")
        finally:
            temporary_backup.unlink(missing_ok=True)
            working.unlink(missing_ok=True)

    def _migrate(self) -> None:
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS knowledge_points (
            id TEXT PRIMARY KEY, title TEXT NOT NULL UNIQUE, parent_id TEXT REFERENCES knowledge_points(id) ON DELETE SET NULL,
            state TEXT NOT NULL DEFAULT 'active',
            difficulty_streak INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS materials (
            id TEXT PRIMARY KEY, kp_id TEXT NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
            content TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(kp_id, source, content_hash));
        CREATE TABLE IF NOT EXISTS attempts (
            id TEXT PRIMARY KEY, kp_id TEXT NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, state TEXT NOT NULL, start_date TEXT NOT NULL,
            started_at TEXT NOT NULL, ended_at TEXT, finish_reason TEXT);
        CREATE INDEX IF NOT EXISTS attempts_kp_state ON attempts(kp_id, state);
        CREATE TABLE IF NOT EXISTS questions (
            id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL, dimension TEXT NOT NULL, text TEXT NOT NULL,
            asked_at TEXT NOT NULL, UNIQUE(attempt_id, seq));
        CREATE TABLE IF NOT EXISTS answers (
            id TEXT PRIMARY KEY, question_id TEXT NOT NULL UNIQUE REFERENCES questions(id) ON DELETE CASCADE,
            text TEXT NOT NULL, answered_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reviews (
            id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, total INTEGER, rating INTEGER, created_at TEXT NOT NULL,
            revised_at TEXT);
        CREATE TABLE IF NOT EXISTS dimension_scores (
            review_id TEXT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
            dimension TEXT NOT NULL, score INTEGER NOT NULL, question_id TEXT NOT NULL,
            answer_id TEXT NOT NULL, evidence TEXT NOT NULL, rationale TEXT NOT NULL,
            PRIMARY KEY(review_id, dimension));
        CREATE TABLE IF NOT EXISTS review_revisions (
            id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
            dimension TEXT NOT NULL, old_score INTEGER NOT NULL, new_score INTEGER NOT NULL,
            reason TEXT NOT NULL, revised_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fsrs_cycles (
            id TEXT PRIMARY KEY, kp_id TEXT NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
            card_json TEXT NOT NULL, started_at TEXT NOT NULL, closed_at TEXT);
        CREATE TABLE IF NOT EXISTS fsrs_logs (
            id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES fsrs_cycles(id) ON DELETE CASCADE,
            review_id TEXT NOT NULL UNIQUE REFERENCES reviews(id) ON DELETE CASCADE,
            log_json TEXT NOT NULL, card_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS review_tasks (
            id TEXT PRIMARY KEY, kp_id TEXT NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
            review_id TEXT NOT NULL UNIQUE REFERENCES reviews(id) ON DELETE CASCADE,
            due_date TEXT NOT NULL, original_due_date TEXT NOT NULL,
            next_remind_date TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            deferrals INTEGER NOT NULL DEFAULT 0, rule_version TEXT NOT NULL,
            created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT);
        CREATE INDEX IF NOT EXISTS tasks_due ON review_tasks(state, next_remind_date, original_due_date);
        CREATE TABLE IF NOT EXISTS attempt_tasks (
            attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
            task_id TEXT NOT NULL REFERENCES review_tasks(id) ON DELETE CASCADE,
            PRIMARY KEY(attempt_id, task_id));
        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY, kp_id TEXT NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
            attempt_id TEXT REFERENCES attempts(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, text TEXT NOT NULL, explanation TEXT NOT NULL DEFAULT '',
            resolved INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS consents (
            id TEXT PRIMARY KEY, kp_id TEXT NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
            material_hash TEXT NOT NULL, endpoint TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(kp_id, material_hash, endpoint));
        CREATE TABLE IF NOT EXISTS receipts (
            operation_key TEXT PRIMARY KEY, attempt_id TEXT REFERENCES attempts(id) ON DELETE CASCADE,
            result_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kp_id TEXT REFERENCES knowledge_points(id) ON DELETE CASCADE,
            attempt_id TEXT REFERENCES attempts(id) ON DELETE CASCADE,
            type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS llm_calls (
            id TEXT PRIMARY KEY, kp_id TEXT REFERENCES knowledge_points(id) ON DELETE CASCADE,
            attempt_id TEXT REFERENCES attempts(id) ON DELETE CASCADE,
            point TEXT NOT NULL, model TEXT NOT NULL, prompt_hash TEXT NOT NULL, latency_ms INTEGER NOT NULL,
            tokens_in INTEGER, tokens_out INTEGER, status TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS search_calls (
            id TEXT PRIMARY KEY, point TEXT NOT NULL, query TEXT NOT NULL,
            result_count INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS metrics (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
        """)
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 2:
            with self.transaction():
                self.db.execute("""CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    kp_id TEXT NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
                    attempt_id TEXT REFERENCES attempts(id) ON DELETE CASCADE,
                    session_date TEXT NOT NULL,
                    created_at TEXT NOT NULL)""")
                self.db.execute("CREATE INDEX sessions_lookup ON sessions(kp_id, session_date, attempt_id)")
                self.db.execute("""CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant','tool')),
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    attempt_id TEXT REFERENCES attempts(id) ON DELETE CASCADE,
                    question_id TEXT REFERENCES questions(id) ON DELETE CASCADE,
                    answer_id TEXT REFERENCES answers(id) ON DELETE CASCADE,
                    note_id TEXT REFERENCES notes(id) ON DELETE CASCADE,
                    source_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL)""")
                self.db.execute("CREATE INDEX messages_session_order ON messages(session_id, created_at)")
                self.db.execute("CREATE UNIQUE INDEX messages_question_once ON messages(question_id) WHERE question_id IS NOT NULL")
                self.db.execute("CREATE UNIQUE INDEX messages_answer_once ON messages(answer_id) WHERE answer_id IS NOT NULL")
                self.db.execute("""CREATE TABLE message_materials (
                    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
                    PRIMARY KEY(message_id, material_id))""")
                if self.db.execute("PRAGMA foreign_key_check").fetchone():
                    raise sqlite3.IntegrityError("foreign key check failed during migration")
                self.db.execute("PRAGMA user_version=2")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 3:
            with self.transaction():
                self.db.execute("ALTER TABLE questions ADD COLUMN difficulty TEXT NOT NULL DEFAULT 'legacy_unset'")
                self.db.execute("ALTER TABLE questions ADD COLUMN difficulty_basis TEXT NOT NULL DEFAULT ''")
                self.db.execute("ALTER TABLE questions ADD COLUMN rubric_version TEXT NOT NULL DEFAULT 'v1'")
                self.db.execute("""CREATE TRIGGER questions_difficulty_fixed
                    BEFORE UPDATE OF difficulty,difficulty_basis,rubric_version ON questions
                    BEGIN SELECT RAISE(ABORT, 'question difficulty is fixed after creation'); END""")
                self.db.execute("ALTER TABLE dimension_scores RENAME TO dimension_scores_legacy")
                self.db.execute("""CREATE TABLE dimension_scores (
                    review_id TEXT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
                    dimension TEXT NOT NULL,
                    score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 4),
                    rationale TEXT NOT NULL,
                    rubric_version TEXT NOT NULL,
                    PRIMARY KEY(review_id, dimension))""")
                self.db.execute("""CREATE TABLE scoring_submissions (
                    id TEXT PRIMARY KEY,
                    answer_id TEXT NOT NULL REFERENCES answers(id) ON DELETE CASCADE,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL)""")
                self.db.execute("""CREATE TABLE review_question_assessments (
                    review_id TEXT NOT NULL,
                    dimension TEXT NOT NULL,
                    question_id TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                    answer_id TEXT REFERENCES answers(id) ON DELETE CASCADE,
                    submission_id TEXT REFERENCES scoring_submissions(id) ON DELETE SET NULL,
                    judgment TEXT NOT NULL,
                    PRIMARY KEY(review_id, dimension, question_id),
                    FOREIGN KEY(review_id, dimension) REFERENCES dimension_scores(review_id, dimension) ON DELETE CASCADE)""")
                self.db.execute("""CREATE TABLE review_evidence (
                    id TEXT PRIMARY KEY,
                    review_id TEXT NOT NULL,
                    dimension TEXT NOT NULL,
                    question_id TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    start_char INTEGER NOT NULL,
                    end_char INTEGER NOT NULL,
                    FOREIGN KEY(review_id, dimension, question_id)
                        REFERENCES review_question_assessments(review_id, dimension, question_id) ON DELETE CASCADE)""")
                old_scores = self.db.execute("SELECT * FROM dimension_scores_legacy").fetchall()
                for row in old_scores:
                    self.db.execute("INSERT INTO dimension_scores VALUES(?,?,?,?,?)",
                                    (row["review_id"], row["dimension"], row["score"], row["rationale"], "v1"))
                    self.db.execute("INSERT INTO review_question_assessments VALUES(?,?,?,?,?,?)",
                                    (row["review_id"], row["dimension"], row["question_id"], row["answer_id"],
                                     None, "legacy_assessed"))
                    answer = self.db.execute("SELECT text FROM answers WHERE id=?", (row["answer_id"],)).fetchone()
                    start = answer[0].find(row["evidence"]) if answer else -1
                    if start < 0:
                        raise sqlite3.IntegrityError("legacy score evidence is not in its answer")
                    self.db.execute("INSERT INTO review_evidence VALUES(?,?,?,?,?,?,?)",
                                    (new_id(), row["review_id"], row["dimension"], row["question_id"],
                                     row["evidence"], start, start + len(row["evidence"])))
                self.db.execute("DROP TABLE dimension_scores_legacy")
                if self.db.execute("PRAGMA foreign_key_check").fetchone():
                    raise sqlite3.IntegrityError("foreign key check failed during score migration")
                self.db.execute("PRAGMA user_version=3")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 4:
            with self.transaction():
                self.db.execute("""CREATE TABLE summaries (
                    id TEXT PRIMARY KEY, kp_id TEXT NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
                    source_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    source_hash TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
                    generator_version TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, UNIQUE(kind,source_message_id,source_hash))""")
                self.db.execute("""CREATE TABLE semantic_memories (
                    id TEXT PRIMARY KEY, kp_id TEXT REFERENCES knowledge_points(id) ON DELETE CASCADE,
                    scope_key TEXT NOT NULL, key TEXT NOT NULL, content TEXT NOT NULL,
                    source TEXT NOT NULL, revision INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
                self.db.execute("CREATE UNIQUE INDEX semantic_active_key ON semantic_memories(scope_key,key) WHERE active=1")
                self.db.execute("""CREATE TABLE episodic_memories (
                    id TEXT PRIMARY KEY, kp_id TEXT NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
                    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                    review_id TEXT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
                    source_hash TEXT NOT NULL, summary TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    generator_version TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(review_id,source_hash))""")
                self.db.execute("""CREATE TABLE generation_jobs (
                    id TEXT PRIMARY KEY, kp_id TEXT NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL, source_id TEXT NOT NULL, source_hash TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(kind,source_id,source_hash))""")
                self.db.execute("""CREATE TABLE context_builds (
                    id TEXT PRIMARY KEY, kp_id TEXT REFERENCES knowledge_points(id) ON DELETE CASCADE,
                    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
                    attempt_id TEXT REFERENCES attempts(id) ON DELETE CASCADE,
                    stage TEXT NOT NULL, template_version TEXT NOT NULL,
                    source_ids_json TEXT NOT NULL, excluded_json TEXT NOT NULL,
                    budget_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)""")
                self.db.execute("""CREATE TRIGGER scoring_submissions_immutable
                    BEFORE UPDATE ON scoring_submissions
                    BEGIN SELECT RAISE(ABORT, 'scoring submission is immutable'); END""")
                self.db.execute("ALTER TABLE llm_calls ADD COLUMN context_build_id TEXT")
                self.db.execute("ALTER TABLE llm_calls ADD COLUMN stage TEXT")
                self.db.execute("ALTER TABLE llm_calls ADD COLUMN session_id TEXT")
                self.db.execute("ALTER TABLE search_calls ADD COLUMN kp_id TEXT")
                self.db.execute("ALTER TABLE search_calls ADD COLUMN session_id TEXT")
                if self.db.execute("PRAGMA foreign_key_check").fetchone():
                    raise sqlite3.IntegrityError("foreign key check failed during context migration")
                self.db.execute("PRAGMA user_version=4")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 5:
            with self.transaction():
                self.db.execute("""CREATE TABLE cross_day_queries (
                    id TEXT PRIMARY KEY, kp_id TEXT NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
                    question TEXT NOT NULL, source_hash TEXT NOT NULL,
                    candidate_ids_json TEXT NOT NULL, results_json TEXT NOT NULL,
                    next_index INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
                    synthesis TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(kp_id,question,source_hash))""")
                self.db.execute("PRAGMA user_version=5")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 6:
            with self.transaction():
                self.db.execute("""CREATE TABLE review_batches (
                    id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                    dimension TEXT NOT NULL, source_hash TEXT NOT NULL,
                    question_ids_json TEXT NOT NULL, result_json TEXT NOT NULL,
                    model_version TEXT NOT NULL, rubric_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(attempt_id,dimension,source_hash))""")
                self.db.execute("PRAGMA user_version=6")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 7:
            with self.transaction():
                self.db.execute("""CREATE TABLE material_checks (
                    id TEXT PRIMARY KEY, point TEXT NOT NULL, source TEXT NOT NULL,
                    content_hash TEXT NOT NULL, start_char INTEGER NOT NULL,
                    end_char INTEGER NOT NULL, model_version TEXT NOT NULL,
                    template_version TEXT NOT NULL, result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(point,source,content_hash,start_char,end_char,model_version,template_version))""")
                self.db.execute("PRAGMA user_version=7")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 8:
            with self.transaction():
                self.db.execute("ALTER TABLE context_builds ADD COLUMN point TEXT")
                self.db.execute("""UPDATE context_builds SET point=(SELECT point FROM llm_calls
                    WHERE context_build_id=context_builds.id LIMIT 1) WHERE kp_id IS NULL""")
                self.db.execute("PRAGMA user_version=8")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 9:
            with self.transaction():
                self.db.execute("""CREATE TABLE clarification_chunks (
                    id TEXT PRIMARY KEY, kp_id TEXT NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
                    source_id TEXT NOT NULL, source_hash TEXT NOT NULL,
                    question_hash TEXT NOT NULL, start_char INTEGER NOT NULL, end_char INTEGER NOT NULL,
                    model_version TEXT NOT NULL, template_version TEXT NOT NULL,
                    result_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(kp_id,source_id,source_hash,question_hash,start_char,end_char,model_version,template_version))""")
                self.db.execute("PRAGMA user_version=9")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 10:
            with self.transaction():
                self.db.execute("""CREATE TABLE synthesis_batches (
                    id TEXT PRIMARY KEY, kp_id TEXT NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
                    question_hash TEXT NOT NULL, input_hash TEXT NOT NULL,
                    model_version TEXT NOT NULL, template_version TEXT NOT NULL,
                    covered_ids_json TEXT NOT NULL, synthesis TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(kp_id,question_hash,input_hash,model_version,template_version))""")
                self.db.execute("PRAGMA user_version=10")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 11:
            with self.transaction():
                # Fresh schemas already carry these columns; only add what an older file lacks.
                for table, column, ddl in (("dimension_scores", "supplement", "TEXT NOT NULL DEFAULT ''"),
                                           ("questions", "followup_of", "TEXT")):
                    if column not in {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                self.db.execute("PRAGMA user_version=11")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 12:
            with self.transaction():
                # Per-segment extraction progress for one over-budget original answer.
                self.db.execute("""CREATE TABLE IF NOT EXISTS answer_segments (
                    id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                    answer_id TEXT NOT NULL, answer_hash TEXT NOT NULL,
                    start_char INTEGER NOT NULL, end_char INTEGER NOT NULL,
                    model_version TEXT NOT NULL, rubric_version TEXT NOT NULL,
                    result_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(attempt_id,answer_id,answer_hash,start_char,end_char,model_version,rubric_version))""")
                self.db.execute("PRAGMA user_version=12")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 13:
            with self.transaction():
                # One audit row per tool call; search_calls stays as read-only history.
                self.db.execute("""CREATE TABLE IF NOT EXISTS tool_calls (
                    id TEXT PRIMARY KEY, tool TEXT NOT NULL, source TEXT, permission TEXT,
                    kp_id TEXT, attempt_id TEXT, session_id TEXT, point TEXT NOT NULL DEFAULT '',
                    loop_id TEXT, round INTEGER, args_summary_json TEXT, confirmed INTEGER,
                    status TEXT NOT NULL, error_kind TEXT, attempts INTEGER NOT NULL,
                    latency_ms INTEGER NOT NULL, result_count INTEGER, output_chars INTEGER NOT NULL,
                    truncated INTEGER NOT NULL, created_at TEXT NOT NULL)""")
                self.db.execute("PRAGMA user_version=13")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 14:
            with self.transaction():
                # Explicit lesson state replaces inferring it from lesson_closed messages.
                for table, column in (("sessions", "lesson_state"), ("tool_calls", "conversation_id")):
                    if column not in {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
                self.db.execute("""UPDATE sessions SET lesson_state=CASE
                    WHEN attempt_id IS NOT NULL THEN 'tested'
                    WHEN EXISTS(SELECT 1 FROM messages m WHERE m.session_id=sessions.id
                        AND m.kind='lesson_closed') THEN 'closed'
                    ELSE 'teaching' END
                    WHERE EXISTS(SELECT 1 FROM messages m WHERE m.session_id=sessions.id
                        AND m.kind IN ('lesson','lesson_resume'))""")
                self.db.execute("""CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, chat_date TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('open','closed')), end_reason TEXT,
                    mode TEXT NOT NULL, active_kind TEXT, active_id TEXT,
                    goal TEXT NOT NULL DEFAULT '', step TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '', memory_snapshot_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT)""")
                self.db.execute("""CREATE TABLE IF NOT EXISTS conversation_messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    kind TEXT NOT NULL, content TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL, UNIQUE(conversation_id, seq))""")
                self.db.execute("PRAGMA user_version=14")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 15:
            with self.transaction():
                # The chat keeps its learning-status snapshot and rolling summaries of older turns.
                if "status_snapshot_json" not in {row["name"] for row in self.db.execute("PRAGMA table_info(conversations)")}:
                    self.db.execute("ALTER TABLE conversations ADD COLUMN status_snapshot_json TEXT NOT NULL DEFAULT '{}'")
                self.db.execute("""CREATE TABLE IF NOT EXISTS chat_summaries (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    upto_seq INTEGER NOT NULL, source_hash TEXT NOT NULL, content TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(conversation_id, upto_seq, source_hash))""")
                self.db.execute("PRAGMA user_version=15")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 16:
            with self.transaction():
                # Trace: spans are written when they start, so a viewer sees what is running now.
                self.db.execute("""CREATE TABLE IF NOT EXISTS spans (
                    span_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_span_id TEXT,
                    kind TEXT NOT NULL, name TEXT NOT NULL, started_at_ns INTEGER NOT NULL,
                    duration_ms INTEGER, status TEXT NOT NULL, end_reason TEXT, error_kind TEXT,
                    progress_json TEXT NOT NULL DEFAULT '{}',
                    kp_id TEXT, attempt_id TEXT, session_id TEXT, conversation_id TEXT,
                    point TEXT NOT NULL DEFAULT '', attrs_json TEXT NOT NULL DEFAULT '{}')""")
                # Raw model output that failed validation, kept (redacted) so it can become a regression case.
                self.db.execute("""CREATE TABLE IF NOT EXISTS llm_failures (
                    id TEXT PRIMARY KEY, trace_id TEXT, parent_span_id TEXT, context_build_id TEXT,
                    stage TEXT NOT NULL, kp_id TEXT, attempt_id TEXT, conversation_id TEXT,
                    point TEXT NOT NULL DEFAULT '', error TEXT NOT NULL, raw_output TEXT NOT NULL,
                    created_at TEXT NOT NULL, at_ns INTEGER)""")
                added = {"llm_calls": ("trace_id TEXT", "parent_span_id TEXT", "started_at_ns INTEGER",
                                       "tokens_cached INTEGER", "error_kind TEXT"),
                         "tool_calls": ("trace_id TEXT", "parent_span_id TEXT", "started_at_ns INTEGER"),
                         "events": ("trace_id TEXT", "parent_span_id TEXT", "at_ns INTEGER"),
                         "context_builds": ("trace_id TEXT", "parent_span_id TEXT")}
                for table, ddls in added.items():
                    have = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
                    for ddl in ddls:
                        if ddl.split()[0] not in have:
                            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
                for index, table, column in (("spans_trace", "spans", "trace_id"),
                                             ("spans_started", "spans", "started_at_ns"),
                                             ("spans_status", "spans", "status"),
                                             ("llm_calls_trace", "llm_calls", "trace_id"),
                                             ("tool_calls_trace", "tool_calls", "trace_id"),
                                             ("events_trace", "events", "trace_id"),
                                             ("llm_failures_trace", "llm_failures", "trace_id")):
                    self.db.execute(f"CREATE INDEX IF NOT EXISTS {index} ON {table}({column})")
                self.db.execute("PRAGMA user_version=16")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 17:
            with self.transaction():
                # Prompt and reply text of a model call, kept only when ZHIQIU_TRACE_CONTENT=1 (redacted, capped).
                self.db.execute("""CREATE TABLE IF NOT EXISTS llm_contents (
                    llm_call_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, response_text TEXT NOT NULL,
                    created_at TEXT NOT NULL)""")
                self.db.execute("PRAGMA user_version=17")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 18:
            with self.transaction():
                # Keyword index over lessons and chat messages (history.py), filled lazily on first search.
                self.db.execute("""CREATE TABLE IF NOT EXISTS search_docs (
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL, ref_id TEXT NOT NULL, UNIQUE(source, ref_id))""")
                try:
                    self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(terms)")
                except sqlite3.OperationalError:
                    pass  # SQLite built without FTS5: history search stays off.
                else:
                    try:
                        # Deleted rows leave no tokens behind in the index (SQLite 3.42+).
                        self.db.execute("INSERT INTO search_fts(search_fts, rank) VALUES('secure-delete', 1)")
                    except sqlite3.OperationalError:
                        pass
                self.db.execute("PRAGMA user_version=18")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 19:
            with self.transaction():
                # Who let a gated tool call through: policy, user, or the user earlier in the turn.
                if "approval_source" not in {row["name"] for row in self.db.execute("PRAGMA table_info(tool_calls)")}:
                    self.db.execute("ALTER TABLE tool_calls ADD COLUMN approval_source TEXT")
                # Every time a tool asks the user, whichever interface answered (written from Issue 3 on).
                self.db.execute("""CREATE TABLE IF NOT EXISTS approval_requests (
                    id TEXT PRIMARY KEY, conversation_id TEXT, trace_id TEXT, parent_span_id TEXT,
                    tool TEXT NOT NULL, permission TEXT, title TEXT NOT NULL DEFAULT '',
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL CHECK(status IN ('pending','approved','denied','cancelled')),
                    decided_via TEXT, tool_call_id TEXT,
                    created_at_ns INTEGER NOT NULL, decided_at_ns INTEGER, created_at TEXT NOT NULL)""")
                for index, column in (("approval_requests_status", "status"),
                                      ("approval_requests_conversation", "conversation_id"),
                                      ("approval_requests_created", "created_at_ns")):
                    self.db.execute(f"CREATE INDEX IF NOT EXISTS {index} ON approval_requests({column})")
                self.db.execute("PRAGMA user_version=19")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 20:
            with self.transaction():
                # A readable conversation title (rule / model / fallback / user) and a one-line
                # learner-facing feedback per graded dimension; rationale stays for the audit.
                for table, column in (("conversations", "title"), ("conversations", "title_source"),
                                      ("dimension_scores", "feedback")):
                    if column not in {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
                self.db.execute("PRAGMA user_version=20")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 21:
            with self.transaction():
                # Read and write roots added on the workbench page, merged with ZHIQIU_READ_ROOTS /
                # ZHIQIU_WRITE_ROOTS at the start of every turn. Only the page writes here.
                self.db.execute("""CREATE TABLE IF NOT EXISTS file_roots (
                    kind TEXT NOT NULL CHECK(kind IN ('read','write')), path TEXT NOT NULL,
                    files INTEGER NOT NULL, truncated INTEGER NOT NULL, added_at TEXT NOT NULL,
                    PRIMARY KEY(kind, path))""")
                self.db.execute("PRAGMA user_version=21")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 22:
            with self.transaction():
                # The teaching Skill of a knowledge point, the Skill each context build used, and how an
                # answer was entered.
                for table, column, kind in (
                        ("knowledge_points", "skill", "TEXT"), ("knowledge_points", "skill_source", "TEXT"),
                        ("knowledge_points", "skill_at", "TEXT"),
                        ("context_builds", "skill_name", "TEXT"), ("context_builds", "skill_version", "INTEGER"),
                        ("context_builds", "skill_sha", "TEXT"), ("context_builds", "skill_source", "TEXT"),
                        ("answers", "input_source",
                         "TEXT NOT NULL DEFAULT 'text' CHECK(input_source IN ('text','voice'))")):
                    if column not in {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
                self.db.execute("PRAGMA user_version=22")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 23:
            with self.transaction():
                # Streamed model calls: when the first piece of the reply arrived (time to first token),
                # and whether the call was streamed at all. Calls from before stay NULL and 0.
                for column, kind in (("ttft_ms", "INTEGER"), ("streamed", "INTEGER NOT NULL DEFAULT 0")):
                    if column not in {row["name"] for row in self.db.execute("PRAGMA table_info(llm_calls)")}:
                        self.db.execute(f"ALTER TABLE llm_calls ADD COLUMN {column} {kind}")
                self.db.execute("PRAGMA user_version=23")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 24:
            with self.transaction():
                # Request fields an endpoint refused (stream, tools, thinking...): sent no more from then on.
                self.db.execute("""CREATE TABLE IF NOT EXISTS endpoint_capabilities (
                    endpoint TEXT NOT NULL, model TEXT NOT NULL, feature TEXT NOT NULL,
                    supported INTEGER NOT NULL, evidence TEXT NOT NULL DEFAULT '', detected_at TEXT NOT NULL,
                    PRIMARY KEY(endpoint, model, feature))""")
                # A path asked for in a conversation: allowed once, added as a root, or refused.
                for column in ("grant", "root_path"):
                    if column not in {row["name"] for row in self.db.execute("PRAGMA table_info(approval_requests)")}:
                        self.db.execute(f'ALTER TABLE approval_requests ADD COLUMN "{column}" TEXT')
                self.db.execute("PRAGMA user_version=24")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException as exc:
            self.db.rollback()
            if isinstance(exc, flow.IllegalTransition):
                # The refused change was rolled back; the refusal itself is kept.
                guardrails.record(self, "illegal_transition", "blocked", str(exc), entity=exc.entity)
            raise
        else:
            self.db.commit()

    def _ts(self) -> str:
        return self.clock.now().isoformat()

    def _ensure_session(self, kp_id: str, attempt_id: str | None = None) -> str:
        today = self.clock.today().isoformat()
        if attempt_id:
            owner = self.db.execute("SELECT kp_id FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not owner or owner["kp_id"] != kp_id:
                raise ValueError("attempt does not belong to knowledge point")
            row = self.db.execute("""SELECT id FROM sessions WHERE kp_id=? AND session_date=?
                AND attempt_id=? ORDER BY rowid DESC LIMIT 1""", (kp_id, today, attempt_id)).fetchone()
            if row:
                return row["id"]
            row = self.db.execute("""SELECT id,lesson_state FROM sessions WHERE kp_id=? AND session_date=?
                AND attempt_id IS NULL AND lesson_state IS NOT 'closed' ORDER BY rowid DESC LIMIT 1""",
                (kp_id, today)).fetchone()
            if row:
                self.db.execute("UPDATE sessions SET attempt_id=? WHERE id=?", (attempt_id, row["id"]))
                if row["lesson_state"] == "teaching":
                    flow.transition(self, "lesson", row["id"], "tested", "attempt_started",
                                    kp_id=kp_id, attempt_id=attempt_id)
                return row["id"]
        else:
            # A closed lesson stays closed; later messages of the day open a new session.
            row = self.db.execute("""SELECT id FROM sessions WHERE kp_id=? AND session_date=?
                AND attempt_id IS NULL AND lesson_state IS NOT 'closed' ORDER BY rowid DESC LIMIT 1""",
                (kp_id, today)).fetchone()
            if row:
                return row["id"]
        session_id = new_id()
        self.db.execute("INSERT INTO sessions(id,kp_id,attempt_id,session_date,created_at) VALUES(?,?,?,?,?)",
                        (session_id, kp_id, attempt_id, today, self._ts()))
        return session_id

    def _append_message(self, kp_id: str, role: str, kind: str, content: str,
                        attempt_id: str | None = None, question_id: str | None = None,
                        answer_id: str | None = None, note_id: str | None = None,
                        source_message_id: str | None = None) -> str:
        if source_message_id and not self.db.execute("""SELECT 1 FROM messages m
                JOIN sessions s ON s.id=m.session_id WHERE m.id=? AND s.kp_id=?""",
                (source_message_id, kp_id)).fetchone():
            raise ValueError("source message does not belong to knowledge point")
        session_id = self._ensure_session(kp_id, attempt_id)
        message_id = new_id()
        self.db.execute("""INSERT INTO messages
            (id,session_id,role,kind,content,attempt_id,question_id,answer_id,note_id,source_message_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (message_id, session_id, role, kind, content, attempt_id, question_id,
             answer_id, note_id, source_message_id, self._ts()))
        if role == "assistant" and kind in ("lesson", "lesson_resume", "clarification"):
            self.db.execute("""INSERT INTO message_materials(message_id,material_id)
                SELECT ?,id FROM materials WHERE kp_id=? AND active=1""", (message_id, kp_id))
        if kind in ("lesson", "lesson_resume") and attempt_id is None:
            flow.transition(self, "lesson", session_id, "teaching", kind, kp_id=kp_id)
        return message_id

    def record_message(self, kp_id: str, role: str, kind: str, content: str,
                       attempt_id: str | None = None, source_message_id: str | None = None) -> str:
        if role not in ("user", "assistant", "tool") or not kind or not content.strip():
            raise ValueError("invalid message")
        with self.transaction():
            return self._append_message(kp_id, role, kind, content.strip(), attempt_id,
                                        source_message_id=source_message_id)

    def messages_for(self, kp_id: str, session_id: str | None = None) -> list[sqlite3.Row]:
        if session_id:
            return self.db.execute("""SELECT m.* FROM messages m JOIN sessions s ON s.id=m.session_id
                WHERE s.kp_id=? AND s.id=? ORDER BY m.rowid""", (kp_id, session_id)).fetchall()
        return self.db.execute("""SELECT m.* FROM messages m JOIN sessions s ON s.id=m.session_id
            WHERE s.kp_id=? ORDER BY s.rowid,m.rowid""", (kp_id,)).fetchall()

    def correct_message(self, kp_id: str, source_message_id: str, correction: str) -> str:
        if not correction.strip():
            raise ValueError("correction cannot be empty")
        with self.transaction():
            source = self.db.execute("""SELECT m.id,m.kind FROM messages m JOIN sessions s ON s.id=m.session_id
                WHERE m.id=? AND s.kp_id=?""", (source_message_id, kp_id)).fetchone()
            if not source or source["kind"] not in ("lesson", "lesson_resume", "clarification"):
                raise ValueError("source is not a lesson or clarification")
            message_id = self._append_message(kp_id, "user", "correction", correction.strip(),
                                              source_message_id=source_message_id)
            self.db.execute("UPDATE summaries SET active=0 WHERE source_message_id=?", (source_message_id,))
            self.db.execute("UPDATE generation_jobs SET status='stale',updated_at=? WHERE kind='lesson_summary' AND source_id=? AND status!='stale'",
                            (self._ts(), source_message_id))
            return message_id

    def _episode_source_hash(self, review_id: str) -> str:
        review = self.db.execute("SELECT total,kind,revised_at FROM reviews WHERE id=?", (review_id,)).fetchone()
        scores = [tuple(row) for row in self.db.execute("""SELECT dimension,score,rationale FROM dimension_scores
            WHERE review_id=? ORDER BY dimension""", (review_id,))]
        payload = {"review": tuple(review), "scores": scores}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def _queue_episode_job(self, kp_id: str, review_id: str) -> None:
        source_hash = self._episode_source_hash(review_id)
        self.db.execute("""INSERT OR IGNORE INTO generation_jobs
            (id,kp_id,kind,source_id,source_hash,status,created_at,updated_at)
            VALUES(?,?, 'episode', ?, ?, 'pending', ?, ?)""",
            (new_id(), kp_id, review_id, source_hash, self._ts(), self._ts()))

    def pending_lessons(self) -> list[sqlite3.Row]:
        return self.db.execute("""SELECT s.id session_id,s.kp_id,s.session_date,k.title,
            m.id lesson_message_id,m.content lesson
            FROM sessions s JOIN knowledge_points k ON k.id=s.kp_id
            JOIN messages m ON m.id=(SELECT id FROM messages WHERE session_id=s.id
                AND kind IN ('lesson','lesson_resume') ORDER BY rowid DESC LIMIT 1)
            WHERE s.attempt_id IS NULL AND s.lesson_state='teaching'
            ORDER BY s.rowid DESC""").fetchall()

    def lesson_origin(self, message_id: str) -> str | None:
        """The original lesson message: a lesson continued on a later day is a lesson_resume copy that
        points at it."""
        row = self.db.execute("SELECT id,kind,source_message_id FROM messages WHERE id=?", (message_id,)).fetchone()
        while row and row["kind"] == "lesson_resume" and row["source_message_id"]:
            row = self.db.execute("SELECT id,kind,source_message_id FROM messages WHERE id=?",
                                  (row["source_message_id"],)).fetchone()
        return row["id"] if row else None

    def lesson_sections(self, message_id: str) -> tuple[str, list[dict[str, str]]] | None:
        """The original lesson message and its sections; None for a lesson taught before sections existed."""
        origin = self.lesson_origin(message_id)
        if not origin:
            return None
        event = self.db.execute("""SELECT payload_json FROM events WHERE type='LessonSections'
            AND json_extract(payload_json,'$.message_id')=? ORDER BY id DESC LIMIT 1""", (origin,)).fetchone()
        return (origin, json.loads(event["payload_json"])["sections"]) if event else None

    def lesson_markers(self, lesson_id: str) -> list[str]:
        """The section markers left while showing a lesson, across the days it was continued."""
        return [row[0] for row in self.db.execute("""SELECT content FROM messages
            WHERE kind='lesson_section' AND source_message_id=? ORDER BY rowid""", (lesson_id,))]

    def lesson_candidates(self, kp_id: str) -> list[sqlite3.Row]:
        return self.db.execute("""SELECT m.id message_id,m.content,s.id session_id,s.session_date
            FROM messages m JOIN sessions s ON s.id=m.session_id
            WHERE s.kp_id=? AND m.kind='lesson' ORDER BY s.session_date DESC,m.rowid DESC""",
            (kp_id,)).fetchall()

    def record_lesson_retrieval(self, kp_id: str, question: str, result: dict[str, Any]) -> None:
        """Keep why an old lesson was chosen or why the user was asked to choose."""
        with self.transaction():
            self._event(kp_id, None, "LessonRetrieval", {
                "question": question, "decision": result["decision"], "selected": result["selected"],
                "params": result["params"],
                "ranked": [{key: item[key] for key in ("message_id", "score", "signals", "reason")}
                           for item in result["ranked"]]})

    def start_cross_day_query(self, kp_id: str, question: str, candidate_ids: list[str]) -> dict[str, Any]:
        if not question.strip() or not candidate_ids or len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("question and unique lesson candidates are required")
        with self.transaction():
            candidates = {row["message_id"]: row for row in self.lesson_candidates(kp_id)}
            if any(message_id not in candidates for message_id in candidate_ids):
                raise ValueError("candidate does not belong to this knowledge point")
            snapshot = []
            for message_id in candidate_ids:
                corrections = [tuple(row) for row in self.db.execute("""SELECT id,content FROM messages
                    WHERE source_message_id=? AND kind='correction' ORDER BY rowid""", (message_id,))]
                snapshot.append((message_id, candidates[message_id]["content"], corrections))
            source_hash = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False).encode()).hexdigest()
            existing = self.db.execute("""SELECT * FROM cross_day_queries WHERE kp_id=? AND question=?
                AND source_hash=?""", (kp_id, question.strip(), source_hash)).fetchone()
            if existing:
                return dict(existing)
            query_id = new_id()
            self.db.execute("""INSERT INTO cross_day_queries
                (id,kp_id,question,source_hash,candidate_ids_json,results_json,next_index,status,
                 created_at,updated_at) VALUES(?,?,?,?,?,?,0,'pending',?,?)""",
                (query_id, kp_id, question.strip(), source_hash,
                 json.dumps(candidate_ids), "[]", self._ts(), self._ts()))
            return dict(self.db.execute("SELECT * FROM cross_day_queries WHERE id=?", (query_id,)).fetchone())

    def cross_day_query(self, query_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM cross_day_queries WHERE id=?", (query_id,)).fetchone()
        if not row:
            raise ValueError("unknown cross-day query")
        return dict(row)

    def record_cross_day_result(self, query_id: str, message_id: str, explanation: str) -> dict[str, Any]:
        if not explanation.strip():
            raise ValueError("explanation is required")
        with self.transaction():
            row = self.cross_day_query(query_id)
            candidates = json.loads(row["candidate_ids_json"])
            index = row["next_index"]
            if index >= len(candidates) or candidates[index] != message_id:
                raise ValueError("result is not the next candidate")
            results = json.loads(row["results_json"])
            results.append({"message_id": message_id, "explanation": explanation.strip()})
            next_index = index + 1
            status = "synthesis_pending" if next_index == len(candidates) else "pending"
            self.db.execute("""UPDATE cross_day_queries SET results_json=?,next_index=?,status=?,error='',
                updated_at=? WHERE id=?""", (json.dumps(results, ensure_ascii=False), next_index,
                                             status, self._ts(), query_id))
            return self.cross_day_query(query_id)

    def fail_cross_day_query(self, query_id: str, error: str) -> None:
        with self.transaction():
            self.db.execute("UPDATE cross_day_queries SET status='error',error=?,updated_at=? WHERE id=?",
                            (error, self._ts(), query_id))

    def complete_cross_day_query(self, query_id: str, synthesis: str) -> dict[str, Any]:
        if not synthesis.strip():
            raise ValueError("synthesis is required")
        with self.transaction():
            row = self.cross_day_query(query_id)
            if row["next_index"] != len(json.loads(row["candidate_ids_json"])):
                raise ValueError("not all candidates have been processed")
            self.db.execute("""UPDATE cross_day_queries SET status='complete',synthesis=?,error='',updated_at=?
                WHERE id=?""", (synthesis.strip(), self._ts(), query_id))
            return self.cross_day_query(query_id)

    def resume_lesson(self, session_id: str) -> dict[str, str]:
        with self.transaction():
            row = next((item for item in self.pending_lessons() if item["session_id"] == session_id), None)
            if not row:
                raise ValueError("no pending lesson with this session id")
            source_ids = {item[0] for item in self.db.execute(
                "SELECT material_id FROM message_materials WHERE message_id=?", (row["lesson_message_id"],))}
            active_ids = {item[0] for item in self.db.execute(
                "SELECT id FROM materials WHERE kp_id=? AND active=1", (row["kp_id"],))}
            if source_ids != active_ids:
                raise ValueError("lesson materials changed; start learn again with current sources")
            if row["session_date"] == self.clock.today().isoformat():
                return dict(row)
            self.db.execute("""INSERT INTO messages
                (id,session_id,role,kind,content,created_at) VALUES(?,?,?,?,?,?)""",
                (new_id(), session_id, "tool", "lesson_closed", "continued in a new session", self._ts()))
            flow.transition(self, "lesson", session_id, "closed", "continued", kp_id=row["kp_id"])
            self._append_message(row["kp_id"], "assistant", "lesson_resume", row["lesson"],
                                 source_message_id=row["lesson_message_id"])
            return dict(self.pending_lessons()[0])

    def close_lesson(self, session_id: str) -> None:
        with self.transaction():
            row = next((item for item in self.pending_lessons() if item["session_id"] == session_id), None)
            if not row:
                raise ValueError("lesson is not pending")
            self.db.execute("""INSERT INTO messages
                (id,session_id,role,kind,content,created_at) VALUES(?,?,?,?,?,?)""",
                (new_id(), session_id, "tool", "lesson_closed", "ended without self-test", self._ts()))
            flow.transition(self, "lesson", session_id, "closed", "ended", kp_id=row["kp_id"])

    def _event(self, kp_id: str | None, attempt_id: str | None, kind: str, payload: dict[str, Any]) -> None:
        trace_id, parent = tracing.columns(self)
        self.db.execute("""INSERT INTO events(kp_id,attempt_id,type,payload_json,created_at,trace_id,parent_span_id,at_ns)
            VALUES(?,?,?,?,?,?,?,?)""",
                        (kp_id, attempt_id, kind, json.dumps(payload, ensure_ascii=False), self._ts(),
                         trace_id, parent, tracing.now_ns()))

    def record_event(self, kp_id: str | None, attempt_id: str | None, kind: str, payload: dict[str, Any]) -> None:
        """An event outside any business transaction, e.g. a guardrail that stopped a tool call."""
        if self.db.in_transaction:
            self._event(kp_id, attempt_id, kind, payload)
            return
        with self.transaction():
            self._event(kp_id, attempt_id, kind, payload)

    def record_llm_failure(self, stage: str, error: str, raw_output: str, *, context_build_id: str | None = None,
                           kp_id: str | None = None, attempt_id: str | None = None,
                           conversation_id: str | None = None, point: str = "") -> str:
        """Keep a model output that failed validation (redacted, capped) so it can become a regression case."""
        failure_id = new_id()
        trace_id, parent = tracing.columns(self)
        with self.transaction():
            self.db.execute("""INSERT INTO llm_failures(id,trace_id,parent_span_id,context_build_id,stage,kp_id,
                attempt_id,conversation_id,point,error,raw_output,created_at,at_ns) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (failure_id, trace_id, parent, context_build_id, stage, kp_id, attempt_id, conversation_id,
                 point or "", redact(error)[:2000], redact(raw_output)[:LLM_FAILURE_CHARS], self._ts(),
                 tracing.now_ns()))
        return failure_id

    def record_llm_call(self, details: dict[str, Any]) -> None:
        trace_id, parent = tracing.columns(self)
        call_id = new_id()
        with self.transaction():
            if details.get("request_messages") is not None and os.getenv("ZHIQIU_TRACE_CONTENT") == "1":
                request = json.dumps(redact_obj(details["request_messages"]), ensure_ascii=False, default=str)
                self.db.execute("""INSERT INTO llm_contents(llm_call_id,request_json,response_text,created_at)
                    VALUES(?,?,?,?)""", (call_id, request[:TRACE_REQUEST_CHARS],
                                         redact(str(details.get("response_text") or ""))[:TRACE_RESPONSE_CHARS],
                                         self._ts()))
            self.db.execute("""INSERT INTO llm_calls
                (id,kp_id,attempt_id,point,model,prompt_hash,latency_ms,tokens_in,tokens_out,status,
                 created_at,context_build_id,stage,session_id,trace_id,parent_span_id,started_at_ns,
                 tokens_cached,error_kind,ttft_ms,streamed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (call_id, details.get("kp_id"), details.get("attempt_id"), details["point"], details["model"],
                             details["prompt_hash"], details["latency_ms"], details.get("tokens_in"),
                             details.get("tokens_out"), details["status"], self._ts(), details.get("context_build_id"),
                             details.get("stage"), details.get("session_id"), trace_id, parent,
                             details.get("started_at_ns"), details.get("tokens_cached"), details.get("error_kind"),
                             details.get("ttft_ms"), 1 if details.get("streamed") else 0))
            build_id = details.get("context_build_id")
            tokens_in = details.get("tokens_in")
            tokens_out = details.get("tokens_out")
            if build_id and any(type(value) is int and value >= 0 for value in (tokens_in, tokens_out)):
                row = self.db.execute("SELECT budget_json FROM context_builds WHERE id=?", (build_id,)).fetchone()
                if row:
                    budget = json.loads(row["budget_json"])
                    if type(tokens_in) is int and tokens_in >= 0:
                        budget["verified_input_tokens"] = tokens_in
                    if type(tokens_out) is int and tokens_out >= 0:
                        budget["verified_output_tokens"] = tokens_out
                    budget["verified_token_count_source"] = "provider_usage"
                    self.db.execute("UPDATE context_builds SET budget_json=? WHERE id=?",
                                    (json.dumps(budget), build_id))

    def record_tool_call(self, details: dict[str, Any]) -> None:
        confirmed = details.get("confirmed")
        trace_id, parent = tracing.columns(self)
        with self.transaction():
            self.db.execute("""INSERT INTO tool_calls
                (id,tool,source,permission,kp_id,attempt_id,session_id,point,loop_id,round,args_summary_json,
                 confirmed,status,error_kind,attempts,latency_ms,result_count,output_chars,truncated,created_at,
                 conversation_id,trace_id,parent_span_id,started_at_ns,approval_source)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (details["id"], details["tool"], details.get("source"), details.get("permission"),
                 details.get("kp_id"), details.get("attempt_id"), details.get("session_id"),
                 details.get("point") or "", details.get("loop_id"), details.get("round"),
                 json.dumps(redact_obj(details.get("args_summary")), ensure_ascii=False),
                 None if confirmed is None else int(bool(confirmed)), details["status"],
                 details.get("error_kind"), details["attempts"], details["latency_ms"],
                 details.get("result_count"), details["output_chars"], int(bool(details.get("truncated"))),
                 self._ts(), details.get("conversation_id"), trace_id, parent, details.get("started_at_ns"),
                 details.get("approval_source")))
            if details.get("guard"):
                self._event(details.get("kp_id"), details.get("attempt_id"), "GuardrailTriggered", details["guard"])

    # ----- approval requests: one row each time a tool asks the user, whatever interface answers -----

    # ----- endpoint capabilities -----------------------------------------------------------------

    def refused_features(self, endpoint: str, model: str) -> set[str]:
        return {row["feature"] for row in self.db.execute(
            "SELECT feature FROM endpoint_capabilities WHERE endpoint=? AND model=? AND supported=0", (endpoint, model))}

    def record_refused_feature(self, endpoint: str, model: str, feature: str, evidence: str) -> None:
        with self.transaction():
            self.db.execute("""INSERT INTO endpoint_capabilities(endpoint,model,feature,supported,evidence,detected_at)
                VALUES(?,?,?,0,?,?) ON CONFLICT(endpoint,model,feature) DO UPDATE SET supported=0,
                evidence=excluded.evidence, detected_at=excluded.detected_at""",
                (endpoint, model, feature, redact(evidence)[:500], self._ts()))

    def capabilities(self, endpoint: str | None = None, model: str | None = None) -> list[dict[str, Any]]:
        where, args = "", ()
        if endpoint is not None and model is not None:
            where, args = " WHERE endpoint=? AND model=?", (endpoint, model)
        return [dict(row) for row in self.db.execute(
            f"SELECT * FROM endpoint_capabilities{where} ORDER BY endpoint, model, feature", args)]

    def clear_capabilities(self, endpoint: str, model: str) -> int:
        with self.transaction():
            return self.db.execute("DELETE FROM endpoint_capabilities WHERE endpoint=? AND model=?",
                                   (endpoint, model)).rowcount

    def open_approval(self, tool: str, permission: str, title: str, summary: Any,
                      conversation_id: str | None) -> str:
        request_id = new_id()
        trace_id, parent = tracing.columns(self)
        with self.transaction():
            self.db.execute("""INSERT INTO approval_requests(id,conversation_id,trace_id,parent_span_id,tool,permission,
                title,summary_json,status,created_at_ns,created_at) VALUES(?,?,?,?,?,?,?,?,'pending',?,?)""",
                (request_id, conversation_id, trace_id, parent, tool, permission,
                 redact(title)[:APPROVAL_TITLE_CHARS], json.dumps(redact_obj(summary), ensure_ascii=False),
                 tracing.now_ns(), self._ts()))
        return request_id

    def decide_approval(self, request_id: str, approved: bool, via: str, *, grant: str | None = None,
                        root_path: str | None = None) -> None:
        """grant: for a path asked for in a conversation, once / root (added as a root) / denied."""
        with self.transaction():
            self.db.execute("""UPDATE approval_requests SET status=?,decided_via=?,decided_at_ns=?,"grant"=?,root_path=?
                WHERE id=? AND status='pending'""", ("approved" if approved else "denied", via, tracing.now_ns(),
                                                     grant, root_path, request_id))

    def cancel_approval(self, request_id: str) -> None:
        """The prompt went away unanswered (interrupted, or the interface closed)."""
        with self.transaction():
            self.db.execute("""UPDATE approval_requests SET status='cancelled',decided_at_ns=?
                WHERE id=? AND status='pending'""", (tracing.now_ns(), request_id))

    def link_approval(self, request_id: str, tool_call_id: str) -> None:
        with self.transaction():
            self.db.execute("UPDATE approval_requests SET tool_call_id=? WHERE id=?", (tool_call_id, request_id))

    def cancel_stale_approvals(self, conversation_id: str | None = None,
                               older_than_ns: int = APPROVAL_STALE_NS) -> int:
        """Pending requests nobody can answer any more: those of this conversation (its process
        restarted) and those older than the longest wait anywhere."""
        cutoff = tracing.now_ns() - older_than_ns
        with self.transaction():
            return self.db.execute("""UPDATE approval_requests SET status='cancelled',decided_at_ns=?
                WHERE status='pending' AND (conversation_id IS ? OR created_at_ns<?)""",
                (tracing.now_ns(), conversation_id, cutoff)).rowcount

    # ----- file roots: added and removed only through the workbench page, never by the agent -----

    def roots(self, kind: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM file_roots WHERE kind=? ORDER BY added_at,rowid", (kind,)).fetchall()

    def add_root(self, kind: str, path: str, files: int, truncated: bool, via: str = "web") -> None:
        if kind not in ROOT_KINDS:
            raise ValueError(f"unknown root kind: {kind}")
        with self.transaction():
            if self.db.execute("SELECT 1 FROM file_roots WHERE kind=? AND path=?", (kind, path)).fetchone():
                raise ValueError(f"已在白名单中：{path}")
            self.db.execute("INSERT INTO file_roots VALUES(?,?,?,?,?)", (kind, path, files, int(truncated), self._ts()))
            # Only the path and a file count: never the names or contents of what it holds.
            self._event(None, None, "RootsChanged", {"action": "add", "kind": kind, "path": path, "files": files,
                                                     "truncated": bool(truncated), "via": via})

    def remove_root(self, kind: str, path: str) -> None:
        with self.transaction():
            row = self.db.execute("SELECT * FROM file_roots WHERE kind=? AND path=?", (kind, path)).fetchone()
            if not row:
                raise LookupError(f"白名单里没有这个目录：{path}")
            self.db.execute("DELETE FROM file_roots WHERE kind=? AND path=?", (kind, path))
            self._event(None, None, "RootsChanged", {"action": "remove", "kind": kind, "path": path,
                                                     "files": row["files"], "truncated": bool(row["truncated"]),
                                                     "via": "web"})

    def set_skill(self, kp_id: str, name: str | None, source: str | None, reason: str | None = None) -> bool:
        """Record which Skill teaches this knowledge point; None clears it (the model chooses again).

        Returns whether anything changed; only a change leaves a SkillChanged event."""
        if name is not None and source not in SKILL_SOURCES:
            raise ValueError(f"unknown skill source: {source}")
        with self.transaction():
            row = self.db.execute("SELECT skill,skill_source FROM knowledge_points WHERE id=?", (kp_id,)).fetchone()
            if not row:
                raise LookupError("unknown knowledge point")
            source = source if name is not None else None
            if (row["skill"], row["skill_source"]) == (name, source):
                return False
            self.db.execute("UPDATE knowledge_points SET skill=?,skill_source=?,skill_at=? WHERE id=?",
                            (name, source, self._ts() if name is not None else None, kp_id))
            payload: dict[str, Any] = {"from": row["skill"], "to": name, "source": source or "user"}
            if reason:
                payload["reason"] = reason
            self._event(kp_id, None, "SkillChanged", payload)
            return True

    def get_knowledge_point(self, title_or_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM knowledge_points WHERE id=? OR title=?", (title_or_id, title_or_id)).fetchone()

    def ensure_knowledge_point(self, title: str, parent_id: str | None = None) -> str:
        title = title.strip()
        if not title:
            raise ValueError("knowledge point title is required")
        with self.transaction():
            row = self.get_knowledge_point(title)
            if row:
                return row["id"]
            kp_id = new_id()
            self.db.execute("INSERT INTO knowledge_points(id,title,parent_id,created_at) VALUES(?,?,?,?)", (kp_id, title, parent_id, self._ts()))
            self._event(kp_id, None, "KnowledgePointCreated", {"title": title, "parent_id": parent_id})
            flow.enter(self, "knowledge_point", kp_id, "active", "created", kp_id=kp_id)
            return kp_id

    def add_materials(self, kp_id: str, materials: list[dict[str, str]]) -> None:
        with self.transaction():
            self.db.execute("UPDATE materials SET active=0 WHERE kp_id=?", (kp_id,))
            for item in materials:
                self.db.execute("""INSERT INTO materials
                    (id,kp_id,kind,source,content_hash,content,reason,active,created_at) VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(kp_id,source,content_hash) DO UPDATE SET active=1,reason=excluded.reason""",
                    (new_id(), kp_id, item["kind"], item["source"], item["content_hash"],
                     item["content"], item.get("reason", ""), 1, self._ts()))
            self._event(kp_id, None, "MaterialsRecorded", {"sources": [m["source"] for m in materials]})

    def materials_for(self, kp_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM materials WHERE kp_id=? AND active=1 ORDER BY created_at,id", (kp_id,)).fetchall()

    def record_consent(self, kp_id: str, material_hash: str, endpoint: str) -> None:
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO consents VALUES(?,?,?,?,?)",
                            (new_id(), kp_id, material_hash, endpoint, self._ts()))
            self._event(kp_id, None, "MaterialTransferApproved", {"material_hash": material_hash, "endpoint": endpoint})

    def has_consent(self, kp_id: str, material_hash: str, endpoint: str) -> bool:
        return self.db.execute("SELECT 1 FROM consents WHERE kp_id=? AND material_hash=? AND endpoint=?",
                               (kp_id, material_hash, endpoint)).fetchone() is not None

    def active_attempt(self, kp_id: str | None = None) -> sqlite3.Row | None:
        if kp_id:
            return self.db.execute("""SELECT * FROM attempts WHERE kp_id=? AND state IN
                ('in_progress','paused','awaiting_review','needs_self_test') ORDER BY started_at DESC LIMIT 1""", (kp_id,)).fetchone()
        return self.db.execute("""SELECT * FROM attempts WHERE state IN
            ('in_progress','paused','awaiting_review','needs_self_test') ORDER BY started_at DESC LIMIT 1""").fetchone()

    def start_attempt(self, kp_id: str, kind: str, task_ids: list[str] | None = None) -> str:
        if kind not in ("first", "review"):
            raise ValueError("invalid attempt kind")
        with self.transaction():
            kp = self.db.execute("SELECT * FROM knowledge_points WHERE id=?", (kp_id,)).fetchone()
            if not kp:
                raise ValueError("unknown knowledge point")
            existing = self.active_attempt(kp_id)
            if existing:
                if existing["kind"] == kind:
                    return existing["id"]
                raise ValueError("another attempt is still open")
            task_ids = task_ids or []
            if kind == "first" and kp["state"] == "active" and self.db.execute(
                "SELECT 1 FROM review_tasks WHERE kp_id=? AND state IN ('pending','started')", (kp_id,)).fetchone():
                raise ValueError("this knowledge point already has a review task; use review or resume")
            if kind == "review":
                if kp["state"] != "active":
                    raise ValueError("knowledge point requires relearning before review")
                count = self.db.execute("SELECT COUNT(*) FROM attempts WHERE kind='review' AND start_date=?",
                                        (self.clock.today().isoformat(),)).fetchone()[0]
                if count >= DAILY_REVIEW_LIMIT:
                    raise ValueError(f"daily review limit of {DAILY_REVIEW_LIMIT} reached")
                if not task_ids:
                    raise ValueError("review requires a due task")
                for task_id in task_ids:
                    task = self.db.execute("SELECT * FROM review_tasks WHERE id=?", (task_id,)).fetchone()
                    if not task or task["kp_id"] != kp_id or task["state"] not in ("pending", "started") or task["next_remind_date"] > self.clock.today().isoformat():
                        raise ValueError("task is not due for this knowledge point")
            attempt_id = new_id()
            self.db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)",
                            (attempt_id, kp_id, kind, "in_progress", self.clock.today().isoformat(), self._ts(), None, None))
            flow.enter(self, "attempt", attempt_id, "in_progress", f"start_{kind}", kp_id=kp_id, attempt_id=attempt_id)
            self._ensure_session(kp_id, attempt_id)
            for task_id in task_ids:
                self.db.execute("INSERT INTO attempt_tasks VALUES(?,?)", (attempt_id, task_id))
                self.db.execute("UPDATE review_tasks SET started_at=COALESCE(started_at,?) WHERE id=?",
                                (self._ts(), task_id))
                flow.transition(self, "review_task", task_id, "started", "review_started",
                                kp_id=kp_id, attempt_id=attempt_id)
            self._event(kp_id, attempt_id, "AttemptStarted", {"kind": kind, "tasks": task_ids})
            return attempt_id

    def resume(self, attempt_id: str) -> None:
        with self.transaction():
            row = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not row or row["state"] not in ("paused", "in_progress", "needs_self_test"):
                raise ValueError("attempt cannot be resumed")
            if row["state"] == "needs_self_test":
                # A zero-answer first self-test is not a final review. Clear receipts
                # from older versions so the same attempt can later be assessed.
                self.db.execute("DELETE FROM receipts WHERE operation_key=?", (f"finalize:{attempt_id}",))
            flow.transition(self, "attempt", attempt_id, "in_progress", "resume",
                            kp_id=row["kp_id"], attempt_id=attempt_id)
            self.db.execute("UPDATE attempts SET ended_at=NULL,finish_reason=NULL WHERE id=?", (attempt_id,))
            self._ensure_session(row["kp_id"], attempt_id)
            self._event(row["kp_id"], attempt_id, "AttemptResumed", {})

    def pause(self, attempt_id: str) -> None:
        with self.transaction():
            row = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not row or row["state"] != "in_progress":
                raise ValueError("attempt is not in progress")
            flow.transition(self, "attempt", attempt_id, "paused", "pause", kp_id=row["kp_id"], attempt_id=attempt_id)
            self._event(row["kp_id"], attempt_id, "AttemptPaused", {})

    def add_question(self, attempt_id: str, dimension: str, question: str,
                     difficulty: str = "basic", difficulty_basis: str = "V1-compatible default",
                     followup_of: str | None = None) -> tuple[str, int]:
        if (dimension not in ("concept", "mechanism", "application") or not question.strip()
                or difficulty not in ("basic", "advanced", "transfer") or not difficulty_basis.strip()):
            raise ValueError("invalid question")
        with self.transaction():
            attempt = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not attempt or attempt["state"] != "in_progress":
                raise ValueError("attempt is not in progress")
            seq = self.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM questions WHERE attempt_id=?", (attempt_id,)).fetchone()[0]
            if seq > 8:
                raise ValueError("question limit reached")
            if followup_of is not None:
                # One clarifying follow-up per answered original question, in the same dimension.
                target = self.db.execute("""SELECT q.dimension,q.followup_of,a.id answer_id FROM questions q
                    LEFT JOIN answers a ON a.question_id=q.id WHERE q.id=? AND q.attempt_id=?""",
                    (followup_of, attempt_id)).fetchone()
                if (not target or target["answer_id"] is None or target["followup_of"] is not None
                        or target["dimension"] != dimension
                        or self.db.execute("SELECT 1 FROM questions WHERE followup_of=?", (followup_of,)).fetchone()):
                    raise ValueError("invalid follow-up question")
            question_id = new_id()
            self.db.execute("""INSERT INTO questions
                (id,attempt_id,seq,dimension,text,asked_at,difficulty,difficulty_basis,rubric_version,followup_of)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (question_id, attempt_id, seq, dimension, question.strip(), self._ts(),
                 difficulty, difficulty_basis.strip(), "difficulty-v1", followup_of))
            self._append_message(attempt["kp_id"], "assistant", "question", question.strip(),
                                 attempt_id, question_id=question_id)
            self._event(attempt["kp_id"], attempt_id, "QuestionAsked", {"question_id": question_id, "seq": seq})
            return question_id, seq

    def add_answer(self, question_id: str, answer: str, input_source: str = "text") -> str:
        if not answer.strip():
            raise ValueError("empty answer is not a scored response")
        if input_source not in INPUT_SOURCES:
            raise ValueError(f"unknown input source: {input_source}")
        with self.transaction():
            question = self.db.execute("""SELECT q.*,a.kp_id,a.state FROM questions q
                JOIN attempts a ON a.id=q.attempt_id WHERE q.id=?""", (question_id,)).fetchone()
            if not question or question["state"] != "in_progress":
                raise ValueError("question is not in an active attempt")
            prior = self.db.execute("SELECT * FROM answers WHERE question_id=?", (question_id,)).fetchone()
            if prior:
                if prior["text"] != answer:
                    raise ValueError("answer already recorded with different text")
                return prior["id"]
            answer_id = new_id()
            self.db.execute("INSERT INTO answers(id,question_id,text,answered_at,input_source) VALUES(?,?,?,?,?)",
                            (answer_id, question_id, answer, self._ts(), input_source))
            self._append_message(question["kp_id"], "user", "answer", answer,
                                 question["attempt_id"], answer_id=answer_id)
            self._event(question["kp_id"], question["attempt_id"], "AnswerRecorded", {"answer_id": answer_id})
            return answer_id

    def qa_for(self, attempt_id: str) -> list[sqlite3.Row]:
        return queries.qa_for(self.db, attempt_id)

    def mark_awaiting_review(self, attempt_id: str, detail: str) -> dict[str, Any]:
        with self.transaction():
            attempt = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not attempt or attempt["state"] not in ("in_progress", "awaiting_review"):
                raise ValueError("attempt cannot wait for review")
            if not self.db.execute("""SELECT 1 FROM answers a JOIN questions q ON q.id=a.question_id
                WHERE q.attempt_id=?""", (attempt_id,)).fetchone():
                raise ValueError("no answer to review")
            flow.transition(self, "attempt", attempt_id, "awaiting_review", "review_deferred",
                            kp_id=attempt["kp_id"], attempt_id=attempt_id)
            self._event(attempt["kp_id"], attempt_id, "ReviewDeferred", {"detail": detail})
            return {"attempt_id": attempt_id, "kind": "awaiting_review", "detail": detail,
                    "task_id": None, "due_date": None}

    def submit_scoring_answer(self, answer_id: str, text: str) -> str:
        if not text.strip():
            raise ValueError("final answer cannot be empty")
        with self.transaction():
            row = self.db.execute("""SELECT a.id,q.attempt_id,t.kp_id,t.state FROM answers a
                JOIN questions q ON q.id=a.question_id JOIN attempts t ON t.id=q.attempt_id
                WHERE a.id=?""", (answer_id,)).fetchone()
            if not row or row["state"] != "awaiting_review":
                raise ValueError("final scoring answer requires an awaiting review attempt")
            submission_id = new_id()
            self.db.execute("INSERT INTO scoring_submissions VALUES(?,?,?,?)",
                            (submission_id, answer_id, text.strip(), self._ts()))
            self._event(row["kp_id"], row["attempt_id"], "ScoringAnswerSubmitted",
                        {"answer_id": answer_id, "submission_id": submission_id})
            return submission_id

    def wrong_points_for(self, kp_id: str) -> list[dict[str, Any]]:
        """Read recent low scores with their original question and answer as review focus.

        No ids: with a question_id and answer_id each old question looked like one already answered in the
        new attempt, and the model ended the review before asking anything (V7 real reviews)."""
        return [dict(row) for row in self.db.execute("""SELECT d.dimension,d.score,
            q.text question,COALESCE(s.text,a.text) answer,
            COALESCE((SELECT quote FROM review_evidence e WHERE e.review_id=qa.review_id
                AND e.dimension=qa.dimension AND e.question_id=qa.question_id LIMIT 1),'') evidence,
            d.rationale
            FROM dimension_scores d JOIN reviews r ON r.id=d.review_id
            JOIN attempts t ON t.id=r.attempt_id
            JOIN review_question_assessments qa ON qa.review_id=d.review_id AND qa.dimension=d.dimension
            JOIN questions q ON q.id=qa.question_id
            JOIN answers a ON a.id=qa.answer_id
            LEFT JOIN scoring_submissions s ON s.id=qa.submission_id
            WHERE t.kp_id=? AND d.score<=2
            ORDER BY r.created_at DESC,d.rowid DESC LIMIT 10""", (kp_id,))]

    def _difficulty_from_history(self, kp_id: str) -> int:
        streak = 0
        for row in self.db.execute("""SELECT a.kind,a.finish_reason,r.kind review_kind,r.total
            FROM attempts a LEFT JOIN reviews r ON r.attempt_id=a.id
            WHERE a.kp_id=? AND a.state='completed' ORDER BY a.rowid""", (kp_id,)):
            if row["kind"] == "first" and row["review_kind"] == "complete":
                streak = 0
            elif row["kind"] == "review":
                if row["finish_reason"] == "zero":
                    streak += 1
                elif row["review_kind"] == "complete":
                    streak = streak + 1 if row["total"] < 50 else 0
        return streak

    def _scheduler(self) -> Scheduler:
        return Scheduler(desired_retention=0.9, learning_steps=(), relearning_steps=(),
                         maximum_interval=365, enable_fuzzing=False)

    def finalize(self, attempt_id: str, grades: list[DimensionGrade], reason: str = "natural",
                 new_cycle: bool = False) -> dict[str, Any]:
        if reason not in ("natural", "explicit", "limit"):
            raise ValueError("invalid finish reason")
        key = f"finalize:{attempt_id}"
        with self.transaction():
            receipt = self.db.execute("SELECT result_json FROM receipts WHERE operation_key=?", (key,)).fetchone()
            if receipt:
                prior = json.loads(receipt[0])
                if prior.get("review_id") and not self.db.execute("SELECT 1 FROM reviews WHERE id=?", (prior["review_id"],)).fetchone():
                    raise RuntimeError("stored review receipt does not match business state")
                if prior.get("task_id") and not self.db.execute("SELECT 1 FROM review_tasks WHERE id=?", (prior["task_id"],)).fetchone():
                    raise RuntimeError("stored task receipt does not match business state")
                return prior
            attempt = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not attempt or attempt["state"] not in ("in_progress", "awaiting_review"):
                raise ValueError("attempt is not ready for review")
            qa = self.qa_for(attempt_id)
            answers = {row["answer_id"]: (row["question_id"], row["scoring_text"] or row["answer"])
                       for row in qa if row["answer_id"]}
            submissions = {row["answer_id"]: row["scoring_submission_id"] for row in qa if row["answer_id"]}
            questions = {row["question_id"]: (row["dimension"], row["answer_id"]) for row in qa}
            result = calculate(grades, answers, questions)
            if result.total is None and answers:
                raise ValueError("answered attempt cannot be closed as zero-answer")
            if reason == "limit" and result.total is not None:
                # A forced stop cannot claim that the model naturally completed the assessment.
                result = type(result)(result.total, False, result.dimensions)
            if result.total is None:
                return self._finish_zero(attempt)
            review_id = new_id()
            kind = "complete" if result.complete else "partial"
            rating = rating_for(result.total) if result.complete else None
            now = self._ts()
            self.db.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?,?)",
                            (review_id, attempt_id, kind, result.total, rating, now, None))
            for grade in result.dimensions:
                self.db.execute("""INSERT INTO dimension_scores
                    (review_id,dimension,score,rationale,rubric_version,supplement,feedback) VALUES(?,?,?,?,?,?,?)""",
                    (review_id, grade.dimension, grade.score, grade.rationale, RUBRIC_VERSION,
                     grade.supplement, grade.feedback))
                for item in assessments_for(grade):
                    self.db.execute("INSERT INTO review_question_assessments VALUES(?,?,?,?,?,?)",
                                    (review_id, grade.dimension, item.question_id, item.answer_id,
                                     submissions.get(item.answer_id), item.judgment))
                    for quote in item.evidence:
                        text = answers[item.answer_id][1]
                        start = text.find(quote)
                        self.db.execute("INSERT INTO review_evidence VALUES(?,?,?,?,?,?,?)",
                                        (new_id(), review_id, grade.dimension, item.question_id,
                                         quote, start, start + len(quote)))
            if result.complete:
                cycle = self.db.execute("SELECT * FROM fsrs_cycles WHERE kp_id=? AND closed_at IS NULL ORDER BY started_at DESC LIMIT 1",
                                        (attempt["kp_id"],)).fetchone()
                if new_cycle and cycle:
                    self.db.execute("UPDATE fsrs_cycles SET closed_at=? WHERE id=?", (now, cycle["id"]))
                    cycle = None
                card = Card.from_json(cycle["card_json"]) if cycle else Card()
                card, log = self._scheduler().review_card(card, Rating(rating), review_datetime=self.clock.fsrs_datetime())
                cycle_id = cycle["id"] if cycle else new_id()
                if cycle:
                    self.db.execute("UPDATE fsrs_cycles SET card_json=? WHERE id=?", (card.to_json(), cycle_id))
                else:
                    self.db.execute("INSERT INTO fsrs_cycles VALUES(?,?,?,?,?)",
                                    (cycle_id, attempt["kp_id"], card.to_json(), now, None))
                self.db.execute("INSERT INTO fsrs_logs VALUES(?,?,?,?,?)",
                                (new_id(), cycle_id, review_id, log.to_json(), card.to_json()))
                due = card.due.date()
                rule = "fsrs-6.3.2"
            else:
                due = self.clock.today() + timedelta(days=1)
                rule = "partial-next-day-v1"
            streak = self.db.execute("SELECT difficulty_streak FROM knowledge_points WHERE id=?", (attempt["kp_id"],)).fetchone()[0]
            if attempt["kind"] == "review" and result.complete:
                streak = streak + 1 if result.total < 50 else 0
            schedule = not ((new_cycle and not result.complete) or
                            (attempt["kind"] == "review" and result.complete and streak >= 3))
            task_id = new_id() if schedule else None
            if task_id:
                self.db.execute("INSERT INTO review_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                (task_id, attempt["kp_id"], review_id, due.isoformat(), due.isoformat(),
                                 due.isoformat(), "pending", 0, rule, now, None, None))
                flow.enter(self, "review_task", task_id, "pending", "scheduled",
                           kp_id=attempt["kp_id"], attempt_id=attempt_id)
            if attempt["kind"] == "review":
                for (done_id,) in self.db.execute("SELECT task_id FROM attempt_tasks WHERE attempt_id=?",
                                                  (attempt_id,)).fetchall():
                    self._task_to(done_id, "done", "reviewed", attempt["kp_id"], attempt_id, now)
            if new_cycle and result.complete:
                for (old_id,) in self.db.execute("""SELECT id FROM review_tasks WHERE kp_id=?
                        AND state IN ('pending','started') AND id<>?""", (attempt["kp_id"], task_id)).fetchall():
                    self._task_to(old_id, "relearn_handled", "relearned", attempt["kp_id"], attempt_id, now)
                flow.transition(self, "knowledge_point", attempt["kp_id"], "active", "relearned",
                                kp_id=attempt["kp_id"], attempt_id=attempt_id)
                self.db.execute("UPDATE knowledge_points SET difficulty_streak=0 WHERE id=?", (attempt["kp_id"],))
                streak = 0
            if attempt["kind"] == "review" and result.complete:
                if streak >= 3:
                    flow.transition(self, "knowledge_point", attempt["kp_id"], "needs_relearn", "difficulty_streak",
                                    kp_id=attempt["kp_id"], attempt_id=attempt_id)
                self.db.execute("UPDATE knowledge_points SET difficulty_streak=? WHERE id=?", (streak, attempt["kp_id"]))
            flow.transition(self, "attempt", attempt_id, "completed", f"finalize_{reason}",
                            kp_id=attempt["kp_id"], attempt_id=attempt_id)
            self.db.execute("UPDATE attempts SET ended_at=?,finish_reason=? WHERE id=?", (now, reason, attempt_id))
            self._queue_episode_job(attempt["kp_id"], review_id)
            self._event(attempt["kp_id"], attempt_id, "ReviewSaved", {"review_id": review_id, "kind": kind, "total": result.total})
            if task_id:
                self._event(attempt["kp_id"], attempt_id, "TaskScheduled", {"task_id": task_id, "due_date": due.isoformat()})
            output = {"attempt_id": attempt_id, "review_id": review_id, "kind": kind,
                      "total": result.total, "rating": rating, "task_id": task_id,
                      "due_date": due.isoformat() if task_id else None, "needs_relearn": streak >= 3}
            self.db.execute("INSERT INTO receipts VALUES(?,?,?,?)", (key, attempt_id, json.dumps(output), now))
        # Return only a committed result that can be read back by its stable id.
        saved = self.db.execute("SELECT id FROM reviews WHERE id=?", (output["review_id"],)).fetchone()
        task = self.db.execute("SELECT id FROM review_tasks WHERE id=?", (output["task_id"],)).fetchone() if output["task_id"] else None
        if not saved or (output["task_id"] and not task):
            raise RuntimeError("write not confirmed; resume with the same attempt id")
        return output

    def _finish_zero(self, attempt: sqlite3.Row) -> dict[str, Any]:
        attempt_id = attempt["id"]
        now = self._ts()
        if attempt["kind"] == "first":
            flow.transition(self, "attempt", attempt_id, "needs_self_test", "zero_answer",
                            kp_id=attempt["kp_id"], attempt_id=attempt_id)
            self.db.execute("UPDATE attempts SET ended_at=?,finish_reason='zero' WHERE id=?", (now, attempt_id))
            output = {"attempt_id": attempt_id, "kind": "zero", "task_id": None, "due_date": None}
        else:
            task_ids = [r[0] for r in self.db.execute("SELECT task_id FROM attempt_tasks WHERE attempt_id=?", (attempt_id,))]
            next_date = (self.clock.today() + timedelta(days=1)).isoformat()
            for task_id in task_ids:
                flow.transition(self, "review_task", task_id, "pending", "zero_answer_deferred",
                                kp_id=attempt["kp_id"], attempt_id=attempt_id)
                self.db.execute("""UPDATE review_tasks SET next_remind_date=?,
                    deferrals=deferrals+1 WHERE id=?""", (next_date, task_id))
            streak = self.db.execute("SELECT difficulty_streak FROM knowledge_points WHERE id=?", (attempt["kp_id"],)).fetchone()[0] + 1
            flow.transition(self, "knowledge_point", attempt["kp_id"], "needs_relearn" if streak >= 3 else "active",
                            "zero_answer", kp_id=attempt["kp_id"], attempt_id=attempt_id)
            self.db.execute("UPDATE knowledge_points SET difficulty_streak=? WHERE id=?", (streak, attempt["kp_id"]))
            flow.transition(self, "attempt", attempt_id, "completed", "zero_answer",
                            kp_id=attempt["kp_id"], attempt_id=attempt_id)
            self.db.execute("UPDATE attempts SET ended_at=?,finish_reason='zero' WHERE id=?", (now, attempt_id))
            output = {"attempt_id": attempt_id, "kind": "zero", "task_id": None,
                      "due_date": next_date if streak < 3 else None, "needs_relearn": streak >= 3}
        self._event(attempt["kp_id"], attempt_id, "ZeroAnswer", output)
        if attempt["kind"] == "review":
            self.db.execute("INSERT INTO receipts VALUES(?,?,?,?)", (f"finalize:{attempt_id}", attempt_id, json.dumps(output), now))
        return output

    def _task_to(self, task_id: str, state: str, cause: str, kp_id: str, attempt_id: str | None, now: str) -> None:
        flow.transition(self, "review_task", task_id, state, cause, kp_id=kp_id, attempt_id=attempt_id)
        self.db.execute("UPDATE review_tasks SET completed_at=? WHERE id=?", (now, task_id))

    def due_tasks(self) -> list[sqlite3.Row]:
        return queries.due_tasks(self.db, self.clock.today().isoformat())

    def status(self) -> dict[str, Any]:
        return queries.status(self.db, self.clock.today().isoformat())

    def trace(self, attempt_id: str) -> dict[str, Any]:
        return queries.trace(self.db, attempt_id)

    def add_note(self, kp_id: str, kind: str, text: str, explanation: str = "",
                 attempt_id: str | None = None, source_message_id: str | None = None,
                 via: str | None = None) -> str:
        if kind not in ("wrong_point", "clarification", "interest") or not text.strip():
            raise ValueError("invalid note")
        with self.transaction():
            note_id = new_id()
            self.db.execute("INSERT INTO notes VALUES(?,?,?,?,?,?,?,?)",
                            (note_id, kp_id, attempt_id, kind, text.strip(), explanation, 0, self._ts()))
            if kind == "clarification":
                self._append_message(kp_id, "user", "clarification", text.strip(), attempt_id,
                                     note_id=note_id, source_message_id=source_message_id)
            self._event(kp_id, attempt_id, "NoteRecorded",
                        {"note_id": note_id, "kind": kind, **({"via": via} if via else {})})
            return note_id

    def explain_note(self, note_id: str, explanation: str) -> None:
        if not explanation.strip():
            raise ValueError("explanation is required")
        with self.transaction():
            note = self.db.execute("SELECT * FROM notes WHERE id=? AND kind='clarification'", (note_id,)).fetchone()
            if not note:
                raise ValueError("unknown clarification note")
            self.db.execute("UPDATE notes SET explanation=? WHERE id=?", (explanation.strip(), note_id))
            self._append_message(note["kp_id"], "assistant", "clarification", explanation.strip(),
                                 note["attempt_id"], note_id=note_id)
            self._event(note["kp_id"], note["attempt_id"], "ClarificationExplained", {"note_id": note_id})

    def revise(self, review_id: str, dimension: str, score: int, reason: str) -> dict[str, Any]:
        if dimension not in ("concept", "mechanism", "application") or not isinstance(score, int) or not 0 <= score <= 4 or not reason.strip():
            raise ValueError("invalid revision")
        with self.transaction():
            row = self.db.execute("""SELECT r.*,a.kp_id FROM reviews r JOIN attempts a ON a.id=r.attempt_id
                WHERE r.id=?""", (review_id,)).fetchone()
            grade = self.db.execute("SELECT * FROM dimension_scores WHERE review_id=? AND dimension=?",
                                    (review_id, dimension)).fetchone()
            if not row or not grade:
                raise ValueError("review or assessed dimension does not exist")
            if grade["score"] == score:
                raise ValueError("new score equals old score")
            now = self._ts()
            self.db.execute("UPDATE dimension_scores SET score=? WHERE review_id=? AND dimension=?", (score, review_id, dimension))
            self.db.execute("INSERT INTO review_revisions VALUES(?,?,?,?,?,?,?)",
                            (new_id(), review_id, dimension, grade["score"], score, reason.strip(), now))
            rows = self.db.execute("SELECT * FROM dimension_scores WHERE review_id=?", (review_id,)).fetchall()
            grades = []
            for item in rows:
                assessed = self.db.execute("""SELECT * FROM review_question_assessments
                    WHERE review_id=? AND dimension=? ORDER BY rowid""",
                    (review_id, item["dimension"])).fetchall()
                assessments = tuple(QuestionAssessment(a["question_id"], a["answer_id"], a["judgment"],
                    tuple(e["quote"] for e in self.db.execute("""SELECT quote FROM review_evidence
                        WHERE review_id=? AND dimension=? AND question_id=? ORDER BY rowid""",
                        (review_id, item["dimension"], a["question_id"])))) for a in assessed)
                grades.append(DimensionGrade(item["dimension"], item["score"], "", "", "",
                                             item["rationale"], assessments))
            qa = self.qa_for(row["attempt_id"])
            answers = {q["answer_id"]: (q["question_id"], q["scoring_text"] or q["answer"])
                       for q in qa if q["answer_id"]}
            questions = {q["question_id"]: (q["dimension"], q["answer_id"]) for q in qa}
            result = calculate(grades, answers, None if any(item["rubric_version"] == "v1" for item in rows) else questions)
            rating = rating_for(result.total) if row["kind"] == "complete" else None
            self.db.execute("UPDATE reviews SET total=?,rating=?,revised_at=? WHERE id=?", (result.total, rating, now, review_id))
            self.db.execute("UPDATE episodic_memories SET active=0 WHERE review_id=?", (review_id,))
            self.db.execute("UPDATE generation_jobs SET status='stale',updated_at=? WHERE kind='episode' AND source_id=? AND status!='stale'",
                            (now, review_id))
            self._queue_episode_job(row["kp_id"], review_id)
            changed_due: dict[str, str] | None = None
            if row["kind"] == "complete":
                cycle = self.db.execute("""SELECT c.* FROM fsrs_cycles c JOIN fsrs_logs l ON l.cycle_id=c.id
                    WHERE l.review_id=?""", (review_id,)).fetchone()
                if cycle:
                    scheduler = self._scheduler()
                    card = Card()
                    logs = self.db.execute("""SELECT l.*,r.rating FROM fsrs_logs l JOIN reviews r ON r.id=l.review_id
                        WHERE l.cycle_id=? ORDER BY json_extract(l.log_json,'$.review_datetime'),l.rowid""", (cycle["id"],)).fetchall()
                    for log in logs:
                        from fsrs import ReviewLog
                        when = ReviewLog.from_json(log["log_json"]).review_datetime
                        card, updated = scheduler.review_card(card, Rating(log["rating"]), review_datetime=when)
                        self.db.execute("UPDATE fsrs_logs SET log_json=?,card_json=? WHERE id=?",
                                        (updated.to_json(), card.to_json(), log["id"]))
                    self.db.execute("UPDATE fsrs_cycles SET card_json=? WHERE id=?", (card.to_json(), cycle["id"]))
                    pending = self.db.execute("""SELECT t.*,l.card_json FROM review_tasks t
                        JOIN fsrs_logs l ON l.review_id=t.review_id WHERE l.cycle_id=?
                        AND t.state='pending' AND t.started_at IS NULL
                        ORDER BY t.created_at DESC LIMIT 1""", (cycle["id"],)).fetchone()
                    if pending:
                        due = Card.from_json(pending["card_json"]).due.date().isoformat()
                        if due != pending["due_date"]:
                            self.db.execute("UPDATE review_tasks SET due_date=?,next_remind_date=? WHERE id=?",
                                            (due, due, pending["id"]))
                            changed_due = {"task_id": pending["id"], "old_due": pending["due_date"], "new_due": due}
            streak = self._difficulty_from_history(row["kp_id"])
            new_state = "needs_relearn" if streak >= 3 else "active"
            old_state = flow.transition(self, "knowledge_point", row["kp_id"], new_state, "revision",
                                        kp_id=row["kp_id"], attempt_id=row["attempt_id"])
            self.db.execute("UPDATE knowledge_points SET difficulty_streak=? WHERE id=?", (streak, row["kp_id"]))
            if new_state == "needs_relearn":
                for (task_id,) in self.db.execute("""SELECT id FROM review_tasks
                        WHERE kp_id=? AND state='pending' AND started_at IS NULL""", (row["kp_id"],)).fetchall():
                    self._task_to(task_id, "relearn_handled", "revision", row["kp_id"], row["attempt_id"], now)
            elif old_state == "needs_relearn" and not self.db.execute(
                    "SELECT 1 FROM review_tasks WHERE kp_id=? AND state IN ('pending','started')", (row["kp_id"],)).fetchone():
                latest = self.db.execute("""SELECT r.id,l.card_json FROM reviews r
                    JOIN attempts a ON a.id=r.attempt_id JOIN fsrs_logs l ON l.review_id=r.id
                    WHERE a.kp_id=? AND r.kind='complete' ORDER BY a.rowid DESC LIMIT 1""",
                    (row["kp_id"],)).fetchone()
                if latest:
                    due = Card.from_json(latest["card_json"]).due.date().isoformat()
                    task = self.db.execute("SELECT * FROM review_tasks WHERE review_id=?", (latest["id"],)).fetchone()
                    if task and task["state"] == "relearn_handled" and task["started_at"] is None:
                        flow.transition(self, "review_task", task["id"], "pending", "revision",
                                        kp_id=row["kp_id"], attempt_id=row["attempt_id"])
                        self.db.execute("""UPDATE review_tasks SET due_date=?,next_remind_date=?,
                            completed_at=NULL WHERE id=?""", (due, due, task["id"]))
                    elif not task:
                        task_id = new_id()
                        self.db.execute("INSERT INTO review_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                        (task_id, row["kp_id"], latest["id"], due, due, due,
                                         "pending", 0, "fsrs-6.3.2", now, None, None))
                        flow.enter(self, "review_task", task_id, "pending", "revision",
                                   kp_id=row["kp_id"], attempt_id=row["attempt_id"])
                        self._event(row["kp_id"], row["attempt_id"], "TaskScheduledAfterRevision",
                                    {"task_id": task_id, "due_date": due})
            self._event(row["kp_id"], row["attempt_id"], "ReviewRevised",
                        {"review_id": review_id, "dimension": dimension, "old": grade["score"], "new": score,
                         "reason": reason.strip(), "changed_due": changed_due,
                         "difficulty_streak": streak, "state": new_state})
            return {"review_id": review_id, "total": result.total, "rating": rating,
                    "difficulty_streak": streak, "state": new_state}

    def export_json(self) -> dict[str, Any]:
        tables = ("knowledge_points", "materials", "material_checks", "clarification_chunks", "synthesis_batches", "attempts", "sessions", "messages", "message_materials", "summaries",
                  "semantic_memories", "episodic_memories", "generation_jobs", "context_builds", "cross_day_queries",
                  "questions", "answers", "scoring_submissions", "reviews",
                  "dimension_scores", "review_question_assessments", "review_evidence", "review_batches",
                  "answer_segments", "review_revisions", "fsrs_cycles", "fsrs_logs", "review_tasks",
                  "attempt_tasks", "notes", "consents", "receipts", "events", "llm_calls",
                  "search_calls", "tool_calls", "metrics", "conversations", "conversation_messages", "chat_summaries",
                  "spans", "llm_failures", "llm_contents", "approval_requests", "file_roots",
                  "endpoint_capabilities")
        return {table: [dict(r) for r in self.db.execute(f"SELECT * FROM {table}")] for table in tables}

    def forget(self, kp_id: str) -> None:
        with self.transaction():
            point = self.db.execute("SELECT title FROM knowledge_points WHERE id=?", (kp_id,)).fetchone()
            if not point:
                raise ValueError("unknown knowledge point")
            # These rows do not all point directly to the knowledge point.
            self.db.execute("DELETE FROM events WHERE kp_id=?", (kp_id,))
            self.db.execute("DELETE FROM receipts WHERE attempt_id IN (SELECT id FROM attempts WHERE kp_id=?)", (kp_id,))
            self.db.execute("DELETE FROM llm_calls WHERE point=?", (point["title"],))
            self.db.execute("DELETE FROM search_calls WHERE point=?", (point["title"],))
            self.db.execute("DELETE FROM tool_calls WHERE kp_id=? OR point=?", (kp_id, point["title"]))
            self.db.execute("DELETE FROM context_builds WHERE point=?", (point["title"],))
            self.db.execute("DELETE FROM material_checks WHERE point=?", (point["title"],))
            self.db.execute("DELETE FROM spans WHERE kp_id=? OR point=?", (kp_id, point["title"]))
            self.db.execute("DELETE FROM llm_failures WHERE kp_id=? OR point=?", (kp_id, point["title"]))
            self.db.execute("DELETE FROM llm_contents WHERE llm_call_id NOT IN (SELECT id FROM llm_calls)")
            self.db.execute("DELETE FROM knowledge_points WHERE id=?", (kp_id,))
            history.purge(self.db)
            self.db.execute("INSERT INTO metrics(name,value) VALUES('forgotten_count',1) ON CONFLICT(name) DO UPDATE SET value=value+1")

    def forget_preparation(self, point: str) -> bool:
        if self.get_knowledge_point(point):
            raise ValueError("knowledge point exists; use forget")
        with self.transaction():
            count = sum(self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE point=?", (point,)).fetchone()[0]
                        for table in ("material_checks", "context_builds", "llm_calls", "search_calls", "tool_calls"))
            for table in ("material_checks", "context_builds", "llm_calls", "search_calls", "tool_calls",
                          "spans", "llm_failures"):
                self.db.execute(f"DELETE FROM {table} WHERE point=?", (point,))
            self.db.execute("DELETE FROM llm_contents WHERE llm_call_id NOT IN (SELECT id FROM llm_calls)")
            return count > 0
