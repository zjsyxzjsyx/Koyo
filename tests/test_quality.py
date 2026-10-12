from datetime import date

import pytest

from zhiqiu.clock import FixedClock
from zhiqiu.cli import _show_outcome
from zhiqiu.quality import evaluate_labels, label_template
from zhiqiu.scoring import DimensionGrade
from zhiqiu.store import Store


def test_human_labels_check_exact_scores_and_original_evidence(tmp_path, capsys):
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 2)))
    point = store.ensure_knowledge_point("二叉树遍历")
    attempt = store.start_attempt(point, "first")
    grades = []
    for dimension, score in (("concept", 4), ("mechanism", 3), ("application", 1)):
        question, _ = store.add_question(attempt, dimension, dimension)
        answer = store.add_answer(question, f"关于 {dimension} 的原始回答")
        grades.append(DimensionGrade(dimension, score, question, answer, "原始回答", "人工可核对"))
    outcome = store.finalize(attempt, grades)
    review_id = outcome["review_id"]
    _show_outcome(store, outcome)
    visible = capsys.readouterr().out
    assert "概念准确：4/4" in visible and "原题：concept" in visible
    assert "原答：关于 concept 的原始回答" in visible
    template = label_template(store)
    assert len(template) == 3 and all(row["human_score"] is None for row in template)
    assert template[0]["knowledge_point"] == "二叉树遍历"
    trace = store.trace(attempt)
    assert len(trace["dimension_scores"]) == 3 and len(trace["tasks"]) == 1
    labels = [{"review_id": review_id, "dimension": grade.dimension, "human_score": grade.score}
              for grade in grades]
    result = evaluate_labels(store, labels, minimum_samples=3)
    assert result["passed"] is True
    assert result["exact_agreement"] == 1 and result["locatable_evidence"] == 3

    labels[0]["human_score"] = 0
    assert evaluate_labels(store, labels, minimum_samples=3)["passed"] is False
    labels[0]["human_score"] = 4
    store.db.execute("UPDATE review_evidence SET quote='无来源' WHERE review_id=? AND dimension='concept'", (review_id,))
    assert evaluate_labels(store, labels, minimum_samples=3)["passed"] is False
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_labels(store, labels + [labels[0]], minimum_samples=3)
