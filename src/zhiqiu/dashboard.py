"""`zhiqiu dashboard`: a local, read-only web view of traces, metrics and learning records;
`zhiqiu web`: the same pages plus a chat workbench.

One standard-library HTTP server, bound to 127.0.0.1, that opens the database
read-only for each request (the terminal or the workbench may be writing at the same
time). Every response goes through value-based redaction. The read-only dashboard has no
write endpoint at all. The workbench adds /api/chat/*, which only answers requests from
this machine that carry the token embedded in the served page (other sites cannot read
it), come from the page's own origin and send JSON.
"""
from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from datetime import date, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import contracts, guardrails, metrics, pricing, traces
from .clock import Clock
from .queries import due_tasks, open_readonly, schedule as review_schedule, trace as attempt_trace
from .redact import redact_obj

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
BUSY_MS = 2000
# The largest workbench request body (a chat message or an answer).
MAX_BODY = 64 * 1024
# Statistics of this many recent transcriptions are kept in memory for the inputs that follow them.
VOICE_KEPT = 20
INPUT_SOURCES = ("text", "voice")
TOKEN_HEADER = "X-Zhiqiu-Token"
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
    return {"events": bench.io.since_switch(after), "next": bench.io.last_seq, "busy": bench.busy,
            "pending": bench.io.pending(), "conversation_id": bench.conversation_id}


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
        approvals = [dict(item) for item in db.execute("""SELECT tool,status,decided_via,title,created_at_ns
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
                    "summarized": summaries > 0, "network": not context.get("offline", False)},
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
    from . import voice
    state = voice.status()
    return {"available": state["available"], "reason": state["reason"], "reason_text": voice.REASONS[state["reason"]],
            "max_seconds": state["max_seconds"]}


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


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, db_path: Path, port: int = DEFAULT_PORT, workbench: Any = None):
        self.db_path = Path(db_path)
        # With a workbench the server also serves /api/chat/*; the token proves a request came from our page.
        self.workbench = workbench
        self.token = secrets.token_urlsafe(32) if workbench is not None else None
        # Voice input: one recognizer, loaded on the first recording; only numbers of recent transcriptions
        # are kept (never text or audio), so a sent input can say how it was spoken.
        self._transcriber: Any = None
        self._voice_lock = threading.Lock()
        self.voice_stats: OrderedDict[str, dict[str, Any]] = OrderedDict()
        super().__init__((HOST, port), Handler)

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

    @property
    def port(self) -> int:
        return self.server_address[1]

    def connect(self) -> sqlite3.Connection:
        db = open_readonly(self.db_path)
        db.execute(f"PRAGMA busy_timeout={BUSY_MS}")
        return db


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = "zhiqiu-dashboard"

    def log_message(self, format: str, *args: Any) -> None:  # keep the terminal quiet
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # 'self' scripts: only the fixed /voice-worklet.js, which the microphone recorder needs as a module.
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "connect-src 'self'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        body = json.dumps(redact_obj(data), ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        # A page on another site cannot reach this server through DNS rebinding: the Host must be local.
        host = self.headers.get("Host", "")
        return host in (f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}")

    def do_GET(self) -> None:
        if not self._host_ok():
            self._json(HTTPStatus.FORBIDDEN, {"error": "只接受来自本机地址的请求"})
            return
        parts = urlsplit(self.path)
        if parts.path in ("/", "/index.html"):
            self._send(HTTPStatus.OK, page(self.server.token), "text/html; charset=utf-8")
            return
        if parts.path == "/voice-worklet.js" and self.server.workbench is not None:
            self._send(HTTPStatus.OK, resources.files("zhiqiu").joinpath("voice-worklet.js").read_bytes(),
                       "text/javascript; charset=utf-8")
            return
        query = parse_qs(parts.query)
        if parts.path in CHAT_GETS and self.server.workbench is not None:
            # Chat events are conversation content: only our page, which holds the token, may read them.
            if not self._token_ok():
                self._json(HTTPStatus.FORBIDDEN, {"error": "缺少或错误的工作台令牌"})
                return
            try:
                self._json(HTTPStatus.OK, CHAT_GETS[parts.path](self.server, query))
            except LookupError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except (ValueError, sqlite3.Error) as exc:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"数据库暂时繁忙：{exc}"})
            return
        handler = ROUTES.get(parts.path)
        detail = next(((prefix, item) for prefix, item in DETAIL_ROUTES.items() if parts.path.startswith(prefix)), None)
        if handler is None and detail is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "没有这个接口"})
            return
        try:
            db = self.server.connect()
        except (ValueError, sqlite3.Error) as exc:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            return
        try:
            if handler is not None:
                data = handler(db, query)
            else:
                prefix, item = detail  # type: ignore[misc]
                data = item(db, parts.path[len(prefix):])
            self._json(HTTPStatus.OK, data)
        except LookupError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except BadRequest as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except sqlite3.OperationalError as exc:
            # e.g. the terminal holds the write lock a moment longer than busy_timeout; the page retries.
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"数据库暂时繁忙：{exc}"})
        finally:
            db.close()

    def _refuse(self) -> None:
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "看板只读，只接受 GET"})

    do_PUT = do_PATCH = do_DELETE = _refuse  # type: ignore[assignment]

    # ----- workbench writes ---------------------------------------------------------------

    def _token_ok(self) -> bool:
        sent = self.headers.get(TOKEN_HEADER, "")
        return bool(self.server.token) and hmac.compare_digest(sent.encode("utf-8"), self.server.token.encode("utf-8"))

    def _origin_ok(self) -> bool:
        # Browsers send Origin on POST; a page on another site cannot fake ours. Tools like curl send none.
        origin = self.headers.get("Origin")
        return origin is None or origin in (f"http://127.0.0.1:{self.server.port}", f"http://localhost:{self.server.port}")

    def _deny(self, status: HTTPStatus, reason: str) -> None:
        record_refusal(self.server.db_path, f"{self.command} {urlsplit(self.path).path}：{reason}")
        self._json(status, {"error": reason})

    def _raw(self, content_type: str, limit: int) -> bytes | None:
        """The request body after every workbench check (host, token, origin, type, size); None if refused."""
        if not self._host_ok():
            self._deny(HTTPStatus.FORBIDDEN, "只接受来自本机地址的请求")
        elif not self._token_ok():
            self._deny(HTTPStatus.FORBIDDEN, "缺少或错误的工作台令牌")
        elif not self._origin_ok():
            self._deny(HTTPStatus.FORBIDDEN, "请求来自其他网站")
        elif self.headers.get_content_type() != content_type:
            self._deny(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, f"只接受 {content_type}")
        else:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > limit:
                self._deny(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"请求体超过 {limit} 字节或长度无效")
                return None
            return self.rfile.read(length)
        return None

    def _body(self) -> dict[str, Any] | None:
        """The JSON object sent, after every workbench check; None when the request was refused."""
        raw = self._raw("application/json", MAX_BODY)
        if raw is None:
            return None
        try:
            data = json.loads(raw or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            return data
        self._json(HTTPStatus.BAD_REQUEST, {"error": "请求体必须是 JSON 对象"})
        return None

    def _source(self, data: dict[str, Any]) -> tuple[str, dict[str, Any] | None] | None:
        """How the text was entered (and, for voice, the numbers of its transcription); None after a 400."""
        source = data.get("input_source", "text")
        if source not in INPUT_SOURCES:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "input_source 只能是 text 或 voice"})
            return None
        return source, self.server.voice_meta(data.get("voice_id")) if source == "voice" else None

    def _transcribe(self) -> None:
        """Speech to text, on this HTTP thread: a quiz waiting for its answer is exactly when it is needed,
        and nothing here touches the agent. The audio is dropped as soon as it is transcribed."""
        from . import voice
        audio = self._raw("audio/wav", voice.MAX_WAV_BYTES)
        if audio is None:
            return
        state = voice.status()
        if not state["available"]:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"语音输入不可用：{voice.REASONS[state['reason']]}",
                                                        "reason": state["reason"]})
            return
        try:
            result = self.server.transcriber().transcribe(audio)
        except voice.VoiceError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except voice.Busy as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return
        finally:
            del audio
        try:
            db = self.server.connect()
            try:
                known = voice.terms(db)
            finally:
                db.close()
        except (ValueError, sqlite3.Error):
            known = []
        text, replacements = voice.correct(result["raw"], known)
        voice_id = self.server.remember_voice({"duration_ms": result["duration_ms"], "elapsed_ms": result["elapsed_ms"],
                                               "replacements": len(replacements)})
        self._json(HTTPStatus.OK, {"text": text, "raw": result["raw"], "replacements": replacements,
                                   "duration_ms": result["duration_ms"], "elapsed_ms": result["elapsed_ms"],
                                   "voice_id": voice_id})

    def do_POST(self) -> None:
        bench = self.server.workbench
        if bench is None:
            self._refuse()
            return
        path = urlsplit(self.path).path
        if path == "/api/chat/transcribe":
            self._transcribe()
            return
        data = self._body()
        if data is None:
            return
        if path == "/api/chat/input":
            text = data.get("text")
            source = self._source(data)
            if source is None:
                return
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_BODY:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "text 必须是非空字符串"})
            elif bench.submit(text, *source):
                self._json(HTTPStatus.ACCEPTED, {"accepted": True, "next": bench.io.last_seq})
            else:
                self._json(HTTPStatus.CONFLICT, {"error": "上一轮还没结束"})
        elif path == "/api/chat/respond":
            source = self._source(data)
            if source is None:
                return
            try:
                bench.io.respond(str(data.get("request_id", "")), data.get("value"), str(data.get("origin") or "chat"),
                                 *source)
            except LookupError as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            else:
                self._json(HTTPStatus.OK, {"answered": True})
        elif path == "/api/chat/stop":
            self._json(HTTPStatus.OK, {"stopped": bench.io.stop()})
        elif path in ("/api/chat/switch", "/api/chat/new", "/api/chat/rename"):
            self._conversation_control(bench, path.rsplit("/", 1)[1], data)
        elif path in ("/api/roots/preview", "/api/roots/add", "/api/roots/remove"):
            self._roots_change(bench, path.rsplit("/", 1)[1], data)
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "没有这个接口"})


    def _roots_change(self, bench: Any, action: str, data: dict[str, Any]) -> None:
        """Preview, add or remove a root. Checked here (the file system read-only); the worker writes it
        between turns, so a change never lands in the middle of one."""
        from .files import RootRefused, _key, _real, check_root
        kind, raw = data.get("kind"), data.get("path")
        if kind not in ("read", "write"):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "kind 只能是 read 或 write"})
            return
        try:
            roots, listing = _roots_now(self.server)
        except (ValueError, sqlite3.Error) as exc:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"数据库暂时繁忙：{exc}"})
            return
        items = listing[kind]
        if action == "remove":
            if not isinstance(raw, str) or not raw:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "path 必须是非空字符串"})
                return
            item = next((item for item in items if item["path"] == raw), None)
            if item is None and any(_key(_real(env)) == _key(_real(raw)) for env in roots.env[kind]):
                item = {"source": "env"}
            if item is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": f"白名单里没有这个目录：{raw}"})
            elif item["source"] == "env":
                self._json(HTTPStatus.BAD_REQUEST, {"error": "这个目录来自环境变量，请在 .env 中修改"})
            elif bench.control(("roots", "remove", kind, raw, 0, False)):
                self._json(HTTPStatus.ACCEPTED, {"accepted": True})
            else:
                self._json(HTTPStatus.CONFLICT, {"error": "等这一轮结束再改"})
            return
        stored = sum(item["source"] == "workbench" for item in items)
        try:
            preview = check_root(kind, raw, [item["path"] for item in items], stored)
        except RootRefused as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        if action == "preview":
            self._json(HTTPStatus.OK, preview.as_dict())
        elif bench.control(("roots", "add", kind, preview.path, preview.files, preview.truncated)):
            self._json(HTTPStatus.ACCEPTED, {"accepted": True, "preview": preview.as_dict()})
        else:
            self._json(HTTPStatus.CONFLICT, {"error": "等这一轮结束再改"})

    def _conversation_control(self, bench: Any, action: str, data: dict[str, Any]) -> None:
        """Switch, start or rename a conversation. Checked here, then done by the worker between turns."""
        from .conversations import clean_title
        conversation_id = data.get("conversation_id")
        if action != "new":
            if not isinstance(conversation_id, str) or not conversation_id:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "conversation_id 必须是非空字符串"})
                return
            db = self.server.connect()
            try:
                known = db.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            finally:
                db.close()
            if not known:
                self._json(HTTPStatus.NOT_FOUND, {"error": f"没有这段对话：{conversation_id}"})
                return
        if action == "rename":
            title = clean_title(data.get("title")) if isinstance(data.get("title"), str) else ""
            if not title or len(title) > RENAME_CHARS:
                self._json(HTTPStatus.BAD_REQUEST, {"error": f"标题清洗后须为 1 到 {RENAME_CHARS} 字"})
                return
            command: tuple[Any, ...] = ("rename", conversation_id, title)
        else:
            command = ("switch", conversation_id) if action == "switch" else ("new",)
        if bench.control(command):
            self._json(HTTPStatus.ACCEPTED, {"accepted": True, "next": bench.io.last_seq})
        else:
            self._json(HTTPStatus.CONFLICT, {"error": "先回答或停止当前提示，等这一轮结束再切换"})


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
