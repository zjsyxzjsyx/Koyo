from __future__ import annotations

from typing import Any

from .scoring import DIMENSIONS
from .store import Store


def label_template(store: Store) -> list[dict[str, Any]]:
    """Export score rows for human review without inventing human labels."""
    result = []
    for row in store.db.execute("""
        SELECT d.review_id,d.dimension,d.score model_score,d.rationale,d.supplement,k.title knowledge_point
        FROM dimension_scores d JOIN reviews r ON r.id=d.review_id
        JOIN attempts t ON t.id=r.attempt_id
        JOIN knowledge_points k ON k.id=t.kp_id
        ORDER BY r.created_at,d.review_id,d.dimension"""):
        assessments = [dict(item) for item in store.db.execute("""
            SELECT qa.question_id,qa.answer_id,qa.judgment,q.text question,q.difficulty,
                a.text answer,s.text submitted_answer
            FROM review_question_assessments qa JOIN questions q ON q.id=qa.question_id
            LEFT JOIN answers a ON a.id=qa.answer_id
            LEFT JOIN scoring_submissions s ON s.id=qa.submission_id
            WHERE qa.review_id=? AND qa.dimension=? ORDER BY q.seq""",
            (row["review_id"], row["dimension"]))]
        for item in assessments:
            item["evidence"] = [dict(e) for e in store.db.execute("""SELECT quote,start_char,end_char
                FROM review_evidence WHERE review_id=? AND dimension=? AND question_id=? ORDER BY rowid""",
                (row["review_id"], row["dimension"], item["question_id"]))]
        result.append(dict(row) | {"assessments": assessments, "human_score": None})
    return result


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
        row = store.db.execute("SELECT score FROM dimension_scores WHERE review_id=? AND dimension=?", key).fetchone()
        if row is None:
            raise ValueError(f"saved dimension score not found: {review_id}/{dimension}")
        if row["score"] == human_score:
            exact += 1
        else:
            disagreements.append({"review_id": review_id, "dimension": dimension,
                                  "model_score": row["score"], "human_score": human_score})
        assessed = store.db.execute("""SELECT qa.question_id,qa.answer_id,qa.submission_id,
            a.question_id original_question_id,a.text original_answer,s.text submitted_answer
            FROM review_question_assessments qa LEFT JOIN answers a ON a.id=qa.answer_id
            LEFT JOIN scoring_submissions s ON s.id=qa.submission_id
            WHERE qa.review_id=? AND qa.dimension=?""", key).fetchall()
        valid = bool(assessed)
        for item in assessed:
            if item["answer_id"] is None:
                if store.db.execute("""SELECT 1 FROM review_evidence WHERE review_id=? AND dimension=?
                    AND question_id=?""", (*key, item["question_id"])).fetchone():
                    valid = False
                continue
            answer_text = item["submitted_answer"] or item["original_answer"]
            if item["question_id"] != item["original_question_id"] or answer_text is None:
                valid = False
            for evidence in store.db.execute("""SELECT quote,start_char,end_char FROM review_evidence
                    WHERE review_id=? AND dimension=? AND question_id=?""", (*key, item["question_id"])):
                if answer_text[evidence["start_char"]:evidence["end_char"]] != evidence["quote"]:
                    valid = False
        if valid:
            evidence_ok += 1
    sample_count = len(labels)
    agreement = exact / sample_count if sample_count else 0.0
    return {"sample_count": sample_count, "minimum_samples": minimum_samples,
            "exact_matches": exact, "exact_agreement": agreement,
            "locatable_evidence": evidence_ok, "disagreements": disagreements,
            "passed": sample_count >= minimum_samples and agreement >= 0.8 and evidence_ok == sample_count}
