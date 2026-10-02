from datetime import date
from pathlib import Path

import pytest

from zhiqiu.clock import FixedClock
from zhiqiu.materials import Material
from zhiqiu.runtime import LearningRuntime
from zhiqiu.scoring import DimensionGrade
from zhiqiu.store import Store


class FakeModel:
    endpoint = "https://model.example/v1"

    def __init__(self, enough=True, query="知识点 解释"):
        self.enough = enough
        self.query = query
        self.assess_calls = 0
        self.last_focus = None

    def assess(self, point, sources):
        self.assess_calls += 1
        return {"sufficient": self.enough or any(s["kind"] == "web" for s in sources),
                "reason": "本地资料缺少解释", "gap": "解释"}

    def teach(self, point, sources):
        return {"lesson": f"学习 {point}，依据 {sources[0]['source']}", "suggestions": ["关联知识点"]}

    def next_question(self, point, context, sources, focus):
        self.last_focus = focus
        dimensions = ("concept", "mechanism", "application")
        if len(context) == 3:
            return {"done": True}
        return {"done": False, "dimension": dimensions[len(context)], "question": f"解释 {dimensions[len(context)]}"}

    def grade(self, point, context, sources):
        scores = (4, 3, 1)
        return {"grades": [{"dimension": row["dimension"], "score": scores[i],
                "question_id": row["question_id"], "answer_id": row["answer_id"],
                "evidence": row["answer"], "rationale": "依据原答"}
                for i, row in enumerate(context) if row["answer_id"]]}

    def clarify(self, point, question, sources):
        return {"explanation": f"根据资料解释：{question}", "followup": "请举一例"}

    def search_query(self, point, gap):
        return self.query


class FakeSearch:
    def __init__(self, results):
        self.results = results
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        return self.results


class NeverDoneModel(FakeModel):
    def next_question(self, point, context, sources, focus):
        return {"done": False, "dimension": ("concept", "mechanism", "application")[len(context) % 3],
                "question": f"第 {len(context) + 1} 题"}

    def grade(self, point, context, sources):
        first = context[0]
        return {"grades": [{"dimension": "concept", "score": 2,
                "question_id": first["question_id"], "answer_id": first["answer_id"],
                "evidence": first["answer"], "rationale": "已有部分证据"}]}


def prepare_service(tmp_path, model=None, search=None):
    path = tmp_path / "a.md"
    path.write_text("二叉树遍历的概念、机制和应用。", encoding="utf-8")
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 2)))
    return store, LearningRuntime(store, model or FakeModel(), search), path


def complete_first(store, service, kp):
    attempt = store.start_attempt(kp, "first")
    for i in range(3):
        question = service.next_question(attempt)
        store.add_answer(question["question_id"], f"第 {i+1} 个真实回答")
    assert service.next_question(attempt)["done"] is True
    return service.finish(attempt, "natural")


def test_local_material_to_first_task(tmp_path):
    store, service, path = prepare_service(tmp_path)
    kp, lesson, sources = service.prepare("二叉树遍历", path, lambda *_: True)
    assert "学习" in lesson["lesson"] and len(sources) == 1
    result = complete_first(store, service, kp)
    assert result["total"] == 78 and result["due_date"] == "2026-10-04"
    assert store.get_knowledge_point("关联知识点") is None
    store.close()
    reopened = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 4)))
    assert reopened.status()["due"][0]["id"] == result["task_id"]


def test_eight_question_limit_forces_partial_next_day(tmp_path):
    store, service, path = prepare_service(tmp_path, NeverDoneModel())
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    attempt = store.start_attempt(kp, "first")
    for number in range(8):
        question = service.next_question(attempt)
        assert question["seq"] == number + 1
        store.add_answer(question["question_id"], f"第 {number + 1} 个回答")
    assert service.next_question(attempt) == {"done": True, "reason": "limit"}
    result = service.finish(attempt, "limit")
    assert result["kind"] == "partial" and result["due_date"] == "2026-10-03"
    assert store.db.execute("SELECT COUNT(*) FROM fsrs_logs").fetchone()[0] == 0


def test_review_questions_receive_original_low_score_evidence(tmp_path):
    store, service, path = prepare_service(tmp_path)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    first = complete_first(store, service, kp)
    store.close()
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 4)))
    model = FakeModel()
    service = LearningRuntime(store, model)
    attempt = store.start_attempt(kp, "review", [first["task_id"]])
    service.next_question(attempt)
    assert len(model.last_focus) == 1
    assert model.last_focus[0]["dimension"] == "application"
    assert model.last_focus[0]["answer"] == "第 3 个真实回答"
    store.revise(first["review_id"], "application", 3, "原答支持基本正确")
    assert store.wrong_points_for(kp) == []


