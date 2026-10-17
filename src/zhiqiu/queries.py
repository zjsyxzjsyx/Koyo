"""Read-only business queries shared by Store and the read-only tools / MCP server.

Store(path) migrates and may even replace the database file, so read-only callers
open it with open_readonly() instead and reuse these functions.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from contextlib import closing
from datetime import date, timedelta
from typing import Any, Callable

from .tools import ToolError, ToolSpec


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing, current database without creating, migrating or writing anything."""
    from .store import SCHEMA_VERSION

    path = Path(path)
    if not path.is_file():
        raise ValueError(f"数据库不存在：{path}；请先用 zhiqiu CLI 打开一次")
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        db.close()
        raise ValueError(f"数据库版本为 {version}，需要 {SCHEMA_VERSION}；请先用 zhiqiu CLI 打开一次完成迁移")
    return db


def due_tasks(db: sqlite3.Connection, today: str) -> list[sqlite3.Row]:
    return db.execute("""SELECT t.*,k.title FROM review_tasks t JOIN knowledge_points k ON k.id=t.kp_id
        WHERE k.state='active' AND t.state IN ('pending','started') AND t.next_remind_date<=?
        ORDER BY t.original_due_date,t.created_at,t.kp_id""", (today,)).fetchall()


def status(db: sqlite3.Connection, today: str) -> dict[str, Any]:
    return {"due": [dict(row) for row in due_tasks(db, today)],
            "open_attempts": [dict(r) for r in db.execute("""SELECT a.*,k.title FROM attempts a
                JOIN knowledge_points k ON k.id=a.kp_id WHERE a.state IN
                ('in_progress','paused','awaiting_review','needs_self_test') ORDER BY a.started_at""")],
            "needs_relearn": [dict(r) for r in db.execute("SELECT id,title FROM knowledge_points WHERE state='needs_relearn' ORDER BY title")]}


def qa_for(db: sqlite3.Connection, attempt_id: str) -> list[sqlite3.Row]:
    return db.execute("""SELECT q.id question_id,q.seq,q.dimension,q.text question,
        q.difficulty,q.difficulty_basis,q.rubric_version,q.followup_of,
        a.id answer_id,a.text answer,
        (SELECT id FROM scoring_submissions s WHERE s.answer_id=a.id ORDER BY rowid DESC LIMIT 1) scoring_submission_id,
        (SELECT text FROM scoring_submissions s WHERE s.answer_id=a.id ORDER BY rowid DESC LIMIT 1) scoring_text
        FROM questions q LEFT JOIN answers a ON a.question_id=q.id
        WHERE q.attempt_id=? ORDER BY q.seq""", (attempt_id,)).fetchall()


def _without_empty_skill(build: dict[str, Any]) -> dict[str, Any]:
    """Only teach and clarify builds name a Skill; elsewhere the four empty columns are left out."""
    if build.get("skill_name") is None:
        for key in ("skill_name", "skill_version", "skill_sha", "skill_source"):
            build.pop(key, None)
    return build


