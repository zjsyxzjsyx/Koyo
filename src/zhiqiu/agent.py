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

from .context import ContextBudgetError, ContextTask
from .conversations import Conversations
from .files import read_root_file_spec
from .flow import IllegalTransition
from .flows import FlowInterrupted, FlowIO, resume_lesson, run_lesson, run_quiz, show_outcome, summarize_outcome
from .materials import Material
from .memory import MemoryService
from .model import ModelContextOverflowError
from .queries import status as status_query
from .store import Store
from .toolloop import LOOP_SECONDS, MAX_ROUNDS, LoopResult, ToolLoop, query_leak_check
from .tools import ToolError, ToolExecutor, ToolRegistry, ToolScope, ToolSpec

CHAT_PERMISSIONS = frozenset({"read", "network", "write_file", "external_mcp", "propose"})
PROPOSE_TOOLS = frozenset({"propose_learn", "propose_review", "propose_resume", "propose_memory"})
MEMORY_PREFIXES = ("pref.", "soul.")
MEMORY_ITEM_CHARS = 300
MEMORY_TOTAL_CHARS = 2000
# Unsummarized history beyond this count is summarized when no input budget is configured.
HISTORY_MESSAGES = 40
# With a budget, summarize once unsummarized history pushes the request past this share of it.
SUMMARY_TRIGGER = 0.7
# The newest messages always stay verbatim.
KEEP_RECENT = 10
CLOSING = "不要再调用工具，直接用文字给出本轮的回答。"
HELP = ("对话命令：/status 查看状态；/learn 知识点 [--from 路径] [--url 网址] [--notion 页面]；"
        "/review [知识点]；/resume [ID]；/memory 查看记忆；/new 开新对话；/exit 退出。其他输入直接与知秋对话。")
_REASON = {"type": "string", "maxLength": 300}
_STRINGS = {"type": "array", "maxItems": 5, "items": {"type": "string", "minLength": 1, "maxLength": 1000}}


class ChatIO:
    """Wraps the user's IO; flows report their stage here so the conversation mode follows them."""

    def __init__(self, inner: FlowIO, on_stage: Callable[[str, str, str], None]):
        self.inner = inner
        self.on_stage = on_stage

    def ask(self, prompt: str) -> str:
        return self.inner.ask(prompt)

    def say(self, text: str) -> None:
        self.inner.say(text)

    def confirm(self, text: str) -> bool:
        return self.inner.confirm(text)

    def confirm_tool(self, spec: Any, args: dict[str, Any]) -> bool:
        return self.inner.confirm_tool(spec, args)

    def stage(self, mode: str, active_kind: str, active_id: str) -> None:
        self.on_stage(mode, active_kind, active_id)


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


