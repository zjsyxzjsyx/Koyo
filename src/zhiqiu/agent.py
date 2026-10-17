"""`zhiqiu chat`: a free conversation (ReAct) around the learning flows.

The model may answer, call tools within the chat scope, or *propose* entering a
flow or remembering a preference. Proposals only end the tool loop; the program
shows them, asks the user, and only then switches the conversation mode and runs
the flow. Grading, scheduling and state changes stay with the program.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Sequence

import httpx

from . import guardrails, tracing
from . import history as history_search
from .approvals import ApprovalPolicy
from .context import ContextBudgetError, ContextTask
from .conversations import Conversations, clean_title
from .files import ADD_ROOT_HINT, FileRoots, read_root_file_spec
from .flow import IllegalTransition
from .flows import (MIN_PREFIX, FlowInterrupted, FlowIO, change_skill, describe_skills, outbound_lines, preapproved,
                    resolve_prefix, skills_of,
                    resume_lesson, resume_target, run_lesson, run_quiz, say, short_id, show_outcome,
                    show_outcome_detail, summarize_outcome)
from .materials import Material
from .memory import MemoryService
from .model import ModelContextOverflowError
from .queries import status as status_query
from .redact import redact
from .store import DAILY_REVIEW_LIMIT, Store
from .toolloop import LOOP_SECONDS, MAX_ROUNDS, Draft, LoopResult, ToolLoop, query_leak_check
from .tools import Approval, ToolError, ToolExecutor, ToolRegistry, ToolScope, ToolSpec, summarize_args

CHAT_PERMISSIONS = frozenset({"read", "network", "write_file", "external_mcp", "propose"})
PROPOSE_TOOLS = frozenset({"propose_learn", "propose_review", "propose_resume", "propose_memory"})
TRACED_COMMANDS = frozenset({"/learn", "/review", "/resume"})
MEMORY_PREFIXES = ("pref.", "soul.")
MEMORY_ITEM_CHARS = 300
MEMORY_TOTAL_CHARS = 2000
# Once the user allows one of these tools in a turn, its later calls in the same turn run without asking again.
TURN_APPROVAL_PERMISSIONS = frozenset({"network"})
# How the context describes network access under each approval decision for the network class.
NETWORK_TEXT = {"allow": "可用，自动执行", "ask": "可用，每次需用户批准", "deny": "已被审批策略禁止"}
# Unsummarized history beyond this count is summarized when no input budget is configured.
HISTORY_MESSAGES = 40
# With a budget, summarize once unsummarized history pushes the request past this share of it.
SUMMARY_TRIGGER = 0.7
# The newest messages always stay verbatim.
KEEP_RECENT = 10
CLOSING = "不要再调用工具，直接用文字给出本轮的回答。"
HELP = ("对话命令：/status 查看状态；/learn 知识点 [--from 路径] [--url 网址] [--notion 页面]；"
        "/review [知识点]；/resume [短号]；/detail [短号] 查看评阅详情；/chats 列出最近的对话；/open 编号或短号 切换对话；"
        "/title 新标题 给对话改名；/memory 查看记忆；/skill 查看讲法，/skill 名称 知识点 指定讲法；/new 开新对话。"
        "其他输入直接与知秋对话。")
# How many conversations /chats lists.
CHATS_LISTED = 10
# A title the model gives is kept when it is this long (the prompt asks for at most 12); a user's up to 30.
MODEL_TITLE_CHARS = (2, 16)
USER_TITLE_CHARS = 30
# What the title stage sees of the first message and the first reply.
TITLE_INPUT_CHARS = 500
# A conversation opened by one of these commands is named after it, without a model call.
RULE_TITLES = {"/learn": "学习：{}", "/review": "复习：{}", "/resume": "继续：{}"}
_REASON = {"type": "string", "maxLength": 300}
_STRINGS = {"type": "array", "maxItems": 5, "items": {"type": "string", "minLength": 1, "maxLength": 1000}}


class ChatIO:
    """Wraps the user's IO; flows report their stage here so the conversation mode follows them."""

    say_kinds = True

    def __init__(self, inner: FlowIO, on_stage: Callable[[str, str, str], None]):
        self.inner = inner
        self.on_stage = on_stage

    def ask(self, prompt: str) -> str:
        return self.inner.ask(prompt)

    def say(self, text: str, kind: str = "text", **meta: Any) -> None:
        say(self.inner, text, kind, **meta)

    def confirm(self, text: str) -> bool:
        return self.inner.confirm(text)

    def last_input_source(self) -> str:
        source = getattr(self.inner, "last_input_source", None)
        return source() if callable(source) else "text"

    def confirm_tool(self, spec: Any, args: dict[str, Any], request_id: str | None = None) -> bool:
        # An IO that shows approvals as records of their own (the web workbench) gets the request id.
        hook = getattr(self.inner, "confirm_request", None)
        if request_id and callable(hook):
            return hook(spec, args, request_id)
        return self.inner.confirm_tool(spec, args)

    @property
    def channel(self) -> str:
        """Which interface answers prompts ("terminal" or "web"), recorded with each approval."""
        return getattr(self.inner, "channel", "terminal")

    def stage(self, mode: str, active_kind: str, active_id: str) -> None:
        self.on_stage(mode, active_kind, active_id)

    # ----- streaming (an IO that can show text as it arrives: the workbench, the terminal) -----

    @property
    def streams(self) -> bool:
        return callable(getattr(self.inner, "stream", None)) and callable(getattr(self.inner, "stream_reset", None))

    def stream(self, stream_id: str, text: str) -> None:
        self.inner.stream(stream_id, text)

    def stream_reset(self, stream_id: str) -> None:
        self.inner.stream_reset(stream_id)

    def progress(self, stage: str, chars: int) -> None:
        hook = getattr(self.inner, "progress", None)
        if callable(hook):
            hook(stage, chars)


def _describe_external(spec: ToolSpec) -> Callable[[dict[str, Any]], str]:
    def describe(args: dict[str, Any]) -> str:
        if spec.name == "web_search":
            return f"联网搜索（查询发往外部搜索服务）：{args.get('query', '')}"
        if spec.name == "fetch_url":
            return f"获取网页正文：{args.get('url', '')}"
        shown = json.dumps(args, ensure_ascii=False)
        return f"调用外部工具 {spec.name}（{spec.source}）：{shown[:500]}"
    return describe