def trace(db: sqlite3.Connection, attempt_id: str) -> dict[str, Any]:
    attempt = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    if not attempt:
        raise ValueError("unknown attempt")
    return {"attempt": dict(attempt), "questions": [dict(r) for r in qa_for(db, attempt_id)],
            "sessions": [dict(r) for r in db.execute("SELECT * FROM sessions WHERE attempt_id=? ORDER BY rowid", (attempt_id,))],
            "messages": [dict(r) for r in db.execute("""SELECT m.* FROM messages m
                JOIN sessions s ON s.id=m.session_id WHERE s.attempt_id=? ORDER BY s.rowid,m.rowid""", (attempt_id,))],
            "message_materials": [dict(r) for r in db.execute("""SELECT mm.* FROM message_materials mm
                JOIN messages m ON m.id=mm.message_id JOIN sessions s ON s.id=m.session_id
                WHERE s.attempt_id=? ORDER BY m.rowid""", (attempt_id,))],
            "review": [dict(r) for r in db.execute("SELECT * FROM reviews WHERE attempt_id=?", (attempt_id,))],
            "dimension_scores": [dict(r) for r in db.execute("""SELECT d.* FROM dimension_scores d
                JOIN reviews r ON r.id=d.review_id WHERE r.attempt_id=?
                ORDER BY CASE d.dimension WHEN 'concept' THEN 1 WHEN 'mechanism' THEN 2 ELSE 3 END""", (attempt_id,))],
            "question_assessments": [dict(r) for r in db.execute("""SELECT qa.* FROM review_question_assessments qa
                JOIN reviews r ON r.id=qa.review_id WHERE r.attempt_id=? ORDER BY qa.rowid""", (attempt_id,))],
            "review_evidence": [dict(r) for r in db.execute("""SELECT e.* FROM review_evidence e
                JOIN reviews r ON r.id=e.review_id WHERE r.attempt_id=? ORDER BY e.rowid""", (attempt_id,))],
            "review_batches": [dict(r) for r in db.execute("SELECT * FROM review_batches WHERE attempt_id=? ORDER BY rowid", (attempt_id,))],
            "answer_segments": [dict(r) for r in db.execute("SELECT * FROM answer_segments WHERE attempt_id=? ORDER BY start_char", (attempt_id,))],
            "tasks": [dict(r) for r in db.execute("""SELECT t.* FROM review_tasks t
                JOIN reviews r ON r.id=t.review_id WHERE r.attempt_id=?""", (attempt_id,))],
            "events": [dict(r) for r in db.execute("SELECT * FROM events WHERE attempt_id=? ORDER BY id", (attempt_id,))],
            "llm_calls": [dict(r) for r in db.execute("SELECT * FROM llm_calls WHERE attempt_id=? ORDER BY created_at", (attempt_id,))],
            "tool_calls": [dict(r) for r in db.execute("""SELECT c.* FROM tool_calls c, attempts a
                JOIN knowledge_points k ON k.id=a.kp_id WHERE a.id=? AND (c.attempt_id=a.id
                OR (c.attempt_id IS NULL AND (c.kp_id=a.kp_id OR c.point=k.title))) ORDER BY c.rowid""", (attempt_id,))],
            "context_builds": [_without_empty_skill(dict(r)) for r in db.execute(
                "SELECT * FROM context_builds WHERE attempt_id=? ORDER BY rowid", (attempt_id,))],
            "clarification_chunks": [dict(r) for r in db.execute("""SELECT c.* FROM clarification_chunks c
                JOIN attempts a ON a.kp_id=c.kp_id WHERE a.id=? ORDER BY c.rowid""", (attempt_id,))],
            "episodic_memories": [dict(r) for r in db.execute("SELECT * FROM episodic_memories WHERE attempt_id=? ORDER BY rowid", (attempt_id,))],
            "generation_jobs": [dict(r) for r in db.execute("""SELECT j.* FROM generation_jobs j
                JOIN reviews r ON r.id=j.source_id WHERE j.kind='episode' AND r.attempt_id=? ORDER BY j.rowid""",
                (attempt_id,))]}


def upcoming_tasks(db: sqlite3.Connection, until: str) -> list[dict[str, Any]]:
    """Open review tasks that will be due on or before a date, for planning ahead."""
    return [dict(r) for r in db.execute("""SELECT t.id,t.kp_id,k.title,t.due_date,t.next_remind_date,t.state
        FROM review_tasks t JOIN knowledge_points k ON k.id=t.kp_id
        WHERE k.state='active' AND t.state IN ('pending','started') AND t.next_remind_date<=?
        ORDER BY t.next_remind_date,t.original_due_date,t.kp_id""", (until,))]


FORECAST_DAYS = 14
_RULES = {"partial-next-day-v1": "次日补测"}


