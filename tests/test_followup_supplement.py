from datetime import date

import pytest

from zhiqiu.clock import FixedClock
from zhiqiu.materials import combined_hash
from zhiqiu.quality import label_template
from zhiqiu.runtime import LearningRuntime
from zhiqiu.store import Store


def store_at(tmp_path):
    return Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 9)))


class ScriptedModel:
    endpoint = "https://model.example/v1"
    model = "scripted-followup"

    def __init__(self, responses):
        self.responses = responses
        self.stages = []

    def request(self, request):
        self.stages.append(request.stage)
        return self.responses[request.stage](request)


def prepared(store, model):
    kp = store.ensure_knowledge_point("二叉搜索树")
    store.record_consent(kp, combined_hash([]), model.endpoint)
    return kp, store.start_attempt(kp, "first")


def test_follow_up_targets_one_answered_original_in_same_dimension(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("二叉搜索树")
    attempt = store.start_attempt(kp, "first")
    original, _ = store.add_question(attempt, "concept", "定义是什么？")
    unanswered, _ = store.add_question(attempt, "mechanism", "为什么中序有序？")
    with pytest.raises(ValueError, match="invalid follow-up"):
        store.add_question(attempt, "mechanism", "再具体些？", followup_of=unanswered)
    store.add_answer(original, "左边小右边大")
    store.add_answer(unanswered, "因为左根右")
    with pytest.raises(ValueError, match="invalid follow-up"):
        store.add_question(attempt, "mechanism", "换个维度追问", followup_of=original)
    followup, _ = store.add_question(attempt, "concept", "左子树所有节点都比根小吗？", followup_of=original)
    store.add_answer(followup, "是的，整棵左子树都比根小")
    with pytest.raises(ValueError, match="invalid follow-up"):
        store.add_question(attempt, "concept", "再追问一次同一题", followup_of=original)
    with pytest.raises(ValueError, match="invalid follow-up"):
        store.add_question(attempt, "concept", "追问追问题", followup_of=followup)
    rows = {row["question_id"]: row for row in store.qa_for(attempt)}
    assert rows[followup]["followup_of"] == original
    assert rows[original]["followup_of"] is None


def test_runtime_marks_follow_up_and_rejects_disallowed_target(tmp_path):
    store = store_at(tmp_path)
    proposals = []
    model = ScriptedModel({"next_question": lambda _: proposals.pop(0)})
    _, attempt = prepared(store, model)
    original, _ = store.add_question(attempt, "concept", "定义是什么？")
    store.add_answer(original, "左边小右边大")
    service = LearningRuntime(store, model)
    proposals.append({"done": False, "dimension": "concept", "question": "左子树所有节点都比根小吗？",
                      "difficulty": "basic", "difficulty_basis": "澄清定义", "followup_of": original})
    asked = service.next_question(attempt)
    assert asked["followup"] is True
    store.add_answer(asked["question_id"], "是的")
    proposals.append({"done": False, "dimension": "concept", "question": "还要再追问吗？",
                      "difficulty": "basic", "difficulty_basis": "重复追问", "followup_of": original})
    with pytest.raises(ValueError, match="不允许的追问"):
        service.next_question(attempt)
    proposals.append({"done": False, "dimension": "mechanism", "question": "为什么中序有序？",
                      "difficulty": "advanced", "difficulty_basis": "解释机制", "followup_of": None})
    assert service.next_question(attempt)["followup"] is False


def test_supplement_is_saved_with_score_and_exported_for_labels(tmp_path):
    store = store_at(tmp_path)
    ids = {}

    def grade(_request):
        return {"grades": [{"dimension": "mechanism", "score": 4, "rationale": "两问都答对",
                            "supplement": "hi = lo + 1 时 mid == lo，区间不再缩小。",
                            "assessments": [{"question_id": ids["question"], "answer_id": ids["answer"],
                                             "judgment": "正确", "evidence": ["区间可能不再缩小"]}]}]}

    model = ScriptedModel({"grade": grade})
    _, attempt = prepared(store, model)
    ids["question"], _ = store.add_question(attempt, "mechanism", "为什么不能写 lo = mid？", "advanced", "边界")
    ids["answer"] = store.add_answer(ids["question"], "写成 lo = mid，区间可能不再缩小，导致死循环。")
    outcome = LearningRuntime(store, model).finish(attempt, "explicit")
    assert outcome["kind"] == "partial"
    saved = store.trace(attempt)["dimension_scores"][0]
    assert saved["supplement"] == "hi = lo + 1 时 mid == lo，区间不再缩小。"
    assert saved["rubric_version"] == "rubric-v7"
    assert label_template(store)[0]["supplement"] == saved["supplement"]


def test_v10_database_gains_follow_up_and_supplement_columns(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path, FixedClock(date(2026, 10, 9)))
    store.db.execute("ALTER TABLE dimension_scores DROP COLUMN supplement")
    store.db.execute("ALTER TABLE questions DROP COLUMN followup_of")
    store.db.execute("PRAGMA user_version=10")
    store.close()
    reopened = Store(path, FixedClock(date(2026, 10, 9)))
    assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == 12
    assert "supplement" in {r["name"] for r in reopened.db.execute("PRAGMA table_info(dimension_scores)")}
    assert "followup_of" in {r["name"] for r in reopened.db.execute("PRAGMA table_info(questions)")}
    assert path.with_suffix(".sqlite.pre-v11.bak").exists()
    reopened.close()