class ChatAgent:
    def __init__(self, store: Store, service: Any, io: FlowIO, *, read_roots: Sequence[Path] = (),
                 write_roots: Sequence[Path] = (), offline: bool = False,
                 max_rounds: int = MAX_ROUNDS, deadline_s: float = LOOP_SECONDS):
        self.store = store
        self.service = service
        self.chats = Conversations(store)
        self.memory = MemoryService(store)
        self.read_roots = list(read_roots)
        self.write_roots = list(write_roots)
        self.offline = offline
        self.max_rounds = max_rounds
        self.deadline_s = deadline_s
        self.io = ChatIO(io, self._on_stage)
        # Private text read in this run; outgoing tool arguments may not copy it.
        self.private: list[Material] = []
        self.conversation: dict[str, Any] | None = None
        self._goal = ""
        self.registry = ToolRegistry()
        for spec in service.tools.specs():
            if spec.name in ("read_file", "notion_page") or not spec.in_loops:
                continue
            if spec.permission in ("network", "external_mcp"):
                # Outside learn there is no up-front consent, so every outgoing call is confirmed.
                spec = replace(spec, requires_confirmation=True, describe=_describe_external(spec))
            self.registry.register(spec)
        self.registry.register(read_root_file_spec(self.read_roots, self.private.append))
        for spec in self._proposal_specs():
            self.registry.register(spec)
        self.executor = ToolExecutor(self.registry, audit=store.record_tool_call)

    # ----- conversation lifecycle -------------------------------------------------

    @property
    def conversation_id(self) -> str:
        assert self.conversation is not None, "call start() first"
        return self.conversation["id"]

    def _snapshot(self) -> list[dict[str, str]]:
        return [{"key": item["key"], "content": item["content"]} for item in self.memory.list_semantic(None)]

    def start(self, resume_id: str | None = None) -> dict[str, Any]:
        self.conversation = (self.chats.reopen(resume_id) if resume_id
                             else self.chats.open_today(self._snapshot(), self._status()))
        self.chats.reconcile(self.conversation_id)
        # The status in the prompt is a snapshot; refreshing it only here and after flows keeps
        # the request prefix stable in between.
        self.chats.set_status_snapshot(self.conversation_id, self._status())
        self.conversation = self.chats.get(self.conversation_id)
        state = self.store.status()
        if state["open_attempts"] or self.store.pending_lessons():
            self.io.say("有未完成的自测或讲解：可以直接说“继续”，或输入 /resume。")
        count = len(self.chats.messages(self.conversation_id))
        self.io.say(f"对话 {self.conversation_id}" + (f"（接着今天的 {count} 条消息）" if count else "（新对话）")
                    + "。输入 /help 查看命令，/exit 退出。")
        return self.conversation

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

    def handle(self, text: str) -> None:
        if text.startswith("/"):
            self._command(text)
            return
        self.chats.add_message(self.conversation_id, "user", "chat", text)
        try:
            result = self._think(text)
        except KeyboardInterrupt:
            self.chats.add_message(self.conversation_id, "assistant", "error", "已取消")
            self.io.say("已取消本轮。")
            return
        except (ContextBudgetError, ModelContextOverflowError, ValueError, httpx.HTTPError) as exc:
            self._fail(f"本轮未完成：{exc}")
            return
        if result.status == "proposal" and result.stopped_by:
            self._proposal(*result.stopped_by, loop=result)
            return
        if not result.text:
            self._fail(f"本轮未完成：{result.reason}，模型没有给出回答。")
            return
        self.chats.add_message(self.conversation_id, "assistant", "chat", result.text,
                               {"status": result.status, "rounds": result.rounds, "loop_id": result.loop_id})
        self.io.say(result.text)

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
        writable = "；".join(str(root) for root in self.write_roots) or "（未配置，不能写文件）"
        return (f"今天：{today}\n学习状态（快照，最新状态请调用 zhiqiu_status）：{status}\n"
                f"偏好与人设记忆（本段对话开始时的快照）：\n{memory}\n"
                f"可读目录：{readable}\n可写目录：{writable}\n联网：{'已关闭（--offline）' if self.offline else '可用，每次需确认'}")

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

    def _check(self, spec: ToolSpec, args: dict[str, Any]) -> None:
        if spec.permission in ("network", "external_mcp"):
            query_leak_check(list(self.private))(spec, args)

    def _tool_names(self) -> list[str]:
        names = []
        for name in self.registry.names():
            spec = self.registry.get(name)
            if self.offline and spec.permission in ("network", "external_mcp"):
                continue
            names.append(name)
        return names

    def _think(self, text: str) -> LoopResult:
        model = self.service.model
        if not callable(getattr(model, "tool_round", None)):
            raise ValueError("当前模型适配器不支持对话")
        context = self._context()
        summary, history = self._summary_and_history(context, text)
        request = self.service.context.build_chat(context, history, text, self.conversation_id, summary)
        scope = ToolScope(CHAT_PERMISSIONS, confirm=self.io.confirm_tool, check=self._check,
                          conversation_id=self.conversation_id)
        loop = ToolLoop(model, self.service.context, self.registry, self.executor, self.max_rounds, self.deadline_s)
        try:
            result = loop.run(request, self._tool_names(), scope, require_final=True,
                              final_instruction=CLOSING, stop_on=PROPOSE_TOOLS)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                raise
            # Some endpoints refuse tool declarations; answer without tools rather than fail.
            self.io.say("模型端点不接受工具调用，本轮不使用工具。")
            result = loop.run(request, [], scope, require_final=True, final_instruction=CLOSING)
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
        self.chats.add_message(self.conversation_id, "assistant", "flow_result", summary,
                               {"kind": getattr(result, "kind", None),
                                "attempt_id": getattr(result, "attempt_id", None)})
        self.io.say(f"已回到对话。{summary}")

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
        return description, lambda: run_lesson(self.io, self.store, self.service, point, source=source,
                                               urls=urls, notion_pages=notion, offline=self.offline)

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
            raise ValueError(f"资料不在 ZHIQIU_READ_ROOTS 白名单目录内：{path}")
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
                                 (today,)).fetchone()[0] >= 3:
            raise ValueError("今天已完成 3 次复习，达到每日上限")
        kp_id, title = due[0]["kp_id"], due[0]["title"]
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
            lesson = next((row for row in lessons if row["session_id"] == target), None)
            if not lesson:
                raise ValueError(f"没有可继续的尝试或讲解：{target}")
            return self._resume_lesson_plan(lesson)
        if not attempt:
            open_attempt = self.store.active_attempt()
            if open_attempt:
                attempt = self.store.db.execute("SELECT a.*,k.title FROM attempts a JOIN knowledge_points k "
                                                "ON k.id=a.kp_id WHERE a.id=?", (open_attempt["id"],)).fetchone()
            elif len(lessons) == 1:
                return self._resume_lesson_plan(lessons[0])
            elif lessons:
                raise ValueError("有多个待继续的讲解，请指定会话 ID："
                                 + "；".join(f"{row['title']} {row['session_id']}" for row in lessons))
            else:
                raise ValueError("没有可继续的自测、复习或讲解")
        if attempt["state"] == "completed":
            raise ValueError("这次尝试已经结束")
        attempt_id = attempt["id"]
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
            self.io.say(f"这个建议现在无法执行：{exc}")
            return
        message = self.chats.add_message(self.conversation_id, "assistant", "proposal", description,
                                         {"tool": name, "args": args, "status": "pending",
                                          "loop_id": loop.loop_id if loop else None})
        self.io.say(f"建议：{description}" + (f"（{reason}）" if reason else ""))
        if not self.io.confirm("要开始吗？"):
            self.chats.update_payload(message["id"], status="rejected")
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
            self.io.say("好的，不记。")
            return
        self.memory.set_semantic(key, content, f"chat:{message['id']}")
        self.chats.update_payload(message["id"], status="saved")
        self.io.say("已记住；从下一段对话开始生效。")

    def _memory_precheck(self, args: dict[str, Any]) -> None:
        key, content = args["key"].strip(), args["content"].strip()
        if not key.startswith(MEMORY_PREFIXES) or len(key) <= 5:
            raise ToolError("invalid_args", "记忆的 key 必须以 pref. 或 soul. 开头，并带上名称")
        if len(content) > MEMORY_ITEM_CHARS:
            raise ToolError("invalid_args", f"单条记忆不能超过 {MEMORY_ITEM_CHARS} 字，请写得更精炼")
        others = sum(len(item["content"]) for item in self.memory.list_semantic(None)
                     if item["key"].startswith(MEMORY_PREFIXES) and item["key"] != key)
        if others + len(content) > MEMORY_TOTAL_CHARS:
            raise ToolError("invalid_args", f"偏好与人设记忆合计不能超过 {MEMORY_TOTAL_CHARS} 字；"
                                            "请先合并旧条目，或请用户用 memory delete 删除")

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
        self.chats.add_message(self.conversation_id, "user", "command", text)
        try:
            parts = _split_command(text)
        except ValueError as exc:
            self.io.say(f"命令格式有误：{exc}")
            return
        name, rest = parts[0], parts[1:]
        try:
            if name == "/help":
                self.io.say(HELP)
            elif name == "/status":
                self._show_status()
            elif name == "/new":
                self.conversation = self.chats.start_new(self._snapshot(), self._status())
                self.private.clear()
                self.io.say(f"已开始新对话 {self.conversation_id}。")
            elif name == "/memory":
                items = self.memory.list_semantic(None)
                self.io.say("\n".join(f"  {item['key']}：{item['content']}" for item in items) or "还没有记忆。")
            elif name == "/learn":
                self._run_plan(*self._plan_learn(self._parse_learn(rest), typed=True))
            elif name == "/review":
                self._run_plan(*self._plan_review({"point": " ".join(rest)} if rest else {}))
            elif name == "/resume":
                self._run_plan(*self._plan_resume({"target_id": rest[0]} if rest else {}))
            else:
                self.io.say(f"未知命令：{name}。{HELP}")
        except ValueError as exc:
            self.io.say(str(exc))

    def _run_plan(self, description: str, runner: Callable[[], Any]) -> None:
        # The user typed the command, so it needs no second confirmation.
        self._run_flow(description, runner)

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
        lines += [f"待继续：{a['title']}  {a['state']}  尝试 {a['id']}" for a in state["open_attempts"]]
        lines += [f"待继续讲解：{row['title']}  会话 {row['session_id']}" for row in self.store.pending_lessons()]
        lines += [f"需补学：{p['title']}" for p in state["needs_relearn"]]
        self.io.say("\n".join(lines))