def _rule(version: str) -> str:
    return "FSRS" if version.startswith("fsrs-") else _RULES.get(version, version)


def schedule(db: sqlite3.Connection, today: str, start: str, end: str) -> dict[str, Any]:
    """Open review tasks by the day they will be reminded, reviews done on past days in [start, end],
    a 14-day load forecast and the points that need relearning. Only each point's next review is
    known (FSRS schedules one at a time), and no task id leaves here: a review is started by title."""
    from .store import DAILY_REVIEW_LIMIT

    items = []
    for row in db.execute("""SELECT t.kp_id,k.title,t.next_remind_date,t.due_date,t.original_due_date,t.state,
            t.deferrals,t.rule_version,r.total FROM review_tasks t JOIN knowledge_points k ON k.id=t.kp_id
            LEFT JOIN reviews r ON r.id=t.review_id
            WHERE k.state='active' AND t.state IN ('pending','started')
            ORDER BY t.next_remind_date,t.original_due_date,k.title"""):
        items.append({"kp_id": row["kp_id"], "title": row["title"], "date": row["next_remind_date"],
                      "due_date": row["due_date"], "original_due_date": row["original_due_date"],
                      "deferrals": row["deferrals"], "state": row["state"], "rule": _rule(row["rule_version"]),
                      "last_total": row["total"], "due": row["next_remind_date"] <= today})
    done: dict[str, list[dict[str, Any]]] = {}
    for row in db.execute("""SELECT a.start_date,k.title,r.total FROM attempts a
            JOIN knowledge_points k ON k.id=a.kp_id LEFT JOIN reviews r ON r.attempt_id=a.id
            WHERE a.kind='review' AND a.ended_at IS NOT NULL AND a.start_date BETWEEN ? AND ?
            ORDER BY a.started_at""", (start, end)):
        done.setdefault(row["start_date"], []).append({"title": row["title"], "total": row["total"]})
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    days = []
    for offset in range((last - first).days + 1):
        day = (first + timedelta(days=offset)).isoformat()
        days.append({"date": day, "items": [item for item in items if item["date"] == day and day >= today],
                     "done": done.get(day, [])})
    now = date.fromisoformat(today)
    forecast = []
    for offset in range(FORECAST_DAYS):
        day = (now + timedelta(days=offset)).isoformat()
        count = len({item["kp_id"] for item in items if item["date"] == day})
        forecast.append({"date": day, "count": count, "over_limit": count > DAILY_REVIEW_LIMIT})
    overdue = [item for item in items if item["date"] < today]
    week_end = (now + timedelta(days=7)).isoformat()
    relearn = [row["title"] for row in db.execute(
        "SELECT title FROM knowledge_points WHERE state='needs_relearn' ORDER BY title")]
    return {"today": today, "start": start, "end": end, "limit": DAILY_REVIEW_LIMIT,
            "done_today": db.execute("SELECT COUNT(*) FROM attempts WHERE kind='review' AND start_date=?",
                                     (today,)).fetchone()[0],
            "summary": {"overdue": len(overdue), "today": sum(item["date"] == today for item in items),
                        "next7": sum(today < item["date"] <= week_end for item in items), "relearn": len(relearn)},
            "overdue": overdue, "days": days, "forecast": forecast, "backlog": len(overdue), "relearn": relearn}


