"""Lessons taught in sections, the learner's "学完了", and one consent for a proposed lesson."""
import json
from datetime import date

import pytest

from zhiqiu import lessons
from zhiqiu.clock import FixedClock
from zhiqiu.context import TEMPLATES
from zhiqiu.flows import outbound_lines, preapproved, resume_lesson, run_lesson
from zhiqiu.materials import Material
from zhiqiu.quality import prompt_fingerprint
from zhiqiu.runtime import LearningRuntime
from zhiqiu.store import Store

from test_flows import ScriptedIO, lesson_state
from test_runtime import FakeModel, prepare_service

SECTIONS = [
    {"title": "为什么要遍历", "content": "把树上每个节点都访问一次。", "check": "遍历要访问几次每个节点？"},
    {"title": "层序遍历", "content": "像排队一样一层一层地走，用队列。", "check": "层序遍历用什么数据结构？"},
    {"title": "前中后序", "content": "区别只在什么时候访问根。", "check": "中序遍历先访问谁？"},
]


class SectionModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.questions = []

    def teach(self, point, sources):
        return {"sections": SECTIONS, "suggestions": ["二叉搜索树"]}

    def clarify(self, point, question, sources):
        self.questions.append(question)
        return super().clarify(point, question, sources)


def shown_markers(store):
    return [row[0] for row in store.db.execute("SELECT content FROM messages WHERE kind='lesson_section' ORDER BY rowid")]


def test_normalize_accepts_sections_and_the_old_single_lesson():
    assert lessons.normalize({"sections": SECTIONS})[1]["title"] == "层序遍历"
    assert lessons.normalize({"lesson": " 整篇讲解 "}) == [{"title": "", "content": "整篇讲解", "check": ""}]
    with pytest.raises(ValueError):
        lessons.normalize({"sections": [{"title": "缺正文", "content": " "}]})
    with pytest.raises(ValueError):
        lessons.normalize({"sections": SECTIONS * 3})
    with pytest.raises(ValueError, match="学习说明"):
        lessons.normalize({"lesson": ""})
    text = lessons.render(lessons.normalize({"sections": SECTIONS}))
    assert text.startswith("第 1/3 节：为什么要遍历") and "想一想：中序遍历先访问谁？" in text
    assert lessons.render(lessons.normalize({"lesson": "整篇讲解"})) == "整篇讲解"


def test_intent_matches_only_exact_phrases():
    assert lessons.intent("学完了") == lessons.intent(" 学完了。") == lessons.intent("开始自测！") == "done"
    assert lessons.intent("继续") == lessons.intent("下一节") == lessons.intent("Next") == "next"
    for answer in ("我还没学完了解前序", "懂了", "明白了", "继续讲讲为什么用队列", "队列"):
        assert lessons.intent(answer) is None


def test_teach_and_clarify_prompts_ask_for_small_steps_without_touching_grading():
    assert "sections" in TEMPLATES["teach"] and "检查问题" in TEMPLATES["teach"]
    assert "200 字" in TEMPLATES["clarify"] and "current_section" in TEMPLATES["clarify"]
    assert "不要宣布或承诺程序动作" in TEMPLATES["clarify"]
    # teach and clarify are not grading stages: the recorded grading fingerprint still applies.
    assert prompt_fingerprint()["fingerprint"].startswith("c5820b23016b")


