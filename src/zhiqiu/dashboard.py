"""`zhiqiu dashboard`: a local, read-only web view of traces, metrics and learning records.

One standard-library HTTP server, bound to 127.0.0.1, that opens the database
read-only for each request (the terminal may be writing at the same time). It has
no write endpoint at all; every response goes through value-based redaction.
"""
from __future__ import annotations

import json
import sqlite3
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import contracts, metrics, pricing, traces
from .queries import open_readonly, trace as attempt_trace
from .redact import redact_obj

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
BUSY_MS = 2000


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


ROUTES: dict[str, Callable[[sqlite3.Connection, dict[str, list[str]]], dict[str, Any]]] = {
    "/api/live": live,
    "/api/traces": trace_list,
    "/api/metrics": lambda db, query: metrics.collect(db, _number(query, "days", 7, 0.01, 3650)),
    "/api/failures": lambda db, query: contracts.failures(db, _number(query, "days", 7, 0.01, 3650)),
    "/api/check": lambda db, query: contracts.check(db, _number(query, "days", 7, 0.01, 3650)),
    "/api/attempts": attempt_list,
    "/api/summary": lambda db, query: {**traces.summary(db, _number(query, "days", 7, 0.01, 3650)),
                                       "failures": contracts.failures(db, _number(query, "days", 7, 0.01, 3650))["total"]},
}
DETAIL_ROUTES: dict[str, Callable[[sqlite3.Connection, str], dict[str, Any]]] = {
    "/api/trace/": trace_detail,
    "/api/attempt/": attempt_detail,
    "/api/llm/": traces.llm_detail,
}


def page() -> bytes:
    return resources.files("zhiqiu").joinpath("dashboard.html").read_bytes()


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, db_path: Path, port: int = DEFAULT_PORT):
        self.db_path = Path(db_path)
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
            self._send(HTTPStatus.OK, page(), "text/html; charset=utf-8")
            return
        query = parse_qs(parts.query)
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

    do_POST = do_PUT = do_PATCH = do_DELETE = _refuse  # type: ignore[assignment]


def serve(db_path: Path, port: int = DEFAULT_PORT, on_ready: Callable[[str], None] | None = None) -> None:
    server = DashboardServer(db_path, port)
    url = f"http://{HOST}:{server.port}/"
    if on_ready:
        on_ready(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
