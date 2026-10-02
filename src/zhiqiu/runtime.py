from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .materials import Material, combined_hash, read_local
from .model import Model
from .scoring import DimensionGrade, calculate
from .search import Search
from .store import Store


class LearningRuntime:
    def __init__(self, store: Store, model: Model, search: Search | None = None):
        self.store = store
        self.model = model
        self.search = search

    @staticmethod
    def _sources(materials: list[Material]) -> list[dict[str, str]]:
        return [m.as_record() for m in materials]

    def prepare(self, point: str, path: Path, approve: Callable[[list[Material], str], bool],
                offline: bool = False, force_web: bool = False, force_local: bool = False) -> tuple[str, dict[str, Any], list[Material]]:
        materials = read_local(path, allow_empty=not offline and not force_local and self.search is not None)
        if not approve(materials, self.model.endpoint):
            raise ValueError("未获发送资料授权；学习未开始")
        assessment = self.model.assess(point, self._sources(materials))
        if not isinstance(assessment.get("sufficient"), bool):
            raise ValueError("模型未给出有效资料判断")
        if force_web or (not assessment["sufficient"] and not force_local):
            if offline:
                raise ValueError(f"本地资料不足且处于离线模式：{assessment.get('reason', '')}")
            if not self.search:
                raise ValueError("需要 Web 补充，但搜索服务未配置")
            # Ask the model for a search call, then restrict any query based on private
            # local files to the knowledge point entered by the user.
            proposed_query = self.model.search_query(point, str(assessment.get("gap", "")))
            query = point if materials else proposed_query or point
            try:
                web = self.search.search(query)
            except Exception:
                self.store.record_search_call(point, query, 0, "error")
                raise
            self.store.record_search_call(point, query, len(web), "ok")
            if not web:
                raise ValueError("Web 未找到可靠资料；学习未开始")
            gap = str(assessment.get("gap", "")).strip()
            reason = f"补充缺口：{gap}" if gap else "用户要求补充 Web"
            materials.extend(Material(item.kind, item.source, item.content_hash, item.content,
                                      f"{reason}；{item.reason}" if item.reason else reason)
                             for item in web)
            assessment = self.model.assess(point, self._sources(materials))
            if assessment.get("sufficient") is not True:
                raise ValueError(f"本地和 Web 资料仍不足：{assessment.get('reason', '')}")
        elif not assessment["sufficient"] and force_local:
            # An explicit user correction can override the model's adequacy judgment.
            # Teaching still receives only the selected local sources.
            pass
        lesson = self.model.teach(point, self._sources(materials))
        if not isinstance(lesson.get("lesson"), str) or not lesson["lesson"].strip():
            raise ValueError("模型未生成可靠的学习说明")
        kp_id = self.store.ensure_knowledge_point(point)
        self.store.add_materials(kp_id, self._sources(materials))
        self.store.record_consent(kp_id, combined_hash([m for m in materials if m.kind == "local"]), self.model.endpoint)
        return kp_id, lesson, materials

    def sources_for(self, kp_id: str) -> list[dict[str, str]]:
        sources = [dict(r) for r in self.store.materials_for(kp_id)]
        local = [Material(row["kind"], row["source"], row["content_hash"], row["content"])
                 for row in sources if row["kind"] == "local"]
        if not self.store.has_consent(kp_id, combined_hash(local), self.model.endpoint):
            raise ValueError("当前资料或模型端点未获发送授权；请用 learn 重新确认")
        return sources

    def clarify(self, kp_id: str, question: str, attempt_id: str | None = None) -> dict[str, str]:
        if not question.strip():
            raise ValueError("澄清问题不能为空")
        point = self.store.get_knowledge_point(kp_id)
        if not point:
            raise ValueError("unknown knowledge point")
        note_id = self.store.add_note(kp_id, "clarification", question, attempt_id=attempt_id)
        if hasattr(self.model, "set_trace_context"):
            self.model.set_trace_context(kp_id, attempt_id)
        result = self.model.clarify(point["title"], question, self.sources_for(kp_id))
        explanation = result.get("explanation")
        followup = result.get("followup", "")
        if not isinstance(explanation, str) or not explanation.strip() or not isinstance(followup, str):
            raise ValueError("模型未能给出有效澄清解释；问题仍可稍后重试")
        self.store.explain_note(note_id, explanation)
        return {"explanation": explanation.strip(), "followup": followup.strip()}

    def next_question(self, attempt_id: str) -> dict[str, Any]:
        attempt = self.store.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if not attempt:
            raise ValueError("unknown attempt")
        point = self.store.db.execute("SELECT title FROM knowledge_points WHERE id=?", (attempt["kp_id"],)).fetchone()[0]
        if hasattr(self.model, "set_trace_context"):
            self.model.set_trace_context(attempt["kp_id"], attempt_id)
        qa = [dict(r) for r in self.store.qa_for(attempt_id)]
        unanswered = next((row for row in qa if row["answer_id"] is None), None)
        if unanswered:
            return {"done": False, "question_id": unanswered["question_id"], "question": unanswered["question"],
                    "dimension": unanswered["dimension"], "seq": unanswered["seq"]}
        if len(qa) >= 8:
            return {"done": True, "reason": "limit"}
        focus = self.store.wrong_points_for(attempt["kp_id"]) if attempt["kind"] == "review" else []
        proposal = self.model.next_question(point, qa, self.sources_for(attempt["kp_id"]), focus)
        if proposal.get("done") is True:
            return {"done": True, "reason": "natural"}
        dimension = proposal.get("dimension")
        question = proposal.get("question")
        if dimension not in ("concept", "mechanism", "application") or not isinstance(question, str) or not question.strip():
            raise ValueError("模型返回了无效问题；问答保持可恢复")
        question_id, seq = self.store.add_question(attempt_id, dimension, question)
        return {"done": False, "question_id": question_id, "question": question, "dimension": dimension, "seq": seq}

    def finish(self, attempt_id: str, reason: str) -> dict[str, Any]:
        attempt = self.store.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if not attempt:
            raise ValueError("unknown attempt")
        point_row = self.store.db.execute("SELECT * FROM knowledge_points WHERE id=?", (attempt["kp_id"],)).fetchone()
        if hasattr(self.model, "set_trace_context"):
            self.model.set_trace_context(attempt["kp_id"], attempt_id)
        qa = [dict(r) for r in self.store.qa_for(attempt_id)]
        if not any(row["answer_id"] for row in qa):
            return self.store.finalize(attempt_id, [], reason, new_cycle=point_row["state"] == "needs_relearn")
        error: Exception | None = None
        for _ in range(2):
            try:
                proposal = self.model.grade(point_row["title"], qa, self.sources_for(attempt["kp_id"]))
                raw = proposal.get("grades")
                if not isinstance(raw, list):
                    raise ValueError("模型未返回分维度评分数组")
                grades = [DimensionGrade(g["dimension"], g["score"], g["question_id"],
                                         g["answer_id"], g["evidence"], g["rationale"]) for g in raw]
                answers = {row["answer_id"]: (row["question_id"], row["answer"]) for row in qa if row["answer_id"]}
                verified = calculate(grades, answers)
                if verified.total is None:
                    raise ValueError("已有回答，但模型未给出可验证的评分证据")
                return self.store.finalize(attempt_id, grades, reason, new_cycle=point_row["state"] == "needs_relearn")
            except (ValueError, KeyError, TypeError) as exc:
                error = exc
        raise ValueError(f"评阅未通过校验，原问答已保存，可稍后恢复：{error}")
