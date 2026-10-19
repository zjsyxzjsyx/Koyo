"""`zhiqiu dashboard`: a local, read-only web view of traces, metrics and learning records;
`zhiqiu web`: the same pages plus a chat workbench.

One FastAPI app on uvicorn (`zhiqiu.web`), bound to 127.0.0.1, that opens the database
read-only for each request (the terminal or the workbench may be writing at the same
time). Every response goes through value-based redaction. The read-only dashboard has no
write endpoint at all. The workbench adds /api/chat/* (with an SSE event stream), which only
answers requests from this machine that carry the token embedded in the served page (other
sites cannot read it), come from the page's own origin and send JSON.

This module holds what each route returns and the server object; `zhiqiu.web` holds the routes
and the checks.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import socket
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from datetime import date, timedelta
from http import HTTPStatus
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from . import contracts, guardrails, metrics, pricing, traces
from .clock import Clock
from .queries import due_tasks, open_readonly, schedule as review_schedule, trace as attempt_trace
from .web.security import TOKEN_HEADER  # noqa: F401  (re-exported for callers and tests)

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
BUSY_MS = 2000
# The largest workbench request body (a chat message or an answer).
MAX_BODY = 64 * 1024
# Statistics of this many recent transcriptions are kept in memory for the inputs that follow them.
VOICE_KEPT = 20
INPUT_SOURCES = ("text", "voice")
# A title typed in the sidebar, as /title allows.
RENAME_CHARS = 30


class BadRequest(ValueError):
    """A read route's parameters are invalid: answered with 400, not the busy-database 503."""


def _number(query: dict[str, list[str]], key: str, default: float, low: float, high: float) -> float:
    try:
        value = float(query.get(key, [default])[0])
    except ValueError:
        return default
    return min(max(value, low), high)


def live(db: sqlite3.Connection, query: dict[str, list[str]]) -> dict[str, Any]:
    latest = traces.list_traces(db, days=30, limit=1)
    # The file name lets the page show which database it reads, so a mismatched --db is visible.
    path = db.execute("PRAGMA database_list").fetchone()["file"]
    return {"now_ns": time.time_ns(), "running": traces.live(db), "latest": latest[0] if latest else None,
            "currency": pricing.currency(), "db": Path(path).name if path else ""}


def trace_list(db: sqlite3.Connection, query: dict[str, list[str]]) -> dict[str, Any]:
    days = _number(query, "days", 7, 0.01, 3650)
    limit = int(_number(query, "limit", 100, 1, 500))
    return {"traces": traces.list_traces(db, days, limit), "currency": pricing.currency()}


def trace_detail(db: sqlite3.Connection, ident: str) -> dict[str, Any]:
    trace_ids = traces.resolve(db, ident)
    if not trace_ids:
        raise LookupError(f"没有与 {ident} 关联的 trace")
    return {"now_ns": time.time_ns(), "trees": [traces.tree(db, trace_id) for trace_id in trace_ids]}


def attempt_list(db: sqlite3.Connection, query: dict[str, list[str]]) -> dict[str, Any]:
    limit = int(_number(query, "limit", 50, 1, 500))
    rows = db.execute("""SELECT a.id,a.kind,a.state,a.started_at,a.start_date,k.title,
        (SELECT total FROM reviews r WHERE r.attempt_id=a.id ORDER BY rowid DESC LIMIT 1) total,
        (SELECT kind FROM reviews r WHERE r.attempt_id=a.id ORDER BY rowid DESC LIMIT 1) review_kind,
        (SELECT COUNT(*) FROM questions q WHERE q.attempt_id=a.id) questions
        FROM attempts a JOIN knowledge_points k ON k.id=a.kp_id ORDER BY a.started_at DESC LIMIT ?""",
                      (limit,)).fetchall()
    return {"attempts": [dict(row) for row in rows]}


