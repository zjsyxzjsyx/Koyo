"""Exercise the real V2 learning path using only disposable, public test data."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from zhiqiu.clock import FixedClock
from zhiqiu.local_env import load_local_env
from zhiqiu.model import OpenAICompatibleModel
from zhiqiu.runtime import LearningRuntime
from zhiqiu.search import ExaSearch
from zhiqiu.store import Store


ANSWERS = {
    "concept": "Inorder traversal visits the left subtree, then the root, then the right subtree.",
    "mechanism": "At each node, recursively traverse left, record the root, and recursively traverse right. Empty children end recursion.",
    "application": "For a binary search tree with smaller keys on the left and larger keys on the right, inorder traversal lists keys in ascending order.",
}


def main(all_model_questions: bool = False) -> dict[str, object]:
    load_local_env()
    cache = Path(__file__).resolve().parents[1] / ".cache"
    cache.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="zhiqiu-live-v2-", dir=cache) as folder:
        root = Path(folder).resolve()
        if not root.is_relative_to(cache.resolve()):
            raise RuntimeError("temporary workspace escaped project cache")
        source = root / "public-tree.md"
        source.write_text(
            "Inorder traversal of a binary tree visits the left subtree, then its root, "
            "then the right subtree. Recursion stops at an empty child. In a binary "
            "search tree whose left keys are smaller and right keys are larger, inorder "
            "traversal lists keys in ascending order. Preorder differs by visiting the "
            "root before its children.\n", encoding="utf-8")
        store = Store(root / "state.sqlite", FixedClock(date(2026, 10, 6)))
        stage = "prepare_with_web"
        question_shape: dict[str, object] = {}
        try:
            model = OpenAICompatibleModel(audit=store.record_llm_call)
            original_request = model.request

            def observe_request(request):
                response = original_request(request)
                if request.stage == "next_question":
                    question_shape.update({
                        "done": response.get("done"),
                        "dimension_valid": response.get("dimension") in ("concept", "mechanism", "application"),
                        "question_present": isinstance(response.get("question"), str) and bool(response["question"].strip()),
                        "difficulty_valid": response.get("difficulty") in ("basic", "advanced", "transfer"),
                        "difficulty_basis_present": isinstance(response.get("difficulty_basis"), str)
                        and bool(response["difficulty_basis"].strip()),
                    })
                return response

            model.request = observe_request
            service = LearningRuntime(store, model, ExaSearch())
            kp_id, lesson, materials = service.prepare(
                "binary search tree inorder traversal", source, lambda *_: True,
                force_web=not all_model_questions)
            stage = "start_attempt"
            attempt_id = store.start_attempt(kp_id, "first")
            stage = "generate_question"
            model_questions = 0
            fixed_questions = 0
            covered: set[str] = set()
            if all_model_questions:
                for _ in range(8):
                    generated = service.next_question(attempt_id)
                    if generated.get("done"):
                        break
                    model_questions += 1
                    covered.add(generated["dimension"])
                    store.add_answer(generated["question_id"], ANSWERS[generated["dimension"]])
                    if covered == set(ANSWERS):
                        break
                if covered != set(ANSWERS):
                    raise ValueError("model did not cover all dimensions")
            else:
                generated = service.next_question(attempt_id)
                if generated.get("done"):
                    raise ValueError("model ended assessment before asking a question")
                model_questions = 1
                store.add_answer(generated["question_id"], ANSWERS[generated["dimension"]])
                for dimension in ("concept", "mechanism", "application"):
                    if dimension == generated["dimension"]:
                        continue
                    question_id, _ = store.add_question(
                        attempt_id, dimension, f"Explain {dimension} of inorder traversal.",
                        "basic", "public synthetic smoke question")
                    store.add_answer(question_id, ANSWERS[dimension])
                    fixed_questions += 1
            stage = "review_and_schedule"
            outcome = service.finish(attempt_id, "explicit")
            usage = store.db.execute("""SELECT COUNT(*) calls,
                COALESCE(SUM(tokens_in),0) input_tokens,
                COALESCE(SUM(tokens_out),0) output_tokens FROM llm_calls""").fetchone()
            return {
                "smoke_verified": bool(outcome.get("review_id") and outcome.get("task_id")),
                "lesson_saved": bool(lesson.get("lesson")),
                "web_sources": sum(item.kind == "web" for item in materials),
                "model_questions": model_questions,
                "fixed_questions": fixed_questions,
                "dimensions_covered": len(covered) if all_model_questions else 3,
                "review_kind": outcome["kind"],
                "review_saved": bool(outcome.get("review_id")),
                "review_task_scheduled": bool(outcome.get("task_id")),
                "llm_calls": usage["calls"],
                "provider_input_tokens": usage["input_tokens"],
                "provider_output_tokens": usage["output_tokens"],
            }
        except Exception as exc:
            last_build = store.db.execute("SELECT stage,status FROM context_builds ORDER BY rowid DESC LIMIT 1").fetchone()
            return {"smoke_verified": False, "phase": stage, "error_type": type(exc).__name__,
                    "http_status": exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None,
                    "last_context_stage": last_build["stage"] if last_build else None,
                    "last_context_status": last_build["status"] if last_build else None,
                    "question_response_shape": question_shape or None,
                    "llm_calls": store.db.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0],
                    "web_calls": store.db.execute("SELECT COUNT(*) FROM search_calls").fetchone()[0]}
        finally:
            store.close()


if __name__ == "__main__":
    try:
        if sys.argv[1:] not in ([], ["--all-model-questions"]):
            raise ValueError("invalid smoke option")
        result = main(all_model_questions=sys.argv[1:] == ["--all-model-questions"])
        print(json.dumps(result, ensure_ascii=False))
        if not result["smoke_verified"]:
            raise SystemExit(1)
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({"smoke_verified": False, "error_type": type(exc).__name__,
                          "http_status": exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None}))
        raise SystemExit(1) from None
