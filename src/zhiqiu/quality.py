from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import pricing
from .scoring import DIMENSIONS, RUBRIC_VERSION, WEIGHTS
from .store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_RUNS = REPO_ROOT / "labels" / "eval-runs.jsonl"
# The stages whose prompts decide questions and scores; editing any of them needs a new graded run.
GRADED_STAGES = ("next_question", "classify_turn", "grade", "grade_extract", "grade_segment", "grade_combine")


def prompt_fingerprint() -> dict[str, Any]:
    """sha256 over everything that shapes grading: the stage prompts, the shared data rule, rubric and weights.

    TEMPLATE_VERSION is left out on purpose: it also changes for unrelated stages such as chat.
    """
    from .context import DATA_RULE, TEMPLATES
    parts = {f"template.{stage}": TEMPLATES[stage] for stage in GRADED_STAGES}
    parts["data_rule"] = DATA_RULE
    parts["rubric_version"] = RUBRIC_VERSION
    parts["weights"] = json.dumps({key: str(value) for key, value in WEIGHTS.items()}, sort_keys=True)
    digests = {key: hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] for key, value in parts.items()}
    whole = hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return {"fingerprint": whole, "parts": digests}


def _commit(root: Path = REPO_ROOT) -> str | None:
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True,
                              timeout=10)
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root,
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if head.returncode != 0:
        return None
    return head.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")


def grading_usage(stores: list[Store]) -> dict[str, int]:
    """Tokens the grading of these label databases used, from their llm_calls."""
    tokens_in = tokens_out = cached = 0
    for store in stores:
        row = store.db.execute("""SELECT SUM(tokens_in),SUM(tokens_out),SUM(tokens_cached) FROM llm_calls
            WHERE stage LIKE 'grade%'""").fetchone()
        tokens_in, tokens_out, cached = tokens_in + (row[0] or 0), tokens_out + (row[1] or 0), cached + (row[2] or 0)
    return {"tokens_in": tokens_in, "tokens_out": tokens_out, "tokens_cached": cached}


def record_eval_run(reports: list[dict[str, Any]], set_name: str, usage: dict[str, int],
                    path: Path | None = None, model: str | None = None) -> dict[str, Any]:
    """Append one graded evaluation (one or more runs over the same labels) to the versioned record."""
    if not reports:
        raise ValueError("no evaluation reports to record")
    agreements = [round(report["exact_agreement"], 4) for report in reports]
    mean = round(sum(agreements) / len(agreements), 4)
    evidence_ok = all(report["locatable_evidence"] == report["sample_count"] for report in reports)
    enough = all(report["sample_count"] >= report["minimum_samples"] for report in reports)
    record = {"ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "commit": _commit(),
              **{key: value for key, value in prompt_fingerprint().items() if key == "fingerprint"},
              "rubric_version": RUBRIC_VERSION, "model": model or os.getenv("ZHIQIU_LLM_MODEL", ""),
              "set": set_name, "samples": reports[0]["sample_count"], "runs": agreements,
              "mean_agreement": mean, "evidence_ok": evidence_ok,
              "passed": enough and evidence_ok and mean >= 0.8, **usage,
              "cost": pricing.cost(usage.get("tokens_in"), usage.get("tokens_out"), usage.get("tokens_cached"),
                                   model or None)}
    path = path or EVAL_RUNS
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def eval_runs(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or EVAL_RUNS
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def passing_run(fingerprint: str, path: Path | None = None) -> dict[str, Any] | None:
    """The latest passing evaluation recorded for exactly this grading prompt."""
    return next((run for run in reversed(eval_runs(path))
                 if run.get("fingerprint") == fingerprint and run.get("passed") is True), None)


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
