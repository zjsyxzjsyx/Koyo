"""Exercise the V5 observation layer against the configured real model, with disposable public data.

One chat turn that calls a tool, then a learn proposal that is accepted and runs the lesson,
the quiz and grading. Afterwards it checks what V5 promises: each turn is one connected
trace tree, every model and tool call hangs on an existing span, the run contracts have no
FAIL, and metrics see the tokens. No personal material is read; nothing goes to the web.

    uv run python examples/live_v5_smoke.py                       # temporary database
    uv run python examples/live_v5_smoke.py --db D:/tmp/v5.sqlite --pause 3
        # keep the database and slow the scripted user down, so that
        # `zhiqiu --db D:/tmp/v5.sqlite dashboard` can watch the live progress
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from zhiqiu import contracts, metrics, traces
from zhiqiu.agent import ChatAgent
from zhiqiu.clock import FixedClock
from zhiqiu.flows import FlowInterrupted
from zhiqiu.local_env import load_local_env
from zhiqiu.model import OpenAICompatibleModel
from zhiqiu.runtime import LearningRuntime
from zhiqiu.store import Store

NOTES = ("Inorder traversal of a binary tree visits the left subtree, then its root, then the right subtree. "
         "Recursion stops at an empty child. In a binary search tree whose left keys are smaller and right keys "
         "are larger, inorder traversal lists keys in ascending order. Preorder visits the root before its "
         "children.\n")
ANSWERS = ["先遍历左子树，再访问根，最后遍历右子树。",
           "递归到空子节点停止，每个节点按左、根、右的顺序处理。",
           "二叉搜索树左小右大，所以中序遍历会得到升序序列。"]


class AutoIO:
    """Says yes to every prompt and replays inputs, optionally pausing like a person typing."""

    def __init__(self, inputs: list[str], pause: float = 0.0):
        self.inputs = list(inputs)
        self.pause = pause
        self.prompts: list[str] = []

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.inputs:
            raise FlowInterrupted
        time.sleep(self.pause)
        return self.inputs.pop(0)

    def say(self, text: str) -> None:
        pass

    def confirm(self, text: str) -> bool:
        return True

    def confirm_tool(self, spec, args) -> bool:
        return True


def tree_problems(store: Store) -> list[str]:
    spans = {row["span_id"]: dict(row) for row in store.db.execute("SELECT * FROM spans")}
    problems = [f"span {row['name']} 仍在运行" for row in spans.values() if row["status"] == "running"]
    problems += [f"span {row['name']} 的父节点不存在" for row in spans.values()
                 if row["parent_span_id"] and row["parent_span_id"] not in spans]
    for table in ("llm_calls", "tool_calls", "context_builds"):
        for row in store.db.execute(f"SELECT id,trace_id,parent_span_id FROM {table}"):
            parent = spans.get(row["parent_span_id"])
            if parent is None or parent["trace_id"] != row["trace_id"]:
                problems.append(f"{table} {row['id']} 没有挂在本 trace 的 span 上")
    return problems


def run(db_path: Path, notes: Path, pause: float) -> dict[str, object]:
    store = Store(db_path, FixedClock(date(2026, 10, 14)))
    report: dict[str, object] = {"db": str(db_path)}
    stage = "chat_tool_turn"
    try:
        model = OpenAICompatibleModel(audit=store.record_llm_call)
        service = LearningRuntime(store, model)
        io = AutoIO(["/done", *ANSWERS, "/end"], pause)
        agent = ChatAgent(store, service, io, read_roots=[notes.parent])
        agent.start()
        agent.handle("我今天有到期的复习吗？顺便看看我的学习状态。")
        stage = "learn_flow"
        agent.handle(f"我想学二叉搜索树的中序遍历，资料是可读目录里的 {notes.name}，请帮我开始学习。")
        stage = "observation"
        roots = [dict(row) for row in store.db.execute(
            "SELECT kind,name,status,end_reason,trace_id FROM spans WHERE parent_span_id IS NULL ORDER BY started_at_ns")]
        learn = store.db.execute("SELECT trace_id,parent_span_id FROM spans WHERE name='learn'").fetchone()
        quiz = store.db.execute("SELECT parent_span_id FROM spans WHERE name='quiz'").fetchone()
        check = contracts.check(store.db, 1)
        data = metrics.collect(store.db, 1)
        attempt = store.db.execute("SELECT id,state FROM attempts WHERE kind='first'").fetchone()
        review = store.db.execute("SELECT kind,total FROM reviews").fetchone()
        report.update({
            "roots": [(row["kind"], row["name"], row["status"], row["end_reason"]) for row in roots],
            "learn_inside_chat_turn": bool(learn and learn["parent_span_id"]),
            "quiz_inside_learn": bool(quiz) and store.db.execute(
                "SELECT name FROM spans WHERE span_id=?", (quiz["parent_span_id"],)).fetchone()[0] == "learn",
            "attempt": dict(attempt) if attempt else None, "review": dict(review) if review else None,
            "tree_problems": tree_problems(store),
            "contracts": {item["check"]: item["status"] for item in check["results"]},
            "contracts_passed": check["passed"],
            "metrics": {"calls": data["model"]["calls"], "tokens_in": data["model"]["tokens_in"],
                        "tokens_cached": data["model"]["tokens_cached"], "tokens_out": data["model"]["tokens_out"],
                        "cost": data["model"]["cost"], "stages": sorted(data["model"]["by_stage"]),
                        "tools": data["tools"]["by_status"], "guardrails": data["guardrails"]},
            "failures": {item["category"]: item["count"] for item in contracts.failures(store.db, 1)["categories"]},
            "timeline_lines": sum(len(traces.render(traces.tree(store.db, row["trace_id"])).splitlines())
                                  for row in roots),
        })
        ok = (not report["tree_problems"] and check["passed"] and report["learn_inside_chat_turn"]
              and report["quiz_inside_learn"] and data["model"]["calls"] > 0 and data["model"]["tokens_in"] > 0)
        if ok:
            report["passed_stage"] = "all"
        else:
            report["failed_stage"] = "observation"
    except Exception as exc:
        # Never print the exception text: provider errors can echo request headers.
        report.update({"failed_stage": stage, "error_type": type(exc).__name__})
    finally:
        store.close()
    return report


def main() -> dict[str, object]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, help="保留数据库到这个路径（便于用 dashboard 查看）")
    parser.add_argument("--pause", type=float, default=0.0, help="模拟用户每次输入前停顿的秒数")
    args = parser.parse_args()
    load_local_env()
    cache = Path(__file__).resolve().parents[1] / ".cache"
    cache.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="zhiqiu-live-v5-", dir=cache) as folder:
        notes = Path(folder).resolve() / "notes" / "public-tree.md"
        notes.parent.mkdir()
        notes.write_text(NOTES, encoding="utf-8")
        db_path = args.db.resolve() if args.db else Path(folder) / "state.sqlite"
        if args.db and db_path.exists():
            raise SystemExit(f"{db_path} 已存在；请换一个路径")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"数据库：{db_path}", file=sys.stderr, flush=True)
        return run(db_path, notes, args.pause)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    result = main()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("passed_stage") == "all" else 1)
