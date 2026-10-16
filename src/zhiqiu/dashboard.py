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
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import contracts, guardrails, metrics, pricing, traces
from .clock import Clock
from .queries import open_readonly, trace as attempt_trace
from .redact import redact_obj

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
BUSY_MS = 2000
# The largest workbench request body (a chat message or an answer).
MAX_BODY = 64 * 1024
TOKEN_HEADER = "X-Zhiqiu-Token"


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
    kp = db.execute("SELECT id,title,state FROM knowledge_points WHERE id=?", (data["attempt"]["kp_id"],)).fetchone()
    task_ids = [task["id"] for task in data["tasks"]]
    states = [dict(row) for row in db.execute("""SELECT id,payload_json,created_at,trace_id FROM events
        WHERE type='StateChanged' AND (attempt_id=? OR json_extract(payload_json,'$.id') IN
        (SELECT value FROM json_each(?))) ORDER BY id""", (attempt_id, json.dumps([attempt_id, *task_ids])))]
    for row in states:
        row["payload"] = json.loads(row.pop("payload_json"))
    notes = [dict(row) for row in db.execute("""SELECT kind,text,explanation,created_at FROM notes
        WHERE attempt_id=? ORDER BY rowid""", (attempt_id,))]
    keep = ("attempt", "questions", "review", "dimension_scores", "question_assessments", "review_evidence", "tasks")
    return {**{key: data[key] for key in keep}, "knowledge_point": dict(kp) if kp else None,
            "state_events": states, "notes": notes, "trace_ids": traces.resolve(db, attempt_id)}


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
    "/api/summary": lambda db, query: {**traces.summary(db, _number(query, "days", 7, 0.01, 3650)),
                                       "failures": contracts.failures(db, _number(query, "days", 7, 0.01, 3650))["total"],
                                       "approvals_pending": db.execute("""SELECT COUNT(*) FROM approval_requests
                                           WHERE status='pending'""").fetchone()[0]},
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
    return {"events": bench.io.events(after), "next": bench.io.last_seq, "busy": bench.busy,
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
                 "pending": [{"kind": item["kind"], "tool": item.get("tool"),
                              "text": (item.get("text") or item.get("prompt") or "").split("\n")[0]} for item in pending],
                 "recent": approvals},
        "result": result,
        "verification": verification,
    }


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
    """Messages the conversation already had when the workbench started (later ones arrive as events)."""
    bench = server.workbench
    if not bench.started_conversation or not bench.history_upto:
        return {"conversation_id": bench.started_conversation, "messages": []}
    db = server.connect()
    try:
        rows = db.execute(f"""SELECT seq,role,kind,content,created_at FROM conversation_messages
            WHERE conversation_id=? AND seq<=? AND kind IN ({",".join("?" * len(HISTORY_KINDS))}) ORDER BY seq""",
                          (bench.started_conversation, bench.history_upto, *HISTORY_KINDS)).fetchall()
    finally:
        db.close()
    return {"conversation_id": bench.started_conversation, "messages": [dict(row) for row in rows]}


CHAT_GETS: dict[str, Callable[["DashboardServer", dict[str, list[str]]], dict[str, Any]]] = {
    "/api/chat/events": chat_events,
    "/api/chat/state": chat_state,
    "/api/chat/history": chat_history,
}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, db_path: Path, port: int = DEFAULT_PORT, workbench: Any = None):
        self.db_path = Path(db_path)
        # With a workbench the server also serves /api/chat/*; the token proves a request came from our page.
        self.workbench = workbench
        self.token = secrets.token_urlsafe(32) if workbench is not None else None
        super().__init__((HOST, port), Handler)

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
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
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
        query = parse_qs(parts.query)
        if parts.path in CHAT_GETS and self.server.workbench is not None:
            # Chat events are conversation content: only our page, which holds the token, may read them.
            if not self._token_ok():
                self._json(HTTPStatus.FORBIDDEN, {"error": "缺少或错误的工作台令牌"})
                return
            try:
                self._json(HTTPStatus.OK, CHAT_GETS[parts.path](self.server, query))
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

    def _body(self) -> dict[str, Any] | None:
        """The JSON object sent, after every workbench check; None when the request was refused."""
        if not self._host_ok():
            self._deny(HTTPStatus.FORBIDDEN, "只接受来自本机地址的请求")
        elif not self._token_ok():
            self._deny(HTTPStatus.FORBIDDEN, "缺少或错误的工作台令牌")
        elif not self._origin_ok():
            self._deny(HTTPStatus.FORBIDDEN, "请求来自其他网站")
        elif self.headers.get_content_type() != "application/json":
            self._deny(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "只接受 application/json")
        else:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self._deny(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"请求体超过 {MAX_BODY} 字节或长度无效")
                return None
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict):
                return data
            self._json(HTTPStatus.BAD_REQUEST, {"error": "请求体必须是 JSON 对象"})
        return None

    def do_POST(self) -> None:
        bench = self.server.workbench
        if bench is None:
            self._refuse()
            return
        data = self._body()
        if data is None:
            return
        path = urlsplit(self.path).path
        if path == "/api/chat/input":
            text = data.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_BODY:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "text 必须是非空字符串"})
            elif bench.submit(text):
                self._json(HTTPStatus.ACCEPTED, {"accepted": True, "next": bench.io.last_seq})
            else:
                self._json(HTTPStatus.CONFLICT, {"error": "上一轮还没结束"})
        elif path == "/api/chat/respond":
            try:
                bench.io.respond(str(data.get("request_id", "")), data.get("value"))
            except LookupError as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            else:
                self._json(HTTPStatus.OK, {"answered": True})
        elif path == "/api/chat/stop":
            self._json(HTTPStatus.OK, {"stopped": bench.io.stop()})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "没有这个接口"})


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
