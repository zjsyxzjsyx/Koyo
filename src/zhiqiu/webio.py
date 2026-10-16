"""The web workbench's side of a conversation: a FlowIO backed by an event log, and the worker
thread that runs the chat agent.

The agent and the flows only know FlowIO (ask, say, confirm, confirm_tool), so the browser is
one more implementation of it. Everything said becomes a numbered event the page polls for;
every question blocks the worker thread until the page answers it, the user stops it, or it
times out. The agent and its database connection live on the worker thread only.
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .flows import FlowInterrupted
from .store import Store

# The longest a prompt waits for an answer; afterwards it is treated as stopped.
WAIT_S = 30 * 60
# Events kept in memory; older ones are dropped (the page reloads history from the database).
KEEP_EVENTS = 2000
# Kinds of prompt that wait for an answer, and the type of answer each accepts.
PROMPTS = {"ask": str, "confirm": bool, "approval": bool}
_STOPPED = object()
_CLOSE = object()


class WebIO:
    """FlowIO for the browser. Thread-safe: the worker thread asks, HTTP threads answer and read."""

    channel = "web"

    def __init__(self, wait_s: float = WAIT_S, keep: int = KEEP_EVENTS):
        self.wait_s = wait_s
        self.keep = keep
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._seq = 0
        self._waiting: dict[str, tuple[str, queue.Queue[Any]]] = {}
        self._closed = False

    # ----- events ----------------------------------------------------------------------

    def emit(self, kind: str, **data: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "kind": kind, "at_ns": time.time_ns(), **data}
            self._events.append(event)
            del self._events[:-self.keep]
            return event

    def events(self, after: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self._events if event["seq"] > after]

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def pending(self) -> list[dict[str, Any]]:
        """Prompts waiting for an answer, as their original events (at most one in practice)."""
        with self._lock:
            ids = set(self._waiting)
            return [dict(event) for event in self._events if event.get("request_id") in ids
                    and event["kind"] in PROMPTS]

    # ----- answering (HTTP threads) ------------------------------------------------------

    def respond(self, request_id: str, value: Any) -> None:
        with self._lock:
            waiting = self._waiting.get(request_id)
            if waiting is None:
                raise LookupError("没有这个等待中的提示，可能已经回答或已停止")
            kind, answers = waiting
            if not isinstance(value, PROMPTS[kind]):
                raise ValueError(f"这个提示需要{'文字' if PROMPTS[kind] is str else '是或否'}")
            del self._waiting[request_id]
        answers.put(value)

    def stop(self) -> int:
        """Give up on every waiting prompt: questions raise FlowInterrupted, confirmations say no."""
        with self._lock:
            waiting = list(self._waiting.values())
            self._waiting.clear()
        for _, answers in waiting:
            answers.put(_STOPPED)
        return len(waiting)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.stop()

    # ----- FlowIO (worker thread) --------------------------------------------------------

    def _wait(self, kind: str, **data: Any) -> Any:
        request_id = uuid.uuid4().hex
        answers: queue.Queue[Any] = queue.Queue(maxsize=1)
        with self._lock:
            if self._closed:
                return _STOPPED
            self._waiting[request_id] = (kind, answers)
        self.emit(kind, request_id=request_id, **data)
        try:
            answer = answers.get(timeout=self.wait_s)
            reason = "stopped" if answer is _STOPPED else "answered"
        except queue.Empty:
            with self._lock:
                self._waiting.pop(request_id, None)
            answer, reason = _STOPPED, "timeout"
        self.emit("resolved", request_id=request_id, reason=reason,
                  value=None if answer is _STOPPED else answer)
        return answer

    def say(self, text: str) -> None:
        self.emit("say", text=text)

    def ask(self, prompt: str) -> str:
        answer = self._wait("ask", prompt=prompt)
        if answer is _STOPPED:
            raise FlowInterrupted
        return answer

    def confirm(self, text: str) -> bool:
        return self._wait("confirm", text=text) is True

    def confirm_tool(self, spec: Any, args: dict[str, Any]) -> bool:
        return self.confirm_request(spec, args, None)

    def confirm_request(self, spec: Any, args: dict[str, Any], approval_request_id: str | None) -> bool:
        """confirm_tool with the id of the approval request the agent recorded for it.

        A stopped or timed-out approval raises instead of answering no: nobody decided, so the
        agent records the request as cancelled and the gate refuses the call."""
        text = spec.describe(args) if spec.describe else f"工具 {spec.name}：{args}"
        answer = self._wait("approval", tool=spec.name, permission=spec.permission, text=text,
                            approval_request_id=approval_request_id)
        if answer is _STOPPED:
            raise FlowInterrupted
        return answer is True


class Workbench:
    """Runs one chat agent on its own thread and feeds it the browser's input, one turn at a time.

    make_agent(store, io) builds the agent on the worker thread, where its Store was opened:
    a SQLite connection is never shared across threads.
    """

    def __init__(self, db_path: Path, make_agent: Callable[[Store, WebIO], Any], *,
                 clock: Any = None, resume: str | None = None, io: WebIO | None = None):
        self.db_path = Path(db_path)
        self.make_agent = make_agent
        self.clock = clock
        self.resume = resume
        self.io = io or WebIO()
        self.conversation_id: str | None = None
        # The conversation opened at start and its last message then: the page shows those messages
        # as history, and everything after them as events.
        self.started_conversation: str | None = None
        self.history_upto = 0
        # What the agent may read, write and send, for the status panel; plain values copied at start.
        self.context: dict[str, Any] = {}
        self._inputs: queue.Queue[Any] = queue.Queue()
        self._lock = threading.Lock()
        self._busy = False
        self._closed = False
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="zhiqiu-workbench", daemon=True)

    def start(self, timeout: float = 30) -> None:
        """Start the worker and wait until the conversation is open; startup errors are raised here."""
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("工作台启动超时")
        if self._error is not None:
            raise self._error

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def submit(self, text: str) -> bool:
        """Queue one input; False while a turn is still running (or after close)."""
        text = text.strip()
        if not text:
            raise ValueError("输入不能为空")
        with self._lock:
            if self._busy or self._closed or not self._ready.is_set() or self._error is not None:
                return False
            self._busy = True
        self._inputs.put(text)
        return True

    def close(self, timeout: float = 5) -> bool:
        """Stop waiting prompts, end the worker and wait for it; True when it has ended."""
        with self._lock:
            self._closed = True
        self.io.close()
        self._inputs.put(_CLOSE)
        if self._thread.is_alive():
            self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        store: Store | None = None
        try:
            store = Store(self.db_path, self.clock) if self.clock is not None else Store(self.db_path)
            agent = self.make_agent(store, self.io)
            agent.start(self.resume)
            self.conversation_id = self.started_conversation = agent.conversation_id
            messages = agent.chats.messages(self.conversation_id)
            self.history_upto = messages[-1]["seq"] if messages else 0
            policy = getattr(agent, "policy", None)
            self.context = {
                "read_roots": [str(root) for root in getattr(agent, "read_roots", [])],
                "write_roots": [str(root) for root in getattr(agent, "write_roots", [])],
                "offline": bool(getattr(agent, "offline", False)),
                "policy": policy.summary() if policy else None,
                "policy_text": policy.text() if policy else ""}
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            if store is not None:
                store.close()
            return
        self._ready.set()
        self.io.emit("idle", conversation_id=self.conversation_id)
        try:
            while True:
                text = self._inputs.get()
                if text is _CLOSE:
                    break
                self.io.emit("user", text=text)
                try:
                    agent.handle(text)
                except FlowInterrupted:
                    self.io.emit("status", text="已停止。")
                except Exception as exc:
                    # A bug in one turn must not end the workbench; the turn's trace records it.
                    self.io.emit("error", text=f"本轮出错：{type(exc).__name__}: {exc}")
                finally:
                    # /new switches conversations; the page follows the agent.
                    self.conversation_id = agent.conversation_id
                    with self._lock:
                        self._busy = False
                    self.io.emit("idle", conversation_id=self.conversation_id)
        finally:
            store.close()
