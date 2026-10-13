"""Exercise the V4 chat shell against the configured real model, with disposable public data.

Scenarios (PRD §2): S1 chat with a status lookup, S2 a learn proposal that is accepted and runs
the lesson and quiz, S4 a question asked mid-quiz, S5 a remembered preference that appears in the
next conversation's snapshot, S6 (Issue 9) older turns folded into a rolling summary. No personal
material is read; nothing goes to the web.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from zhiqiu.agent import ChatAgent
from zhiqiu.clock import FixedClock
from zhiqiu.conversations import Conversations
from zhiqiu.flows import FlowInterrupted
from zhiqiu.local_env import load_local_env
from zhiqiu.memory import MemoryService
from zhiqiu.model import OpenAICompatibleModel
from zhiqiu.runtime import LearningRuntime
from zhiqiu.store import Store

NOTES = ("Inorder traversal of a binary tree visits the left subtree, then its root, then the right subtree. "
         "Recursion stops at an empty child. In a binary search tree whose left keys are smaller and right keys "
         "are larger, inorder traversal lists keys in ascending order. Preorder visits the root before its "
         "children.\n")
SIDE_QUESTION = "等一下，这里说的“根”是指当前子树的根吗？"
ANSWERS = ["先遍历左子树，再访问根，最后遍历右子树。",
           "递归到空子节点停止，每个节点按左、根、右的顺序处理。",
           "二叉搜索树左小右大，所以中序遍历会得到升序序列。"]


class AutoIO:
    """Says yes to every prompt and replays inputs; records what was shown."""

    def __init__(self, inputs: list[str]):
        self.inputs = list(inputs)
        self.said: list[str] = []
        self.prompts: list[str] = []

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.inputs:
            raise FlowInterrupted
        return self.inputs.pop(0)

    def say(self, text: str) -> None:
        self.said.append(text)

    def confirm(self, text: str) -> bool:
        self.said.append(text)
        return True

    def confirm_tool(self, spec, args) -> bool:
        return True


def tools_used(store: Store, conversation_id: str) -> list[str]:
    return [row[0] for row in store.db.execute(
        "SELECT tool FROM tool_calls WHERE conversation_id=? ORDER BY rowid", (conversation_id,))]


def main() -> dict[str, object]:
    load_local_env()
    cache = Path(__file__).resolve().parents[1] / ".cache"
    cache.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="zhiqiu-live-v4-", dir=cache) as folder:
        root = Path(folder).resolve()
        notes = root / "notes"
        notes.mkdir()
        (notes / "public-tree.md").write_text(NOTES, encoding="utf-8")
        store = Store(root / "state.sqlite", FixedClock(date(2026, 10, 13)))
        report: dict[str, object] = {}
        stage = "s1_status"
        try:
            model = OpenAICompatibleModel(audit=store.record_llm_call)
            service = LearningRuntime(store, model)
            # S2 inputs: /done, the side question (enter = confirm it is a question), answers, then /end.
            io = AutoIO(["/done", SIDE_QUESTION, "", *ANSWERS, "/end"])
            agent = ChatAgent(store, service, io, read_roots=[notes])
            agent.start()
            chats = Conversations(store)
            agent.handle("我今天有到期的复习吗？顺便看看我的学习状态。")
            first = chats.messages(agent.conversation_id)
            report["s1"] = {"tools": tools_used(store, agent.conversation_id),
                            "answered": first[-1]["kind"] == "chat" and bool(first[-1]["content"])}

            stage = "s2_learn_proposal"
            agent.handle("我想学二叉搜索树的中序遍历，资料是可读目录里的 public-tree.md，请帮我开始学习。")
            messages = chats.messages(agent.conversation_id)
            proposal = next((m for m in messages if m["kind"] == "proposal"), None)
            result = next((m for m in messages if m["kind"] == "flow_result"), None)
            moves = [json.loads(r[0]) for r in store.db.execute(
                "SELECT payload_json FROM events WHERE type='StateChanged' ORDER BY id")]
            report["s2"] = {"proposal_tool": proposal["payload"].get("tool") if proposal else None,
                            "proposal_status": proposal["payload"].get("status") if proposal else None,
                            "flow_result": result["content"] if result else None,
                            "conversation_moves": [(m["from"], m["to"]) for m in moves
                                                   if m["entity"] == "conversation"]}

            stage = "s4_side_question"
            side = store.db.execute("SELECT text,explanation FROM notes WHERE kind='clarification'").fetchall()
            answers = [r[0] for r in store.db.execute("SELECT text FROM answers")]
            attempt = store.db.execute("SELECT state,finish_reason FROM attempts WHERE kind='first'").fetchone()
            report["s2"]["attempt"] = dict(attempt) if attempt else None
            report["s2"]["answers"] = len(answers)
            report["s2"]["prompts"] = io.prompts
            report["s2"]["last_error"] = chats.get(agent.conversation_id)["last_error"]
            report["s4"] = {"classified_as_question": any(row[0] == SIDE_QUESTION for row in side),
                            "explained": any(row[0] == SIDE_QUESTION and row[1] for row in side),
                            "stored_as_answer": SIDE_QUESTION in answers,
                            "classify_calls": store.db.execute(
                                "SELECT COUNT(*) FROM llm_calls WHERE stage='classify_turn'").fetchone()[0]}

            stage = "s5_memory"
            agent.handle("以后给我讲解时都先给结论，再展开细节。请记住这个偏好。")
            saved = MemoryService(store).list_semantic(None)
            agent.handle("/new")
            snapshot = json.loads(chats.get(agent.conversation_id)["memory_snapshot_json"])
            report["s5"] = {"saved": [(m["key"], m["content"], m["source"].startswith("chat:")) for m in saved],
                            "in_next_snapshot": bool(saved) and any(item["key"] == saved[0]["key"] for item in snapshot)}
            stage = "s6_summary"
            # Synthetic earlier turns; without a window the summary is triggered by message count.
            for index in range(21):
                chats.add_message(agent.conversation_id, "user", "chat", f"第 {index} 个问题：二叉树第 {index} 层最多有几个节点？")
                chats.add_message(agent.conversation_id, "assistant", "chat", f"最多 2^{index} 个节点。")
            window = agent.service.context.window
            agent.service.context.window = None
            try:
                agent.handle("我们刚才讨论的规律是什么？一句话回答。")
            finally:
                agent.service.context.window = window
            summary = chats.latest_summary(agent.conversation_id)
            last = chats.messages(agent.conversation_id)[-1]
            report["s6"] = {"summary_upto_seq": summary["upto_seq"] if summary else None,
                            "summary_chars": len(summary["content"]) if summary else 0,
                            "answered": last["kind"] == "chat",
                            "last_error": chats.get(agent.conversation_id)["last_error"]}
            report["llm_calls"] = store.db.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
            report["tokens_in"] = store.db.execute("SELECT COALESCE(SUM(tokens_in),0) FROM llm_calls").fetchone()[0]
            report["passed_stage"] = "all"
        except Exception as exc:
            # Never print the exception text: provider errors can echo request headers.
            report.update({"failed_stage": stage, "error_type": type(exc).__name__})
        finally:
            store.close()
        return report


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    result = main()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("passed_stage") == "all" else 1)