def _proposal_spec(name: str, description: str, params: dict[str, Any],
                   precheck: Callable[[dict[str, Any]], None] | None = None) -> ToolSpec:
    # The handler only echoes the validated suggestion; nothing happens until the user agrees.
    return ToolSpec(name, description, params, "propose", lambda args: {"proposal": name, **args},
                    source="zhiqiu.agent", precheck=precheck)


def _split_command(text: str) -> list[str]:
    """Split a slash command like a shell would, keeping Windows backslashes in paths."""
    if os.name != "nt":
        return shlex.split(text)
    quoted = ('"', "'")
    return [part[1:-1] if len(part) >= 2 and part[0] == part[-1] and part[0] in quoted else part
            for part in shlex.split(text, posix=False)]


def _command_parts(text: str) -> list[str]:
    """/review takes the rest of the line as the point's title, which may hold quotes or runs of spaces
    (the schedule page sends titles as they are); other commands split like a shell."""
    head, *tail = text.split(None, 1)
    if head == "/review":
        point = _unquote(tail[0]) if tail else ""
        return [head, point] if point else [head]
    if head == "/skill":
        # /skill [名称|auto [标题]]: the title is the rest of the line, as for /review.
        name, _, title = (tail[0].strip() if tail else "").partition(" ")
        return [head] + ([name] if name else []) + ([_unquote(title)] if _unquote(title) else [])
    return _split_command(text)


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


class _Runner:
    """A flow to run once the user accepts a proposal; `notice` is shown with the proposal before asking."""

    def __init__(self, run: Callable[[], Any], notice: Sequence[str] = ()):
        self.run, self.notice = run, list(notice)

    def __call__(self) -> Any:
        return self.run()


