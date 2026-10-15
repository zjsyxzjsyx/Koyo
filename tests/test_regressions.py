"""Replay every recorded model failure in tests/regressions/ through the same validation.

Add a case with `zhiqiu failures export FAILURE_ID --to tests/regressions/<name>.json`.
"""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zhiqiu.cli import app
from zhiqiu.regressions import case_from_failure, validate_case

from test_runtime import FakeModel, prepare_service

CASES = sorted((Path(__file__).parent / "regressions").glob("*.json"))
VALID_QUESTION = {"done": False, "dimension": "concept", "question": "解释概念", "difficulty": "basic",
                  "difficulty_basis": "直接解释"}


class ReplayModel(FakeModel):
    """Answers the replayed stage from a queue: the recorded output first, then (for recovery) a valid one."""

    def __init__(self, stage, outputs):
        super().__init__()
        self.stage = stage
        self.outputs = list(outputs)

    def next_question(self, point, context, sources, focus):
        if self.stage != "next_question" or not self.outputs:
            return super().next_question(point, context, sources, focus)
        return self.outputs.pop(0)

    def grade(self, point, context, sources):
        if self.stage != "grade" or not self.outputs:
            return super().grade(point, context, sources)
        output = self.outputs.pop(0)
        return super().grade(point, context, sources) if output is None else output


def replay(tmp_path, case):
    """Replay a case in an attempt whose three original questions are already answered."""
    stage, raw, expect = case["stage"], case["raw_output"], case["expect"]
    follow = {"recovered": VALID_QUESTION if stage == "next_question" else None, "accepted": None}
    outputs = [raw] * 3 if expect == "rejected" else [raw, follow[expect]]
    model = ReplayModel(stage, outputs)
    store, service, path = prepare_service(tmp_path, model)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    attempt = store.start_attempt(kp, "first")
    for index in range(3):
        question = FakeModel.next_question(model, "二叉树遍历", [None] * index, [], [])
        question_id, _ = store.add_question(attempt, question["dimension"], question["question"])
        store.add_answer(question_id, f"第 {index + 1} 个回答")
    if stage == "grade":
        return store, service.finish(attempt, "natural")
    if expect == "rejected":
        with pytest.raises(ValueError):
            service.next_question(attempt)
        return store, None
    return store, service.next_question(attempt)


@pytest.mark.parametrize("path", CASES, ids=[path.stem for path in CASES])
def test_recorded_failure_replays_as_expected(tmp_path, path):
    case = json.loads(path.read_text(encoding="utf-8"))
    validate_case(case)
    store, result = replay(tmp_path, case)
    failures = [row["stage"] for row in store.db.execute("SELECT stage FROM llm_failures")]
    expect = case["expect"]
    if expect == "accepted":
        assert failures == []  # the very output that once failed is now accepted
    else:
        assert failures and set(failures) == {case["stage"]}
    if case["stage"] == "next_question":
        assert (result is not None and result["done"] is False) == (expect != "rejected")
        if expect == "accepted" and case["raw_output"].get("followup_of"):
            target = store.db.execute("SELECT followup_of FROM questions WHERE id=?", (result["question_id"],)).fetchone()[0]
            original = store.db.execute("SELECT dimension FROM questions WHERE id=?", (target,)).fetchone()[0]
            assert result["followup"] is True and original == case["raw_output"]["dimension"]
    else:
        assert (result["kind"] == "awaiting_review") == (expect == "rejected")


def test_an_ambiguous_follow_up_target_is_still_refused(tmp_path):
    raw = {"done": False, "dimension": "concept", "question": "追问", "difficulty": "basic",
           "difficulty_basis": "追问", "followup_of": "1"}
    model = ReplayModel("next_question", [raw, raw])
    store, service, path = prepare_service(tmp_path, model)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    attempt = store.start_attempt(kp, "first")
    for text in ("定义一", "定义二"):  # two answered originals in the same dimension: no unique target
        question_id, _ = store.add_question(attempt, "concept", text)
        store.add_answer(question_id, "回答")
    with pytest.raises(ValueError, match="不允许的追问"):
        service.next_question(attempt)


def test_there_is_at_least_one_regression_case():
    assert CASES


def test_a_stored_failure_exports_as_a_replayable_case(tmp_path):
    class Broken(FakeModel):
        def next_question(self, point, context, sources, focus):
            return {"done": False, "dimension": "concept", "question": "q", "difficulty": "hard",
                    "difficulty_basis": "?"}

    store, service, path = prepare_service(tmp_path, Broken())
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    with pytest.raises(ValueError):
        service.next_question(store.start_attempt(kp, "first"))
    failure_id = store.db.execute("SELECT id FROM llm_failures ORDER BY rowid LIMIT 1").fetchone()[0]
    case = case_from_failure(store.db, failure_id, "rejected", "难度取值越界")
    validate_case(case)
    assert case["raw_output"]["difficulty"] == "hard" and case["source"]["failure_id"] == failure_id
    store.close()
    target = tmp_path / "cases" / "bad_difficulty.json"
    db = str(tmp_path / "state.sqlite")
    result = CliRunner().invoke(app, ["--db", db, "failures", "export", failure_id, "--to", str(target)])
    assert result.exit_code == 0 and json.loads(target.read_text(encoding="utf-8"))["expect"] == "rejected"
    listed = CliRunner().invoke(app, ["--db", db, "failures", "--json"])
    categories = {item["category"]: item for item in json.loads(listed.output)["categories"]}
    assert categories["invalid_output:next_question"]["count"] == 2
    assert categories["invalid_output:next_question"]["samples"][0]["failure_id"]
    # The exported case replays: the same output is still refused.
    (tmp_path / "replay").mkdir()
    store, result = replay(tmp_path / "replay", json.loads(target.read_text(encoding="utf-8")))
    assert result is None
    assert store.db.execute("SELECT COUNT(*) FROM llm_failures").fetchone()[0] == 2


def test_forget_removes_stored_failures(tmp_path):
    store, service, path = prepare_service(tmp_path, ReplayModel("next_question", [{"done": "?"}] * 2))
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    with pytest.raises(ValueError):
        service.next_question(store.start_attempt(kp, "first"))
    assert store.db.execute("SELECT COUNT(*) FROM llm_failures").fetchone()[0] == 2
    store.forget(kp)
    assert store.db.execute("SELECT COUNT(*) FROM llm_failures").fetchone()[0] == 0
