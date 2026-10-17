from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

DIMENSIONS = ("concept", "mechanism", "application")
WEIGHTS = {"concept": Decimal("0.5"), "mechanism": Decimal("0.3"), "application": Decimal("0.2")}
RUBRIC_VERSION = "rubric-v7"
# Anchors match the human-labelled samples under labels/. There is deliberately no explicit
# rule for a correct hard question with a wrong basic one: stating it pushed those scores
# below the human labels, while the model already weighs such conflicts as labelled.
RUBRIC = ("0=回答不会，或完全错误、没有任何正确点；只要答对一些就至少给 1。"
          "“答对一些”指回答中有与题目要求相关、且本身正确的陈述；复述题目已给出的结论，"
          "或把正确的词用在错误的推理里，不算正确点。"
          "1=有零散正确点，但关键结论错误或核心缺失。"
          "2=基础要求正确，但深层原因、前提条件或机制解释不足。"
          "3=基本完成题目要求，但有一处较明显的不足。"
          "4=完全答对题目要求，或只有非关键遗漏（可能只是没讲到，不代表不懂）；"
          "不因题目难度不是 transfer 而封顶。"
          "缺少的内容若是区分正确理解与常见误解的核心条件（例如省略了定义的适用范围或限定条件，导致无法与常见误解区分），"
          "不算非关键遗漏，最高给 2；含糊到无法判断是否理解时，应先追问。"
          "有非关键遗漏时不扣分，但须在 supplement 中简要补充遗漏点；没有遗漏时 supplement 为空字符串。"
          "追问的回答与原题回答一起作为该维度证据；追问澄清了原答的含糊表述时，按澄清后的理解评分。"
          "同一维度有未答题时，未答题覆盖的要求视为未被证明，该维度不给 4：只答了基础题且答对时最高给 2，"
          "已答题中有答对的进阶或迁移题时最高给 3；须在理由中标明证据范围。")
FOLLOWUP_RULES = ("进行费曼问答，返回 JSON。先覆盖尚无回答的 concept、mechanism、application 维度。"
                  "本次问答已有的题和回答只看 current_attempt；focus 是以前学习中得分低的旧题、旧回答和评分理由，"
                  "只用来决定复习重点：focus 里的题不算本次已作答，也不能被追问。"
                  "复习时优先针对 focus 中的错点出新题，不要原样重复旧题。"
                  "三个维度都已作答后，结束前必须逐题检查已答题：若某题回答含糊到无法判断是否理解"
                  "（只有结论或口语化概括，缺少区分正确理解与常见误解的关键条件），且该题尚未被追问，"
                  "则 done=false，在同一维度追问一次，followup_of 填该题 question_id。"
                  "明确答对或明确答错（包括表述清楚的误解）的回答不追问。"
                  "追问只针对原答里最含糊的一句：可以用引号引用学习者的原话，问一个比原题更小的问题，"
                  "请学习者说明理由或举一个例子；不得重复或改写原题，不得要求“展开讲”“完整讲一遍”。"
                  "追问不得在问题中给出正确结论、关键词或正确做法，也不得指出原答哪里错。不得追问追问题或重复追问同一题。"
                  "没有需要追问的题时 done=true；非追问的题 followup_of=null。"
                  "字段：done 布尔值；未结束时 dimension 为 concept/mechanism/application 之一，"
                  "question 与 difficulty_basis 非空，difficulty 为 basic/advanced/transfer 之一。"
                  "难度只根据题目要求确定，不能在看到答案后修改。"
                  "每题只问一件事：只含一个问号，题干不超过 60 字，学习者用 2 到 4 句话能答完；"
                  "不要用“并且”“同时”“另外请”把多个要求串在一起。"
                  "需要手推执行过程的题，题目自己给一个最小例子（例如三个节点的树），只问其中一步。")


# A quiz question asks one thing: at most one question mark and this many characters (spaces not counted).
# The prompt asks for 60; the check allows 80. Not part of the prompt fingerprint, so a change here must be
# written down in the PRD's implementation record.
QUESTION_MAX_CHARS = 80
QUESTION_MAX_MARKS = 1


def question_shape(question: str) -> str | None:
    """Why a proposed question is not "one question, one thing", or None when it is."""
    marks = question.count("？") + question.count("?")
    if marks > QUESTION_MAX_MARKS:
        return f"一题只能问一件事：题目里有 {marks} 个问号"
    length = len("".join(question.split()))
    if length > QUESTION_MAX_CHARS:
        return f"题目过长：{length} 字，上限 {QUESTION_MAX_CHARS}"
    return None


@dataclass(frozen=True)
class QuestionAssessment:
    question_id: str
    answer_id: str | None
    judgment: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class DimensionGrade:
    dimension: str
    score: int
    question_id: str
    answer_id: str
    evidence: str
    rationale: str
    assessments: tuple[QuestionAssessment, ...] = ()
    supplement: str = ""
    # One sentence for the learner (shown in the grading summary); never part of the score.
    feedback: str = ""


@dataclass(frozen=True)
class GradeResult:
    total: int | None
    complete: bool
    dimensions: tuple[DimensionGrade, ...]


def assessments_for(grade: DimensionGrade) -> tuple[QuestionAssessment, ...]:
    if grade.assessments:
        return grade.assessments
    return (QuestionAssessment(grade.question_id, grade.answer_id, grade.rationale,
                               (grade.evidence,)),)


def calculate(grades: Iterable[DimensionGrade], answers: dict[str, tuple[str, str]],
              questions: dict[str, tuple[str, str | None]] | None = None) -> GradeResult:
    items = tuple(grades)
    if len({g.dimension for g in items}) != len(items):
        raise ValueError("duplicate score dimension")
    if questions is not None:
        answered_dimensions = {dimension for dimension, answer_id in questions.values() if answer_id}
        if {g.dimension for g in items} != answered_dimensions:
            raise ValueError("every answered dimension must be assessed")
    for grade in items:
        if grade.dimension not in WEIGHTS or type(grade.score) is not int or not 0 <= grade.score <= 4:
            raise ValueError("invalid dimension or score")
        if not isinstance(grade.rationale, str) or not grade.rationale.strip():
            raise ValueError("dimension score requires a rationale")
        assessments = assessments_for(grade)
        ids = [item.question_id for item in assessments]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate question assessment")
        if questions is not None:
            expected = {qid for qid, (dimension, _) in questions.items() if dimension == grade.dimension}
            if set(ids) != expected or not any(questions[qid][1] for qid in expected):
                raise ValueError("dimension assessment must cover every question and an answer")
        for item in assessments:
            if not item.judgment.strip():
                raise ValueError("question assessment requires a judgment")
            if questions is not None:
                question = questions.get(item.question_id)
                if not question or question[0] != grade.dimension or question[1] != item.answer_id:
                    raise ValueError("assessment does not match the question and answer")
            if item.answer_id is None:
                if item.evidence:
                    raise ValueError("unanswered question cannot have evidence")
                continue
            answer = answers.get(item.answer_id)
            if answer is None or answer[0] != item.question_id:
                raise ValueError("score does not reference the original question and answer")
            if not isinstance(item.evidence, tuple) or any(
                    not isinstance(quote, str) or not quote or quote not in answer[1]
                    for quote in item.evidence):
                raise ValueError("score evidence must quote the original answer")
            if not grade.assessments and not item.evidence:
                raise ValueError("score evidence must quote the original answer")
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
