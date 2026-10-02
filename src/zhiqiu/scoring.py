from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

DIMENSIONS = ("concept", "mechanism", "application")
WEIGHTS = {"concept": Decimal("0.5"), "mechanism": Decimal("0.3"), "application": Decimal("0.2")}


@dataclass(frozen=True)
class DimensionGrade:
    dimension: str
    score: int
    question_id: str
    answer_id: str
    evidence: str
    rationale: str


@dataclass(frozen=True)
class GradeResult:
    total: int | None
    complete: bool
    dimensions: tuple[DimensionGrade, ...]


def calculate(grades: Iterable[DimensionGrade], answers: dict[str, tuple[str, str]]) -> GradeResult:
    items = tuple(grades)
    if len({g.dimension for g in items}) != len(items):
        raise ValueError("duplicate score dimension")
    for grade in items:
        if grade.dimension not in WEIGHTS or not isinstance(grade.score, int) or not 0 <= grade.score <= 4:
            raise ValueError("invalid dimension or score")
        answer = answers.get(grade.answer_id)
        if answer is None or answer[0] != grade.question_id:
            raise ValueError("score does not reference the original question and answer")
        if not grade.evidence or grade.evidence not in answer[1] or not grade.rationale:
            raise ValueError("score evidence must quote the original answer and explain the decision")
    if not items:
        return GradeResult(None, False, items)
    weight = sum((WEIGHTS[g.dimension] for g in items), Decimal(0))
    weighted = sum((WEIGHTS[g.dimension] * Decimal(g.score) for g in items), Decimal(0))
    total = int((Decimal(100) * weighted / (Decimal(4) * weight)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return GradeResult(total, len(items) == 3, items)


def rating_for(total: int) -> int:
    if not 0 <= total <= 100:
        raise ValueError("score outside 0..100")
    return 1 if total < 50 else 2 if total < 70 else 3 if total < 85 else 4
