from datetime import date
import json
import sqlite3

import pytest

from zhiqiu.clock import FixedClock
from zhiqiu.cli import _run_all_old_lessons
from zhiqiu.context import ContextBudgetError, ContextManager, ContextTask
from zhiqiu.memory import MemoryService
from zhiqiu.materials import read_local
from zhiqiu.model import ModelContextOverflowError
from zhiqiu.runtime import LearningRuntime
from zhiqiu.scoring import DimensionGrade
from zhiqiu.store import Store


def store_at(tmp_path):
    return Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 2)))


def test_semantic_memory_is_explicit_and_point_value_overrides_global(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("遍历")
    memory = MemoryService(store)
    global_id = memory.set_semantic("语言", "Python", "用户明确设置")
    point_id = memory.set_semantic("语言", "伪代码", "用户明确纠正", kp)
    assert memory.set_semantic("语言", "伪代码", "用户明确纠正", kp) == point_id
    assert memory.list_semantic(kp)[0]["content"] == "伪代码"
    assert memory.list_semantic()[0]["id"] == global_id
    assert memory.delete_semantic("语言", kp) is True
    assert memory.list_semantic(kp)[0]["content"] == "Python"
    assert store.db.execute("SELECT COUNT(*) FROM semantic_memories").fetchone()[0] == 2


def test_episode_job_is_idempotent_and_revision_invalidates_old_memory(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("遍历")
    attempt = store.start_attempt(kp, "first")
    question, _ = store.add_question(attempt, "concept", "什么是前序")
    answer = store.add_answer(question, "先访问根节点")
    result = store.finalize(attempt, [DimensionGrade("concept", 2, question, answer,
                                                     "先访问根节点", "只覆盖基础")])
    memory = MemoryService(store)
    assert store.db.execute("SELECT status FROM generation_jobs").fetchone()[0] == "pending"
    assert memory.process_episode_jobs(kp) == 1
    assert memory.process_episode_jobs(kp) == 0
    old = memory.episodes_for(kp)[0]
    assert "2/4" in old["summary"]
    store.revise(result["review_id"], "concept", 3, "原答足以支持 3 分")
    assert memory.episodes_for(kp) == []
    assert memory.process_episode_jobs(kp) == 1
    assert "3/4" in memory.episodes_for(kp)[0]["summary"]
    assert store.db.execute("SELECT COUNT(*) FROM episodic_memories WHERE active=1").fetchone()[0] == 1


class SummaryModel:
    def __init__(self):
        self.calls = 0

    def summarize(self, lesson, corrections):
        self.calls += 1
        return f"摘要 {self.calls}：{lesson}；更正：{'；'.join(corrections)}"


def test_lesson_summary_cache_is_invalidated_by_correction(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("遍历")
    source = store.record_message(kp, "assistant", "lesson", "先访问根节点")
    memory = MemoryService(store)
    model = SummaryModel()
    first = memory.lesson_summary(kp, source, model)
    assert memory.lesson_summary(kp, source, model)["id"] == first["id"]
    assert model.calls == 1
    store.correct_message(kp, source, "后序应最后访问根节点")
    assert store.db.execute("SELECT active FROM summaries WHERE id=?", (first["id"],)).fetchone()[0] == 0
    second = memory.lesson_summary(kp, source, model)
    assert second["id"] != first["id"] and "后序" in second["content"]
    assert model.calls == 2


def test_correction_during_summary_keeps_old_generation_job_stale(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("遍历")
    source = store.record_message(kp, "assistant", "lesson", "先看根节点")

    class CorrectingModel:
        def summarize(self, *_):
            store.correct_message(kp, source, "纠正：后序最后看根节点")
            return "旧摘要"

    with pytest.raises(ValueError, match="changed"):
        MemoryService(store).lesson_summary(kp, source, CorrectingModel())
    assert store.db.execute("SELECT status FROM generation_jobs").fetchone()[0] == "stale"
    assert store.db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0


def test_explicit_old_lesson_summary_is_required_context(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("遍历")
    source = store.record_message(kp, "assistant", "lesson", "根节点先访问")

    class LargeSummary:
        def summarize(self, *_):
            return "必须核验的旧讲解" * 500

    manager = ContextManager(store, LargeSummary(), window=1600, output_reserve=200, margin=100)
    with pytest.raises(ContextBudgetError):
        manager.build(ContextTask("clarify", "遍历", kp, current_input="此前怎么讲？",
                                  source_message_id=source))
    audit = store.db.execute("SELECT source_ids_json,status FROM context_builds ORDER BY rowid DESC LIMIT 1").fetchone()
    assert source in audit["source_ids_json"]
    assert audit["status"] == "required_over_budget"


def test_long_lesson_summary_resumes_saved_parts(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("遍历")
    source = store.record_message(kp, "assistant", "lesson", "根左右。" * 350)

    class PartModel:
        def __init__(self):
            self.calls = 0
            self.fail = True

        def summarize(self, lesson, corrections):
            self.calls += 1
            if self.fail and self.calls == 3:
                raise ValueError("temporary summary failure")
            return "片段要点" if not corrections and "[" not in lesson else "完整讲解摘要"

    model = PartModel()
    memory = MemoryService(store)
    with pytest.raises(ValueError, match="temporary summary failure"):
        memory.lesson_summary(kp, source, model, max_input_bytes=1500)
    assert store.db.execute("SELECT COUNT(*) FROM summaries WHERE kind LIKE 'lesson_part:%'").fetchone()[0] == 2
    model.fail = False
    result = memory.lesson_summary(kp, source, model, max_input_bytes=1500)
    assert result["content"] == "完整讲解摘要"
    parts = store.db.execute("SELECT kind FROM summaries WHERE kind LIKE 'lesson_part:%'").fetchall()
    ranges = sorted(tuple(map(int, row["kind"].split(":")[1:])) for row in parts)
    assert ranges[0][0] == 0 and ranges[-1][1] == len("根左右。" * 350)
    assert all(a[1] == b[0] for a, b in zip(ranges, ranges[1:]))


def test_context_build_excludes_optional_memory_but_never_truncates_grade_evidence(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("遍历")
    memory = MemoryService(store)
    memory.set_semantic("偏好", "详细解释" * 500, "用户明确设置", kp)
    context = ContextManager(store, object(), memory, window=1600, output_reserve=200, margin=100)
    request = context.build(ContextTask("clarify", "遍历", kp, current_input="如何遍历"))
    assert request.budget["input_estimate"] <= request.budget["input_limit"]
    audit = store.db.execute("SELECT excluded_json FROM context_builds WHERE id=?", (request.build_id,)).fetchone()[0]
    assert "optional_context_over_budget" in audit
    long_answer = "根左右" * 1000
    with pytest.raises(ContextBudgetError):
        context.build(ContextTask("grade", "遍历", kp, current_input="评阅",
                                  question_history=({"question_id": "q", "answer": long_answer},)))
    assert store.db.execute("SELECT status FROM context_builds ORDER BY rowid DESC LIMIT 1").fetchone()[0] == "required_over_budget"


def test_long_material_checks_cover_every_character_and_resume_after_combine_failure(tmp_path):
    class ChunkModel:
        endpoint = "https://model.example/v1"
        model = "chunk-model-v1"

        def __init__(self):
            self.chunk_calls = 0
            self.fail_combine = True
            self.next_sources = []

        def request(self, request):
            if request.stage == "assess_chunk":
                self.chunk_calls += 1
                return {"relevant": True, "summary": "资料中的遍历规则", "gap": ""}
            if request.stage == "assess_combine":
                if self.fail_combine:
                    raise ValueError("temporary combine failure")
                return {"sufficient": True, "reason": "所有片段已覆盖", "gap": ""}
            if request.stage == "teach":
                return {"lesson": "根据已检查片段讲解遍历", "suggestions": []}
            if request.stage == "next_question":
                import json
                self.next_sources = json.loads(request.messages[-1]["content"])["sources"]
                return {"done": False, "dimension": "concept", "question": "遍历是什么？",
                        "difficulty": "basic", "difficulty_basis": "直接解释"}
            raise AssertionError(request.stage)

    source = tmp_path / "long.txt"
    source.write_text("遍历规则与示例。\n\n" * 90, encoding="utf-8")
    store = store_at(tmp_path)
    model = ChunkModel()
    runtime = LearningRuntime(store, model)
    # Every checked summary carries the absolute source path, so scale the window with it
    # to keep the combine request within budget regardless of where tmp_path lives. 2900 (was 2800) fits the
    # longer question rules of 7.2 and still forces the material into several checked chunks.
    path_bytes = len(str(source.resolve()).encode("utf-8"))
    runtime.context = ContextManager(store, model, window=2900 + 4 * path_bytes,
                                     output_reserve=250, margin=150)
    with pytest.raises(ValueError, match="temporary combine failure"):
        runtime.prepare("遍历", source, lambda *_: True, offline=True)
    rows = store.db.execute("SELECT start_char,end_char FROM material_checks ORDER BY start_char").fetchall()
    assert len(rows) > 1
    assert rows[0]["start_char"] == 0 and rows[-1]["end_char"] == len(source.read_text(encoding="utf-8"))
    assert all(left["end_char"] == right["start_char"] for left, right in zip(rows, rows[1:]))
    assert store.db.execute("SELECT COUNT(*) FROM knowledge_points").fetchone()[0] == 0
    prior_calls = model.chunk_calls
    model.fail_combine = False
    kp, lesson, materials = runtime.prepare("遍历", source, lambda *_: True, offline=True)
    assert kp and lesson["lesson"] and len(materials) == 1
    assert model.chunk_calls == prior_calls
    attempt = store.start_attempt(kp, "first")
    assert runtime.next_question(attempt)["dimension"] == "concept"
    assert model.next_sources and all(row["kind"] == "checked_summary" for row in model.next_sources)


def test_duplicate_content_files_keep_distinct_sources_and_forget_removes_prelearning_audit(tmp_path):
    folder = tmp_path / "sources"
    folder.mkdir()
    for name in ("a.txt", "b.txt"):
        (folder / name).write_text("相同内容", encoding="utf-8")
    materials = read_local(folder)
    assert len(materials) == 2
    assert materials[0].source != materials[1].source
    store = store_at(tmp_path)
    ContextManager(store, object()).build(ContextTask("assess", "遍历", sources=tuple(
        item.as_record() for item in materials)))
    kp = store.ensure_knowledge_point("遍历")
    store.db.execute("""INSERT INTO material_checks VALUES(?,?,?,?,?,?,?,?,?,?)""",
                     ("check", "遍历", materials[0].source, materials[0].content_hash,
                      0, len(materials[0].content), "model", "context-v1", "{}", store._ts()))
    store.forget(kp)
    assert store.db.execute("SELECT COUNT(*) FROM context_builds").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM material_checks").fetchone()[0] == 0


def test_failed_preparation_can_be_forgotten_before_point_exists(tmp_path):
    store = store_at(tmp_path)
    ContextManager(store, object()).build(ContextTask("assess", "未完成知识点"))
    assert store.forget_preparation("未完成知识点") is True
    assert store.db.execute("SELECT COUNT(*) FROM context_builds").fetchone()[0] == 0


def test_model_specific_budget_prefers_exact_endpoint_and_validates_entry(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    model = type("Model", (), {"endpoint": "https://provider.example/v1", "model": "m1"})()
    monkeypatch.setenv("ZHIQIU_MODEL_BUDGETS", json.dumps({
        "m1": {"window": 4000, "output_reserve": 400, "margin": 200, "verified": False},
        "https://provider.example/v1|m1": {"window": 8000, "output_reserve": 500,
                                           "margin": 300, "verified": True}}))
    manager = ContextManager(store, model)
    request = manager.build(ContextTask("assess", "遍历"))
    assert request.budget["input_limit"] == 7200
    assert request.budget["output_reserve_tokens"] == 500
    assert request.budget["margin_tokens"] == 300
    assert request.budget["window_verified"] is True
    store.record_llm_call({"point": "遍历", "model": "m1", "prompt_hash": "synthetic",
                           "latency_ms": 1, "tokens_in": 123, "tokens_out": 4,
                           "status": "ok", "context_build_id": request.build_id})
    audited = json.loads(store.db.execute("SELECT budget_json FROM context_builds WHERE id=?",
                                          (request.build_id,)).fetchone()[0])
    assert audited["input_estimate_bytes"] == request.budget["input_estimate_bytes"]
    assert audited["verified_input_tokens"] == 123
    assert audited["verified_output_tokens"] == 4
    assert audited["verified_token_count_source"] == "provider_usage"
    monkeypatch.setenv("ZHIQIU_MODEL_BUDGETS", '{"m1":{"window":true}}')
    with pytest.raises(ValueError, match="window"):
        ContextManager(store, model)


def test_provider_usage_reused_only_for_exact_prefix_and_new_messages(tmp_path):
    store = store_at(tmp_path)
    model = type("Model", (), {"endpoint": "https://model.example/v1", "model": "m1"})()
    manager = ContextManager(store, model, window=4000, output_reserve=200, margin=100)
    task = ContextTask("assess", "遍历")
    first = manager.build(task)
    manager.observe_usage(first, 47)

    same = manager.build(task)
    assert same.budget["budget_check_method"] == "provider_usage_plus_new_messages"
    assert same.budget["input_estimate_tokens"] == 47
    assert same.budget["verified_prefix_tokens"] == 47
    assert same.budget["estimated_new_messages_tokens"] == 0
    assert same.budget["verified_input_tokens"] is None

    appended = list(first.messages) + [{"role": "user", "content": "新增问题：为什么先根？"}]
    estimate, method, baseline, delta = manager._budget_estimate(appended)
    assert method == "provider_usage_plus_new_messages"
    assert baseline == 47 and delta > 0 and estimate == baseline + delta

    changed = manager.build(ContextTask("assess", "另一知识点"))
    assert changed.budget["budget_check_method"] == "utf8_bytes"
    assert changed.budget["input_estimate_tokens"] is None
    assert changed.budget["input_estimate"] == changed.budget["input_estimate_bytes"]
    manager.observe_usage(changed, 53)
    # An interleaved request must not evict a still reusable prefix.
    estimate, method, baseline, delta = manager._budget_estimate(appended)
    assert method == "provider_usage_plus_new_messages"
    assert baseline == 47 and delta > 0 and estimate == baseline + delta
    from dataclasses import replace
    manager.observe_usage(replace(first, messages=tuple(appended)), 70)
    longer = appended + [{"role": "user", "content": "再问一个"}]
    estimate, method, baseline, delta = manager._budget_estimate(longer)
    assert method == "provider_usage_plus_new_messages"
    assert baseline == 70 and delta > 0 and estimate == baseline + delta
    model.model = "m2"
    assert manager.build(task).budget["budget_check_method"] == "utf8_bytes"


def test_verified_usage_can_admit_same_prompt_while_byte_fallback_rejects_changed_prompt(tmp_path):
    store = store_at(tmp_path)
    manager = ContextManager(store, object(), window=4000, output_reserve=200, margin=100)
    task = ContextTask("assess", "遍历", current_input="汉字" * 200)
    first = manager.build(task)
    assert first.budget["required_estimate_bytes"] > 300
    manager.observe_usage(first, 60)
    manager.window = 500
    same = manager.build(task)
    assert same.budget["input_estimate_tokens"] == 60
    with pytest.raises(ContextBudgetError):
        manager.build(ContextTask("assess", "遍历", current_input="不同内容" * 200))


def test_runtime_records_provider_usage_for_next_budget(tmp_path):
    class UsageModel:
        endpoint = "https://model.example/v1"
        model = "usage-model"

        def request(self, request):
            self.last_usage_request_build_id = request.build_id
            self.last_usage = {"prompt_tokens": 61}
            return {"sufficient": True, "reason": "合成资料", "gap": ""}

    store = store_at(tmp_path)
    runtime = LearningRuntime(store, UsageModel())
    task = ContextTask("assess", "遍历")
    runtime._invoke(task, lambda: {})
    next_request = runtime.context.build(task)
    assert next_request.budget["verified_prefix_tokens"] == 61
    assert next_request.budget["input_estimate_tokens"] == 61


def test_clarifications_append_turns_and_new_sources_after_measured_prefix(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("遍历")
    manager = ContextManager(store, object(), window=4000, output_reserve=200, margin=100)
    first_source = {"source": "preorder", "content_hash": "h1", "content": "前序：根左右"}
    second_source = {"source": "inorder", "content_hash": "h2", "content": "中序：左根右"}
    first_note = store.add_note(kp, "clarification", "为什么先根？")
    first = manager.build(ContextTask("clarify", "遍历", kp, current_input="为什么先根？",
                                      sources=(first_source,), current_note_id=first_note))
    manager.observe_usage(first, 40)
    store.explain_note(first_note, "前序从根开始")
    second_note = store.add_note(kp, "clarification", "中序呢？")
    second = manager.build(ContextTask("clarify", "遍历", kp, current_input="中序呢？",
                                       sources=(first_source, second_source), current_note_id=second_note))
    assert second.budget["assembly_method"] == "append_session_events"
    assert second.budget["verified_prefix_tokens"] == 40
    assert second.messages[:len(first.messages)] == first.messages
    tail = second.messages[len(first.messages):]
    assert [message["role"] for message in tail] == ["assistant", "user", "user"]
    assert "前序从根开始" in tail[0]["content"]
    assert "中序：左根右" in tail[1]["content"]
    assert tail[2]["content"] == "中序呢？"

    changed = manager.build(ContextTask("clarify", "遍历", kp, current_input="中序呢？",
                                        sources=({"source": "preorder", "content_hash": "new",
                                                  "content": "修订资料"},), current_note_id=second_note))
    assert changed.budget["assembly_method"] == "fresh_package"
    assert "前序：根左右" not in str(changed.messages)


def test_append_can_add_older_context_excluded_from_initial_request(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("遍历")
    manager = ContextManager(store, object(), window=4000, output_reserve=200, margin=100)
    store.record_message(kp, "assistant", "lesson", "较早的讲解：中序是左根右")
    first_note = store.add_note(kp, "clarification", "先问前序")
    first = manager.build(ContextTask("clarify", "遍历", kp, current_input="先问前序",
                                      current_note_id=first_note), drop_optional=True)
    assert "较早的讲解" not in str(first.messages)
    store.explain_note(first_note, "前序是根左右")
    second_note = store.add_note(kp, "clarification", "再问中序")
    second = manager.build(ContextTask("clarify", "遍历", kp, current_input="再问中序",
                                       current_note_id=second_note))
    assert second.messages[:len(first.messages)] == first.messages
    tail = str(second.messages[len(first.messages):])
    assert "较早的讲解：中序是左根右" in tail
    assert "先问前序" not in tail


def test_next_question_appends_saved_question_and_answer_without_extra_marker(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("遍历")
    attempt = store.start_attempt(kp, "first")
    manager = ContextManager(store, object(), window=4000, output_reserve=200, margin=100)
    task = ContextTask("next_question", "遍历", kp, attempt_id=attempt)
    first = manager.build(task)
    question_id, _ = store.add_question(attempt, "concept", "前序是什么？", "basic", "直接解释")
    store.add_answer(question_id, "根左右")
    history = tuple(dict(row) for row in store.qa_for(attempt))
    second = manager.build(ContextTask("next_question", "遍历", kp, attempt_id=attempt,
                                       question_history=history))
    assert second.budget["assembly_method"] == "append_session_events"
    assert second.messages[:len(first.messages)] == first.messages
    tail = second.messages[len(first.messages):]
    assert [message["role"] for message in tail] == ["assistant", "user"]
    assert json.loads(tail[0]["content"])["dimension"] == "concept"
    assert tail[1]["content"] == "根左右"


def test_provider_overflow_retries_once_without_optional_context(tmp_path):
    class OverflowModel:
        endpoint = "https://model.example/v1"
        model = "overflow-model"

        def __init__(self):
            self.requests = []

        def request(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                raise ModelContextOverflowError("provider overflow")
            return {"explanation": "有来源的解释", "followup": ""}

    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("遍历")
    from zhiqiu.materials import combined_hash
    model = OverflowModel()
    store.record_consent(kp, combined_hash([]), model.endpoint)
    store.record_message(kp, "assistant", "lesson", "旧讲解")
    result = LearningRuntime(store, model).clarify(kp, "为什么先根？")
    assert result["explanation"] == "有来源的解释"
    assert len(model.requests) == 2
    assert len(model.requests[0].messages) == 3
    assert len(model.requests[1].messages) == 2
    audit = store.db.execute("SELECT status,excluded_json FROM context_builds ORDER BY rowid").fetchall()
    assert audit[0]["status"] == "provider_context_overflow"
    assert "provider_context_overflow_retry" in audit[1]["excluded_json"]


def test_provider_overflow_splits_multi_question_grade_without_duplicate_task(tmp_path):
    class OverflowGradeModel:
        endpoint = "https://model.example/v1"
        model = "overflow-grade-v1"

        def request(self, request):
            history = json.loads(request.messages[-1]["content"])["current_attempt"]
            if request.stage == "grade" or (request.stage == "grade_extract" and len(history) > 1):
                raise ModelContextOverflowError("provider overflow")
            if request.stage == "grade_extract":
                item = history[0]
                return {"assessments": [{"question_id": item["question_id"],
                                         "answer_id": item["answer_id"], "judgment": "基础正确",
                                         "evidence": [item["answer"]]}]}
            if request.stage == "grade_combine":
                return {"score": 2, "rationale": "两题均给出基础证据"}
            raise AssertionError(request.stage)

    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("遍历")
    model = OverflowGradeModel()
    from zhiqiu.materials import combined_hash
    store.record_consent(kp, combined_hash([]), model.endpoint)
    attempt = store.start_attempt(kp, "first")
    for text, answer in (("前序是什么？", "先根后子树"), ("中序是什么？", "左根右")):
        question_id, _ = store.add_question(attempt, "concept", text)
        store.add_answer(question_id, answer)
    result = LearningRuntime(store, model).finish(attempt, "explicit")
    assert result["task_id"]
    assert store.db.execute("SELECT COUNT(*) FROM review_question_assessments").fetchone()[0] == 2
    assert store.db.execute("SELECT COUNT(*) FROM review_tasks").fetchone()[0] == 1


def test_long_clarification_checks_all_ranges_and_resumes_cached_findings(tmp_path):
    class ClarifyModel:
        endpoint = "https://model.example/v1"
        model = "clarify-v1"

        def __init__(self):
            self.chunk_calls = 0
            self.fail_combine = True

        def request(self, request):
            if request.stage == "clarify_chunk":
                self.chunk_calls += 1
                return {"relevant": True, "finding": "先访问根节点", "uncertainty": ""}
            if request.stage == "clarify_combine":
                if self.fail_combine:
                    raise ValueError("temporary combine failure")
                return {"explanation": "检查全部片段后：先访问根节点", "followup": ""}
            raise AssertionError(request.stage)

    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("遍历")
    content = "根节点先访问。\n\n" * 95
    from zhiqiu.materials import Material, combined_hash
    material = Material("local", "local.txt", "hash-v1", content)
    store.add_materials(kp, [material.as_record()])
    model = ClarifyModel()
    store.record_consent(kp, combined_hash([material]), model.endpoint)
    service = LearningRuntime(store, model)
    service.context = ContextManager(store, model, window=2600, output_reserve=200, margin=100)
    with pytest.raises(ValueError, match="temporary combine failure"):
        service.clarify(kp, "这个规则是什么？")
    rows = store.db.execute("SELECT start_char,end_char FROM clarification_chunks ORDER BY start_char").fetchall()
    assert len(rows) > 1
    assert rows[0]["start_char"] == 0 and rows[-1]["end_char"] == len(content)
    assert all(a["end_char"] == b["start_char"] for a, b in zip(rows, rows[1:]))
    previous = model.chunk_calls
    model.fail_combine = False
    assert "全部片段" in service.clarify(kp, "这个规则是什么？")["explanation"]
    assert model.chunk_calls == previous


def test_cross_day_synthesis_splits_and_reuses_completed_batches(tmp_path):
    class BatchModel:
        endpoint = "https://model.example/v1"
        model = "batch-synthesis-v1"

        def __init__(self):
            self.pair_calls = 0
            self.fail_final = True

        def request(self, request):
            assert request.stage == "synthesize_batch"
            items = json.loads(request.messages[-1]["content"])["current_attempt"]
            covered = [source_id for item in items for source_id in item["covered_ids"]]
            if len(covered) == 4 and self.fail_final:
                raise ValueError("temporary final failure")
            if len(covered) == 2:
                self.pair_calls += 1
            return {"covered_ids": covered, "synthesis": "合并" + "、".join(covered)}

    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("遍历")
    model = BatchModel()
    service = LearningRuntime(store, model)
    service.context = ContextManager(store, model, window=2600, output_reserve=200, margin=100)
    results = [{"message_id": f"lesson-{index}", "explanation": "讲解内容" * 45}
               for index in range(4)]
    with pytest.raises(ValueError, match="temporary final failure"):
        service.synthesize_old_answers(kp, "共同点是什么？", results)
    assert store.db.execute("SELECT COUNT(*) FROM synthesis_batches").fetchone()[0] == 2
    assert model.pair_calls == 2
    model.fail_final = False
    assert "lesson-3" in service.synthesize_old_answers(kp, "共同点是什么？", results)
    assert model.pair_calls == 2


class SimpleGradeModel:
    endpoint = "https://model.example/v1"

    def grade(self, point, context, sources):
        row = context[0]
        return {"grades": [{"dimension": "concept", "score": 2,
                            "question_id": row["question_id"], "answer_id": row["answer_id"],
                            "evidence": row["answer"], "rationale": "根据采用的答案"}]}


def test_over_budget_review_waits_then_scores_compact_answer_without_overwriting_original(tmp_path):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("遍历")
    model = SimpleGradeModel()
    from zhiqiu.materials import combined_hash
    store.record_consent(kp, combined_hash([]), model.endpoint)
    attempt = store.start_attempt(kp, "first")
    question, _ = store.add_question(attempt, "concept", "解释前序遍历")
    original = "根左右" * 1500
    answer_id = store.add_answer(question, original)
    service = LearningRuntime(store, model)
    service.context = ContextManager(store, model, window=4000, output_reserve=200, margin=100)
    pending = service.finish(attempt, "explicit")
    assert pending["kind"] == "awaiting_review"
    assert store.db.execute("SELECT state FROM attempts WHERE id=?", (attempt,)).fetchone()[0] == "awaiting_review"
    assert store.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM review_tasks").fetchone()[0] == 0
    submission = store.submit_scoring_answer(answer_id, "精简答案：前序先访问根节点")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        store.db.execute("UPDATE scoring_submissions SET text='被覆盖' WHERE id=?", (submission,))
    result = service.finish(attempt, "explicit")
    assert result["kind"] == "partial" and result["task_id"]
    trace = store.trace(attempt)
    assert trace["questions"][0]["answer"] == original
    assert trace["questions"][0]["scoring_text"] == "精简答案：前序先访问根节点"
    assert trace["question_assessments"][0]["submission_id"] == submission
    assert trace["review_evidence"][0]["quote"] == "精简答案：前序先访问根节点"


def test_runtime_sends_saved_history_and_explicit_preference_through_context_request(tmp_path):
    class RequestModel:
        endpoint = "https://model.example/v1"

        def __init__(self):
            self.requests = []

        def request(self, request):
            self.requests.append(request)
            return {"explanation": "先访问根节点", "followup": ""}

        def clarify(self, *_):
            raise AssertionError("legacy entry should not be called")

    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("遍历")
    from zhiqiu.materials import combined_hash
    model = RequestModel()
    store.record_consent(kp, combined_hash([]), model.endpoint)
    store.record_message(kp, "assistant", "lesson", "前序从根开始")
    MemoryService(store).set_semantic("解释语言", "Python", "用户明确设置", kp)
    runtime = LearningRuntime(store, model)
    result = runtime.clarify(kp, "为什么先根？")
    assert result["explanation"] == "先访问根节点"
    request = model.requests[0]
    assert request.stage == "clarify"
    assert "前序从根开始" in str(request.messages)
    assert "解释语言" in str(request.messages)
    assert str(request.messages).count("为什么先根？") == 1
    assert store.db.execute("SELECT stage FROM context_builds WHERE id=?", (request.build_id,)).fetchone()[0] == "clarify"
    runtime.clarify(kp, "那中序呢？")
    assert model.requests[1].messages[:len(request.messages)] == request.messages
    assert model.requests[1].budget["assembly_method"] == "append_session_events"


def test_all_old_lessons_resume_at_saved_candidate_without_duplicates(tmp_path, monkeypatch, capsys):
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("遍历")
    first = store.record_message(kp, "assistant", "lesson", "第一种讲解")
    second = store.record_message(kp, "assistant", "lesson", "第二种讲解")
    query = store.start_cross_day_query(kp, "比较两次讲解", [first, second])

    class Service:
        def __init__(self):
            self.model = SummaryModel()
            self.calls = []
            self.failed_once = False

        def clarify(self, kp_id, question, source_message_id):
            self.calls.append(source_message_id)
            if source_message_id == second and not self.failed_once:
                self.failed_once = True
                raise ValueError("transient model failure")
            return {"explanation": f"针对 {source_message_id}", "followup": ""}

        def synthesize_old_answers(self, kp_id, question, results):
            assert len(results) == 2
            return "共同点与差异已归纳"

    service = Service()
    monkeypatch.setattr("zhiqiu.cli.runtime", lambda _: service)
    _run_all_old_lessons(store, query["id"])
    assert store.cross_day_query(query["id"])["next_index"] == 1
    _run_all_old_lessons(store, query["id"])
    final = store.cross_day_query(query["id"])
    assert final["status"] == "complete" and final["next_index"] == 2
    assert service.calls == [first, second, second]
    assert "共同点与差异已归纳" in capsys.readouterr().out


def test_multi_question_review_batches_resume_without_reextracting_evidence(tmp_path):
    class BatchModel:
        endpoint = "https://model.example/v1"
        model = "scripted-v2"

        def __init__(self):
            self.extractions = 0
            self.combine_calls = 0

        def request(self, request):
            if request.stage == "grade_extract":
                self.extractions += 1
                rows = __import__("json").loads(request.messages[-1]["content"])["current_attempt"]
                return {"assessments": [{"question_id": row["question_id"],
                                          "answer_id": row["answer_id"],
                                          "judgment": "部分掌握", "evidence": [row["answer"][:12]]}
                                         for row in rows]}
            if request.stage == "grade_combine":
                self.combine_calls += 1
                if self.combine_calls == 1:
                    raise ValueError("transient combine failure")
                return {"score": 2, "rationale": "整体证据支持基础掌握"}
            raise AssertionError(f"unexpected stage: {request.stage}")

    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("遍历")
    from zhiqiu.materials import combined_hash
    model = BatchModel()
    store.record_consent(kp, combined_hash([]), model.endpoint)
    attempt = store.start_attempt(kp, "first")
    for index in range(6):
        question, _ = store.add_question(attempt, "concept", f"解释第 {index} 种情况")
        store.add_answer(question, f"第 {index} 题答案：" + "根左右" * 70)
    service = LearningRuntime(store, model)
    service.context = ContextManager(store, model, window=4000, output_reserve=200, margin=100)
    pending = service.finish(attempt, "explicit")
    assert pending["kind"] == "awaiting_review"
    batch_count = store.db.execute("SELECT COUNT(*) FROM review_batches").fetchone()[0]
    assert batch_count >= 2
    assert store.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    result = service.finish(attempt, "explicit")
    assert result["kind"] == "partial" and result["total"] == 50
    assert store.db.execute("SELECT COUNT(*) FROM review_batches").fetchone()[0] == batch_count
    assert model.extractions == batch_count
    assert len(store.trace(attempt)["question_assessments"]) == 6


class SegmentingModel:
    endpoint = "https://model.example/v1"
    model = "segment-model"

    def __init__(self, fail_combine=0):
        self.stages = []
        self.fail_combine = fail_combine

    def request(self, request):
        self.stages.append(request.stage)
        payload = json.loads(request.messages[-1]["content"])
        if request.stage == "grade_segment":
            text = payload["current_attempt"][0]["segment"]["text"]
            return {"correct_points": ["先访问根"], "errors": [], "open_issues": [], "evidence": [text[:6]]}
        if request.stage == "grade_combine":
            if self.fail_combine:
                self.fail_combine -= 1
                raise ValueError("temporary combine failure")
            assert "分段评阅" in payload["current_attempt"][0]["judgment"]
            return {"score": 3, "rationale": "全部片段一致", "supplement": ""}
        raise AssertionError(f"unexpected stage {request.stage}")


def _long_answer_attempt(tmp_path, model):
    from zhiqiu.materials import combined_hash
    store = store_at(tmp_path)
    kp = store.ensure_knowledge_point("遍历")
    store.record_consent(kp, combined_hash([]), model.endpoint)
    attempt = store.start_attempt(kp, "first")
    question, _ = store.add_question(attempt, "concept", "解释前序遍历")
    answer = "\n".join(f"第{index}段：前序遍历先访问根节点，再左子树，最后右子树。" * 6 for index in range(12))
    answer_id = store.add_answer(question, answer)
    service = LearningRuntime(store, model)
    service.context = ContextManager(store, model, window=4000, output_reserve=200, margin=100)
    return store, service, attempt, answer_id, answer


def test_long_answer_segments_stay_off_until_enabled(tmp_path):
    store, service, attempt, _, _ = _long_answer_attempt(tmp_path, SegmentingModel())
    assert service.finish(attempt, "explicit")["kind"] == "awaiting_review"
    with pytest.raises(ValueError, match="分段评阅默认关闭"):
        service.finish(attempt, "explicit", grade_original=True)
    assert store.db.execute("SELECT COUNT(*) FROM answer_segments").fetchone()[0] == 0


def test_long_answer_segments_cover_original_and_resume_from_cache(tmp_path):
    model = SegmentingModel(fail_combine=1)
    store, service, attempt, answer_id, answer = _long_answer_attempt(tmp_path, model)
    service.long_answer_segments = True
    assert service.finish(attempt, "explicit", grade_original=True)["kind"] == "awaiting_review"
    segments = store.db.execute("SELECT start_char,end_char FROM answer_segments ORDER BY start_char").fetchall()
    assert len(segments) > 1 and segments[0][0] == 0 and segments[-1][1] == len(answer)
    assert all(left[1] == right[0] for left, right in zip(segments, segments[1:]))
    assert store.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    calls = model.stages.count("grade_segment")
    result = service.finish(attempt, "explicit", grade_original=True)
    assert result["kind"] != "awaiting_review"
    assert model.stages.count("grade_segment") == calls
    assert store.db.execute("SELECT score FROM dimension_scores").fetchone()[0] == 3
    assert store.db.execute("SELECT text FROM answers WHERE id=?", (answer_id,)).fetchone()[0] == answer
    evidence = store.db.execute("SELECT quote,start_char,end_char FROM review_evidence").fetchall()
    assert evidence and all(answer[row[1]:row[2]] == row[0] for row in evidence)


def test_long_answer_segment_quote_outside_segment_keeps_review_pending(tmp_path):
    class WrongQuoteModel(SegmentingModel):
        def request(self, request):
            if request.stage == "grade_segment":
                return {"correct_points": ["点"], "errors": [], "open_issues": [], "evidence": ["不存在的引文"]}
            return super().request(request)

    store, service, attempt, _, _ = _long_answer_attempt(tmp_path, WrongQuoteModel())
    service.long_answer_segments = True
    assert service.finish(attempt, "explicit", grade_original=True)["kind"] == "awaiting_review"
    assert store.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM review_tasks").fetchone()[0] == 0


def test_cli_grade_original_explains_that_segmenting_is_disabled(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from zhiqiu.cli import app

    monkeypatch.setattr("zhiqiu.cli.load_local_env", lambda: None)
    monkeypatch.delenv("ZHIQIU_LONG_ANSWER_SEGMENTS", raising=False)
    model = SegmentingModel()
    store, service, attempt, _, _ = _long_answer_attempt(tmp_path, model)
    assert service.finish(attempt, "explicit")["kind"] == "awaiting_review"
    path = store.path
    store.close()
    monkeypatch.setattr("zhiqiu.cli.runtime", lambda s: LearningRuntime(s, model))
    result = CliRunner().invoke(app, ["--db", str(path), "resume", attempt, "--grade-original"])
    assert result.exit_code != 0
    assert "分段评阅默认关闭" in result.output
    assert model.stages == []


def test_long_answer_segment_cap_and_quote_prefix_keep_combine_bounded(tmp_path):
    class LongQuoteModel(SegmentingModel):
        def request(self, request):
            if request.stage == "grade_segment":
                self.stages.append(request.stage)
                text = json.loads(request.messages[-1]["content"])["current_attempt"][0]["segment"]["text"]
                return {"correct_points": ["先访问根"], "errors": [], "open_issues": [], "evidence": [text]}
            return super().request(request)

    store, service, attempt, _, answer = _long_answer_attempt(tmp_path, LongQuoteModel())
    service.long_answer_segments = True
    service.segment_max_chars = 120
    # Many small segments may still leave the combine over budget; that keeps the review pending.
    service.finish(attempt, "explicit", grade_original=True)
    ranges = store.db.execute("SELECT start_char,end_char,result_json FROM answer_segments ORDER BY start_char").fetchall()
    assert all(end - start <= 120 for start, end, _ in ranges)
    assert ranges[-1][1] == len(answer)
    quotes = [quote for *_, raw in ranges for quote in json.loads(raw)["evidence"]]
    assert all(len(quote) <= 80 and quote in answer for quote in quotes)