class ChatAgent:
    def __init__(self, store: Store, service: Any, io: FlowIO, *, read_roots: Sequence[Path] = (),
                 write_roots: Sequence[Path] = (), offline: bool = False,
                 max_rounds: int = MAX_ROUNDS, deadline_s: float = LOOP_SECONDS,
                 policy: ApprovalPolicy | None = None):
        self.store = store
        self.service = service
        # How the current input was entered (empty when typed); see handle.
        self._input: dict[str, Any] = {}
        self.chats = Conversations(store)
        self.memory = MemoryService(store)
        # The environment's roots plus those added on the workbench page, taken again at every input.
        self.roots = FileRoots(read_roots, write_roots)
        self.read_roots: list[Path] = []
        self.write_roots: list[Path] = []
        self.offline = offline
        self.max_rounds = max_rounds
        self.deadline_s = deadline_s
        # Which governed tools run without asking, need the user's approval, or are withheld.
        self.policy = policy or ApprovalPolicy()
        self.io = ChatIO(io, self._on_stage)
        # JSON stages (lessons, questions, grading) report how much of their reply has arrived.
        if hasattr(service, "progress") and callable(getattr(io, "progress", None)):
            service.progress = self.io.progress
        # Private text read in this run; outgoing tool arguments may not copy it.
        self.private: list[Material] = []
        self.conversation: dict[str, Any] | None = None
        self._goal = ""
        # The knowledge point the last planned flow is about (for a rule title), and whether the
        # input being handled is the conversation's first.
        self._point = ""
        self._first = False
        # The conversations the last /chats listed; its numbers are good for the very next input only.
        self._listed: list[str] | None = None
        # Called with the conversation id whenever this agent opens another conversation (the workbench
        # follows it there).
        self.on_switch: Callable[[str], None] | None = None
        # Tools the user allowed during the current turn (see TURN_APPROVAL_PERMISSIONS).
        self._approved: set[str] = set()
        self.registry = ToolRegistry()
        for spec in service.tools.specs():
            if spec.name in ("read_file", "notion_page") or not spec.in_loops:
                continue
            if spec.permission in ("network", "external_mcp"):
                # Every outgoing call passes the gate, where _confirm_tool applies the approval policy.
                spec = replace(spec, requires_confirmation=True, describe=_describe_external(spec))
            self.registry.register(spec)
        self.refresh_roots()
        self.registry.register(read_root_file_spec(lambda: self.read_roots, self.private.append))
        if history_search.available(store.db):
            self.registry.register(history_search.tool_spec(
                store, lambda: self.conversation["id"] if self.conversation else None))
        for spec in self._proposal_specs():
            self.registry.register(spec)
        # The approval request opened for the call being gated; its audit row links back to it.
        self._asked: str | None = None
        self.executor = ToolExecutor(self.registry, audit=self._audit)
        self.policy_warnings = self.policy.check(self.registry.specs())

    # ----- conversation lifecycle -------------------------------------------------

    @property
    def conversation_id(self) -> str:
        assert self.conversation is not None, "call start() first"
        return self.conversation["id"]

    def _snapshot(self) -> list[dict[str, str]]:
        return [{"key": item["key"], "content": item["content"]} for item in self.memory.list_semantic(None)]

    def start(self, resume_id: str | None = None) -> dict[str, Any]:
        self._open(self.chats.reopen(resume_id) if resume_id
                   else self.chats.open_today(self._snapshot(), self._status()))
        for warning in self.policy_warnings:
            self.io.say(warning, kind="note")
        state = self.store.status()
        if state["open_attempts"] or self.store.pending_lessons():
            self.io.say("有未完成的自测或讲解：可以直接说“继续”，或输入 /resume。", kind="note")
        count = len(self.chats.messages(self.conversation_id))
        self.io.say((f"接着「{self._title()}」（{count} 条消息）" if count else "新对话") + "。输入 /help 查看命令。",
                    kind="note")
        return self.conversation

    def _open(self, conversation: dict[str, Any]) -> None:
        """Make a conversation the current one: at start-up, for /new and when switching."""
        self.conversation = conversation
        self.chats.reconcile(self.conversation_id)
        # Nobody is waiting on a request left pending by an earlier run of this conversation.
        self.store.cancel_stale_approvals(self.conversation_id)
        # The status in the prompt is a snapshot; refreshing it only here and after flows keeps
        # the request prefix stable in between.
        self.chats.set_status_snapshot(self.conversation_id, self._status())
        self.conversation = self.chats.get(self.conversation_id)
        # Private text read in another conversation is not this one's to guard.
        self.private.clear()
        if self.on_switch is not None:
            self.on_switch(self.conversation_id)

    def _title(self, conversation_id: str | None = None) -> str:
        return self.chats.display_title(self.chats.get(conversation_id or self.conversation_id))

    def switch(self, conversation_id: str) -> None:
        """Continue another conversation; only between turns (the workbench and /open make sure)."""
        if conversation_id == self.conversation_id:
            self.io.say(f"已经在「{self._title()}」里了。", kind="note")
            return
        self._open(self.chats.reopen(conversation_id))
        count = len(self.chats.messages(self.conversation_id))
        self.io.say(f"已切换到「{self._title()}」（{count} 条消息）。", kind="note")

    def new(self) -> None:
        self._open(self.chats.start_new(self._snapshot(), self._status()))
        self.io.say("已开始新对话。", kind="note")

    def rename(self, conversation_id: str, title: str) -> str:
        """A user's title for any conversation (the workbench sidebar renames rows other than the current)."""
        cleaned = clean_title(title)
        if not cleaned:
            raise ValueError("用法：/title 新标题")
        if len(cleaned) > USER_TITLE_CHARS:
            raise ValueError(f"标题最多 {USER_TITLE_CHARS} 字（现在 {len(cleaned)} 字）")
        return self.chats.set_title(conversation_id, cleaned, "user")

    def run(self) -> None:
        while True:
            try:
                text = self.io.ask("你：").strip()
            except FlowInterrupted:
                self.io.say("对话已保存；再次运行 zhiqiu 可以接着聊。")
                return
            if not text:
                continue
            if text in ("/exit", "/quit"):
                self.io.say("对话已保存。")
                return
            self.handle(text)

    def refresh_roots(self) -> None:
        """Directories added or removed on the workbench page apply from the next input, never mid-turn."""
        self.read_roots = self.roots.current(self.store, "read")
        self.write_roots = self.roots.current(self.store, "write")
        refresh = getattr(self.service, "refresh_roots", None)
        if callable(refresh):
            refresh()

    def change_roots(self, action: str, kind: str, path: str, files: int = 0, truncated: bool = False) -> None:
        """Add or remove a root the user chose on the workbench page. Runs between turns on the worker;
        no tool reaches it, so the model cannot widen what it may read or write."""
        name = {"read": "可读", "write": "可写"}[kind]
        if action == "add":
            self.store.add_root(kind, path, files, truncated)
            self.refresh_roots()
            self.io.say(f"已添加{name}目录：{path}（下一句起生效）", kind="note")
        elif action == "remove":
            self.store.remove_root(kind, path)
            self.refresh_roots()
            self.io.say(f"已移除{name}目录：{path}", kind="note")
        else:
            raise ValueError(f"unknown roots action: {action}")

    def handle(self, text: str, input_source: str = "text", voice: dict[str, Any] | None = None) -> None:
        """One user input. Each chat turn, and each slash command that runs a flow, is one trace.

        input_source says whether the text was typed or is a transcript the user confirmed (voice)."""
        self._input = {"input_source": input_source, **({"voice": voice} if voice else {})} if input_source != "text" else {}
        self.refresh_roots()
        # A command that opens the conversation gives it its title.
        self._first = not self.chats.messages(self.conversation_id)
        # /chats numbers hold for the next input only.
        self._offered, self._listed = self._listed, None
        if text.startswith("/"):
            name = text.split()[0]
            if name not in TRACED_COMMANDS:
                self._command(text)
                return
            with tracing.span(self.store, "turn", name, conversation_id=self.conversation_id):
                self._command(text)
            return
        with tracing.span(self.store, "turn", "chat", conversation_id=self.conversation_id) as span:
            span.set(approval_policy=self.policy.fingerprint(), **self._input)
            span.set(end_reason=self._turn(text))

    def _turn(self, text: str) -> str:
        self.chats.add_message(self.conversation_id, "user", "chat", text, self._input or None)
        # The answer is shown as it streams in when the IO can; whatever ends the turn without an
        # answer withdraws what was shown.
        draft = Draft(self.io) if self.io.streams else None
        try:
            result = self._think(text, draft)
        except KeyboardInterrupt:
            if draft is not None:
                draft.withdraw()
            self.chats.add_message(self.conversation_id, "assistant", "error", "已取消")
            self.io.say("已取消本轮。")
            return "cancelled"
        except (ContextBudgetError, ModelContextOverflowError, ValueError, httpx.HTTPError) as exc:
            if draft is not None:
                draft.withdraw()
            self._fail(f"本轮未完成：{exc}")
            return "error"
        if result.status == "proposal" and result.stopped_by:
            if draft is not None:
                draft.withdraw()
            self._proposal(*result.stopped_by, loop=result)
            return "proposal"
        if not result.text:
            if draft is not None:
                draft.withdraw()
            self._fail(f"本轮未完成：{result.reason}，模型没有给出回答。")
            return "no_answer"
        self.chats.add_message(self.conversation_id, "assistant", "chat", result.text,
                               {"status": result.status, "rounds": result.rounds, "loop_id": result.loop_id})
        self.io.say(result.text, **({"stream_id": draft.stream_id} if draft is not None and draft.shown else {}))
        if not self.chats.get(self.conversation_id)["title_source"]:
            self._auto_title(text, result.text)
        return result.status

    def _auto_title(self, text: str, reply: str) -> None:
        """Name the conversation after its first answered turn, once. The reply is already shown; a
        failure only leaves a record and the first message as the title, never a message to the user."""
        model = self.service.model
        legacy = getattr(model, "conversation_title", None)
        title = ""
        if callable(getattr(model, "request", None)) or callable(legacy):
            task = ContextTask("conversation_title", "", current_input=json.dumps(
                {"message": redact(text[:TITLE_INPUT_CHARS]), "reply": redact(reply[:TITLE_INPUT_CHARS])},
                ensure_ascii=False))
            try:
                result = self.service._invoke(task, lambda: legacy(text, reply))
            except Exception as exc:
                if type(exc) is ValueError:
                    # The runtime already kept the unparsable output; link it to this conversation so
                    # forgetting the conversation removes it.
                    with self.store.transaction():
                        self.store.db.execute("""UPDATE llm_failures SET conversation_id=? WHERE trace_id IS ?
                            AND stage='conversation_title' AND conversation_id IS NULL""",
                                              (self.conversation_id, tracing.columns(self.store)[0]))
                else:
                    self.store.record_llm_failure("conversation_title", f"{type(exc).__name__}: {exc}", "",
                                                  conversation_id=self.conversation_id)
            else:
                value = result.get("title") if isinstance(result, dict) else None
                title = clean_title(value) if isinstance(value, str) else ""
                low, high = MODEL_TITLE_CHARS
                if not low <= len(title) <= high:
                    build_id = getattr(self.service, "last_build_id", None)
                    self.service.mark_invalid_output(build_id)
                    self.store.record_llm_failure(
                        "conversation_title", f"标题不合格：清洗后 {len(title)} 字，应为 {low} 到 {high} 字",
                        json.dumps(result, ensure_ascii=False, default=str), context_build_id=build_id,
                        conversation_id=self.conversation_id)
                    title = ""
        if title:
            self.chats.set_title(self.conversation_id, title, "model")
        else:
            self.chats.set_title(self.conversation_id, self.chats.fallback_title(self.conversation_id), "fallback")

    def _rule_title(self, name: str) -> None:
        """The title of a conversation opened by /learn, /review or /resume, set before the flow runs."""
        if self._first and self._point and not self.chats.get(self.conversation_id)["title_source"]:
            self.chats.set_title(self.conversation_id, RULE_TITLES[name].format(self._point), "rule")

    def _fail(self, message: str) -> None:
        self.chats.set_error(self.conversation_id, message)
        self.chats.add_message(self.conversation_id, "assistant", "error", message)
        self.io.say(message)

    # ----- one model turn ---------------------------------------------------------

    def _status(self) -> dict[str, Any]:
        state = status_query(self.store.db, self.store.clock.today().isoformat())
        return {
            "due": [{"title": t["title"], "task_id": t["id"], "due": t["original_due_date"]} for t in state["due"]],
            "open_attempts": [{"title": a["title"], "attempt_id": a["id"], "kind": a["kind"], "state": a["state"]}
                              for a in state["open_attempts"]],
            "pending_lessons": [{"title": row["title"], "session_id": row["session_id"]}
                                for row in self.store.pending_lessons()],
            "needs_relearn": [p["title"] for p in state["needs_relearn"]]}

    def _context(self) -> str:
        today = self.store.clock.today().isoformat()
        conversation = self.chats.get(self.conversation_id)
        status = conversation["status_snapshot_json"]
        snapshot = json.loads(conversation["memory_snapshot_json"])
        memory = "\n".join(f"- {item['key']}: {item['content']}" for item in snapshot) or "（无）"
        readable = "；".join(str(root) for root in self.read_roots) or "（未配置，不能读文件）"
        writable = ("；".join(str(root) for root in self.write_roots) + f"（写入{self.policy.describe('write_file')}）"
                    if self.write_roots else "（未配置，不能写文件）")
        network = "已关闭（--offline）" if self.offline else NETWORK_TEXT[self.policy.permissions["network"]]
        return (f"今天：{today}\n学习状态（快照，最新状态请调用 zhiqiu_status）：{status}\n"
                f"偏好与人设记忆（本段对话开始时的快照）：\n{memory}\n"
                f"可读目录：{readable}\n可写目录：{writable}\n联网：{network}")

    def _history(self, summary: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Messages after the latest summary, without the current input; oldest first."""
        after = summary["upto_seq"] if summary else 0
        items = []
        for message in self.chats.messages(self.conversation_id)[:-1]:
            if message["seq"] <= after:
                continue
            if message["kind"] == "chat":
                items.append({"id": message["id"], "seq": message["seq"], "role": message["role"],
                              "content": message["content"]})
            elif message["kind"] in ("proposal", "memory_proposal", "flow_result"):
                status = message["payload"].get("status")
                note = f"（{message['content']}" + (f"；结果：{status}）" if status else "）")
                items.append({"id": message["id"], "seq": message["seq"], "role": "assistant", "content": note})
        return items

    def _confirm_tool(self, spec: ToolSpec, args: dict[str, Any]) -> Approval:
        """Apply the approval policy. When asking, one approval covers the same network tool for the
        rest of the turn. Each call is still shown, and the answer says who let it through."""
        if self.policy.decide(spec) == "allow":
            self.io.say((spec.describe(args) if spec.describe else f"工具 {spec.name}") + "（自动）", kind="tool")
            return Approval(True, "policy")
        if spec.permission in TURN_APPROVAL_PERMISSIONS and spec.name in self._approved:
            self.io.say((spec.describe(args) if spec.describe else f"工具 {spec.name}") + "（本轮已允许）", kind="tool")
            return Approval(True, "turn")
        title = (spec.describe(args) if spec.describe else f"工具 {spec.name}").splitlines()[0]
        summary = summarize_args(args)
        if isinstance(args.get("content"), str):
            # What would be written is the user's content; the request keeps only its size.
            summary["content"] = f"（{len(args['content'])} 字，不保存）"
        request = self.store.open_approval(spec.name, spec.permission, title, summary, self.conversation_id)
        self._asked = request
        try:
            approved = self.io.confirm_tool(spec, args, request) is True
        except BaseException as exc:
            self.store.cancel_approval(request)
            if not isinstance(exc, Exception):
                # Ctrl+C leaves the executor without an audit row to link this request to.
                self._asked = None
            raise
        self.store.decide_approval(request, approved, self.io.channel)
        if approved and spec.permission in TURN_APPROVAL_PERMISSIONS:
            self._approved.add(spec.name)
        return Approval(approved, "user")

    def _audit(self, details: dict[str, Any]) -> None:
        self.store.record_tool_call(details)
        if self._asked:
            self.store.link_approval(self._asked, details["id"])
            self._asked = None

    def _check(self, spec: ToolSpec, args: dict[str, Any]) -> None:
        if self.policy.decide(spec) == "deny":
            raise ToolError("denied", f"审批策略禁止使用 {spec.name}", rule="policy_denied")
        if spec.permission in ("network", "external_mcp"):
            query_leak_check(list(self.private))(spec, args)

    def _tool_names(self) -> list[str]:
        names = []
        for name in self.registry.names():
            spec = self.registry.get(name)
            if self.offline and spec.permission in ("network", "external_mcp"):
                continue
            if self.policy.decide(spec) == "deny":
                continue
            if name == "write_file" and not getattr(self.service, "write_roots", self.write_roots):
                continue
            names.append(name)
        return names

    def _think(self, text: str, draft: Draft | None = None) -> LoopResult:
        model = self.service.model
        if not callable(getattr(model, "tool_round", None)):
            raise ValueError("当前模型适配器不支持对话")
        context = self._context()
        summary, history = self._summary_and_history(context, text)
        request = self.service.context.build_chat(context, history, text, self.conversation_id, summary)
        self._approved = set()
        self._asked = None
        scope = ToolScope(CHAT_PERMISSIONS, confirm=self._confirm_tool, check=self._check,
                          conversation_id=self.conversation_id)
        loop = ToolLoop(model, self.service.context, self.registry, self.executor, self.max_rounds, self.deadline_s)
        try:
            result = loop.run(request, self._tool_names(), scope, require_final=True,
                              final_instruction=CLOSING, stop_on=PROPOSE_TOOLS, draft=draft)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                raise
            # Some endpoints refuse tool declarations; answer without tools rather than fail.
            self.io.say("模型端点不接受工具调用，本轮不使用工具。", kind="note")
            if draft is not None:
                draft.withdraw()
            result = loop.run(request, [], scope, require_final=True, final_instruction=CLOSING, draft=draft)
        self._record_build(request.build_id, result.status)
        return result

    # ----- rolling summary of older turns ------------------------------------------------

    def _summary_and_history(self, context: str, text: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        summary = self.chats.latest_summary(self.conversation_id)
        history = self._history(summary)
        if not self._needs_summary(context, summary, history, text):
            return summary, history
        try:
            summary = self._summarize(summary, history[:-KEEP_RECENT])
        except Exception as exc:
            # Falling back to dropping the oldest turns still answers; the reason is kept.
            self.chats.set_error(self.conversation_id, f"对话摘要生成失败，本轮改为丢弃最旧的消息：{exc}")
            return summary, history
        return summary, self._history(summary)

    def _needs_summary(self, context: str, summary: dict[str, Any] | None,
                       history: list[dict[str, Any]], text: str) -> bool:
        if len(history) <= KEEP_RECENT:
            return False
        head = context + (summary["content"] if summary else "")
        draft = ([{"role": "system", "content": head}]
                 + [{"role": item["role"], "content": item["content"]} for item in history]
                 + [{"role": "user", "content": text}])
        estimate, limit = self.service.context.estimate(draft)
        if limit is None:
            return len(history) > HISTORY_MESSAGES
        return estimate > SUMMARY_TRIGGER * limit

    def _summarize(self, summary: dict[str, Any] | None, older: list[dict[str, Any]]) -> dict[str, Any]:
        """Fold older turns into the summary with the chat_summary stage; halve the batch if too long."""
        model = self.service.model
        legacy = getattr(model, "summarize_chat", None)
        previous = summary["content"] if summary else ""
        while True:
            items = [{"role": item["role"], "content": item["content"]} for item in older]
            task = ContextTask("chat_summary", "", current_input=json.dumps(
                {"previous_summary": previous, "messages": items}, ensure_ascii=False))

            def fallback() -> dict[str, Any]:
                if not callable(legacy):
                    raise ValueError("当前模型不支持对话摘要")
                return legacy(previous, items)
            try:
                result = self.service._invoke(task, fallback)
                break
            except ContextBudgetError:
                if len(older) <= 1:
                    raise
                older = older[:len(older) // 2]
        content = result.get("summary")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("模型没有返回有效的对话摘要")
        source = {"previous": summary["id"] if summary else None,
                  "messages": [(item["id"], item["content"]) for item in older]}
        source_hash = hashlib.sha256(json.dumps(source, ensure_ascii=False).encode("utf-8")).hexdigest()
        return self.chats.add_summary(self.conversation_id, older[-1]["seq"], source_hash, content)

    def _record_build(self, build_id: str, status: str) -> None:
        with self.store.transaction():
            self.store.db.execute("UPDATE context_builds SET status=? WHERE id=?", (f"tool_loop_{status}", build_id))

    # ----- flows and mode -----------------------------------------------------------

    def _on_stage(self, mode: str, active_kind: str, active_id: str) -> None:
        current = self.chats.get(self.conversation_id)
        if current["mode"] == mode:
            return
        try:
            self.chats.set_mode(self.conversation_id, mode, "flow_stage", active_kind=active_kind,
                                active_id=active_id, goal=self._goal, step=mode)
        except IllegalTransition:
            # e.g. a quiz entered straight from chat goes chat -> self_test, which is declared;
            # anything else is a bug in the caller, but must not break the user's flow.
            self.chats.set_error(self.conversation_id, f"未记录的模式切换：{current['mode']} -> {mode}")

    def _run_flow(self, goal: str, runner: Callable[[], Any]) -> None:
        self._goal = goal
        error = ""
        try:
            result = runner()
        except FlowInterrupted:
            result = None
        except KeyboardInterrupt:
            self.io.say("已取消；已保存的进度可以用 /resume 继续。")
            result = None
        except ValueError as exc:
            error = str(exc)
            self._fail(f"{goal}未完成：{error}")
            result = None
        finally:
            if self.chats.get(self.conversation_id)["mode"] != "chat":
                self.chats.set_mode(self.conversation_id, "chat", "flow_finished")
        self.chats.set_status_snapshot(self.conversation_id, self._status())
        summary = (f"{goal}：出错停止（{error}），已保存的进度可以用 /resume 继续" if error
                   else self._summarize_flow(goal, result))
        outcome = result if isinstance(result, dict) else getattr(result, "outcome", None)
        attempt_id = outcome.get("attempt_id") if isinstance(result, dict) else getattr(result, "attempt_id", None)
        self.chats.add_message(self.conversation_id, "assistant", "flow_result", summary,
                               {"kind": "outcome" if isinstance(result, dict) else getattr(result, "kind", None),
                                "attempt_id": attempt_id})
        # A grading summary was just shown; saying the total again would repeat it.
        graded = bool(outcome) and outcome.get("total") is not None and not error
        self.io.say("已回到对话。" if graded else f"已回到对话。{summary}", kind="note")

    @staticmethod
    def _summarize_flow(goal: str, result: Any) -> str:
        if result is None:
            return f"{goal}：已中断"
        if isinstance(result, dict):
            return f"{goal}：{summarize_outcome(result)}"
        if result.outcome:
            return f"{goal}：{summarize_outcome(result.outcome)}"
        return {"paused": f"{goal}：已暂停，可以用 /resume 继续",
                "ended": f"{goal}：已结束，未创建自测",
                "interrupted": f"{goal}：已中断，可以用 /resume 继续"}.get(result.kind, f"{goal}：{result.kind}")

    def _plan_learn(self, args: dict[str, Any], typed: bool = False) -> tuple[str, Callable[[], Any]]:
        """typed: the user wrote /learn with a path, which is consent like `learn --from`;
        a model proposal may only point inside ZHIQIU_READ_ROOTS."""
        point = args["point"].strip()
        self._point = point
        path = args.get("path")
        urls = list(args.get("urls") or [])
        notion = list(args.get("notion") or [])
        if not (path or urls or notion):
            raise ValueError("学习需要资料：本地文件或目录、网页或 Notion 页面至少一项")
        if self.offline and (urls or notion):
            raise ValueError("离线模式不能使用网页或 Notion 资料")
        source = self._learn_path(path, typed) if path else None
        where = "；".join(([str(source)] if source else []) + urls + notion)
        description = f"开始学习“{point}”，资料：{where}"
        outbound = getattr(self.service, "outbound", None)
        if typed or not callable(outbound):
            # A typed /learn asks once, inside the flow, what may be sent.
            return description, lambda: run_lesson(self.io, self.store, self.service, point, source=source,
                                                   urls=urls, notion_pages=notion, offline=self.offline)
        # A proposal is confirmed once: the send-out list is shown with it, and the flow asks again
        # only if what it would send turns out different.
        plan = outbound(source, self.offline, urls=urls, notion_pages=notion)
        lines = outbound_lines(plan.materials, plan.endpoint, plan.remote)
        return description, _Runner(lambda: run_lesson(self.io, self.store, self.service, point, source=source,
                                                        urls=urls, notion_pages=notion, offline=self.offline,
                                                        approve=preapproved(self.io, lines)), lines)

    def _learn_path(self, path: str, typed: bool = False) -> Path:
        target = Path(path).expanduser()
        if typed:
            if not target.exists():
                raise ValueError(f"资料不存在：{path}")
            return target
        if not target.is_absolute() and self.read_roots:
            target = self.read_roots[0] / target
        if ".." in target.parts:
            raise ValueError(f"路径中不能含 ..：{path}")
        resolved = target.resolve()
        if not any(resolved.is_relative_to(root.resolve()) for root in self.read_roots):
            raise ValueError(f"资料不在可读目录白名单内：{path}；{ADD_ROOT_HINT}")
        if not resolved.exists():
            raise ValueError(f"资料不存在：{path}")
        return resolved

    def _plan_review(self, args: dict[str, Any]) -> tuple[str, Callable[[], Any]]:
        due = self.store.due_tasks()
        point = (args.get("point") or "").strip()
        if point:
            kp = self.store.get_knowledge_point(point)
            if not kp:
                raise ValueError(f"没有这个知识点：{point}")
            due = [task for task in due if task["kp_id"] == kp["id"]]
        if not due:
            raise ValueError("现在没有到期的复习")
        today = self.store.clock.today().isoformat()
        if self.store.db.execute("SELECT COUNT(*) FROM attempts WHERE kind='review' AND start_date=?",
                                 (today,)).fetchone()[0] >= DAILY_REVIEW_LIMIT:
            raise ValueError(f"今天已完成 {DAILY_REVIEW_LIMIT} 次复习，达到每日上限")
        kp_id, title = due[0]["kp_id"], due[0]["title"]
        self._point = title
        task_ids = [task["id"] for task in due if task["kp_id"] == kp_id]

        def runner() -> Any:
            attempt = self.store.start_attempt(kp_id, "review", task_ids)
            return run_quiz(self.io, self.store, self.service, attempt)
        return f"开始复习“{title}”（{len(task_ids)} 个到期任务）", runner

    def _plan_resume(self, args: dict[str, Any]) -> tuple[str, Callable[[], Any]]:
        target = (args.get("target_id") or "").strip()
        attempt = (self.store.db.execute("SELECT a.*,k.title FROM attempts a JOIN knowledge_points k ON k.id=a.kp_id "
                                         "WHERE a.id=?", (target,)).fetchone() if target else None)
        lessons = self.store.pending_lessons()
        if target and not attempt:
            kind, found = resume_target(self.store, target)
            if kind == "lesson":
                return self._resume_lesson_plan(next(row for row in lessons if row["session_id"] == found))
            attempt = self.store.db.execute("SELECT a.*,k.title FROM attempts a JOIN knowledge_points k "
                                            "ON k.id=a.kp_id WHERE a.id=?", (found,)).fetchone()
        if not attempt:
            open_attempt = self.store.active_attempt()
            if open_attempt:
                attempt = self.store.db.execute("SELECT a.*,k.title FROM attempts a JOIN knowledge_points k "
                                                "ON k.id=a.kp_id WHERE a.id=?", (open_attempt["id"],)).fetchone()
            elif len(lessons) == 1:
                return self._resume_lesson_plan(lessons[0])
            elif lessons:
                raise ValueError("有多个待继续的讲解，请用 /resume 短号 指定："
                                 + "；".join(f"{row['title']} {short_id(row['session_id'])}" for row in lessons))
            else:
                raise ValueError("没有可继续的自测、复习或讲解")
        if attempt["state"] == "completed":
            raise ValueError("这次尝试已经结束")
        attempt_id = attempt["id"]
        self._point = attempt["title"]
        if attempt["state"] == "awaiting_review":
            def grade() -> Any:
                outcome = self.service.finish(attempt_id, "explicit")
                show_outcome(self.io, self.store, outcome)
                return outcome
            return f"继续评阅“{attempt['title']}”的待评阅尝试", grade
        label = "复习" if attempt["kind"] == "review" else "自测"
        return (f"继续“{attempt['title']}”的{label}",
                lambda: run_quiz(self.io, self.store, self.service, attempt_id))

    def _resume_lesson_plan(self, lesson: Any) -> tuple[str, Callable[[], Any]]:
        session_id = lesson["session_id"]
        self._point = lesson["title"]
        return (f"继续讲解“{lesson['title']}”",
                lambda: resume_lesson(self.io, self.store, session_id, lambda: self.service))

    def _plan(self, name: str, args: dict[str, Any]) -> tuple[str, Callable[[], Any]]:
        if name == "propose_learn":
            return self._plan_learn(args)
        if name == "propose_review":
            return self._plan_review(args)
        return self._plan_resume(args)

    # ----- proposals -------------------------------------------------------------------

    def _proposal(self, name: str, value: dict[str, Any], loop: LoopResult | None = None) -> None:
        args = {key: item for key, item in value.items() if key != "proposal"}
        if name == "propose_memory":
            self._memory_proposal(args)
            return
        reason = args.get("reason") or ""
        try:
            description, runner = self._plan(name, args)
        except ValueError as exc:
            self.chats.add_message(self.conversation_id, "assistant", "proposal", f"建议：{name}",
                                   {"tool": name, "args": args, "status": "refused", "detail": str(exc)})
            guardrails.record(self.store, "proposal_refused", "blocked", str(exc), tool=name)
            self.io.say(f"这个建议现在无法执行：{exc}")
            return
        message = self.chats.add_message(self.conversation_id, "assistant", "proposal", description,
                                         {"tool": name, "args": args, "status": "pending",
                                          "loop_id": loop.loop_id if loop else None})
        self.io.say(f"建议：{description}" + (f"（{reason}）" if reason else ""))
        notice = getattr(runner, "notice", [])
        for line in notice:
            self.io.say(line)
        if notice:
            self.io.say("开始学习即同意发送上面的资料，进入后不再另行确认。")
        if not self.io.confirm("要开始吗？"):
            self.chats.update_payload(message["id"], status="rejected")
            guardrails.record(self.store, "proposal_rejected", "denied_by_user", description, tool=name)
            self.io.say("好的，先不进入。")
            return
        self.execute_proposal(message["id"], description, runner)

    def execute_proposal(self, message_id: str, description: str, runner: Callable[[], Any]) -> bool:
        """Run an accepted proposal once; a second confirmation of the same proposal does nothing."""
        key = f"proposal:{message_id}"
        with self.store.transaction():
            if self.store.db.execute("SELECT 1 FROM receipts WHERE operation_key=?", (key,)).fetchone():
                self.io.say("这个建议已经执行过了。")
                return False
            self.store.db.execute("INSERT INTO receipts(operation_key,attempt_id,result_json,created_at) "
                                  "VALUES(?,NULL,?,?)", (key, json.dumps({"status": "accepted"}), self.store._ts()))
        self.chats.update_payload(message_id, status="accepted")
        self._run_flow(description, runner)
        self.chats.update_payload(message_id, status="executed")
        return True

    def _memory_proposal(self, args: dict[str, Any]) -> None:
        key, content = args["key"].strip(), args["content"].strip()
        old = next((item for item in self.memory.list_semantic(None) if item["key"] == key), None)
        message = self.chats.add_message(self.conversation_id, "assistant", "memory_proposal",
                                         f"记住 {key}：{content}",
                                         {"tool": "propose_memory", "args": args, "status": "pending",
                                          "old": old["content"] if old else None})
        self.io.say(f"建议记住（之后的对话与讲解会参考）：\n  {key}：{content}"
                    + (f"\n  将替换原来的：{old['content']}" if old else "")
                    + (f"\n  理由：{args['reason']}" if args.get("reason") else ""))
        if not self.io.confirm("要记住吗？"):
            self.chats.update_payload(message["id"], status="rejected")
            guardrails.record(self.store, "proposal_rejected", "denied_by_user", key, tool="propose_memory")
            self.io.say("好的，不记。")
            return
        self.memory.set_semantic(key, content, f"chat:{message['id']}")
        self.chats.update_payload(message["id"], status="saved")
        self.io.say("已记住；从下一段对话开始生效。")

    def _memory_precheck(self, args: dict[str, Any]) -> None:
        key, content = args["key"].strip(), args["content"].strip()
        if not key.startswith(MEMORY_PREFIXES) or len(key) <= 5:
            raise ToolError("invalid_args", "记忆的 key 必须以 pref. 或 soul. 开头，并带上名称", rule="memory_limit")
        if len(content) > MEMORY_ITEM_CHARS:
            raise ToolError("invalid_args", f"单条记忆不能超过 {MEMORY_ITEM_CHARS} 字，请写得更精炼",
                            rule="memory_limit")
        others = sum(len(item["content"]) for item in self.memory.list_semantic(None)
                     if item["key"].startswith(MEMORY_PREFIXES) and item["key"] != key)
        if others + len(content) > MEMORY_TOTAL_CHARS:
            raise ToolError("invalid_args", f"偏好与人设记忆合计不能超过 {MEMORY_TOTAL_CHARS} 字；"
                                            "请先合并旧条目，或请用户用 memory delete 删除", rule="memory_limit")

    def _proposal_specs(self) -> list[ToolSpec]:
        return [
            _proposal_spec("propose_learn", "建议开始学习一个新知识点。用户确认后进入学习流程（会再次确认发送哪些资料）。"
                           "path 必须是可读目录中的文件或目录；也可以给网页或 Notion 页面。",
                           {"type": "object", "additionalProperties": False, "required": ["point"],
                            "properties": {"point": {"type": "string", "minLength": 1, "maxLength": 200},
                                           "path": {"type": "string", "minLength": 1, "maxLength": 1000},
                                           "urls": _STRINGS, "notion": _STRINGS, "reason": _REASON}}),
            _proposal_spec("propose_review", "建议开始一次到期复习；不给知识点时取最早到期的一项。",
                           {"type": "object", "additionalProperties": False,
                            "properties": {"point": {"type": "string", "minLength": 1, "maxLength": 200},
                                           "reason": _REASON}}),
            _proposal_spec("propose_resume", "建议继续未完成的讲解、自测、复习或待评阅尝试；target_id 为尝试 ID 或讲解会话 ID，可省略。",
                           {"type": "object", "additionalProperties": False,
                            "properties": {"target_id": {"type": "string", "minLength": 1, "maxLength": 64},
                                           "reason": _REASON}}),
            _proposal_spec("propose_memory", "建议把用户明确表达的长期偏好或人设记下来（key 以 pref. 或 soul. 开头）。用户确认后才保存。",
                           {"type": "object", "additionalProperties": False, "required": ["key", "content"],
                            "properties": {"key": {"type": "string", "minLength": 6, "maxLength": 60},
                                           "content": {"type": "string", "minLength": 1, "maxLength": 1000},
                                           "reason": _REASON}},
                           precheck=self._memory_precheck),
        ]

    # ----- slash commands --------------------------------------------------------------

    def _command(self, text: str) -> None:
        # /new typed into a conversation that has nothing yet leaves it empty (and out of the sidebar).
        if not (text.split()[0] == "/new" and self._first):
            self.chats.add_message(self.conversation_id, "user", "command", text)
        try:
            parts = _command_parts(text)
        except ValueError as exc:
            self.io.say(f"命令格式有误：{exc}")
            return
        name, rest = parts[0], parts[1:]
        try:
            if name == "/help":
                self.io.say(self._help())
            elif name == "/status":
                self._show_status()
            elif name == "/new":
                self.new()
            elif name == "/chats":
                self._show_chats()
            elif name == "/open":
                self._open_listed(rest[0] if rest else "")
            elif name == "/detail":
                self._show_detail(rest[0] if rest else None)
            elif name == "/memory":
                items = self.memory.list_semantic(None)
                self.io.say("\n".join(f"  {item['key']}：{item['content']}" for item in items) or "还没有记忆。")
            elif name == "/title":
                self.io.say(f"已改名为「{self._rename(' '.join(rest))}」。", kind="note")
            elif name == "/learn":
                self._run_plan(*self._plan_learn(self._parse_learn(rest), typed=True), name)
            elif name == "/review":
                self._run_plan(*self._plan_review({"point": rest[0]} if rest else {}), name)
            elif name == "/skill":
                self._skill(rest)
            elif name == "/resume":
                self._run_plan(*self._plan_resume({"target_id": rest[0]} if rest else {}), name)
            else:
                self.io.say(f"未知命令：{name}。{self._help()}")
        except ValueError as exc:
            self.io.say(str(exc))

    def _skill(self, rest: list[str]) -> None:
        """`/skill` lists the Skills; `/skill 名称 标题` or `/skill auto 标题` sets one knowledge point's."""
        library = skills_of(self.service)
        if not rest:
            self.io.say(describe_skills(library))
            return
        if len(rest) < 2:
            raise ValueError("在对话里请写出知识点：/skill 名称 知识点标题（讲解或自测中可以只写 /skill 名称）")
        point = self.store.get_knowledge_point(rest[1])
        if point is None:
            raise ValueError(f"没有这个知识点：{rest[1]}")
        change_skill(self.io, self.store, library, point["id"], rest[0])

    def _show_detail(self, prefix: str | None) -> None:
        """The full grading of the latest graded flow in this conversation, or of the attempt a prefix names."""
        if prefix:
            attempt_id = resolve_prefix(prefix, [row[0] for row in self.store.db.execute(
                "SELECT id FROM attempts ORDER BY started_at DESC")], "尝试")
        else:
            attempt_id = next((message["payload"]["attempt_id"] for message in
                               reversed(self.chats.messages(self.conversation_id))
                               if message["kind"] == "flow_result" and message["payload"].get("attempt_id")), None)
            if not attempt_id:
                raise ValueError("本段对话还没有评阅结果；可以用 /detail 短号 查看某次尝试")
        receipt = self.store.db.execute("SELECT result_json FROM receipts WHERE operation_key=?",
                                        (f"finalize:{attempt_id}",)).fetchone()
        if not receipt:
            raise ValueError(f"尝试 {short_id(attempt_id)} 还没有评阅结果")
        show_outcome_detail(self.io, self.store, json.loads(receipt[0]))

    def _show_chats(self) -> None:
        """`/chats`: the recent conversations with messages, numbered for one following /open."""
        rows = [row for row in self.chats.recent(CHATS_LISTED * 3)
                if row["messages"] or row["id"] == self.conversation_id][:CHATS_LISTED]
        self._listed = [row["id"] for row in rows]
        today = self.store.clock.today().isoformat()
        lines = [f"{number}. {row['title']} · {'今天' if row['chat_date'] == today else row['chat_date'][5:]} · "
                 f"{row['messages']} 条" + ("（当前）" if row["id"] == self.conversation_id else "")
                 for number, row in enumerate(rows, 1)]
        self.io.say("\n".join(lines + ["输入 /open 编号 切换（编号只对下一条输入有效）。"]))

    def _open_listed(self, value: str) -> None:
        """`/open 编号|短号`: a number from the /chats just shown, or a conversation's short id."""
        if not value:
            raise ValueError("用法：/open 编号（先用 /chats 列出）或 /open 短号")
        if value.isdigit() and len(value) < MIN_PREFIX:
            if not self._offered:
                raise ValueError("编号已失效，请先用 /chats 重新列出。")
            number = int(value)
            if not 1 <= number <= len(self._offered):
                raise ValueError(f"没有编号 {number}；/chats 列出了 {len(self._offered)} 段对话。")
            target = self._offered[number - 1]
        else:
            target = resolve_prefix(value, [row[0] for row in self.store.db.execute(
                "SELECT id FROM conversations ORDER BY rowid DESC")], "对话")
        self.switch(target)

    @staticmethod
    def _resolve_prefix(prefix: str, matches: list[str], label: str) -> str:
        return resolve_prefix(prefix, matches, label)

    def _help(self) -> str:
        # /exit only means something in the terminal.
        return HELP + ("/exit 退出。" if self.io.channel == "terminal" else "")

    def _run_plan(self, description: str, runner: Callable[[], Any], name: str | None = None) -> None:
        # The user typed the command, so it needs no second confirmation.
        if name:
            self._rule_title(name)
        self._run_flow(description, runner)

    def _rename(self, title: str) -> str:
        """`/title`: the user's title, kept from then on."""
        return self.rename(self.conversation_id, title)

    @staticmethod
    def _parse_learn(rest: list[str]) -> dict[str, Any]:
        point_parts: list[str] = []
        args: dict[str, Any] = {"urls": [], "notion": []}
        items = iter(rest)
        for item in items:
            if item in ("--from", "--url", "--notion"):
                value = next(items, None)
                if value is None:
                    raise ValueError(f"{item} 后面需要一个值")
                if item == "--from":
                    args["path"] = value
                else:
                    args["urls" if item == "--url" else "notion"].append(value)
            else:
                point_parts.append(item)
        if not point_parts:
            raise ValueError("用法：/learn 知识点 [--from 路径] [--url 网址] [--notion 页面]")
        args["point"] = " ".join(point_parts)
        return args

    def _show_status(self) -> None:
        state = self.store.status()
        lines = [f"到期复习：{task['title']}（{task['original_due_date']}）" for task in state["due"]] or ["当前没有到期复习。"]
        lines += [f"待继续：{a['title']}  {a['state']}  短号 {short_id(a['id'])}" for a in state["open_attempts"]]
        lines += [f"待继续讲解：{row['title']}  短号 {short_id(row['session_id'])}" for row in self.store.pending_lessons()]
        lines += [f"需补学：{p['title']}" for p in state["needs_relearn"]]
        self.io.say("\n".join(lines))
