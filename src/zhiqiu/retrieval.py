"""Explainable local ranking of old lessons for a cross-day follow-up question.

Signals are deliberately simple and inspectable: IDF-weighted Chinese character
bigram overlap, the longest shared phrase, and recency. Parameters come from
the fixed Chinese samples in labels/retrieval (see examples/calibrate_retrieval.py);
re-run that calibration before changing them.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable


@dataclass(frozen=True)
class RetrievalParams:
    # Calibrated on labels/retrieval/cases.json with examples/calibrate_retrieval.py:
    # no wrong automatic selection on either the calibration or the holdout half.
    phrase_weight: float = 0.3
    recency_weight: float = 0.05
    half_life_days: float = 7.0
    phrase_cap: int = 8
    min_score: float = 0.4
    margin: float = 0.3
    top_k: int = 3


DEFAULT_PARAMS = RetrievalParams()


# Question and function phrases carry no topic; they split text instead of forming bigrams.
_STOP_PHRASES = ("再讲一下", "再讲一遍", "那部分", "那节课", "为什么", "是什么", "怎么样", "有没有", "而不是",
                 "什么", "怎么", "怎样", "如何", "为何", "是否", "还是", "多少", "哪些", "哪个", "讲一下",
                 "讲一遍", "一下", "一遍", "主要", "讲了", "分别", "可以", "需要", "的", "了", "吗", "呢", "吧", "啊")
_STOP = re.compile("|".join(map(re.escape, _STOP_PHRASES)))
_LATIN = re.compile(r"[a-z0-9]+")


def _segments(text: str) -> list[str]:
    """Lowercased runs of CJK characters, split at punctuation, Latin words and stop phrases."""
    lowered = text.lower()
    runs = re.split(r"[^\u4e00-\u9fff]+", _STOP.sub(" ", lowered))
    return [run for run in runs if run]


def _terms(text: str) -> list[str]:
    """CJK character bigrams within segments plus whole Latin or digit words of two or more characters."""
    terms = [run[index:index + 2] for run in _segments(text) for index in range(max(1, len(run) - 1))]
    return terms + [word for word in _LATIN.findall(text.lower()) if len(word) >= 2]


def _normalize(text: str) -> str:
    return "\x00".join(_segments(text))


def _longest_common(left: str, right: str) -> int:
    if not left or not right:
        return 0
    best = 0
    previous = [0] * (len(right) + 1)
    for char in left:
        current = [0] * (len(right) + 1)
        for index, other in enumerate(right, 1):
            if char == other and char != "\x00":
                current[index] = previous[index - 1] + 1
                best = max(best, current[index])
        previous = current
    return best


def rank_lessons(question: str, candidates: Iterable[dict[str, Any]], today: date,
                 params: RetrievalParams = DEFAULT_PARAMS) -> dict[str, Any]:
    """Rank candidates ({message_id, content, session_date}) and decide whether one is a clear match.

    Returns {"decision": "selected" | "ambiguous" | "no_match", "selected": id | None,
    "ranked": [...]} where each ranked item explains its score.
    """
    rows = list(candidates)
    query = set(_terms(question))
    documents = [set(_terms(row["content"])) for row in rows]
    total = len(rows)
    idf = {term: math.log((total + 1) / (sum(term in doc for doc in documents) + 1)) + 1 for term in query}
    query_weight = sum(idf.values()) or 1.0
    normalized_question = _normalize(question)
    ranked = []
    for row, terms in zip(rows, documents):
        matched = sorted(query & terms)
        overlap = sum(idf[term] for term in matched) / query_weight
        phrase = _longest_common(normalized_question, _normalize(row["content"]))
        phrase_score = min(phrase, params.phrase_cap) / params.phrase_cap if phrase >= 3 else 0.0
        age = max(0, (today - date.fromisoformat(str(row["session_date"]))).days)
        recency = 0.5 ** (age / params.half_life_days)
        score = overlap + params.phrase_weight * phrase_score + params.recency_weight * recency
        ranked.append({"message_id": row["message_id"], "session_date": str(row["session_date"]),
                       "score": round(score, 4),
                       "signals": {"bigram_overlap": round(overlap, 4), "longest_phrase": phrase,
                                   "age_days": age, "recency": round(recency, 4)},
                       "matched_terms": matched[:12]})
    # Highest score first; on equal scores the newer lesson comes first.
    ranked.sort(key=lambda item: item["session_date"], reverse=True)
    ranked.sort(key=lambda item: -item["score"])
    if not ranked or ranked[0]["score"] < params.min_score:
        decision = "no_match"
    elif len(ranked) > 1 and ranked[0]["score"] - ranked[1]["score"] < params.margin:
        decision = "ambiguous"
    else:
        decision = "selected"
    for index, item in enumerate(ranked):
        if decision == "selected" and index == 0:
            item["reason"] = "最高分且领先次高分超过阈值"
        elif item["score"] < params.min_score:
            item["reason"] = "相关度低于阈值"
        elif decision == "selected":
            item["reason"] = "低于已选讲解"
        else:
            item["reason"] = "与其他候选分数接近，需要用户指定"
    return {"decision": decision, "selected": ranked[0]["message_id"] if decision == "selected" else None,
            "ranked": ranked, "params": params.__dict__}
