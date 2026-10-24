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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .flows import FlowInterrupted
from .plan_flow import valid_answer as plan_answer
from .store import Store

# The longest a prompt waits for an answer; afterwards it is treated as stopped.
WAIT_S = 30 * 60
# Events kept in memory; older ones are dropped (the page reloads history from the database).
KEEP_EVENTS = 2000
# Kinds of prompt that wait for an answer, and the type of answer each accepts.
PROMPTS = {"ask": str, "confirm": bool, "approval": bool, "grant": str, "plan": str}
# Where on the page an answer was given: the chat card, or the approvals page for a waiting approval.
ORIGINS = ("chat", "approvals")
# Pieces of a streamed answer are sent together: at most every this many seconds, or once this many
# characters wait, so a reply is tens of events rather than one per token.
DELTA_S = 0.05
DELTA_CHARS = 40
# A stage's "characters received" is reported at most this often.
PROGRESS_S = 1.0


@dataclass(frozen=True)
class UserInput:
    """One input from the page's box: typed, or a voice transcript the user read and sent."""
    text: str
    source: str = "text"
    voice: dict[str, Any] | None = None
# What the page can ask the worker to do besides a line of input; each runs between turns only.
CONTROLS = {"switch": 1, "new": 0, "rename": 2, "roots": 5, "compact": 0, "model": 1, "new_in": 1, "plan": 1}
_STOPPED = object()
_CLOSE = object()


