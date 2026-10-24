import json
import re
from datetime import date
from pathlib import Path

import pytest

from zhiqiu import context
from zhiqiu.clock import FixedClock
from zhiqiu.context import CLARIFY_RULES, DATA_RULE, SKILL_STAGES, TEMPLATE_VERSION, system_message
from zhiqiu.quality import prompt_fingerprint
from zhiqiu.runtime import LearningRuntime
from zhiqiu.skills import Library, parse_text
from zhiqiu.store import Store

V14 = json.loads((Path(__file__).parent / "data" / "v14_skill_templates.json").read_text(encoding="utf-8"))
LEETCODE = """---
name: leetcode
version: 1
title: 刷题讲解
description: 算法题：先手推小例子，再给解题模板和复杂度
stages: [teach]
sections: 2-4
section_chars: 250
diagrams: 0
---

## teach

先用一个很小的输入手推一遍，写出每一步的中间状态，再归纳出解题模板和时间复杂度。
"""


def fragments(text):
    """v14 split into its clauses; a leading 但 joined two clauses that now sit in different parts."""
    parts = (part.strip() for part in re.split(r"[。；：，]", text))
    return [part.removeprefix("但") for part in parts if part]


@pytest.mark.parametrize("stage", SKILL_STAGES)
def test_the_general_skill_keeps_every_v14_clause(stage):
    prompt = system_message(stage)["content"]
    assert prompt.startswith(f"[{TEMPLATE_VERSION}/{stage} · general@2] ")
    prompt = prompt.replace("，可选 diagram 对象", "")  # the one addition inside a v14 clause
    missing = [part for part in fragments(V14[stage]) if part not in prompt]
    assert missing == []
    assert prompt.endswith(DATA_RULE)


def test_new_text_in_the_general_teach_prompt_is_only_the_diagram_rule():
    prompt = system_message("teach")["content"]
    assert "diagram" in prompt and "最多 2 张" in prompt
    assert "diagram" not in V14["teach"]


def test_skill_parameters_shape_the_length_diagram_and_answer_sentences():
    skill = parse_text(LEETCODE, "x", "user", "leetcode")
    teach = system_message("teach", skill)["content"]
    assert "[context-v16/teach · leetcode@1]" in teach and "分成 2 到 4 节，每节正文不超过 250 字" in teach
    assert "diagram" not in teach and "先用一个很小的输入手推一遍" in teach
    # Fixed rules come after the Skill's text, so a Skill cannot drop them.
    assert teach.index("先用一个很小的输入") < teach.index("返回 JSON：sections") < teach.index(DATA_RULE)
    feynman = Library(include_user=False).get("feynman")
    clarify = system_message("clarify", feynman)["content"]
    assert "最多两处问题" in clarify and clarify.index("最多两处问题") < clarify.index(CLARIFY_RULES)
    combine = system_message("clarify_combine", feynman)["content"]
    assert "不得编造来源未提供的原句" in combine and "最多两处问题" in combine


def test_grading_prompts_and_fingerprint_are_untouched():
    # Skills left grading alone; only the 7.2 focus sentence in FOLLOWUP_RULES moved the fingerprint.
    assert prompt_fingerprint()["fingerprint"].startswith("4a7d7e1fac39")
    for stage in ("assess", "assess_combine"):
        assert context.TEMPLATES[stage].startswith(V14[stage]) and "skill_reason" in context.TEMPLATES[stage]


class SkillModel:
    endpoint = "https://model.example/v1"
    model = "skill-model"

    def __init__(self, choice="feynman", reason="遍历顺序容易混，适合先用白话讲清再复述"):
        self.choice = choice
        self.reason = reason
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        if request.stage == "assess":
            answer = {"sufficient": True, "reason": "够用", "gap": ""}
            if self.choice is not None:
                answer["skill"] = self.choice
                answer["skill_reason"] = self.reason
            return answer
        if request.stage == "teach":
            return {"sections": [{"title": "直觉", "content": "先看一棵小树。", "check": "用自己的话说说前序？"}],
                    "suggestions": []}
        return {"explanation": "对，根在最前。", "followup": ""}

    def by_stage(self, stage):
        return [request for request in self.requests if request.stage == stage]


def runtime(tmp_path, model, user_dir=None):
    path = tmp_path / "a.md"
    path.write_text("二叉树遍历的概念、机制和应用。", encoding="utf-8")
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 2)))
    library = Library(user_dir or tmp_path / "no-skills")
    return store, LearningRuntime(store, model, skills=library), path


def span_attrs(store, stage):
    rows = store.db.execute("SELECT attrs_json FROM spans WHERE kind='stage' AND name=? ORDER BY rowid",
                            (stage,)).fetchall()
    return [json.loads(row[0] or "{}") for row in rows]