def test_a_lesson_is_shown_one_section_at_a_time(tmp_path):
    store, service, path = prepare_service(tmp_path, SectionModel())
    io = ScriptedIO("继续", "/pause")
    result = run_lesson(io, store, service, "二叉树遍历", source=path)
    assert "第 1/3 节：为什么要遍历\n把树上每个节点都访问一次。\n想一想：遍历要访问几次每个节点？" in io.said
    assert any(text.startswith("第 2/3 节：层序遍历") for text in io.said)
    assert not any(text.startswith("第 3/3 节") for text in io.said)
    assert not any(text.startswith("建议 1") for text in io.said)  # suggestions wait for the last section
    assert shown_markers(store) == ["已讲到第 1/3 节《为什么要遍历》；检查问题：遍历要访问几次每个节点？",
                                    "已讲到第 2/3 节《层序遍历》；检查问题：层序遍历用什么数据结构？"]
    # The stored lesson is still one message with the whole text, for the quiz and grading.
    lesson = store.db.execute("SELECT content FROM messages WHERE kind='lesson'").fetchone()[0]
    assert "第 3/3 节：前中后序" in lesson
    assert result.kind == "paused"


def test_the_last_section_brings_the_suggestions_and_the_way_to_the_quiz(tmp_path):
    store, service, path = prepare_service(tmp_path, SectionModel())
    io = ScriptedIO("继续", "下一节", "继续", "/pause")
    run_lesson(io, store, service, "二叉树遍历", source=path)
    assert "建议 1: 二叉搜索树" in io.said
    assert "讲解完了。说“学完了”或输入 /done 开始自测，也可以继续提问。" in io.said
    assert "已经是最后一节了。说“学完了”或输入 /done 开始自测，也可以继续提问。" in io.said


def test_saying_done_asks_once_then_starts_the_quiz(tmp_path):
    store, service, path = prepare_service(tmp_path, SectionModel())
    io = ScriptedIO("学完了", "根左右", "递归栈", "表达式树")
    result = run_lesson(io, store, service, "二叉树遍历", source=path)
    assert "还有 2 节没看。结束讲解，开始自测吗？" in io.said
    assert result.kind == "outcome" and lesson_state(store, result.kp_id) == ["tested"]


class NoQuizIO(ScriptedIO):
    """Agrees to send the material but says no when asked to start the quiz."""

    def confirm(self, text):
        self.said.append(text)
        return "开始自测吗" not in text


def test_declining_the_quiz_stays_in_the_lesson_and_done_skips_the_question(tmp_path):
    store, service, path = prepare_service(tmp_path, SectionModel())
    io = NoQuizIO("学完了", "/pause")
    result = run_lesson(io, store, service, "二叉树遍历", source=path)
    assert "好的，继续讲解。" in io.said and result.kind == "paused"
    assert store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0

    (tmp_path / "direct").mkdir()
    store, service, path = prepare_service(tmp_path / "direct", SectionModel())
    io = ScriptedIO("/done", "根左右", "递归栈", "表达式树")
    result = run_lesson(io, store, service, "二叉树遍历", source=path)
    assert result.kind == "outcome" and not any("结束讲解，开始自测吗" in text for text in io.said)


def test_answers_and_questions_go_to_clarification_with_the_section_on_screen(tmp_path):
    model = SectionModel()
    store, service, path = prepare_service(tmp_path, model)
    seen = []
    original = service.clarify

    def clarify(kp_id, question, **kwargs):
        seen.append(kwargs.get("section"))
        return original(kp_id, question, **kwargs)

    service.clarify = clarify
    run_lesson(ScriptedIO("每个一次", "继续", "队列", "/pause"), store, service, "二叉树遍历", source=path)
    assert model.questions == ["每个一次", "队列"]
    assert [section["number"] for section in seen] == ["1/3", "2/3"]
    assert seen[1]["check"] == "层序遍历用什么数据结构？"


def test_the_section_reaches_the_model_request(tmp_path):
    class RequestModel:
        endpoint = "https://model.example/v1"
        model = "request-model"

        def __init__(self):
            self.clarify_inputs = []

        def request(self, request):
            if request.stage == "assess":
                return {"sufficient": True, "reason": "够用", "gap": ""}
            if request.stage == "teach":
                return {"sections": SECTIONS, "suggestions": []}
            self.clarify_inputs.append(json.loads(request.messages[-1]["content"]))  # the required part
            return {"explanation": "对，用队列。", "followup": ""}

    model = RequestModel()
    path = tmp_path / "a.md"
    path.write_text("二叉树遍历的概念、机制和应用。", encoding="utf-8")
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 2)))
    service = LearningRuntime(store, model)
    run_lesson(ScriptedIO("继续", "队列", "/pause"), store, service, "二叉树遍历", source=path)
    assert model.clarify_inputs[0]["current_section"]["title"] == "层序遍历"
    assert model.clarify_inputs[0]["current_section"]["number"] == "2/3"