def attempt_detail(db: sqlite3.Connection, attempt_id: str) -> dict[str, Any]:
    try:
        data = attempt_trace(db, attempt_id)
    except ValueError:
        raise LookupError(f"没有这次尝试：{attempt_id}") from None
    kp = db.execute("SELECT id,title,state,skill,skill_source FROM knowledge_points WHERE id=?",
                    (data["attempt"]["kp_id"],)).fetchone()
    # Answers that came from a confirmed voice transcript; grading never sees this mark.
    voice_answers = [row[0] for row in db.execute("""SELECT a.id FROM answers a JOIN questions q ON q.id=a.question_id
        WHERE q.attempt_id=? AND a.input_source='voice'""", (attempt_id,))]
    task_ids = [task["id"] for task in data["tasks"]]
    states = [dict(row) for row in db.execute("""SELECT id,payload_json,created_at,trace_id FROM events
        WHERE type='StateChanged' AND (attempt_id=? OR json_extract(payload_json,'$.id') IN
        (SELECT value FROM json_each(?))) ORDER BY id""", (attempt_id, json.dumps([attempt_id, *task_ids])))]
    for row in states:
        row["payload"] = json.loads(row.pop("payload_json"))
    notes = [dict(row) for row in db.execute("""SELECT kind,text,explanation,created_at FROM notes
        WHERE attempt_id=? ORDER BY rowid""", (attempt_id,))]
    # How each unprefixed quiz input was read (answer / question / both) and who decided.
    classifications = [{**json.loads(row["payload_json"]), "created_at": row["created_at"]}
                       for row in data["events"] if row["type"] == "TurnClassified"]
    keep = ("attempt", "questions", "review", "dimension_scores", "question_assessments", "review_evidence", "tasks")
    return {**{key: data[key] for key in keep}, "knowledge_point": dict(kp) if kp else None,
            "state_events": states, "notes": notes, "classifications": classifications,
            "voice_answers": voice_answers, "trace_ids": traces.resolve(db, attempt_id)}


def schedule(db: sqlite3.Connection, query: dict[str, list[str]]) -> dict[str, Any]:
    """Review tasks for the week (Monday to Sunday) or the month grid (whole weeks) around a date."""
    view = query.get("view", ["week"])[0]
    if view not in ("week", "month"):
        raise BadRequest(f"view 只能是 week 或 month：{view}")
    today = Clock().today()
    raw = query.get("date", [""])[0]
    try:
        anchor = date.fromisoformat(raw) if raw else today
    except ValueError:
        raise BadRequest(f"date 应为 YYYY-MM-DD 日期：{raw}") from None
    if view == "week":
        start = anchor - timedelta(days=anchor.weekday())
        end = start + timedelta(days=6)
    else:
        first = anchor.replace(day=1)
        last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        start = first - timedelta(days=first.weekday())
        end = last + timedelta(days=6 - last.weekday())
    data = review_schedule(db, today.isoformat(), start.isoformat(), end.isoformat())
    return {**data, "view": view, "anchor": anchor.isoformat()}


APPROVAL_STATUSES = ("pending", "approved", "denied", "cancelled")