def test_the_model_picks_from_names_and_descriptions_and_the_lesson_uses_that_skill(tmp_path):
    model = SkillModel()
    store, service, path = runtime(tmp_path, model)
    kp, lesson, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    assess = model.by_stage("assess")[0]
    required = json.loads(assess.messages[-1]["content"])
    assert [item["name"] for item in required["skills"]] == ["general", "feynman"]
    assert all(set(item) == {"name", "title", "description"} for item in required["skills"])
    feynman = service.skills.get("feynman")
    assert feynman.body("teach") not in json.dumps(required, ensure_ascii=False)  # never the body
    teach = model.by_stage("teach")[0]
    assert teach.messages[0]["content"].startswith(f"[{TEMPLATE_VERSION}/teach · feynman@2]")
    assert feynman.body("teach") in teach.messages[0]["content"]
    assert lesson["skill"] == {"name": "feynman", "title": "费曼学习法", "label": "feynman@2", "by": "model",
                               "missing": None}
    row = store.db.execute("SELECT skill,skill_source FROM knowledge_points WHERE id=?", (kp,)).fetchone()
    assert tuple(row) == ("feynman", "model")
    event = json.loads(store.db.execute("SELECT payload_json FROM events WHERE type='SkillChanged'").fetchone()[0])
    assert event == {"from": None, "to": "feynman", "source": "model", "reason": model.reason}
    build = store.db.execute("""SELECT skill_name,skill_version,skill_sha,skill_source FROM context_builds
        WHERE stage='teach'""").fetchone()
    assert tuple(build) == ("feynman", 2, feynman.sha, "builtin")
    assert store.db.execute("SELECT skill_name FROM context_builds WHERE stage='assess'").fetchone()[0] is None
    assert {"skill_choice": "feynman", "skill_invalid": False,
            "skill_reason": model.reason}.items() <= span_attrs(store, "assess")[0].items()
    assert {"skill": "feynman@2", "skill_by": "model", "skill_source": "builtin"}.items() <= span_attrs(store, "teach")[0].items()

    # Clarifying this knowledge point uses the recorded Skill.
    service.clarify(kp, "前序是什么")
    clarify = model.by_stage("clarify")[-1]
    assert "最多两处问题" in clarify.messages[0]["content"]
    assert {"skill": "feynman@2", "skill_by": "model"}.items() <= span_attrs(store, "clarify")[-1].items()


@pytest.mark.parametrize("choice", ["nonexistent", "", None, 3, ["feynman"]])
def test_an_unusable_choice_falls_back_to_general_without_failing(tmp_path, choice):
    model = SkillModel(choice=choice)
    store, service, path = runtime(tmp_path, model)
    kp, lesson, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    assert lesson["skill"]["name"] == "general" and lesson["skill"]["by"] == "fallback"
    assert model.by_stage("teach")[0].messages[0]["content"].startswith(f"[{TEMPLATE_VERSION}/teach · general@2]")
    assert span_attrs(store, "assess")[0]["skill_invalid"] is True
    assert store.db.execute("SELECT skill FROM knowledge_points WHERE id=?", (kp,)).fetchone()[0] == "general"


def test_a_teach_only_skill_is_chosen_and_clarifying_falls_back_to_general(tmp_path):
    user = tmp_path / "skills"
    (user / "leetcode").mkdir(parents=True)
    (user / "leetcode" / "SKILL.md").write_text(LEETCODE, encoding="utf-8")
    model = SkillModel(choice="leetcode")
    store, service, path = runtime(tmp_path, model, user)
    kp, lesson, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    assert lesson["skill"]["label"] == "leetcode@1"
    teach = store.db.execute("SELECT skill_name,skill_source FROM context_builds WHERE stage='teach'").fetchone()
    assert tuple(teach) == ("leetcode", "user")
    service.clarify(kp, "复杂度怎么算")
    assert model.by_stage("clarify")[-1].messages[0]["content"].startswith(
        f"[{TEMPLATE_VERSION}/clarify · general@2]")
    assert span_attrs(store, "clarify")[-1]["skill_by"] == "fallback"


def test_a_user_setting_wins_over_the_model_until_its_skill_is_gone(tmp_path):
    model = SkillModel(choice="feynman")
    store, service, path = runtime(tmp_path, model)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    store.set_skill(kp, "general", "user")
    _, lesson, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    assert lesson["skill"]["name"] == "general" and lesson["skill"]["by"] == "user"
    assert span_attrs(store, "assess")[-1]["skill_choice"] == "feynman"  # still recorded
    assert tuple(store.db.execute("SELECT skill,skill_source FROM knowledge_points").fetchone()) == ("general", "user")
    # The user's Skill disappears: teach with general this time, keep the setting, say so.
    store.set_skill(kp, "vanished", "user")
    _, lesson, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    assert lesson["skill"] == {"name": "general", "title": "通用讲解", "label": "general@2", "by": "fallback",
                               "missing": "vanished"}
    assert tuple(store.db.execute("SELECT skill,skill_source FROM knowledge_points").fetchone()) == ("vanished", "user")


def test_a_knowledge_point_from_before_skills_clarifies_with_general(tmp_path):
    model = SkillModel()
    store, service, path = runtime(tmp_path, model)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    store.db.execute("UPDATE knowledge_points SET skill=NULL,skill_source=NULL WHERE id=?", (kp,))
    service.clarify(kp, "这是什么")
    assert model.by_stage("clarify")[-1].messages[0]["content"].startswith(f"[{TEMPLATE_VERSION}/clarify · general@2]")


def test_changing_the_skill_starts_a_new_clarify_stream(tmp_path):
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 2)))
    manager = context.ContextManager(store, SkillModel())
    library = Library(include_user=False)
    general, feynman = library.general(), library.get("feynman")
    assert manager._stream_key("s", "clarify", general) != manager._stream_key("s", "clarify", feynman)
    assert manager._stream_key("s", "next_question", general) == manager._stream_key("s", "next_question", feynman)
