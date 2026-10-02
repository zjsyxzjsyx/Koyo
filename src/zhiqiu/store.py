from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator

from fsrs import Card, Rating, Scheduler

from .clock import Clock
from .scoring import DimensionGrade, calculate, rating_for


def new_id() -> str:
    return str(uuid.uuid4())


class Store:
    """The authority for learning state. Every state change is one SQLite transaction."""

    def __init__(self, path: Path, clock: Clock | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or Clock()
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=10000")
        self._migrate()

    def close(self) -> None:
        self.db.close()

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

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def _ts(self) -> str:
        return self.clock.now().isoformat()

    def _event(self, kp_id: str | None, attempt_id: str | None, kind: str, payload: dict[str, Any]) -> None:
        self.db.execute("INSERT INTO events(kp_id,attempt_id,type,payload_json,created_at) VALUES(?,?,?,?,?)",
                        (kp_id, attempt_id, kind, json.dumps(payload, ensure_ascii=False), self._ts()))

    def record_llm_call(self, details: dict[str, Any]) -> None:
        with self.transaction():
            self.db.execute("INSERT INTO llm_calls VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (new_id(), details.get("kp_id"), details.get("attempt_id"), details["point"], details["model"],
                             details["prompt_hash"], details["latency_ms"], details.get("tokens_in"),
                             details.get("tokens_out"), details["status"], self._ts()))

    def record_search_call(self, point: str, query: str, result_count: int, status: str) -> None:
        with self.transaction():
            self.db.execute("INSERT INTO search_calls VALUES(?,?,?,?,?,?)",
                            (new_id(), point, query, result_count, status, self._ts()))

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
                if count >= 3:
                    raise ValueError("daily review limit of three reached")
                if not task_ids:
                    raise ValueError("review requires a due task")
                for task_id in task_ids:
                    task = self.db.execute("SELECT * FROM review_tasks WHERE id=?", (task_id,)).fetchone()
                    if not task or task["kp_id"] != kp_id or task["state"] not in ("pending", "started") or task["next_remind_date"] > self.clock.today().isoformat():
                        raise ValueError("task is not due for this knowledge point")
            attempt_id = new_id()
            self.db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)",
                            (attempt_id, kp_id, kind, "in_progress", self.clock.today().isoformat(), self._ts(), None, None))
            for task_id in task_ids:
                self.db.execute("INSERT INTO attempt_tasks VALUES(?,?)", (attempt_id, task_id))
                self.db.execute("UPDATE review_tasks SET state='started',started_at=COALESCE(started_at,?) WHERE id=?",
                                (self._ts(), task_id))
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
            self.db.execute("UPDATE attempts SET state='in_progress',ended_at=NULL,finish_reason=NULL WHERE id=?", (attempt_id,))
            self._event(row["kp_id"], attempt_id, "AttemptResumed", {})

    def pause(self, attempt_id: str) -> None:
        with self.transaction():
            row = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not row or row["state"] != "in_progress":
                raise ValueError("attempt is not in progress")
            self.db.execute("UPDATE attempts SET state='paused' WHERE id=?", (attempt_id,))
            self._event(row["kp_id"], attempt_id, "AttemptPaused", {})

    def add_question(self, attempt_id: str, dimension: str, question: str) -> tuple[str, int]:
        if dimension not in ("concept", "mechanism", "application") or not question.strip():
            raise ValueError("invalid question")
        with self.transaction():
            attempt = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not attempt or attempt["state"] != "in_progress":
                raise ValueError("attempt is not in progress")
            seq = self.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM questions WHERE attempt_id=?", (attempt_id,)).fetchone()[0]
            if seq > 8:
                raise ValueError("question limit reached")
            question_id = new_id()
            self.db.execute("INSERT INTO questions VALUES(?,?,?,?,?,?)",
                            (question_id, attempt_id, seq, dimension, question.strip(), self._ts()))
            self._event(attempt["kp_id"], attempt_id, "QuestionAsked", {"question_id": question_id, "seq": seq})
            return question_id, seq

    def add_answer(self, question_id: str, answer: str) -> str:
        if not answer.strip():
            raise ValueError("empty answer is not a scored response")
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
            self.db.execute("INSERT INTO answers VALUES(?,?,?,?)", (answer_id, question_id, answer, self._ts()))
            self._event(question["kp_id"], question["attempt_id"], "AnswerRecorded", {"answer_id": answer_id})
            return answer_id

    def qa_for(self, attempt_id: str) -> list[sqlite3.Row]:
        return self.db.execute("""SELECT q.id question_id,q.seq,q.dimension,q.text question,
            a.id answer_id,a.text answer FROM questions q LEFT JOIN answers a ON a.question_id=q.id
            WHERE q.attempt_id=? ORDER BY q.seq""", (attempt_id,)).fetchall()

    def wrong_points_for(self, kp_id: str) -> list[dict[str, Any]]:
        """Read recent low scores with their original question and answer as review focus."""
        return [dict(row) for row in self.db.execute("""SELECT d.review_id,d.dimension,d.score,
            d.question_id,q.text question,d.answer_id,a.text answer,d.evidence,d.rationale
            FROM dimension_scores d JOIN reviews r ON r.id=d.review_id
            JOIN attempts t ON t.id=r.attempt_id
            JOIN questions q ON q.id=d.question_id
            JOIN answers a ON a.id=d.answer_id
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
            if not attempt or attempt["state"] != "in_progress":
                raise ValueError("attempt is not in progress")
            qa = self.qa_for(attempt_id)
            answers = {row["answer_id"]: (row["question_id"], row["answer"]) for row in qa if row["answer_id"]}
            result = calculate(grades, answers)
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
                self.db.execute("INSERT INTO dimension_scores VALUES(?,?,?,?,?,?,?)",
                                (review_id, grade.dimension, grade.score, grade.question_id,
                                 grade.answer_id, grade.evidence, grade.rationale))
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
            if attempt["kind"] == "review":
                self.db.execute("""UPDATE review_tasks SET state='done',completed_at=? WHERE id IN
                    (SELECT task_id FROM attempt_tasks WHERE attempt_id=?)""", (now, attempt_id))
            if new_cycle and result.complete:
                self.db.execute("""UPDATE review_tasks SET state='relearn_handled',completed_at=?
                    WHERE kp_id=? AND state IN ('pending','started') AND id<>?""", (now, attempt["kp_id"], task_id))
                self.db.execute("UPDATE knowledge_points SET state='active',difficulty_streak=0 WHERE id=?", (attempt["kp_id"],))
                streak = 0
            if attempt["kind"] == "review" and result.complete:
                if streak >= 3:
                    self.db.execute("UPDATE knowledge_points SET state='needs_relearn',difficulty_streak=? WHERE id=?",
                                    (streak, attempt["kp_id"]))
                else:
                    self.db.execute("UPDATE knowledge_points SET difficulty_streak=? WHERE id=?", (streak, attempt["kp_id"]))
            self.db.execute("UPDATE attempts SET state='completed',ended_at=?,finish_reason=? WHERE id=?", (now, reason, attempt_id))
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
            self.db.execute("UPDATE attempts SET state='needs_self_test',ended_at=?,finish_reason='zero' WHERE id=?", (now, attempt_id))
            output = {"attempt_id": attempt_id, "kind": "zero", "task_id": None, "due_date": None}
        else:
            task_ids = [r[0] for r in self.db.execute("SELECT task_id FROM attempt_tasks WHERE attempt_id=?", (attempt_id,))]
            next_date = (self.clock.today() + timedelta(days=1)).isoformat()
            for task_id in task_ids:
                self.db.execute("""UPDATE review_tasks SET state='pending',next_remind_date=?,
                    deferrals=deferrals+1 WHERE id=?""", (next_date, task_id))
            streak = self.db.execute("SELECT difficulty_streak FROM knowledge_points WHERE id=?", (attempt["kp_id"],)).fetchone()[0] + 1
            self.db.execute("UPDATE knowledge_points SET difficulty_streak=?,state=? WHERE id=?",
                            (streak, "needs_relearn" if streak >= 3 else "active", attempt["kp_id"]))
            self.db.execute("UPDATE attempts SET state='completed',ended_at=?,finish_reason='zero' WHERE id=?", (now, attempt_id))
            output = {"attempt_id": attempt_id, "kind": "zero", "task_id": None,
                      "due_date": next_date if streak < 3 else None, "needs_relearn": streak >= 3}
        self._event(attempt["kp_id"], attempt_id, "ZeroAnswer", output)
        if attempt["kind"] == "review":
            self.db.execute("INSERT INTO receipts VALUES(?,?,?,?)", (f"finalize:{attempt_id}", attempt_id, json.dumps(output), now))
        return output

    def due_tasks(self) -> list[sqlite3.Row]:
        return self.db.execute("""SELECT t.*,k.title FROM review_tasks t JOIN knowledge_points k ON k.id=t.kp_id
            WHERE k.state='active' AND t.state IN ('pending','started') AND t.next_remind_date<=?
            ORDER BY t.original_due_date,t.created_at,t.kp_id""", (self.clock.today().isoformat(),)).fetchall()

    def status(self) -> dict[str, Any]:
        return {"due": [dict(row) for row in self.due_tasks()],
                "open_attempts": [dict(r) for r in self.db.execute("""SELECT a.*,k.title FROM attempts a
                    JOIN knowledge_points k ON k.id=a.kp_id WHERE a.state IN
                    ('in_progress','paused','awaiting_review','needs_self_test') ORDER BY a.started_at""")],
                "needs_relearn": [dict(r) for r in self.db.execute("SELECT id,title FROM knowledge_points WHERE state='needs_relearn' ORDER BY title")]}

    def trace(self, attempt_id: str) -> dict[str, Any]:
        attempt = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if not attempt:
            raise ValueError("unknown attempt")
        return {"attempt": dict(attempt), "questions": [dict(r) for r in self.qa_for(attempt_id)],
                "review": [dict(r) for r in self.db.execute("SELECT * FROM reviews WHERE attempt_id=?", (attempt_id,))],
                "dimension_scores": [dict(r) for r in self.db.execute("""SELECT d.* FROM dimension_scores d
                    JOIN reviews r ON r.id=d.review_id WHERE r.attempt_id=?
                    ORDER BY CASE d.dimension WHEN 'concept' THEN 1 WHEN 'mechanism' THEN 2 ELSE 3 END""", (attempt_id,))],
                "tasks": [dict(r) for r in self.db.execute("""SELECT t.* FROM review_tasks t
                    JOIN reviews r ON r.id=t.review_id WHERE r.attempt_id=?""", (attempt_id,))],
                "events": [dict(r) for r in self.db.execute("SELECT * FROM events WHERE attempt_id=? ORDER BY id", (attempt_id,))],
                "llm_calls": [dict(r) for r in self.db.execute("SELECT * FROM llm_calls WHERE attempt_id=? ORDER BY created_at", (attempt_id,))]}

    def add_note(self, kp_id: str, kind: str, text: str, explanation: str = "", attempt_id: str | None = None) -> str:
        if kind not in ("wrong_point", "clarification", "interest") or not text.strip():
            raise ValueError("invalid note")
        with self.transaction():
            note_id = new_id()
            self.db.execute("INSERT INTO notes VALUES(?,?,?,?,?,?,?,?)",
                            (note_id, kp_id, attempt_id, kind, text.strip(), explanation, 0, self._ts()))
            self._event(kp_id, attempt_id, "NoteRecorded", {"note_id": note_id, "kind": kind})
            return note_id

    def explain_note(self, note_id: str, explanation: str) -> None:
        if not explanation.strip():
            raise ValueError("explanation is required")
        with self.transaction():
            note = self.db.execute("SELECT * FROM notes WHERE id=? AND kind='clarification'", (note_id,)).fetchone()
            if not note:
                raise ValueError("unknown clarification note")
            self.db.execute("UPDATE notes SET explanation=? WHERE id=?", (explanation.strip(), note_id))
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
            rows = self.db.execute("""SELECT d.*,a.text answer FROM dimension_scores d
                JOIN answers a ON a.id=d.answer_id WHERE d.review_id=?""", (review_id,)).fetchall()
            grades = [DimensionGrade(r["dimension"], r["score"], r["question_id"], r["answer_id"],
                                     r["evidence"], r["rationale"]) for r in rows]
            answers = {r["answer_id"]: (r["question_id"], r["answer"]) for r in rows}
            result = calculate(grades, answers)
            rating = rating_for(result.total) if row["kind"] == "complete" else None
            self.db.execute("UPDATE reviews SET total=?,rating=?,revised_at=? WHERE id=?", (result.total, rating, now, review_id))
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
            old_state = self.db.execute("SELECT state FROM knowledge_points WHERE id=?", (row["kp_id"],)).fetchone()[0]
            new_state = "needs_relearn" if streak >= 3 else "active"
            self.db.execute("UPDATE knowledge_points SET difficulty_streak=?,state=? WHERE id=?",
                            (streak, new_state, row["kp_id"]))
            if new_state == "needs_relearn":
                self.db.execute("""UPDATE review_tasks SET state='relearn_handled',completed_at=?
                    WHERE kp_id=? AND state='pending' AND started_at IS NULL""", (now, row["kp_id"]))
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
                        self.db.execute("""UPDATE review_tasks SET state='pending',due_date=?,next_remind_date=?,
                            completed_at=NULL WHERE id=?""", (due, due, task["id"]))
                    elif not task:
                        task_id = new_id()
                        self.db.execute("INSERT INTO review_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                        (task_id, row["kp_id"], latest["id"], due, due, due,
                                         "pending", 0, "fsrs-6.3.2", now, None, None))
                        self._event(row["kp_id"], row["attempt_id"], "TaskScheduledAfterRevision",
                                    {"task_id": task_id, "due_date": due})
            self._event(row["kp_id"], row["attempt_id"], "ReviewRevised",
                        {"review_id": review_id, "dimension": dimension, "old": grade["score"], "new": score,
                         "reason": reason.strip(), "changed_due": changed_due,
                         "difficulty_streak": streak, "state": new_state})
            return {"review_id": review_id, "total": result.total, "rating": rating,
                    "difficulty_streak": streak, "state": new_state}

    def export_json(self) -> dict[str, Any]:
        tables = ("knowledge_points", "materials", "attempts", "questions", "answers", "reviews",
                  "dimension_scores", "review_revisions", "fsrs_cycles", "fsrs_logs", "review_tasks",
                  "attempt_tasks", "notes", "consents", "receipts", "events", "llm_calls",
                  "search_calls", "metrics")
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
            self.db.execute("DELETE FROM knowledge_points WHERE id=?", (kp_id,))
            self.db.execute("INSERT INTO metrics(name,value) VALUES('forgotten_count',1) ON CONFLICT(name) DO UPDATE SET value=value+1")
