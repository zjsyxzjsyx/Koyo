import json
import os
import time

import pytest
from typer.testing import CliRunner

from zhiqiu.cli import app
from zhiqiu.files import RootRefused, check_root
from zhiqiu.skills import BUILTIN_DIR, MAX_SKILLS, Library, SkillError, parse_text, skill_stage

# A built-in Skill's text is part of the teaching prompt: change it, raise its version and update this table.
BUILTIN_SHAS = {("general", 2): "002824ff501e", ("feynman", 2): "e9376aeb0118"}

GOOD = """---
name: leetcode
version: 2
title: 刷题讲解
description: 算法题：先手推小例子，再给解题模板和复杂度
stages: [teach]
sections: 2-4
diagrams: 1
---

## teach

先用一个很小的输入手推一遍，写出每一步的中间状态，再归纳出解题模板和时间复杂度。
"""


def write_skill(top, name, text):
    folder = top / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(text, encoding="utf-8")
    return folder / "SKILL.md"


def test_builtin_skills_load_and_match_their_recorded_versions(tmp_path):
    library = Library(tmp_path / "none")
    assert [skill.name for skill in library.all()] == ["general", "feynman"]
    assert {(skill.name, skill.version): skill.sha for skill in library.all()} == BUILTIN_SHAS, (
        "改了内置 Skill 的内容请把 version 加 1，并更新 BUILTIN_SHAS")
    general = library.general()
    assert general.stages == ("teach", "clarify") and general.sections == (3, 5)
    assert general.section_chars == 300 and general.answer_chars == 200 and general.diagrams == 2
    assert library.errors == [] and library.status()["count"] == 2
    # Progressive disclosure: the catalog never carries a body.
    catalog = library.catalog()
    assert [set(item) for item in catalog] == [{"name", "title", "description"}] * 2
    assert general.body("teach") not in json.dumps(catalog, ensure_ascii=False)


def test_a_good_user_skill_is_parsed_with_defaults_for_what_it_leaves_out(tmp_path):
    write_skill(tmp_path, "leetcode", GOOD)
    library = Library(tmp_path)
    skill = library.get("leetcode")
    assert skill.source == "user" and skill.version == 2 and skill.sections == (2, 4)
    assert skill.section_chars == 300 and skill.answer_chars == 200 and skill.diagrams == 1
    assert skill.label == "leetcode@2" and len(skill.sha) == 12
    assert [s.name for s in library.all()] == ["general", "feynman", "leetcode"]
    # No clarify section: clarifying falls back to the general Skill's text.
    used, text = library.body(skill, "clarify_combine")
    assert used.name == "general" and text == library.general().body("clarify")
    assert library.body(skill, "teach")[1].startswith("先用一个很小的输入")
    assert library.find("刷题讲解") is skill and library.find("LeetCode") is skill
    assert skill_stage("clarify_combine") == "clarify" and skill_stage("teach") == "teach"


@pytest.mark.parametrize("change, reason", [
    (lambda t: t.replace("---\nname", "name", 1), "开头必须是 ---"),
    (lambda t: t.replace("stages: [teach]\n", "stages: [teach]\ncolor: red\n"), "不认识的键：color"),
    (lambda t: t.replace("title: 刷题讲解\n", ""), "缺少必填项：title"),
    (lambda t: t.replace("name: leetcode", "name: LeetCode"), "name 只能用小写字母"),
    (lambda t: t.replace("name: leetcode", "name: other"), "必须与目录名"),
    (lambda t: t.replace("version: 2", "version: 0"), "version 必须是正整数"),
    (lambda t: t.replace("description: 算法题", "description: " + "长" * 121), "description 必须是"),
    (lambda t: t.replace("stages: [teach]", "stages: [clarify]"), "必须包含 teach"),
    (lambda t: t.replace("stages: [teach]", "stages: [teach, grade]"), "stages 只能从"),
    (lambda t: t.replace("sections: 2-4", "sections: 5-3"), "sections 必须是"),
    (lambda t: t.replace("sections: 2-4", "sections: 1-9"), "sections 必须是"),
    (lambda t: t.replace("diagrams: 1", "diagrams: 4"), "diagrams 必须是"),
    (lambda t: t.replace("diagrams: 1", "diagrams: 1\nsection_chars: 50"), "section_chars 必须是"),
    (lambda t: t.replace("## teach", "## clarify"), "stages 没列出的阶段"),
    (lambda t: t.replace("## teach\n", "前言\n## teach\n"), "正文必须以 ## teach"),
    (lambda t: t + "\n## teach\n\n再写一遍再写一遍再写一遍再写一遍再写一遍再写一遍再写一遍。\n", "写了两次"),
    (lambda t: t.replace("先用一个很小的输入手推一遍，写出每一步的中间状态，再归纳出解题模板和时间复杂度。", "太短"),
     "要 30 到 1500 字"),
    (lambda t: t.replace("解题模板", "{template}"), "不能有花括号"),
])
def test_broken_skill_files_name_the_file_and_the_reason(change, reason):
    with pytest.raises(SkillError, match=reason) as caught:
        parse_text(change(GOOD), "D:/skills/leetcode/SKILL.md", "user", "leetcode")
    assert str(caught.value).startswith("D:/skills/leetcode/SKILL.md：")