def approval_list(db: sqlite3.Connection, query: dict[str, list[str]]) -> dict[str, Any]:
    """Every time a tool asked the user, newest first; pending ones are counted over all time."""
    days = _number(query, "days", 7, 0.01, 3650)
    limit = int(_number(query, "limit", 200, 1, 1000))
    status = query.get("status", [""])[0]
    if status and status not in APPROVAL_STATUSES:
        raise LookupError(f"没有这种审批状态：{status}")
    since_ns = time.time_ns() - int(days * 86_400 * 10**9)
    rows = db.execute(f"""SELECT * FROM approval_requests WHERE created_at_ns>=? {"AND status=?" if status else ""}
        ORDER BY created_at_ns DESC LIMIT ?""", (since_ns, status, limit) if status else (since_ns, limit)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["summary"] = json.loads(item.pop("summary_json") or "{}")
        item["wait_ms"] = ((row["decided_at_ns"] - row["created_at_ns"]) // 1_000_000
                           if row["decided_at_ns"] else None)
        items.append(item)
    counts = {key: value for key, value in db.execute("""SELECT status, COUNT(*) FROM approval_requests
        WHERE created_at_ns>=? GROUP BY status""", (since_ns,))}
    pending = db.execute("SELECT COUNT(*) FROM approval_requests WHERE status='pending'").fetchone()[0]
    return {"approvals": items, "counts": counts, "pending": pending}


ROUTES: dict[str, Callable[[sqlite3.Connection, dict[str, list[str]]], dict[str, Any]]] = {
    "/api/live": live,
    "/api/traces": trace_list,
    "/api/metrics": lambda db, query: metrics.collect(db, _number(query, "days", 7, 0.01, 3650)),
    "/api/failures": lambda db, query: contracts.failures(db, _number(query, "days", 7, 0.01, 3650)),
    "/api/check": lambda db, query: contracts.check(db, _number(query, "days", 7, 0.01, 3650)),
    "/api/attempts": attempt_list,
    "/api/approvals": approval_list,
    "/api/schedule": schedule,
    "/api/summary": lambda db, query: {**traces.summary(db, _number(query, "days", 7, 0.01, 3650)),
                                       "failures": contracts.failures(db, _number(query, "days", 7, 0.01, 3650))["total"],
                                       "approvals_pending": db.execute("""SELECT COUNT(*) FROM approval_requests
                                           WHERE status='pending'""").fetchone()[0],
                                       "reviews_due": len(due_tasks(db, Clock().today().isoformat()))},
}
DETAIL_ROUTES: dict[str, Callable[[sqlite3.Connection, str], dict[str, Any]]] = {
    "/api/trace/": trace_detail,
    "/api/attempt/": attempt_detail,
    "/api/llm/": traces.llm_detail,
}


def page(token: str | None = None) -> bytes:
    """The page; in workbench mode it carries the request token and says so."""
    body = resources.files("zhiqiu").joinpath("dashboard.html").read_bytes()
    if token is None:
        return body
    meta = (f'<meta name="zhiqiu-mode" content="workbench">'
            f'<meta name="zhiqiu-token" content="{token}">').encode("utf-8")
    return body.replace(b"<head>", b"<head>" + meta, 1)


def chat_events(server: "DashboardServer", query: dict[str, list[str]]) -> dict[str, Any]:
    bench = server.workbench
    after = int(_number(query, "after", 0, 0, 2**62))
    # Nothing from before the latest switch: a reloaded page must not mix in the previous conversation.
    # lost: events after `after` were dropped from memory (seq may also skip compacted stream deltas).
    return {"events": bench.io.since_switch(after), "next": bench.io.last_seq, "busy": bench.busy,
            "pending": bench.io.pending(), "conversation_id": bench.conversation_id,
            "lost": bool(after) and bench.io.lost(after)}


def chat_state(server: "DashboardServer", query: dict[str, list[str]]) -> dict[str, Any]:
    """The status panel, in the five parts the L6 guideline asks an interface to show: the current goal,
    the context in scope, what is running, the risky actions and the latest verifiable result."""
    bench = server.workbench
    conversation_id = bench.conversation_id
    pending = bench.io.pending()
    db = server.connect()
    try:
        row = db.execute("""SELECT mode,goal,step,active_kind,active_id,memory_snapshot_json FROM conversations
            WHERE id=?""", (conversation_id,)).fetchone()
        summaries = db.execute("SELECT COUNT(*) FROM chat_summaries WHERE conversation_id=?",
                               (conversation_id,)).fetchone()[0]
        running = [run for run in traces.live(db) if run["chain"][0].get("conversation_id") == conversation_id]
        approvals = [dict(item) for item in db.execute("""SELECT tool,status,decided_via,title,created_at_ns,"grant",root_path
            FROM approval_requests WHERE conversation_id=? ORDER BY created_at_ns DESC LIMIT 5""", (conversation_id,))]
        result = _latest_result(db, conversation_id)
        verification = _conversation_contracts(db, conversation_id)
    finally:
        db.close()
    context = bench.context
    return {
        "conversation_id": conversation_id, "busy": bench.busy, "pending": len(pending),
        "goal": {"mode": row["mode"], "goal": row["goal"], "step": row["step"],
                 "active_kind": row["active_kind"], "active_id": row["active_id"]} if row else None,
        "context": {"read_roots": context.get("read_roots", []), "write_roots": context.get("write_roots", []),
                    "memories": len(json.loads(row["memory_snapshot_json"] or "[]")) if row else 0,
                    "summarized": summaries > 0, "network": not context.get("offline", False),
                    "tools": context.get("tools", True), "path_grants": context.get("path_grants", False)},
        "running": [{"trace_id": run["trace_id"], "chain": run["chain"], "elapsed_ms": run["elapsed_ms"],
                     "llm_calls": run["llm_calls"]} for run in running],
        "risk": {"policy": context.get("policy"), "policy_text": context.get("policy_text", ""),
                 # The full text and the ids let the approvals page answer the same prompt as the chat card.
                 "pending": [{"kind": item["kind"], "tool": item.get("tool"),
                              "text": (item.get("text") or item.get("prompt") or "").split("\n")[0],
                              "full_text": item.get("text") or item.get("prompt") or "",
                              "request_id": item["request_id"],
                              "approval_request_id": item.get("approval_request_id")} for item in pending],
                 "recent": approvals},
        "result": result,
        "verification": verification,
        "skills": context.get("skills"),
        "voice": _voice_state(),
    }


def _voice_state() -> dict[str, Any]:
    from . import voice, voice_install
    state = voice.status()
    mode = voice_install.auto_download()
    return {"available": state["available"], "reason": state["reason"], "reason_text": voice.REASONS[state["reason"]],
            "max_seconds": state["max_seconds"], "auto_download": mode,
            # Missing model, deps in place: the microphone offers to download it (unless never).
            "installable": state["reason"] in ("model_missing", "downloading") and mode != "never",
            "install": {**voice_install.JOB.snapshot(), **voice_install.describe()}}


def _latest_result(db: sqlite3.Connection, conversation_id: str | None) -> dict[str, Any] | None:
    """The newest flow result of the conversation, with the score and next review it led to."""
    row = db.execute("""SELECT content,payload_json,created_at FROM conversation_messages
        WHERE conversation_id=? AND kind='flow_result' ORDER BY seq DESC LIMIT 1""", (conversation_id,)).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload_json"] or "{}")
    item: dict[str, Any] = {"summary": row["content"], "at": row["created_at"], "attempt_id": payload.get("attempt_id"),
                            "total": None, "review_kind": None, "next_review": None}
    if item["attempt_id"]:
        review = db.execute("SELECT id,total,kind FROM reviews WHERE attempt_id=?", (item["attempt_id"],)).fetchone()
        if review:
            item["total"], item["review_kind"] = review["total"], review["kind"]
            task = db.execute("SELECT due_date FROM review_tasks WHERE review_id=?", (review["id"],)).fetchone()
            item["next_review"] = task["due_date"] if task else None
    return item


def _conversation_contracts(db: sqlite3.Connection, conversation_id: str | None) -> dict[str, Any]:
    """Run contracts over the last week and keep only violations whose evidence lies in this conversation."""
    trace_ids = {row[0] for row in db.execute("SELECT DISTINCT trace_id FROM spans WHERE conversation_id=?",
                                              (conversation_id,))}
    if not trace_ids:
        return {"traces": 0, "passed": None, "failed": []}
    data = contracts.check(db, 7)
    failed = [result["check"] for result in data["results"] if result["status"] == contracts.FAIL
              and any(evidence.get("trace_id") in trace_ids for evidence in result["evidence"])]
    return {"traces": len(trace_ids), "passed": not failed, "failed": failed}


HISTORY_KINDS = ("chat", "command", "proposal", "memory_proposal", "flow_result", "error")


def chat_history(server: "DashboardServer", query: dict[str, list[str]]) -> dict[str, Any]:
    """The messages of a conversation (the current one by default). For the current conversation only
    those it had when it was opened; later ones arrive as events."""
    bench = server.workbench
    conversation_id = query.get("conversation_id", [""])[0] or bench.started_conversation
    current = conversation_id == bench.started_conversation
    if not conversation_id or (current and not bench.history_upto):
        return {"conversation_id": conversation_id, "messages": []}
    db = server.connect()
    try:
        if not db.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone():
            raise LookupError(f"没有这段对话：{conversation_id}")
        rows = db.execute(f"""SELECT seq,role,kind,content,created_at,
            json_extract(payload_json,'$.input_source') input_source FROM conversation_messages
            WHERE conversation_id=? AND seq<=? AND kind IN ({",".join("?" * len(HISTORY_KINDS))}) ORDER BY seq""",
                          (conversation_id, bench.history_upto if current else 2**62, *HISTORY_KINDS)).fetchall()
    finally:
        db.close()
    return {"conversation_id": conversation_id, "messages": [dict(row) for row in rows]}


def _resumable(db: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """A conversation left in a flow, or whose last flow result points at an attempt still open."""
    if row["mode"] != "chat":
        return True
    result = db.execute("""SELECT json_extract(payload_json,'$.attempt_id') FROM conversation_messages
        WHERE conversation_id=? AND kind='flow_result' ORDER BY seq DESC LIMIT 1""", (row["id"],)).fetchone()
    return bool(result and result[0] and db.execute(
        "SELECT 1 FROM attempts WHERE id=? AND state!='completed'", (result[0],)).fetchone())


def chat_conversations(server: "DashboardServer", query: dict[str, list[str]]) -> dict[str, Any]:
    """The sidebar: recent conversations with messages (and the current one even when empty)."""
    from .conversations import EMPTY_TITLE, FALLBACK_TITLE_CHARS, clean_title
    bench = server.workbench
    limit = int(_number(query, "limit", 30, 1, 200))
    db = server.connect()
    try:
        rows = db.execute("""SELECT c.*,(SELECT COUNT(*) FROM conversation_messages m WHERE m.conversation_id=c.id) messages,
            (SELECT content FROM conversation_messages m WHERE m.conversation_id=c.id AND m.role='user'
             ORDER BY kind!='chat', seq LIMIT 1) first_message
            FROM conversations c WHERE c.id=? OR EXISTS(SELECT 1 FROM conversation_messages m WHERE m.conversation_id=c.id)
            ORDER BY c.updated_at DESC, c.rowid DESC LIMIT ?""", (bench.conversation_id, limit)).fetchall()
        items = []
        for row in rows:
            title = row["title"]
            if not title:
                # Worked out when shown, the same way Conversations.fallback_title does; not written back.
                first = clean_title(row["first_message"] or "")
                title = (first[:FALLBACK_TITLE_CHARS] + ("…" if len(first) > FALLBACK_TITLE_CHARS else "")) or EMPTY_TITLE
            items.append({"id": row["id"], "short_id": row["id"][:8], "title": title, "title_source": row["title_source"],
                          "chat_date": row["chat_date"], "updated_at": row["updated_at"], "messages": row["messages"],
                          "current": row["id"] == bench.conversation_id, "resumable": _resumable(db, row)})
    finally:
        db.close()
    return {"conversations": items, "current": bench.conversation_id, "busy": bench.busy,
            "pending": len(bench.io.pending())}


class _StoredRoots:
    """Store.roots() over the page's read-only connection."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def roots(self, kind: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM file_roots WHERE kind=? ORDER BY added_at,rowid", (kind,)).fetchall()


def _roots_now(server: "DashboardServer") -> tuple[Any, dict[str, list[dict[str, Any]]]]:
    from .files import FileRoots
    roots = server.workbench.file_roots or FileRoots()
    db = server.connect()
    try:
        return roots, roots.listing(_StoredRoots(db))
    finally:
        db.close()


def roots_list(server: "DashboardServer", query: dict[str, list[str]]) -> dict[str, Any]:
    from .files import ROOTS_MAX
    _, listing = _roots_now(server)
    return {**listing, "limits": {"max": ROOTS_MAX}, "busy": server.workbench.busy,
            "pending": len(server.workbench.io.pending())}


CHAT_GETS: dict[str, Callable[["DashboardServer", dict[str, list[str]]], dict[str, Any]]] = {
    "/api/roots": roots_list,
    "/api/chat/events": chat_events,
    "/api/chat/state": chat_state,
    "/api/chat/history": chat_history,
    "/api/chat/conversations": chat_conversations,
}


class DashboardServer:
    """The uvicorn server behind both commands, with the interface the 7.2 ThreadingHTTPServer had:
    the port is bound on construction (port 0 picks a free one), serve_forever() runs until
    shutdown() (from another thread) or Ctrl+C, server_close() releases the socket."""

    def __init__(self, db_path: Path, port: int = DEFAULT_PORT, workbench: Any = None):
        import uvicorn
        from .web.app import create_app
        self.db_path = Path(db_path)
        # With a workbench the server also serves /api/chat/*; the token proves a request came from our page.
        self.workbench = workbench
        self.token = secrets.token_urlsafe(32) if workbench is not None else None
        # Voice input: one recognizer, loaded on the first recording; only numbers of recent transcriptions
        # are kept (never text or audio), so a sent input can say how it was spoken.
        self._transcriber: Any = None
        self._voice_lock = threading.Lock()
        self.voice_stats: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.socket.bind((HOST, port))
            self.socket.listen(64)
        except OSError:
            self.socket.close()
            raise
        self.server_address = self.socket.getsockname()[:2]
        self.app = create_app(self)
        # Open SSE streams notice the shutdown within sse.WAIT_S; uvicorn waits no longer than this.
        self._uvicorn = uvicorn.Server(uvicorn.Config(self.app, log_level="warning", lifespan="off",
                                                      server_header=False, timeout_graceful_shutdown=2))
        self._stopped = threading.Event()
        self._stopped.set()

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def closing(self) -> bool:
        return bool(self._uvicorn.should_exit)

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        """Serve until shutdown(). Ctrl+C stops the server, then surfaces as KeyboardInterrupt."""
        self._stopped.clear()
        try:
            asyncio.run(self._serve(), loop_factory=self._uvicorn.config.get_loop_factory())
        finally:
            self._stopped.set()

    async def _serve(self) -> None:
        # On Windows a page that goes away mid-connection makes the proactor loop print a
        # ConnectionResetError traceback; nothing is wrong, so the terminal is kept quiet.
        loop = asyncio.get_running_loop()

        def quiet(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
            if not isinstance(context.get("exception"), ConnectionResetError):
                loop.default_exception_handler(context)
        loop.set_exception_handler(quiet)
        await self._uvicorn.serve(sockets=[self.socket])

    def shutdown(self, timeout: float = 10) -> None:
        self._uvicorn.should_exit = True
        self._stopped.wait(timeout)

    def server_close(self) -> None:
        self.socket.close()

    def connect(self) -> sqlite3.Connection:
        db = open_readonly(self.db_path)
        db.execute(f"PRAGMA busy_timeout={BUSY_MS}")
        return db

    async def refuse(self, request: Any, reason: str) -> None:
        """Leave the GuardrailTriggered record of a refused workbench request, off the event loop."""
        from fastapi.concurrency import run_in_threadpool
        await run_in_threadpool(record_refusal, self.db_path, f"{request.method} {request.url.path}：{reason}")

    def reset_transcriber(self) -> None:
        """A model installed while running: the next transcription loads it."""
        with self._voice_lock:
            self._transcriber = None

    def transcriber(self) -> Any:
        from .voice import Transcriber
        with self._voice_lock:
            if self._transcriber is None:
                self._transcriber = Transcriber()
            return self._transcriber

    def remember_voice(self, stats: dict[str, Any]) -> str:
        voice_id = uuid.uuid4().hex
        with self._voice_lock:
            self.voice_stats[voice_id] = stats
            while len(self.voice_stats) > VOICE_KEPT:
                self.voice_stats.popitem(last=False)
        return voice_id

    def voice_meta(self, voice_id: Any) -> dict[str, Any] | None:
        with self._voice_lock:
            return dict(self.voice_stats[voice_id]) if isinstance(voice_id, str) and voice_id in self.voice_stats else None


# ----- workbench writes (run on a worker thread of the server; each returns status and JSON) -----

def voice_install(server: DashboardServer) -> tuple[int, dict[str, Any]]:
    """Start downloading the voice model in the background; progress goes out as voice_install events."""
    from . import voice, voice_install as installer
    state = voice.status()
    if state["reason"] == "ok":
        return HTTPStatus.CONFLICT, {"error": "语音模型已经装好了"}
    if state["reason"] == "not_installed":
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": voice.REASONS["not_installed"]}
    if installer.auto_download() == "never":
        return HTTPStatus.BAD_REQUEST, {"error": f"配置为不自动下载（voice.auto_download = never）；请手动下载 {voice.MODEL_URL}"}

    def changed(snapshot: dict[str, Any]) -> None:
        if snapshot["stage"] == "done":
            server.reset_transcriber()
        server.workbench.io.emit("voice_install", **snapshot)

    if not installer.JOB.start(changed):
        return HTTPStatus.CONFLICT, {"error": "已经在下载语音模型了"}
    return HTTPStatus.ACCEPTED, {"started": True, **installer.describe()}


def transcribe(server: DashboardServer, audio: bytes) -> tuple[int, dict[str, Any]]:
    """Speech to text, on a server thread: a quiz waiting for its answer is exactly when it is needed,
    and nothing here touches the agent. The audio is dropped as soon as it is transcribed."""
    from . import voice
    state = voice.status()
    if not state["available"]:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"语音输入不可用：{voice.REASONS[state['reason']]}",
                                                "reason": state["reason"]}
    try:
        result = server.transcriber().transcribe(audio)
    except voice.VoiceError as exc:
        return HTTPStatus.BAD_REQUEST, {"error": str(exc)}
    except voice.Busy as exc:
        return HTTPStatus.CONFLICT, {"error": str(exc)}
    finally:
        del audio
    try:
        db = server.connect()
        try:
            known = voice.terms(db)
        finally:
            db.close()
    except (ValueError, sqlite3.Error):
        known = []
    text, replacements = voice.correct(result["raw"], known)
    voice_id = server.remember_voice({"duration_ms": result["duration_ms"], "elapsed_ms": result["elapsed_ms"],
                                      "replacements": len(replacements)})
    return HTTPStatus.OK, {"text": text, "raw": result["raw"], "replacements": replacements,
                           "duration_ms": result["duration_ms"], "elapsed_ms": result["elapsed_ms"],
                           "voice_id": voice_id}


def roots_change(server: DashboardServer, action: str, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Preview, add or remove a root. Checked here (the file system read-only); the worker writes it
    between turns, so a change never lands in the middle of one."""
    from .files import RootRefused, _key, _real, check_root
    bench = server.workbench
    kind, raw = data.get("kind"), data.get("path")
    try:
        roots, listing = _roots_now(server)
    except (ValueError, sqlite3.Error) as exc:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"数据库暂时繁忙：{exc}"}
    items = listing[kind]
    if action == "remove":
        if not isinstance(raw, str) or not raw:
            return HTTPStatus.BAD_REQUEST, {"error": "path 必须是非空字符串"}
        item = next((item for item in items if item["path"] == raw), None)
        if item is None and any(_key(_real(env)) == _key(_real(raw)) for env in roots.env[kind]):
            item = {"source": "env"}
        if item is None:
            return HTTPStatus.NOT_FOUND, {"error": f"白名单里没有这个目录：{raw}"}
        if item["source"] == "env":
            return HTTPStatus.BAD_REQUEST, {"error": "这个目录来自环境变量，请在 .env 中修改"}
        if bench.control(("roots", "remove", kind, raw, 0, False)):
            return HTTPStatus.ACCEPTED, {"accepted": True}
        return HTTPStatus.CONFLICT, {"error": "等这一轮结束再改"}
    stored = sum(item["source"] == "workbench" for item in items)
    try:
        preview = check_root(kind, raw, [item["path"] for item in items], stored)
    except RootRefused as exc:
        return HTTPStatus.BAD_REQUEST, {"error": str(exc)}
    if action == "preview":
        return HTTPStatus.OK, preview.as_dict()
    if bench.control(("roots", "add", kind, preview.path, preview.files, preview.truncated)):
        return HTTPStatus.ACCEPTED, {"accepted": True, "preview": preview.as_dict()}
    return HTTPStatus.CONFLICT, {"error": "等这一轮结束再改"}


def conversation_control(server: DashboardServer, action: str, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Switch, start or rename a conversation. Checked here, then done by the worker between turns."""
    from .conversations import clean_title
    bench = server.workbench
    conversation_id = data.get("conversation_id")
    if action != "new":
        if not isinstance(conversation_id, str) or not conversation_id:
            return HTTPStatus.BAD_REQUEST, {"error": "conversation_id 必须是非空字符串"}
        db = server.connect()
        try:
            known = db.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        finally:
            db.close()
        if not known:
            return HTTPStatus.NOT_FOUND, {"error": f"没有这段对话：{conversation_id}"}
    if action == "rename":
        title = clean_title(data.get("title")) if isinstance(data.get("title"), str) else ""
        if not title or len(title) > RENAME_CHARS:
            return HTTPStatus.BAD_REQUEST, {"error": f"标题清洗后须为 1 到 {RENAME_CHARS} 字"}
        command: tuple[Any, ...] = ("rename", conversation_id, title)
    else:
        command = ("switch", conversation_id) if action == "switch" else ("new",)
    if bench.control(command):
        return HTTPStatus.ACCEPTED, {"accepted": True, "next": bench.io.last_seq}
    return HTTPStatus.CONFLICT, {"error": "先回答或停止当前提示，等这一轮结束再切换"}


def record_refusal(db_path: Path, reason: str) -> None:
    """Leave a GuardrailTriggered event for a refused workbench request, through a short-lived connection.

    Best effort, like every guardrail record: the refusal holds even when the event cannot be written."""
    try:
        db = sqlite3.connect(db_path, timeout=BUSY_MS / 1000)
        try:
            with db:
                db.execute("INSERT INTO events(type,payload_json,created_at,at_ns) VALUES(?,?,?,?)",
                           (guardrails.EVENT, json.dumps(guardrails.payload("web_request_refused", "blocked", reason),
                                                         ensure_ascii=False), Clock().now().isoformat(), time.time_ns()))
        finally:
            db.close()
    except sqlite3.Error:
        pass


def serve(db_path: Path, port: int = DEFAULT_PORT, on_ready: Callable[[str], None] | None = None,
          workbench: Any = None) -> None:
    server = DashboardServer(db_path, port, workbench)
    url = f"http://{HOST}:{server.port}/"
    if on_ready:
        on_ready(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if workbench is not None:
            workbench.close()
