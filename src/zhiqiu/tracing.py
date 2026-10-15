"""Trace: one tree of spans per command or chat turn, kept in SQLite.

A span is written when it starts (status "running") and updated when it ends,
so a viewer such as `zhiqiu dashboard` can show which step is running right now.
The current span lives in a context variable; the Store's audit writers read it
to attach model calls, tool calls, events and context builds to the tree, so no
caller has to pass trace ids around.

Tracing never decides anything and must never break a flow: every write is
best-effort and a failed write only loses that record.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

LINKS = ("kp_id", "attempt_id", "session_id", "conversation_id", "point")
KINDS = ("command", "turn", "flow", "loop", "stage")
# A running span older than this whose process cannot be checked is treated as abandoned.
STALE_NS = 6 * 3600 * 10**9

_current: ContextVar["Span | None"] = ContextVar("zhiqiu_span", default=None)


def now_ns() -> int:
    return time.time_ns()


def _hex(size: int) -> str:
    return os.urandom(size).hex()


def _execute(store: Any, sql: str, params: tuple[Any, ...]) -> None:
    # Outside a transaction the connection autocommits, so the row is visible at once;
    # inside one it is committed (or rolled back) with the caller's work.
    try:
        store.db.execute(sql, params)
    except sqlite3.Error:
        pass


@dataclass
class Span:
    store: Any
    trace_id: str
    span_id: str
    parent_span_id: str | None
    kind: str
    name: str
    links: dict[str, Any]
    started_ns: int
    progress_data: dict[str, Any] = field(default_factory=dict)
    attrs: dict[str, Any] = field(default_factory=dict)
    end_reason: str | None = None

    def progress(self, **values: Any) -> None:
        """Record where this span is right now, e.g. progress(step="出题", question=3)."""
        self.progress_data.update(values)
        _execute(self.store, "UPDATE spans SET progress_json=? WHERE span_id=?",
                 (json.dumps(self.progress_data, ensure_ascii=False, default=str), self.span_id))

    def link(self, **links: Any) -> None:
        """Attach ids learned after the span started, such as the knowledge point a lesson created."""
        changed = {key: value for key, value in links.items() if key in LINKS and value is not None}
        if not changed:
            return
        self.links.update(changed)
        assignments = ",".join(f"{key}=?" for key in changed)
        _execute(self.store, f"UPDATE spans SET {assignments} WHERE span_id=?",
                 (*changed.values(), self.span_id))

    def set(self, end_reason: str | None = None, **attrs: Any) -> None:
        if end_reason is not None:
            self.end_reason = end_reason
        if attrs:
            self.attrs.update(attrs)
            _execute(self.store, "UPDATE spans SET attrs_json=? WHERE span_id=?",
                     (json.dumps(self.attrs, ensure_ascii=False, default=str), self.span_id))


def current() -> Span | None:
    return _current.get()


def columns(store: Any) -> tuple[str | None, str | None]:
    """(trace_id, parent_span_id) for a record written to this store now."""
    span = _current.get()
    if span is None or span.store is not store:
        return None, None
    return span.trace_id, span.span_id


def progress(**values: Any) -> None:
    span = _current.get()
    if span is not None:
        span.progress(**values)


def link(**links: Any) -> None:
    span = _current.get()
    if span is not None:
        span.link(**links)


def _status_for(exc: BaseException) -> tuple[str, str | None]:
    if isinstance(exc, KeyboardInterrupt) or type(exc).__name__ == "FlowInterrupted":
        return "interrupted", None
    if getattr(exc, "exit_code", None) == 0:
        return "ok", None
    return "error", type(exc).__name__


@contextmanager
def span(store: Any, kind: str, name: str, **links: Any) -> Iterator[Span]:
    """Open a span under the current one (or a new trace) and close it on exit.

    Links not given are inherited from the parent, so a model call deep inside a lesson
    still carries the knowledge point. Exceptions pass through after being recorded.
    """
    parent = _current.get()
    if parent is not None and parent.store is not store:
        parent = None
    inherited = {key: parent.links.get(key) for key in LINKS} if parent else {}
    inherited.update({key: value for key, value in links.items() if value is not None})
    item = Span(store, parent.trace_id if parent else _hex(16), _hex(8),
                parent.span_id if parent else None, kind, name, inherited, now_ns(),
                attrs={"pid": os.getpid()})
    _execute(store, """INSERT INTO spans(span_id,trace_id,parent_span_id,kind,name,started_at_ns,status,
        kp_id,attempt_id,session_id,conversation_id,point,attrs_json) VALUES(?,?,?,?,?,?,'running',?,?,?,?,?,?)""",
             (item.span_id, item.trace_id, item.parent_span_id, kind, name, item.started_ns,
              *(inherited.get(key) for key in LINKS[:-1]), inherited.get("point") or "",
              json.dumps(item.attrs)))
    token = _current.set(item)
    began = time.perf_counter_ns()
    status, error_kind = "ok", None
    try:
        yield item
    except BaseException as exc:
        status, error_kind = _status_for(exc)
        raise
    finally:
        _current.reset(token)
        _execute(store, """UPDATE spans SET status=?,duration_ms=?,end_reason=?,error_kind=? WHERE span_id=?""",
                 (status, (time.perf_counter_ns() - began) // 10**6, item.end_reason, error_kind, item.span_id))


def _alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) would send CTRL_C_EVENT on Windows, so ask the kernel instead.
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def abandoned(row: Any, now: int | None = None) -> bool:
    """A running span whose process is gone (or that is too old to still be running)."""
    if row["status"] != "running":
        return False
    try:
        pid = int(json.loads(row["attrs_json"] or "{}").get("pid", 0))
    except (TypeError, ValueError):
        pid = 0
    if (now or now_ns()) - row["started_at_ns"] > STALE_NS and pid != os.getpid():
        return True
    return not _alive(pid)


def reconcile(store: Any) -> int:
    """Mark spans left running by a process that died as interrupted; returns how many."""
    try:
        rows = store.db.execute("SELECT span_id,status,started_at_ns,attrs_json FROM spans WHERE status='running'").fetchall()
    except sqlite3.Error:
        return 0
    stale = [row["span_id"] for row in rows if abandoned(row)]
    for span_id in stale:
        _execute(store, "UPDATE spans SET status='interrupted',end_reason='process_exited' WHERE span_id=?", (span_id,))
    return len(stale)