def test_a_skill_missing_a_listed_stage_is_refused():
    text = GOOD.replace("stages: [teach]", "stages: [teach, clarify]")
    with pytest.raises(SkillError, match="缺少 ## clarify 一节"):
        parse_text(text, "x", "user", "leetcode")


def test_broken_user_skills_are_skipped_and_reported_without_stopping(tmp_path):
    write_skill(tmp_path, "leetcode", GOOD)
    write_skill(tmp_path, "bad", GOOD.replace("name: leetcode", "name: bad").replace("version: 2", "version: x"))
    write_skill(tmp_path, "general", GOOD.replace("name: leetcode", "name: general"))
    (tmp_path / "notes").mkdir()  # a folder without SKILL.md is not a Skill
    library = Library(tmp_path)
    assert [s.name for s in library.all()] == ["general", "feynman", "leetcode"]
    assert library.get("general").source == "builtin"
    assert len(library.errors) == 2
    assert any("version 必须是正整数" in error for error in library.errors)
    assert any("和内置 Skill 同名" in error for error in library.errors)


def test_a_broken_builtin_skill_stops_loading(tmp_path):
    builtin = tmp_path / "builtin"
    write_skill(builtin, "general", GOOD.replace("name: leetcode", "name: general"))
    with pytest.raises(SkillError, match="缺少同时包含 teach 和 clarify 的内置 Skill general"):
        Library(tmp_path / "none", builtin_dir=builtin)
    write_skill(builtin, "general", "not a skill")
    with pytest.raises(SkillError, match="开头必须是"):
        Library(tmp_path / "none", builtin_dir=builtin)


def test_more_than_the_limit_are_skipped(tmp_path):
    for index in range(MAX_SKILLS):
        name = f"s{index:02d}"
        write_skill(tmp_path, name, GOOD.replace("name: leetcode", f"name: {name}"))
    library = Library(tmp_path)
    assert len(library.all()) == MAX_SKILLS
    assert len([error for error in library.errors if "超过" in error]) == 2


def test_a_missing_directory_is_fine_and_edits_are_picked_up(tmp_path):
    library = Library(tmp_path / "skills")
    assert library.errors == [] and len(library.all()) == 2
    path = write_skill(tmp_path / "skills", "leetcode", GOOD)
    library.refresh()
    assert library.get("leetcode").version == 2
    path.write_text(GOOD.replace("version: 2", "version: 3"), encoding="utf-8")
    later = time.time() + 5
    os.utime(path, (later, later))
    library.refresh()
    assert library.get("leetcode").version == 3
    path.unlink()
    library.refresh()
    assert library.get("leetcode") is None


def test_user_skills_are_disabled_while_a_write_root_overlaps_them(tmp_path):
    write_skill(tmp_path / "skills", "leetcode", GOOD)
    library = Library(tmp_path / "skills", write_roots=[tmp_path])
    assert library.get("leetcode") is None and "已停用全部用户 Skill" in library.errors[0]
    library.set_write_roots([tmp_path / "other"])
    library.refresh()
    assert library.get("leetcode") is not None and library.errors == []


def test_a_write_root_may_not_hold_or_sit_in_the_skill_directory(tmp_path):
    skills = tmp_path / "data" / "skills"
    (skills / "leetcode").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    for raw, reason in ((tmp_path / "data", "包含 Skill 目录"), (skills, "包含 Skill 目录"),
                        (skills / "leetcode", "在 Skill 目录里")):
        with pytest.raises(RootRefused, match=reason):
            check_root("write", str(raw), [], 0, home=home, system=[], skills=skills)
    # Reading the Skill directory is harmless.
    assert check_root("read", str(skills), [], 0, home=home, system=[], skills=skills).path == str(skills.resolve())


def test_skills_command_lists_skills_and_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHIQIU_SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.delenv("ZHIQIU_WRITE_ROOTS", raising=False)
    write_skill(tmp_path / "skills", "leetcode", GOOD)
    write_skill(tmp_path / "skills", "broken", "oops")
    runner = CliRunner()
    db = str(tmp_path / "state.sqlite")
    result = runner.invoke(app, ["--db", db, "skills"])
    assert result.exit_code == 0, result.output
    assert "general v2（002824ff501e）· 内置 · 通用讲解" in result.output
    assert "leetcode v2" in result.output and "· 用户 ·" in result.output
    assert "错误：" in result.output and "broken" in result.output
    data = json.loads(runner.invoke(app, ["--db", db, "skills", "--json"]).output)
    assert [item["name"] for item in data["skills"]] == ["general", "feynman", "leetcode"]
    assert len(data["errors"]) == 1


def test_builtin_skill_files_live_in_the_package():
    assert (BUILTIN_DIR / "general" / "SKILL.md").is_file() and (BUILTIN_DIR / "feynman" / "SKILL.md").is_file()