def test_inadequate_material_and_web_failure_create_no_point(tmp_path):
    search = FakeSearch([])
    store, service, path = prepare_service(tmp_path, FakeModel(enough=False), search)
    with pytest.raises(ValueError, match="Web 未找到"):
        service.prepare("二叉树遍历", path, lambda *_: True)
    assert search.queries == ["二叉树遍历"]
    assert store.db.execute("SELECT COUNT(*) FROM knowledge_points").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM review_tasks").fetchone()[0] == 0


def test_missing_search_tool_call_uses_runtime_fallback(tmp_path):
    web = Material("web", "https://example.org/trees", "web-hash", "可靠的遍历解释")
    search = FakeSearch([web])
    store, service, path = prepare_service(tmp_path, FakeModel(enough=False, query=None), search)
    kp, _, sources = service.prepare("二叉树遍历", path, lambda *_: True)
    assert search.queries == ["二叉树遍历"]
    assert {source.kind for source in sources} == {"local", "web"}
    assert next(source.reason for source in sources if source.kind == "web").startswith("补充缺口：解释")
    assert len(store.materials_for(kp)) == 2
    service.prepare("二叉树遍历", path, lambda *_: True, force_local=True)
    assert {row["kind"] for row in store.materials_for(kp)} == {"local"}


def test_empty_local_directory_can_use_web_but_offline_stops(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    web = Material("web", "https://example.org/trees", "web-hash", "可靠的遍历解释")
    search = FakeSearch([web])
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 2)))
    service = LearningRuntime(store, FakeModel(enough=False), search)
    with pytest.raises(ValueError, match="没有可用"):
        service.prepare("二叉树遍历", empty, lambda *_: True, offline=True)
    kp, _, sources = service.prepare("二叉树遍历", empty, lambda *_: True)
    assert [source.kind for source in sources] == ["web"]
    assert store.get_knowledge_point(kp)
    assert search.queries == ["知识点 解释"]


def test_local_material_text_never_becomes_web_query(tmp_path):
    search = FakeSearch([])
    store, service, path = prepare_service(tmp_path, FakeModel(enough=False, query="二叉树遍历的概念、机制和应用"), search)
    with pytest.raises(ValueError, match="Web 未找到"):
        service.prepare("二叉树遍历", path, lambda *_: True)
    assert search.queries == ["二叉树遍历"]


def test_clarification_is_separate_from_wrong_score_and_requires_matching_consent(tmp_path):
    model = FakeModel()
    store, service, path = prepare_service(tmp_path, model)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    result = service.clarify(kp, "这里的遍历顺序是什么？")
    assert result["followup"] == "请举一例"
    note = store.db.execute("SELECT * FROM notes WHERE kp_id=?", (kp,)).fetchone()
    assert note["kind"] == "clarification" and note["explanation"] == result["explanation"]
    assert store.wrong_points_for(kp) == []
    model.endpoint = "https://different-model.example/v1"
    with pytest.raises(ValueError, match="未获发送授权"):
        service.clarify(kp, "切换端点后再问")
    assert store.db.execute("SELECT COUNT(*) FROM notes WHERE kp_id=?", (kp,)).fetchone()[0] == 2


def test_revision_updates_same_pending_task_and_forget_clears_data(tmp_path):
    store, service, path = prepare_service(tmp_path)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    result = complete_first(store, service, kp)
    revised = store.revise(result["review_id"], "concept", 0, "原回答不支持满分")
    assert revised["total"] == 28
    task = store.db.execute("SELECT * FROM review_tasks WHERE id=?", (result["task_id"],)).fetchone()
    assert task["due_date"] == "2026-10-03"
    assert task["original_due_date"] == "2026-10-04"
    assert store.db.execute("SELECT COUNT(*) FROM review_tasks").fetchone()[0] == 1
    store.forget(kp)
    assert store.db.execute("SELECT COUNT(*) FROM knowledge_points").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_zero_review_defers_same_task_and_third_difficulty_requires_relearn(tmp_path):
    store, service, path = prepare_service(tmp_path)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    first = complete_first(store, service, kp)
    store.close()
    for index, day in enumerate((date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6)), 1):
        store = Store(tmp_path / "state.sqlite", FixedClock(day))
        attempt = store.start_attempt(kp, "review", [first["task_id"]])
        outcome = store.finalize(attempt, [], "explicit")
        task = store.db.execute("SELECT * FROM review_tasks WHERE id=?", (first["task_id"],)).fetchone()
        assert task["state"] == "pending" and task["deferrals"] == index
        assert store.db.execute("SELECT COUNT(*) FROM review_tasks").fetchone()[0] == 1
        store.close()
    reopened = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 7)))
    assert outcome["needs_relearn"] is True
    assert reopened.status()["due"] == []
    assert reopened.status()["needs_relearn"][0]["id"] == kp


