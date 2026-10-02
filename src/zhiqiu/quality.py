from __future__ import annotations

from typing import Any

from .scoring import DIMENSIONS
from .store import Store


def label_template(store: Store) -> list[dict[str, Any]]:
    """Export score rows for human review without inventing human labels."""
    return [dict(row) | {"human_score": None} for row in store.db.execute("""
        SELECT d.review_id,d.dimension,d.score model_score,k.title knowledge_point,
            q.text question,a.text answer,d.evidence,d.rationale
        FROM dimension_scores d JOIN reviews r ON r.id=d.review_id
        JOIN attempts t ON t.id=r.attempt_id
        JOIN knowledge_points k ON k.id=t.kp_id
        JOIN questions q ON q.id=d.question_id
        JOIN answers a ON a.id=d.answer_id
        ORDER BY r.created_at,d.review_id,d.dimension""")]


def evaluate_labels(store: Store, labels: list[dict[str, Any]], minimum_samples: int = 30) -> dict[str, Any]:
    """Compare human dimension scores with saved model scores and their original answers."""
    if not isinstance(labels, list):
        raise ValueError("labels must be a JSON array")
    seen: set[tuple[str, str]] = set()
    exact = 0
    evidence_ok = 0
    disagreements: list[dict[str, Any]] = []
    for label in labels:
        if not isinstance(label, dict):
            raise ValueError("each label must be an object")
        review_id = label.get("review_id")
        dimension = label.get("dimension")
        human_score = label.get("human_score")
        if (not isinstance(review_id, str) or dimension not in DIMENSIONS or
                type(human_score) is not int or not 0 <= human_score <= 4):
            raise ValueError("label requires review_id, dimension and human_score 0..4")
        key = (review_id, dimension)
        if key in seen:
            raise ValueError("duplicate human label for one dimension score")
        seen.add(key)
        row = store.db.execute("""SELECT d.score,d.evidence,d.question_id,d.answer_id,
            a.text answer,a.question_id original_question_id
            FROM dimension_scores d JOIN answers a ON a.id=d.answer_id
            WHERE d.review_id=? AND d.dimension=?""", key).fetchone()
        if row is None:
            raise ValueError(f"saved dimension score not found: {review_id}/{dimension}")
        if row["score"] == human_score:
            exact += 1
        else:
            disagreements.append({"review_id": review_id, "dimension": dimension,
                                  "model_score": row["score"], "human_score": human_score})
        if row["question_id"] == row["original_question_id"] and row["evidence"] and row["evidence"] in row["answer"]:
            evidence_ok += 1
    sample_count = len(labels)
    agreement = exact / sample_count if sample_count else 0.0
    return {"sample_count": sample_count, "minimum_samples": minimum_samples,
            "exact_matches": exact, "exact_agreement": agreement,
            "locatable_evidence": evidence_ok, "disagreements": disagreements,
            "passed": sample_count >= minimum_samples and agreement >= 0.8 and evidence_ok == sample_count}