def knowledge_point(db: sqlite3.Connection, title_or_id: str) -> dict[str, Any]:
    """State, material list (no full text), attempts with scores and tasks for one knowledge point."""
    point = db.execute("""SELECT id,title,parent_id,state,difficulty_streak,created_at FROM knowledge_points
        WHERE id=? OR title=?""", (title_or_id, title_or_id)).fetchone()
    if not point:
        raise ValueError(f"未知知识点：{title_or_id}")
    kp_id = point["id"]
    attempts = []
    for row in db.execute("""SELECT a.id,a.kind,a.state,a.started_at,a.ended_at,a.finish_reason,
            r.id review_id,r.kind review_kind,r.total,r.rating FROM attempts a
            LEFT JOIN reviews r ON r.attempt_id=a.id WHERE a.kp_id=? ORDER BY a.started_at""", (kp_id,)):
        item = dict(row)
        item["dimension_scores"] = [dict(r) for r in db.execute("""SELECT dimension,score,rationale
            FROM dimension_scores WHERE review_id=? ORDER BY
            CASE dimension WHEN 'concept' THEN 1 WHEN 'mechanism' THEN 2 ELSE 3 END""", (row["review_id"],))]
        attempts.append(item)
    return {"knowledge_point": dict(point),
            "materials": [dict(r) for r in db.execute("""SELECT kind,source,content_hash,length(content) chars,
                reason,created_at FROM materials WHERE kp_id=? AND active=1 ORDER BY created_at,id""", (kp_id,))],
            "attempts": attempts,
            "tasks": [dict(r) for r in db.execute("""SELECT id,due_date,next_remind_date,state,deferrals,
                completed_at FROM review_tasks WHERE kp_id=? ORDER BY due_date""", (kp_id,))]}


Runner = Callable[[Callable[[sqlite3.Connection], Any]], Any]
_NO_ARGS = {"type": "object", "additionalProperties": False, "properties": {}}


def readonly_runner(path: Path) -> Runner:
    """Each call opens the database read-only and closes it again."""
    def run(query: Callable[[sqlite3.Connection], Any]) -> Any:
        with closing(open_readonly(path)) as db:
            return query(db)
    return run


def _date_arg(args: dict[str, Any], key: str) -> None:
    if key in args:
        try:
            date.fromisoformat(args[key])
        except ValueError as exc:
            raise ToolError("invalid_args", f"{key} 应为 YYYY-MM-DD 日期") from exc


def query_tool_specs(run: Runner, today: Callable[[], str]) -> list[ToolSpec]:
    """Business read queries as tools; every one is permission "read" and never writes."""
    def lookup(query: Callable[[sqlite3.Connection], Any]) -> Any:
        try:
            return run(query)
        except ValueError as exc:
            raise ToolError("not_found", str(exc)) from exc

    return [
        ToolSpec("zhiqiu_status", "知秋当前状态：今天到期的复习任务、未结束的尝试、需要补学的知识点。",
                 _NO_ARGS, "read", lambda args: lookup(lambda db: status(db, today())), source="zhiqiu.queries"),
        ToolSpec("zhiqiu_due", "截至某日（含）将到期或已到期的复习任务；不给日期时为今天。",
                 {"type": "object", "additionalProperties": False,
                  "properties": {"until": {"type": "string", "minLength": 10, "maxLength": 10}}},
                 "read", lambda args: lookup(lambda db: upcoming_tasks(db, args.get("until") or today())),
                 source="zhiqiu.queries", precheck=lambda args: _date_arg(args, "until")),
        ToolSpec("zhiqiu_knowledge_point", "一个知识点的状态、资料清单（不含全文）、历次尝试与维度分、复习任务。",
                 {"type": "object", "additionalProperties": False, "required": ["point"],
                  "properties": {"point": {"type": "string", "minLength": 1, "maxLength": 200}}},
                 "read", lambda args: lookup(lambda db: knowledge_point(db, args["point"])), source="zhiqiu.queries"),
        ToolSpec("zhiqiu_trace", "一次尝试的完整记录：题目、原答、评阅、证据、任务与调用审计；输出有上限。",
                 {"type": "object", "additionalProperties": False, "required": ["attempt_id"],
                  "properties": {"attempt_id": {"type": "string", "minLength": 1, "maxLength": 64}}},
                 "read", lambda args: lookup(lambda db: trace(db, args["attempt_id"])), source="zhiqiu.queries",
                 max_output_chars=20_000),
    ]
