from datetime import date
import json
import sqlite3

import pytest

from zhiqiu.clock import FixedClock
from zhiqiu.quality import evaluate_labels, label_template
from zhiqiu.runtime import LearningRuntime
from zhiqiu.scoring import DimensionGrade, QuestionAssessment
from zhiqiu.store import Store


def test_multi_question_dimension_keeps_one_score_and_all_evidence(tmp_path):
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 2)))
    kp = store.ensure_knowledge_point("遍历")
    attempt = store.start_attempt(kp, "first")
    basic, _ = store.add_question(attempt, "concept", "什么是前序？", "basic", "直接解释")
    basic_answer = store.add_answer(basic, "前序先访问根节点")
    transfer, _ = store.add_question(attempt, "concept", "新树如何迁移？", "transfer", "新情境应用")
    transfer_answer = store.add_answer(transfer, "新树也先访问根节点")
    unanswered, _ = store.add_question(attempt, "concept", "边界是什么？", "advanced", "辨析边界")
    mechanism, _ = store.add_question(attempt, "mechanism", "访问顺序机制？", "advanced", "解释机制")
    mechanism_answer = store.add_answer(mechanism, "按根左右递归")
    application, _ = store.add_question(attempt, "application", "一个应用？", "basic", "举例")
    application_answer = store.add_answer(application, "表达式树")
    grades = [
        DimensionGrade("concept", 4, "", "", "", "迁移题正确，未答边界题不作已掌握断言", (
            QuestionAssessment(basic, basic_answer, "基础正确", ("先访问根节点",)),
            QuestionAssessment(transfer, transfer_answer, "迁移正确", ("新树也先访问根节点",)),
            QuestionAssessment(unanswered, None, "未回答"),
        )),
        DimensionGrade("mechanism", 3, mechanism, mechanism_answer, "根左右递归", "机制基本正确"),
        DimensionGrade("application", 1, application, application_answer, "表达式树", "只举出示例"),
    ]
    result = store.finalize(attempt, grades)
    assert result["total"] == 78
    trace = store.trace(attempt)
    assert len(trace["dimension_scores"]) == 3
    assert len(trace["question_assessments"]) == 5
    assert len(trace["review_evidence"]) == 4
    assert trace["questions"][1]["difficulty"] == "transfer"
    assert trace["questions"][2]["answer_id"] is None
    assert len(label_template(store)) == 3
    assert evaluate_labels(store, [{"review_id": result["review_id"], "dimension": "concept",
                                    "human_score": 4}], minimum_samples=1)["passed"] is True
    old_task = result["task_id"]
    revised = store.revise(result["review_id"], "concept", 2, "边界未覆盖，降低维度分")
    assert revised["total"] == 53
    assert store.db.execute("SELECT id FROM review_tasks WHERE review_id=?", (result["review_id"],)).fetchone()[0] == old_task
    exported = store.export_json()
    assert len(exported["review_question_assessments"]) == 5
    assert len(exported["review_evidence"]) == 4
    store.forget(kp)
    assert store.db.execute("SELECT COUNT(*) FROM review_question_assessments").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM review_evidence").fetchone()[0] == 0


def test_missing_question_or_false_quote_cannot_finish_attempt(tmp_path):
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 2)))
    kp = store.ensure_knowledge_point("遍历")
    attempt = store.start_attempt(kp, "first")
    first, _ = store.add_question(attempt, "concept", "第一题")
    first_answer = store.add_answer(first, "根左右")
    second, _ = store.add_question(attempt, "concept", "第二题")
    second_answer = store.add_answer(second, "根右左")
    omitted = DimensionGrade("concept", 2, first, first_answer, "根左右", "部分掌握")
    with pytest.raises(ValueError, match="cover every question"):
        store.finalize(attempt, [omitted])
    false_quote = DimensionGrade("concept", 2, "", "", "", "部分掌握", (
        QuestionAssessment(first, first_answer, "基本正确", ("根左右",)),
        QuestionAssessment(second, second_answer, "有误", ("不存在的引文",)),
    ))
    with pytest.raises(ValueError, match="evidence"):
        store.finalize(attempt, [false_quote])
    assert store.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM review_tasks").fetchone()[0] == 0
    assert store.active_attempt(kp)["id"] == attempt


def test_difficulty_is_fixed_when_question_is_created(tmp_path):
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 2)))
    kp = store.ensure_knowledge_point("遍历")
    attempt = store.start_attempt(kp, "first")
    question, _ = store.add_question(attempt, "concept", "迁移题", "transfer", "新情境")
    with pytest.raises(sqlite3.DatabaseError, match="fixed after creation"):
        store.db.execute("UPDATE questions SET difficulty='basic' WHERE id=?", (question,))
    assert store.qa_for(attempt)[0]["difficulty"] == "transfer"


def test_grade_request_lists_dimensions_and_retries_with_validation_feedback(tmp_path):
    from zhiqiu.materials import combined_hash

    class SplittingModel:
        endpoint = "https://model.example/v1"
        model = "split-model"

        def __init__(self):
            self.requests = []

        def request(self, request):
            self.requests.append(request)
            payload = json.loads(request.messages[-1]["content"])
            rows = payload["current_attempt"]
            items = [{"question_id": row["question_id"], "answer_id": row["answer_id"],
                      "judgment": "判断", "evidence": [row["answer"][:2]]} for row in rows]
            if len(self.requests) == 1:
                # One grade per question: the shape seen from the real model.
                return {"grades": [{"dimension": "concept", "score": score, "rationale": "逐题",
                                    "supplement": "", "assessments": [item]}
                                   for score, item in zip((0, 4), items)]}
            return {"grades": [{"dimension": "concept", "score": 2, "rationale": "基础题错，难题对不抵消",
                                "supplement": "", "assessments": items}]}

    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 9)))
    kp = store.ensure_knowledge_point("堆")
    model = SplittingModel()
    store.record_consent(kp, combined_hash([]), model.endpoint)
    attempt = store.start_attempt(kp, "first")
    basic, _ = store.add_question(attempt, "concept", "堆数组有序吗？", "basic", "预设")
    hard, _ = store.add_question(attempt, "concept", "为何能用数组存？", "advanced", "预设")
    store.add_answer(basic, "有序，从小到大")
    store.add_answer(hard, "完全二叉树，下标可算")
    outcome = LearningRuntime(store, model).finish(attempt, "explicit")
    assert outcome["kind"] != "awaiting_review"
    first, second = (json.loads(r.messages[-1]["content"]) for r in model.requests)
    assert first["expected_grades"] == [{"dimension": "concept", "question_ids": [basic, hard]}]
    assert first["current_input"] == ""
    assert "duplicate score dimension" in second["current_input"]
    assert store.db.execute("SELECT score FROM dimension_scores").fetchall()[0][0] == 2