class WebIO:
    """FlowIO for the browser. Thread-safe: the worker thread asks, HTTP threads answer and read."""

    channel = "web"
    say_kinds = True

    def __init__(self, wait_s: float = WAIT_S, keep: int = KEEP_EVENTS):
        self.wait_s = wait_s
        self.keep = keep
        self._lock = threading.Lock()
        # Notified on every new event and on close; the SSE stream waits on it.
        self._changed = threading.Condition(self._lock)
        self._events: list[dict[str, Any]] = []
        self._seq = 0
        # The newest seq dropped to keep the log at `keep`: a reader behind it has lost events.
        self._trimmed = 0
        self._waiting: dict[str, tuple[str, queue.Queue[Any]]] = {}
        self._origins: dict[str, str] = {}
        # How each text answer was entered (typed or a confirmed voice transcript), and the latest one.
        self._sources: dict[str, tuple[str, dict[str, Any] | None]] = {}
        self._last_source: tuple[str, dict[str, Any] | None] = ("text", None)
        self._closed = False
        # Per stream: text not sent yet, and when a delta was last sent; per stage, the last progress.
        self._drafts: dict[str, tuple[list[str], float]] = {}
        self._progressed: dict[str, float] = {}

    # ----- events ----------------------------------------------------------------------

    def emit(self, kind: str, **data: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "kind": kind, "at_ns": time.time_ns(), **data}
            self._events.append(event)
            self._trim()
            self._changed.notify_all()
            return event

    def _trim(self) -> None:
        if len(self._events) > self.keep:
            self._trimmed = self._events[-self.keep - 1]["seq"]
            del self._events[:-self.keep]

    def wait_after(self, after: int, timeout: float) -> bool:
        """Block until there is an event newer than `after` (True), or the timeout passes or the
        IO closes (False). Called by the SSE stream, off the event loop."""
        with self._lock:
            if self._seq <= after and not self._closed:
                self._changed.wait(timeout)
            return self._seq > after and not self._closed

    def lost(self, after: int) -> bool:
        """Whether a reader that saw everything up to `after` has missed events dropped from memory."""
        with self._lock:
            return after < self._trimmed

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def events(self, after: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self._events if event["seq"] > after]

    def since_switch(self, after: int = 0) -> list[dict[str, Any]]:
        """Events after `after`, but none from before the latest conversation switch: a page that
        reloads (after=0) or fell behind sees the switch and what came after it, never the old
        conversation's events."""
        with self._lock:
            switch = next((event["seq"] for event in reversed(self._events) if event["kind"] == "switched"), 0)
            start = switch - 1 if switch > after else after
            return [dict(event) for event in self._events if event["seq"] > start]

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

    def respond(self, request_id: str, value: Any, origin: str = "chat", input_source: str = "text",
                voice: dict[str, Any] | None = None) -> None:
        """Answer a waiting prompt once; whichever part of the page answers first wins."""
        if origin not in ORIGINS:
            raise ValueError(f"origin 只能是 {' 或 '.join(ORIGINS)}")
        if input_source not in ("text", "voice"):
            raise ValueError("input_source 只能是 text 或 voice")
        with self._lock:
            waiting = self._waiting.get(request_id)
            if waiting is None:
                raise LookupError("没有这个等待中的提示，可能已经回答或已停止")
            kind, answers = waiting
            if not isinstance(value, PROMPTS[kind]):
                raise ValueError(f"这个提示需要{'文字' if PROMPTS[kind] is str else '是或否'}")
            if kind == "grant" and not (value in ("once", "denied") or value.startswith("root:")):
                raise ValueError("路径授权只能回答 once、denied 或 root:目录")
            if kind == "plan" and not plan_answer(value):
                raise ValueError("学习计划卡片不认识这个回答")
            if origin == "approvals" and kind != "approval":
                raise ValueError("审批页只能回答工具审批")
            del self._waiting[request_id]
            self._origins[request_id] = origin
            if kind == "ask":
                self._sources[request_id] = (input_source, voice)
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
            self._changed.notify_all()
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
        with self._lock:
            origin = self._origins.pop(request_id, None)
            if kind == "ask":
                self._last_source = self._sources.pop(request_id, ("text", None))
        source = self._last_source[0] if kind == "ask" else "text"
        self.emit("resolved", request_id=request_id, reason=reason,
                  value=None if answer is _STOPPED else answer, origin=origin,
                  **({"input_source": source} if source != "text" else {}))
        return answer

    def last_input_source(self) -> str:
        """How the latest text answer was entered: text, or voice (a transcript the user confirmed)."""
        return self._last_source[0]

    def say(self, text: str, kind: str = "text", **meta: Any) -> None:
        """One line of output. With stream_id it is the answer that was streamed under that id: the
        page replaces the draft with it, and the log keeps it instead of the draft's deltas."""
        stream_id = meta.pop("stream_id", None)
        if stream_id:
            self._forget(stream_id)
        self.emit("say", text=text, say_kind=kind, meta=meta, **({"stream_id": stream_id} if stream_id else {}))

    # ----- streaming (worker thread) ------------------------------------------------------

    def stream(self, stream_id: str, text: str) -> None:
        now = time.monotonic()
        with self._lock:
            pending, last = self._drafts.get(stream_id, ([], 0.0))
            pending.append(text)
            if now - last < DELTA_S and sum(map(len, pending)) < DELTA_CHARS:
                self._drafts[stream_id] = (pending, last)
                return
            self._drafts[stream_id] = ([], now)
        self.emit("delta", stream_id=stream_id, text="".join(pending))

    def stream_reset(self, stream_id: str) -> None:
        """The streamed text was not the answer (the round went on to call tools): take it back."""
        self._forget(stream_id)
        self.emit("stream_reset", stream_id=stream_id)

    def _forget(self, stream_id: str) -> None:
        with self._lock:
            self._drafts.pop(stream_id, None)
            self._events = [event for event in self._events
                            if not (event["kind"] == "delta" and event.get("stream_id") == stream_id)]

    def progress(self, stage: str, chars: int) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._progressed.get(stage, -PROGRESS_S) < PROGRESS_S:
                return
            self._progressed[stage] = now
        self.emit("progress", stage=stage, chars=chars)

    def ask(self, prompt: str) -> str:
        answer = self._wait("ask", prompt=prompt)
        if answer is _STOPPED:
            raise FlowInterrupted
        return answer

    def confirm(self, text: str) -> bool:
        return self._wait("confirm", text=text) is True

    def confirm_tool(self, spec: Any, args: dict[str, Any]) -> bool:
        return self.confirm_request(spec, args, None)

    def grant_path(self, offer: dict[str, Any], approval_request_id: str | None = None) -> str:
        """A path outside the roots: the card offers once, adding one of the offered folders, or no."""
        verb = "读取" if offer["access"] == "read" else "写入"
        answer = self._wait("grant", tool=f"{offer['access']}_file", permission=f"{offer['access']}_path",
                            text=f"{verb}白名单外的文件：{offer['path']}", access=offer["access"],
                            path=offer["path"], folders=offer["folders"], preview=offer.get("preview"),
                            endpoint=offer.get("endpoint", ""), approval_request_id=approval_request_id)
        if answer is _STOPPED:
            raise FlowInterrupted
        return answer

    def plan_card(self, card: dict[str, Any]) -> str:
        """A study plan card; stopping or leaving it unanswered keeps the draft for later."""
        answer = self._wait("plan", text=f"学习计划《{card['title']}》", card=card)
        return "later" if answer is _STOPPED else answer

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
                 clock: Any = None, resume: str | None = None, io: WebIO | None = None,
                 price_updater: Callable[[], dict[str, Any]] | None = None,
                 remind_every: float | None = None, local_now: Callable[[], Any] | None = None):
        self.db_path = Path(db_path)
        self.make_agent = make_agent
        # Reads the vendor's price page on a thread of its own (with its own connection); see update_prices.
        self.price_updater = price_updater
        self.prices: dict[str, Any] = {"running": False, "error": "", "models": 0}
        # Reminders are checked every remind_every seconds on a thread of their own (None: not at all).
        self.remind_every = remind_every
        self.local_now = local_now
        self._stopped = threading.Event()
        self.clock = clock
        self.resume = resume
        self.io = io or WebIO()
        self.conversation_id: str | None = None
        # The conversation opened last (at start or by a switch) and its last message then: the page
        # shows those messages as history, and everything after them as events.
        self.started_conversation: str | None = None
        self.history_upto = 0
        # What the agent may read, write and send, for the status panel; plain values copied at start.
        self.context: dict[str, Any] = {}
        # The agent's environment roots (plain paths), so the page can list them beside the stored ones.
        self.file_roots: Any = None
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
        if self.remind_every:
            threading.Thread(target=self._remind, name="zhiqiu-reminders", daemon=True).start()

    def _remind(self) -> None:
        """Say each reminder when it is due, also during a turn; those missed while the workbench was
        closed come first, marked late. A connection of its own: the worker's never leaves its thread."""
        from .reminders import Reminders
        store = Store(self.db_path, self.clock) if self.clock is not None else Store(self.db_path)
        try:
            while not self._stopped.is_set():
                try:
                    fired = Reminders(store).fire(self.local_now() if self.local_now else None)
                except Exception as exc:  # a locked database: try again next time
                    fired = []
                    self.io.emit("status", text=f"读取提醒失败：{type(exc).__name__}")
                for row in fired:
                    self.io.emit("reminder", reminder_id=row["id"], title=row["title"], note=row["note"],
                                 repeat=row["repeat"], due_at=row["due_at"], next_at=row["next_at"], late=row["late"])
                self._stopped.wait(self.remind_every)
        finally:
            store.close()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def submit(self, text: str, input_source: str = "text", voice: dict[str, Any] | None = None) -> bool:
        """Queue one input; False while a turn is still running (or after close)."""
        text = text.strip()
        if not text:
            raise ValueError("输入不能为空")
        return self._queue(UserInput(text, input_source, voice))

    def control(self, action: tuple[Any, ...]) -> bool:
        """Queue a switch, new, rename or compact for the worker; False while busy, waiting on a prompt, or closed.

        The agent and its database connection live on the worker, so the page never changes the
        conversation itself: ("switch", id), ("new",), ("rename", id, title), ("compact",), and the user's root
        changes: ("roots", "add" | "remove", kind, path, files, truncated)."""
        if not action or action[0] not in CONTROLS or len(action) != CONTROLS[action[0]] + 1:
            raise ValueError(f"unknown workbench control: {action!r}")
        if self.io.pending():
            return False
        return self._queue(tuple(action))

    def update_prices(self) -> bool:
        """Read the price page in the background; False when there is no updater or one is running."""
        with self._lock:
            if self.price_updater is None or self.prices["running"] or self._closed:
                return False
            self.prices = {**self.prices, "running": True, "error": ""}
        threading.Thread(target=self._update_prices, name="zhiqiu-prices", daemon=True).start()
        return True

    def _update_prices(self) -> None:
        assert self.price_updater is not None
        try:
            data = self.price_updater()
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            with self._lock:
                self.prices = {**self.prices, "running": False, "error": message}
            self.io.say(f"没能更新模型价格：{message}", kind="note")
            self.io.emit("prices", ok=False)
            return
        with self._lock:
            self.prices = {"running": False, "error": "", "models": len(data.get("models") or {})}
        names = "、".join(list(data.get("models") or {})[:6])
        self.io.say(f"已从{data.get('vendor', '厂商')}价格页更新 {len(data.get('models') or {})} 个模型的价格：{names}。",
                    kind="note")
        self.io.emit("prices", ok=True)

    def _queue(self, item: Any) -> bool:
        with self._lock:
            if self._busy or self._closed or not self._ready.is_set() or self._error is not None:
                return False
            self._busy = True
        self._inputs.put(item)
        return True

    def _switched(self, conversation_id: str, agent: Any) -> None:
        """The agent opened another conversation: what it holds now is history for the page."""
        messages = agent.chats.messages(conversation_id)
        self.history_upto = messages[-1]["seq"] if messages else 0
        self.conversation_id = self.started_conversation = conversation_id
        self.io.emit("switched", conversation_id=conversation_id, history_upto=self.history_upto)

    def close(self, timeout: float = 5) -> bool:
        """Stop waiting prompts, end the worker and wait for it; True when it has ended."""
        with self._lock:
            self._closed = True
        self._stopped.set()
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
            self.file_roots = getattr(agent, "roots", None)
            self.context = {
                "offline": bool(getattr(agent, "offline", False)),
                "policy": policy.summary() if policy else None,
                "policy_text": policy.text() if policy else ""}
            self._roots_context(agent)
            self._tools_context(agent)
            self._usage_context(agent)
            self._model_context(agent)
            self._today_context(agent)
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            if store is not None:
                store.close()
            return
        # From now on every conversation the agent opens (switch, /new, a sidebar click) is followed here.
        if hasattr(agent, "on_switch"):
            agent.on_switch = lambda conversation_id: self._switched(conversation_id, agent)
        self._ready.set()
        self.io.emit("idle", conversation_id=self.conversation_id)
        try:
            while True:
                text = self._inputs.get()
                if text is _CLOSE:
                    break
                try:
                    if isinstance(text, tuple):
                        self._control(agent, text)
                    else:
                        item = text if isinstance(text, UserInput) else UserInput(str(text))
                        self.io.emit("user", text=item.text,
                                     **({"input_source": item.source} if item.source != "text" else {}))
                        agent.handle(item.text, item.source, item.voice)
                except FlowInterrupted:
                    self.io.emit("status", text="已停止。")
                except Exception as exc:
                    # A bug in one turn must not end the workbench; the turn's trace records it.
                    self.io.emit("error", text=f"本轮出错：{type(exc).__name__}: {exc}")
                finally:
                    # /new switches conversations; the page follows the agent.
                    self.conversation_id = agent.conversation_id
                    # A folder granted in the conversation became a root during the turn.
                    self._roots_context(agent)
                    self._tools_context(agent)
                    self._usage_context(agent)
                    self._model_context(agent)
                    self._today_context(agent)
                    with self._lock:
                        self._busy = False
                    self.io.emit("idle", conversation_id=self.conversation_id)
        finally:
            store.close()

    def _roots_context(self, agent: Any) -> None:
        self.context = {**self.context,
                        "read_roots": [str(root) for root in getattr(agent, "read_roots", [])],
                        "write_roots": [str(root) for root in getattr(agent, "write_roots", [])]}
        self._skills_context(agent)

    def _tools_context(self, agent: Any) -> None:
        available = getattr(agent, "tools_available", None)
        policy = getattr(agent, "policy", None)
        search = getattr(agent, "search_status", None)
        self.context = {**self.context, "tools": available() if callable(available) else True,
                        "path_grants": bool(policy is not None and policy.path_grants()),
                        "search": search() if callable(search) else None}

    def _today_context(self, agent: Any) -> None:
        """Today's study plan for the status panel; only a hint, so never an error."""
        desk = getattr(agent, "desk", None)
        try:
            from . import planner
            view = desk.plans.today(agent.store.clock.today()) if desk is not None and planner.enabled() else None
        except Exception:
            view = None
        self.context = {**self.context, "today": view}

    def _model_context(self, agent: Any) -> None:
        self.context = {**self.context, "model": str(getattr(agent, "model_name", "") or "")}

    def _usage_context(self, agent: Any) -> None:
        """How full the conversation's context is, for the status panel; only a hint, so never an error."""
        usage = getattr(agent, "context_usage", None)
        try:
            value = usage() if callable(usage) else None
        except Exception:
            value = None
        self.context = {**self.context, "usage": value}

    def _skills_context(self, agent: Any) -> None:
        """The Skills the next lesson can use and the user's broken ones, read on the worker."""
        library = getattr(getattr(agent, "service", None), "skills", None)
        if library is None:
            return
        library.refresh()
        self.context = {**self.context, "skills": {
            **library.status(), "names": [{"name": skill.name, "title": skill.title, "source": skill.source}
                                          for skill in library.all()]}}

    def _control(self, agent: Any, action: tuple[Any, ...]) -> None:
        name, *args = action
        try:
            if name == "switch":
                agent.switch(*args)
            elif name == "new":
                agent.new()
            elif name == "compact":
                agent.compact()
            elif name == "model":
                agent.use_model(*args)
            elif name == "new_in":
                agent.new(*args)
            elif name == "plan":
                agent.open_plan(*args)
            elif name == "roots":
                agent.change_roots(*args)
                self._roots_context(agent)
                self.io.emit("roots", action=args[0], root_kind=args[1], path=args[2])
            else:
                agent.rename(*args)
        except (ValueError, LookupError) as exc:
            # e.g. the conversation was forgotten from the terminal in the meantime.
            agent.io.say(str(exc), kind="note")
