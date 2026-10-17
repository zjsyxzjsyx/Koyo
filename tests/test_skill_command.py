import json
from datetime import date

from zhiqiu import contracts, traces
from zhiqiu.agent import _command_parts
from zhiqiu.dashboard import attempt_detail
from zhiqiu.flows import run_lesson, run_quiz

from test_agent import make_agent
from test_flows import ScriptedIO
from test_lessons import SectionModel
from test_runtime import prepare_service
from test_skill_selection import SkillModel, runtime


def skill_events(store):
    return [json.loads(row[0]) for row in store.db.execute(
        "SELECT payload_json FROM events WHERE type='SkillChanged' ORDER BY id")]


def test_command_parts_take_the_rest_of_the_line_as_the_title():
    assert _command_parts("/skill") == ["/skill"]
    assert _command_parts("/skill feynman") == ["/skill", "feynman"]
    assert _command_parts("/skill feynman Python's GIL") == ["/skill", "feynman", "Python's GIL"]
    assert _command_parts('/skill auto "二叉树  遍历"') == ["/skill", "auto", "二叉树  遍历"]


def test_a_lesson_shows_how_it_is_taught_and_skill_changes_the_current_point(tmp_path):
    store, service, path = runtime(tmp_path, SkillModel())
    io = ScriptedIO("/skill", "/skill 通用讲解", "/skill nope", "/skill auto", "/pause")
    run_lesson(io, store, service, "二叉树遍历", source=path)
    said = "\n".join(io.said)
    assert "讲法：费曼学习法（/skill 可改）" in said
    assert "「二叉树遍历」的讲法：费曼学习法（feynman，由知秋按资料选择）" in said
    assert "general · 通用讲解（内置）" in said and "feynman · 费曼学习法（内置）" in said
    assert "已改用通用讲解（general）" in said and "想用新方式重讲，结束后再 /learn 一次" in said
    assert "没有这个 Skill：nope" in said
    assert "已清除指定的讲法" in said
    assert [(e["to"], e["source"]) for e in skill_events(store)] == [("feynman", "model"), ("general", "user"),
                                                                      (None, "user")]
    assert tuple(store.db.execute("SELECT skill,skill_source FROM knowledge_points").fetchone()) == (None, None)


def test_skill_in_a_quiz_is_a_command_not_an_answer(tmp_path):
    store, service, path = prepare_service(tmp_path, SectionModel())
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    attempt = store.start_attempt(kp, "first")
    io = ScriptedIO("/skill feynman", "/pause")
    run_quiz(io, store, service, attempt)
    assert store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 0
    assert any("已改用费曼学习法" in line for line in io.said)
    assert store.db.execute("SELECT skill_source FROM knowledge_points").fetchone()[0] == "user"


def test_chat_lists_skills_and_sets_one_by_title(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [])
    kp = store.ensure_knowledge_point("Python's GIL")
    agent.handle("/skill")
    agent.handle("/skill feynman")
    agent.handle("/skill feynman 不存在的知识点")
    agent.handle("/skill feynman Python's GIL")
    said = io.text
    assert "可用的 Skill：" in said and "feynman · 费曼学习法" in said
    assert "在对话里请写出知识点" in said and "没有这个知识点：不存在的知识点" in said
    assert "已改用费曼学习法（feynman）" in said
    assert tuple(store.db.execute("SELECT skill,skill_source FROM knowledge_points WHERE id=?", (kp,)).fetchone()) == (
        "feynman", "user")
    assert "/skill" in agent._help()
    assert model.seen == []  # slash commands never call the model


def test_trace_check_and_attempt_detail_show_the_skill(tmp_path):
    model = SkillModel()
    store, service, path = runtime(tmp_path, model)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    service.clarify(kp, "前序是什么")
    result = next(item for item in contracts.check(store.db, 3650)["results"] if item["check"] == "skills_recorded")
    assert result["status"] == "pass" and result["checked"] == 2
    store.db.execute("UPDATE context_builds SET skill_sha=NULL WHERE stage='clarify'")
    result = next(item for item in contracts.check(store.db, 3650)["results"] if item["check"] == "skills_recorded")
    assert result["status"] == "fail" and result["evidence"][0]["stage"] == "clarify"
    attempt = store.start_attempt(kp, "first")
    detail = attempt_detail(store.db, attempt)
    assert detail["knowledge_point"]["skill"] == "feynman" and detail["knowledge_point"]["skill_source"] == "model"


def test_llm_detail_names_the_skill_of_its_context_build(tmp_path):
    store, service, path = runtime(tmp_path, SkillModel())
    build_id = None
    service.prepare("二叉树遍历", path, lambda *_: True)
    build_id = store.db.execute("SELECT id FROM context_builds WHERE stage='teach'").fetchone()[0]
    store.db.execute("""INSERT INTO llm_calls(id,kp_id,attempt_id,point,model,prompt_hash,latency_ms,tokens_in,
        tokens_out,status,created_at,context_build_id) VALUES('call-1',NULL,NULL,'二叉树遍历','m','h',1,1,1,'ok',
        '2026-10-02T00:00:00',?)""", (build_id,))
    detail = traces.llm_detail(store.db, "call-1")
    assert detail["build"]["skill"]["name"] == "feynman" and detail["build"]["skill"]["source"] == "builtin"