def test_resume_continues_from_the_first_unseen_section(tmp_path):
    store, service, path = prepare_service(tmp_path, SectionModel())
    run_lesson(ScriptedIO("继续", "/pause"), store, service, "二叉树遍历", source=path)
    session_id = store.pending_lessons()[0]["session_id"]
    io = ScriptedIO("继续", "/pause")
    resume_lesson(io, store, session_id, lambda: service)
    assert "本讲共 3 节：1. 为什么要遍历；2. 层序遍历；3. 前中后序" in io.said
    assert "上次看到第 2 节。" in io.said
    assert any(text.startswith("第 3/3 节：前中后序") for text in io.said)
    assert "已经是最后一节了。说“学完了”或输入 /done 开始自测，也可以继续提问。" in io.said


def test_resume_on_a_later_day_keeps_the_progress(tmp_path):
    store, service, path = prepare_service(tmp_path, SectionModel())
    run_lesson(ScriptedIO("继续", "/pause"), store, service, "二叉树遍历", source=path)
    session_id = store.pending_lessons()[0]["session_id"]
    later = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 3)))
    io = ScriptedIO("/pause")
    resume_lesson(io, later, session_id, lambda: LearningRuntime(later, SectionModel()))
    assert "上次看到第 2 节。" in io.said and any(text.startswith("第 3/3 节") for text in io.said)


def test_a_lesson_taught_before_sections_resumes_whole(tmp_path):
    store, service, path = prepare_service(tmp_path)  # FakeModel returns the old single lesson
    io = ScriptedIO("继续", "/pause")
    run_lesson(io, store, service, "二叉树遍历", source=path)
    assert f"学习 二叉树遍历，依据 {path}" in io.said and "建议 1: 关联知识点" in io.said
    assert shown_markers(store) == []
    assert "已经是最后一节了。说“学完了”或输入 /done 开始自测，也可以继续提问。" in io.said
    io = ScriptedIO("/pause")
    resume_lesson(io, store, store.pending_lessons()[0]["session_id"], lambda: service)
    assert f"学习 二叉树遍历，依据 {path}" in io.said and not any("本讲共" in text for text in io.said)


def test_preapproval_asks_again_only_when_the_send_out_changed():
    material = [Material("local", "D:/notes/a.md", "h", "内容")]
    lines = outbound_lines(material, "https://model.example/v1", ["经 Exa 获取网页正文：https://a.example"])
    io = ScriptedIO(confirm=False)
    approve = preapproved(io, lines)
    assert approve(material, "https://model.example/v1", ["经 Exa 获取网页正文：https://a.example"])
    assert io.said == ["发送资料已在开始前确认。"]
    assert not approve(material, "https://other.example/v1", ["经 Exa 获取网页正文：https://a.example"])
    assert "要发送的资料与开始前确认的不同，需要重新确认。" in io.said
    assert "允许本次学习发送这些内容吗？" in io.said


def test_typed_learn_still_asks_for_consent_once(tmp_path):
    from test_agent import make_agent

    (tmp_path / "notes").mkdir(exist_ok=True)
    note = tmp_path / "notes" / "rbtree.md"
    note.write_text("红黑树的定义、旋转与插入修复。", encoding="utf-8")
    store, model, io, agent = make_agent(tmp_path, [], "/pause")
    agent.handle(f"/learn 红黑树 --from {note}")
    assert io.said.count("允许本次学习发送这些内容吗？") == 1
    assert "要开始吗？" not in io.said
