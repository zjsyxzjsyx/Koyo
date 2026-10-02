"""A deterministic local learning-to-due-task demonstration; no API key needed."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from zhiqiu.clock import FixedClock
from zhiqiu.runtime import LearningRuntime
from zhiqiu.store import Store


class DemoModel:
    endpoint = "https://demo.invalid/v1"

    def assess(self, point, sources):
        return {"sufficient": True, "reason": "演示资料可用", "gap": ""}

    def teach(self, point, sources):
        return {"lesson": f"演示教学：{point}", "suggestions": []}

    def next_question(self, point, history, sources, focus):
        dimensions = ("concept", "mechanism", "application")
        if len(history) >= len(dimensions):
            return {"done": True}
        return {"done": False, "dimension": dimensions[len(history)], "question": f"解释 {dimensions[len(history)]}"}

    def grade(self, point, history, sources):
        scores = (4, 3, 1)
        return {"grades": [{"dimension": row["dimension"], "score": scores[i],
                "question_id": row["question_id"], "answer_id": row["answer_id"],
                "evidence": row["answer"], "rationale": "演示评分依据"}
                for i, row in enumerate(history)]}

    def search_query(self, point, gap):
        return None


cache = Path(__file__).resolve().parents[1] / ".cache"
cache.mkdir(exist_ok=True)
with TemporaryDirectory(prefix="zhiqiu-demo-", dir=cache) as directory:
    root = Path(directory)
    source = root / "tree.md"
    source.write_text("二叉树遍历包括前序、中序和后序，顺序取决于根节点访问时机。", encoding="utf-8")
    db_path = root / "state.sqlite"
    store = Store(db_path, FixedClock(date(2026, 10, 2)))
    runtime = LearningRuntime(store, DemoModel())
    kp, lesson, sources = runtime.prepare("二叉树遍历", source, lambda *_: True)
    print(lesson["lesson"])
    print("来源：", sources[0].source)
    attempt = store.start_attempt(kp, "first")
    for answer in ("先访问根节点", "中序在左右子树之间访问根", "可用于表达式树"):
        question = runtime.next_question(attempt)
        print("问题：", question["question"])
        store.add_answer(question["question_id"], answer)
    result = runtime.finish(attempt, "natural")
    print("已保存评分与任务：", result)
    store.close()
    for run in (1, 2):
        reopened = Store(db_path, FixedClock(date(2026, 10, 4)))
        print(f"到期启动 {run}：", [task["id"] for task in reopened.status()["due"]])
        reopened.close()