def test_zero_answers_and_low_score_share_relearn_streak(tmp_path):
    store, service, path = prepare_service(tmp_path)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    first = complete_first(store, service, kp)
    store.close()
    for day in (date(2026, 10, 4), date(2026, 10, 5)):
        store = Store(tmp_path / "state.sqlite", FixedClock(day))
        attempt = store.start_attempt(kp, "review", [first["task_id"]])
        store.finalize(attempt, [], "explicit")
        store.close()
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 6)))
    attempt = store.start_attempt(kp, "review", [first["task_id"]])
    grades = []
    for dimension, score in (("concept", 0), ("mechanism", 0), ("application", 0)):
        question, _ = store.add_question(attempt, dimension, dimension)
        answer_text = "概念正确解释" if dimension == "concept" else "我不会"
        answer = store.add_answer(question, answer_text)
        grades.append(DimensionGrade(dimension, score, question, answer, answer_text, "原答依据"))
    result = store.finalize(attempt, grades)
    assert result["total"] < 50 and result["task_id"] is None
    assert result["needs_relearn"] is True
    assert store.get_knowledge_point(kp)["state"] == "needs_relearn"
    assert store.db.execute("SELECT state FROM review_tasks WHERE id=?", (first["task_id"],)).fetchone()[0] == "done"
    corrected = store.revise(result["review_id"], "concept", 4, "原答实际支持满分")
    assert corrected["state"] == "active" and corrected["total"] == 50
    assert store.db.execute("SELECT COUNT(*) FROM review_tasks WHERE review_id=?", (result["review_id"],)).fetchone()[0] == 1


def test_score_revision_reconciles_relearn_state_and_same_task(tmp_path):
    store, service, path = prepare_service(tmp_path)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    first = complete_first(store, service, kp)
    store.close()
    for day in (date(2026, 10, 4), date(2026, 10, 5)):
        store = Store(tmp_path / "state.sqlite", FixedClock(day))
        attempt = store.start_attempt(kp, "review", [first["task_id"]])
        store.finalize(attempt, [], "explicit")
        store.close()
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 6)))
    attempt = store.start_attempt(kp, "review", [first["task_id"]])
    grades = []
    for dimension, score in (("concept", 4), ("mechanism", 0), ("application", 0)):
        question, _ = store.add_question(attempt, dimension, dimension)
        answer = store.add_answer(question, f"关于 {dimension} 的回答")
        grades.append(DimensionGrade(dimension, score, question, answer,
                                     f"关于 {dimension} 的回答", "原答依据"))
    result = store.finalize(attempt, grades)
    assert result["total"] == 50 and result["task_id"]
    assert store.revise(result["review_id"], "concept", 0, "概念回答实际错误")["state"] == "needs_relearn"
    assert store.db.execute("SELECT state FROM review_tasks WHERE id=?", (result["task_id"],)).fetchone()[0] == "relearn_handled"
    assert store.revise(result["review_id"], "concept", 4, "复核后恢复原分")["state"] == "active"
    assert store.db.execute("SELECT state FROM review_tasks WHERE id=?", (result["task_id"],)).fetchone()[0] == "pending"
    assert store.db.execute("SELECT COUNT(*) FROM review_tasks WHERE review_id=?", (result["review_id"],)).fetchone()[0] == 1


def test_multiple_overdue_tasks_for_one_point_merge_into_one_review(tmp_path):
    store, service, path = prepare_service(tmp_path)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    first = complete_first(store, service, kp)
    store.close()
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 10)))
    # Seed a second pending task to simulate a backlog imported from older history.
    store.db.execute("""INSERT INTO attempts(id,kp_id,kind,state,start_date,started_at,ended_at,finish_reason)
        VALUES('legacy-attempt',?,'review','completed','2026-10-03','2026-10-03','2026-10-03','natural')""", (kp,))
    store.db.execute("""INSERT INTO reviews(id,attempt_id,kind,total,rating,created_at)
        VALUES('legacy-review','legacy-attempt','partial',50,NULL,'2026-10-03')""")
    store.db.execute("""INSERT INTO review_tasks(id,kp_id,review_id,due_date,original_due_date,
        next_remind_date,state,deferrals,rule_version,created_at)
        VALUES('legacy-task',?,'legacy-review','2026-10-03','2026-10-03',
        '2026-10-03','pending',0,'partial-next-day-v1','2026-10-03')""", (kp,))
    due_ids = [task["id"] for task in store.due_tasks()]
    assert due_ids == ["legacy-task", first["task_id"]]
    attempt = store.start_attempt(kp, "review", due_ids)
    assert store.db.execute("SELECT COUNT(*) FROM attempt_tasks WHERE attempt_id=?", (attempt,)).fetchone()[0] == 2
    question, _ = store.add_question(attempt, "concept", "解释概念")
    answer = store.add_answer(question, "部分正确")
    result = store.finalize(attempt, [DimensionGrade("concept", 2, question, answer, "部分正确", "仍有缺口")], "explicit")
    assert result["kind"] == "partial"
    assert [store.db.execute("SELECT state FROM review_tasks WHERE id=?", (task_id,)).fetchone()[0]
            for task_id in due_ids] == ["done", "done"]
    assert store.db.execute("SELECT COUNT(*) FROM review_tasks WHERE state='pending'").fetchone()[0] == 1


def test_review_quota_and_historical_revision_replay(tmp_path):
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 2)))
    first_tasks = []
    points = []
    for index in range(4):
        point = store.ensure_knowledge_point(f"知识点{index}")
        attempt = store.start_attempt(point, "first")
        grades = []
        for dimension, score in (("concept", 4), ("mechanism", 3), ("application", 1)):
            q, _ = store.add_question(attempt, dimension, dimension)
            a = store.add_answer(q, f"回答 {dimension}")
            grades.append(DimensionGrade(dimension, score, q, a, f"回答 {dimension}", "原答依据"))
        first_tasks.append(store.finalize(attempt, grades))
        points.append(point)
    store.close()

    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 4)))
    for i in range(3):
        store.start_attempt(points[i], "review", [first_tasks[i]["task_id"]])
    with pytest.raises(ValueError, match="daily review limit"):
        store.start_attempt(points[3], "review", [first_tasks[3]["task_id"]])
    assert len(store.due_tasks()) == 4

    actual = store.active_attempt(points[0])
    grades = []
    for dimension, score in (("concept", 4), ("mechanism", 3), ("application", 1)):
        q, _ = store.add_question(actual["id"], dimension, dimension)
        a = store.add_answer(q, f"复习回答 {dimension}")
        grades.append(DimensionGrade(dimension, score, q, a, f"复习回答 {dimension}", "复习依据"))
    later = store.finalize(actual["id"], grades)
    pending_before = store.db.execute("SELECT * FROM review_tasks WHERE id=?", (later["task_id"],)).fetchone()
    store.revise(first_tasks[0]["review_id"], "concept", 0, "首次评分错误")
    pending_after = store.db.execute("SELECT * FROM review_tasks WHERE id=?", (later["task_id"],)).fetchone()
    assert pending_after["id"] == pending_before["id"]
    assert pending_after["original_due_date"] == pending_before["original_due_date"]
    assert store.db.execute("SELECT state FROM review_tasks WHERE id=?", (first_tasks[0]["task_id"],)).fetchone()[0] == "done"


def test_relearn_starts_new_fsrs_cycle_and_closes_old_task(tmp_path):
    store, service, path = prepare_service(tmp_path)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    first = complete_first(store, service, kp)
    store.db.execute("UPDATE knowledge_points SET state='needs_relearn',difficulty_streak=3 WHERE id=?", (kp,))
    attempt = store.start_attempt(kp, "first")
    grades = []
    for dimension, score in (("concept", 4), ("mechanism", 3), ("application", 1)):
        q, _ = store.add_question(attempt, dimension, dimension)
        a = store.add_answer(q, f"补学回答 {dimension}")
        grades.append(DimensionGrade(dimension, score, q, a, f"补学回答 {dimension}", "补学依据"))
    result = store.finalize(attempt, grades, new_cycle=True)
    assert result["task_id"] != first["task_id"]
    assert store.db.execute("SELECT state FROM review_tasks WHERE id=?", (first["task_id"],)).fetchone()[0] == "relearn_handled"
    assert store.get_knowledge_point(kp)["state"] == "active"
    assert store.db.execute("SELECT COUNT(*) FROM fsrs_cycles WHERE kp_id=?", (kp,)).fetchone()[0] == 2


def test_partial_relearn_remains_unplanned_until_full_assessment(tmp_path):
    store, service, path = prepare_service(tmp_path)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    store.db.execute("UPDATE knowledge_points SET state='needs_relearn',difficulty_streak=3 WHERE id=?", (kp,))
    attempt = store.start_attempt(kp, "first")
    q, _ = store.add_question(attempt, "concept", "解释概念")
    a = store.add_answer(q, "部分概念")
    result = store.finalize(attempt, [DimensionGrade("concept", 2, q, a, "部分概念", "只覆盖概念")], new_cycle=True)
    assert result["kind"] == "partial" and result["task_id"] is None
    assert store.get_knowledge_point(kp)["state"] == "needs_relearn"
