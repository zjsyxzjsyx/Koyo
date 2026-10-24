import json
from datetime import date

import pytest

from zhiqiu.plans import Plans
from zhiqiu import plan_drafts
from zhiqiu.plan_drafts import DraftInvalid, Signals

from test_agent import ChatModel, make_agent, tool
from test_flows import ScriptedIO
from test_runtime import complete_first

DAY = date(2026, 10, 13)


def plan_units(n=3, source=None):
    units = []
    for i in range(1, n + 1):
        units.append({"key": f"u{i}", "title": f"红黑树第{i}课", "goal": f"学会第{i}课", "minutes": 30,
                      "requires": [f"u{i - 1}"] if i > 1 else [],
                      "source": source(i) if source else {"kind": "lesson_web", "ref": ""}})
    return units


def plan_answer(units, **extra):
    return {"title": "红黑树", "summary": "从插入到删除", "basis_note": "你的笔记", "suggest_web": False,
            "suggest_web_reason": "", "questions": [], "units": units, **extra}


class PlanModel(ChatModel):
    """ChatModel that also answers the plan stage from a script, recording what it was sent."""

    def __init__(self, rounds, plans):
        super().__init__(rounds)
        self.plans = list(plans)
        self.payloads = []

    def study_plan(self, payload):
        self.payloads.append(payload)
        return self.plans.pop(0)


def agent_with(tmp_path, rounds, plans, *inputs, io=None, **kw):
    store, model, scripted, agent = make_agent(tmp_path, rounds, *inputs, day=DAY, **kw)
    planned = PlanModel(rounds, plans)
    agent.service.model = planned
    if io is not None:
        agent.io.inner = io
        scripted = io
    return store, planned, scripted, agent


class CardIO(ScriptedIO):
    """ScriptedIO that answers plan cards from a script and keeps the cards it was shown."""

    def __init__(self, *answers, confirm=True):
        super().__init__(confirm=confirm)
        self.answers = list(answers)
        self.cards = []

    def plan_card(self, card):
        self.cards.append(card)
        return self.answers.pop(0) if self.answers else "later"


def local_source(root):
    return lambda i: {"kind": "local", "ref": str(root / "rbtree.md")} if i == 1 else {"kind": "lesson_web", "ref": ""}


def test_a_plan_proposal_sends_an_outline_drafts_and_confirms(tmp_path):
    root = tmp_path / "notes"
    store, model, io, agent = agent_with(tmp_path, [tool("propose_plan", {"topic": "红黑树", "path": "rbtree.md"})],
                                         [plan_answer(plan_units(3, local_source(root)))])
    agent.handle("我想系统学一下红黑树")
    payload = model.payloads[0]
    # Paths and headings only: the note's text never leaves.
    assert payload["outline"]["local_files"] == [{"path": str(root / "rbtree.md"), "headings": []}]
    assert "染成红色" not in json.dumps(payload, ensure_ascii=False)
    before = io.said.index("要开始吗？")
    assert any("不发送正文" in line for line in io.said[:before])
    assert any(line.startswith("同意即发送上面的内容") for line in io.said[:before])
    plan = Plans(store).list()[0]
    assert plan["state"] == "active" and plan["basis"] == "model"  # one of three units has material
    units = Plans(store).units(plan["id"])
    assert [u["planned_date"] for u in units] == ["2026-10-13", "2026-10-14", "2026-10-15"]
    assert [u["source_kind"] for u in units] == ["local", "lesson_web", "lesson_web"]
    assert store.get_knowledge_point("红黑树第1课")["parent_id"] == store.get_knowledge_point("红黑树")["id"]
    # The reminder was offered and accepted.
    reminder = store.db.execute("SELECT title,repeat,plan_id FROM reminders").fetchone()
    assert tuple(reminder) == ("学习计划：红黑树", "daily", plan["id"])
    assert any("学习计划《红黑树》已生效" in line for line in io.said)


def test_basis_is_materials_when_most_units_use_them(tmp_path):
    root = tmp_path / "notes"
    source = lambda i: {"kind": "local", "ref": str(root / "rbtree.md")}  # noqa: E731
    store, model, io, agent = agent_with(tmp_path, [], [plan_answer(plan_units(2, source))], "y")
    agent.handle("/plan 红黑树 --from " + str(root))
    plan = Plans(store).list()[0]
    assert plan["basis"] == "materials" and plan["basis_note"] == "你的笔记"
    assert "发送以上内容并生成计划草稿吗？" in io.said


def test_an_unusable_draft_is_retried_once_with_the_reason(tmp_path):
    cyclic = plan_units(2)
    cyclic[0]["requires"] = ["u2"]
    store, model, io, agent = agent_with(tmp_path, [], [plan_answer(cyclic), plan_answer(plan_units(2))])
    agent.handle("/plan 红黑树")
    assert "前置关系成环" in model.payloads[1]["previous_error"]
    rule = store.db.execute("""SELECT json_extract(payload_json,'$.rule') FROM events WHERE type='GuardrailTriggered'""").fetchall()
    assert ("output_invalid",) in [tuple(r) for r in rule]
    assert Plans(store).list()[0]["state"] == "active"


def test_too_many_units_twice_stops_with_a_limit_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHIQIU_PLAN_MAX_UNITS", "3")
    store, model, io, agent = agent_with(tmp_path, [], [plan_answer(plan_units(5)), plan_answer(plan_units(4))])
    agent.handle("/plan 红黑树")
    assert Plans(store).list() == []
    assert any("模型给出的计划不合格" in line and "最多 3 个" in line for line in io.said)
    rules = [r[0] for r in store.db.execute("SELECT json_extract(payload_json,'$.rule') FROM events WHERE type='GuardrailTriggered'")]
    assert rules.count("plan_limit") == 2


def test_card_edits_are_revisions_the_program_makes(tmp_path):
    io = CardIO("remove:u3", "move:u2:1", "known:u1", "undo", "preset:intense",
                'pace:{"daily_minutes": 60, "deadline": "2026-10-14"}', "later")
    store, model, _, agent = agent_with(tmp_path, [], [plan_answer(plan_units(3))], io=io)
    agent.handle("/plan 红黑树")
    plans = Plans(store)
    plan = plans.list()[0]
    assert plan["state"] == "draft"
    assert [r["summary"] for r in plans.revisions(plan["id"])] == [
        "初稿", "删除《红黑树第3课》", "《红黑树第1课》标为已掌握", "撤销到第 2 版",
        "节奏改为紧凑：每天 2 个新知识点，约 90 分钟（含复习）", "每天 2 个新知识点，约 60 分钟（含复习），截止 10月14日 周三"]
    assert any("《红黑树第2课》需要先学《红黑树第1课》" in line for line in io.said)
    units = plans.units(plan["id"])
    assert [(u["key"], u["state"], u["planned_date"]) for u in units] == [
        ("u1", "pending", "2026-10-13"), ("u2", "pending", "2026-10-13")]
    card = io.cards[-1]
    assert card["pace"]["new_per_day"] == 2 and card["deadline"] == "2026-10-14" and card["can_undo"]
    assert card["units"][1]["requires"] == ["红黑树第1课"]
    # Nothing real yet.
    assert store.get_knowledge_point("红黑树第1课") is None


def test_dropping_a_draft_and_reopening_one(tmp_path):
    io = CardIO("later", "drop")
    store, model, _, agent = agent_with(tmp_path, [], [plan_answer(plan_units(2))], io=io)
    agent.handle("/plan 红黑树")
    agent.handle("/plans")
    assert any("1. 《红黑树》 草稿  0/2" in line for line in io.said)
    agent.handle("/plan 1")
    assert Plans(store).list(["abandoned"])[0]["title"] == "红黑树"
    assert len(io.cards) == 2


def test_an_already_learned_point_is_skipped_and_can_be_brought_back(tmp_path):
    io = CardIO("unknown:u1", "later")
    store, model, _, agent = agent_with(tmp_path, [], [plan_answer(plan_units(2))], io=io)
    kp, _, _ = agent.service.prepare("红黑树第1课", agent.read_roots[0] / "rbtree.md", lambda *_: True)
    complete_first(store, agent.service, kp)
    agent.handle("/plan 红黑树")
    first = io.cards[0]["units"][0]
    assert first["state"] == "skipped" and first["note"].startswith("已学过（上次")
    assert io.cards[1]["units"][0]["state"] == "pending"


def test_a_one_unit_plan_offers_to_learn_directly(tmp_path):
    io = CardIO("learn_now")
    store, model, _, agent = agent_with(tmp_path, [], [plan_answer(plan_units(1, local_source(tmp_path / "notes")))],
                                        io=io)
    io.inputs = ["/end"]
    agent.handle("/plan 红黑树 --from " + str(tmp_path / "notes"))
    assert io.cards[0]["single"] is True
    assert Plans(store).list(["abandoned"])
    assert store.get_knowledge_point("红黑树第1课") is not None


def test_check_repairs_order_minutes_and_unknown_sources():
    signals = Signals(files=[{"path": "a.md", "headings": []}])
    raw = plan_answer([
        {"key": "u2", "title": "第二", "requires": ["u1"], "minutes": 200, "source": {"kind": "local", "ref": "zz.md"}},
        {"key": "u1", "title": "第一", "requires": [], "minutes": 5, "source": {"kind": "local", "ref": "a.md"}}])
    checked = plan_drafts.check(raw, signals=signals, max_units=30, unit_minutes=30)
    assert [u["key"] for u in checked["units"]] == ["u1", "u2"]
    assert [u["minutes"] for u in checked["units"]] == [15, 60]
    assert checked["units"][1]["source_kind"] == "lesson_web"
    assert checked["notes"][0] == "已按前置关系调整顺序" and "不存在" in checked["notes"][1]
    assert checked["basis"] == "materials"


@pytest.mark.parametrize("units, message", [
    ([{"key": "u1", "title": "概述"}], "太笼统"),
    ([{"key": "u1", "title": "甲"}, {"key": "u2", "title": "甲"}], "重复"),
    ([{"key": "U1", "title": "甲"}], "key 无效"),
    ([{"key": "u1", "title": "甲", "requires": ["u9"]}], "不存在"),
])
def test_check_refuses_what_cannot_be_repaired(units, message):
    with pytest.raises(DraftInvalid, match=message):
        plan_drafts.check(plan_answer(units), signals=Signals(), max_units=30, unit_minutes=30)


def test_outline_keeps_headings_and_python_names_and_shrinks_when_large():
    from zhiqiu.materials import Material
    note = Material("local", "n.md", "h", "# 一\n正文\n## 二\n```\n# 不是标题\n```\n### 三\n")
    code = Material("local", "m.py", "h2", "import os\ndef run():\n    pass\nclass Box:\n    pass\n")
    signals = plan_drafts.outline([note, code])
    assert signals.files == [{"path": "n.md", "headings": ["# 一", "## 二", "### 三"]},
                             {"path": "m.py", "headings": ["def run", "class Box"]}]
    small = plan_drafts.outline([note, code], limit=20)
    assert small.files[0]["headings"] == [] and "只发送了" in small.note


# ----- the card on the workbench and in the terminal --------------------------------------------------

import threading  # noqa: E402
import time  # noqa: E402

from zhiqiu.plan_flow import card_text, terminal_plan_answer  # noqa: E402
from zhiqiu.webio import WebIO  # noqa: E402


def sample_card(tmp_path):
    io = CardIO("later")
    store, model, _, agent = agent_with(tmp_path, [], [plan_answer(plan_units(3))], io=io)
    agent.handle("/plan 红黑树")
    return io.cards[0]


def test_the_web_card_takes_only_card_answers(tmp_path):
    card = sample_card(tmp_path)
    io = WebIO()
    result = {}
    worker = threading.Thread(target=lambda: result.update(answer=io.plan_card(card)))
    worker.start()
    deadline = time.monotonic() + 5
    while not io.pending() and time.monotonic() < deadline:
        time.sleep(0.01)
    event = io.pending()[0]
    assert event["kind"] == "plan" and event["card"]["title"] == "红黑树" and event["text"] == "学习计划《红黑树》"
    with pytest.raises(ValueError, match="学习计划卡片"):
        io.respond(event["request_id"], "rm 1")
    io.respond(event["request_id"], "remove:u2")
    worker.join(5)
    assert result["answer"] == "remove:u2"


def test_a_stopped_web_card_keeps_the_draft(tmp_path):
    card = sample_card(tmp_path)
    io = WebIO()
    result = {}
    worker = threading.Thread(target=lambda: result.update(answer=io.plan_card(card)))
    worker.start()
    deadline = time.monotonic() + 5
    while not io.pending() and time.monotonic() < deadline:
        time.sleep(0.01)
    io.stop()
    worker.join(5)
    assert result["answer"] == "later"


def test_terminal_answers(tmp_path):
    card = sample_card(tmp_path)
    assert [terminal_plan_answer(line, card) for line in ("y", "d", "", "n", "rm 2", "known 1", "back 1", "move 3 1",
                                                           "up 2", "preset relaxed", "days 2", "deadline 2026-11-01",
                                                           "deadline none", "s", "w", "u", "nonsense")] == [
        "activate", "discuss", "later", "drop", "remove:u2", "known:u1", "unknown:u1", "move:u3:1", "move:u2:up",
        "preset:relaxed", 'pace:{"new_per_day": 2}', 'pace:{"deadline": "2026-11-01"}', 'pace:{"deadline": null}',
        "later", "later", "later", "say:nonsense"]


def test_card_text_lists_units_and_review_days(tmp_path):
    card = sample_card(tmp_path)
    card["review_days"] = ["2026-10-14"]
    text = card_text(card)
    assert text.startswith("学习计划草稿《红黑树》：3 个知识点，约 3 天，预计 10月15日 周四 学完")
    assert "大纲依据：模型常识，未联网核对" in text
    lines = text.splitlines()
    assert lines.index("     10月14日 周三  复习日") < next(i for i, l in enumerate(lines) if "红黑树第3课" in l)
    assert "（先学：红黑树第1课）" in text and "[资料待找]" in text


def test_the_terminal_card_reads_one_line(tmp_path, monkeypatch):
    from zhiqiu.cli import TerminalIO
    card = sample_card(tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt="": "rm 3")
    assert TerminalIO().plan_card(card) == "remove:u3"


# ----- confirming ------------------------------------------------------------------------------------


class ConfirmIO(CardIO):
    """CardIO whose yes/no questions are answered in turn."""

    def __init__(self, *answers, confirms=()):
        super().__init__(*answers)
        self.confirms = list(confirms)

    def confirm(self, text):
        self.said.append(text)
        return self.confirms.pop(0) if self.confirms else True


def test_a_second_plan_takes_turns_and_the_first_moves_later(tmp_path):
    io = ConfirmIO("activate", "activate", confirms=[True, False, True, False])
    store, model, _, agent = agent_with(tmp_path, [], [plan_answer(plan_units(3)),
                                                       {**plan_answer(plan_units(2)), "title": "B 树"}], io=io)
    agent.handle("/plan 红黑树")
    agent.handle("/plan B 树")
    plans = Plans(store)
    first, second = plans.list(["active"])
    assert io.cards[1]["moved_plans"] == ["红黑树"]
    dates = {p["title"]: [u["planned_date"] for u in plans.units(p["id"])] for p in (first, second)}
    assert dates == {"红黑树": ["2026-10-13", "2026-10-15", "2026-10-17"], "B 树": ["2026-10-14", "2026-10-16"]}
    assert plans.revisions(first["id"])[-1]["summary"] == "新计划加入，轮流安排"
    # Both reminders were declined.
    assert store.db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


def test_reminders_can_be_turned_off_in_the_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHIQIU_PLAN_REMIND_AT", "off")
    io = ConfirmIO("activate", confirms=[True])
    store, model, _, agent = agent_with(tmp_path, [], [plan_answer(plan_units(2))], io=io)
    agent.handle("/plan 红黑树")
    assert not any("提醒你按计划学习" in line for line in io.said)
    assert Plans(store).list(["active"])


# ----- talking it over -------------------------------------------------------------------------------


def test_discussing_asks_first_then_revises_from_what_the_user_says(tmp_path):
    io = ConfirmIO("discuss", "say:下周五面试，第1课我已经会了", "activate", confirms=[True, False])
    revised = plan_units(3)
    revised[0]["user_known"] = True
    store, model, _, agent = agent_with(tmp_path, [], [
        plan_answer(plan_units(3)),
        plan_answer(plan_units(3), questions=["学来做什么？", "每天能花多少时间？"]),
        plan_answer(revised, reply="第1课标为已掌握，节奏改为每天 2 个。", pace={"new_per_day": 2, "deadline": "2026-10-16"}),
    ], io=io)
    agent.handle("/plan 红黑树")
    assert model.payloads[1]["mode"] == "discuss" and model.payloads[1]["user_message"] == ""
    assert model.payloads[2]["user_message"] == "下周五面试，第1课我已经会了"
    assert [u["key"] for u in model.payloads[2]["current_plan"]["units"]] == ["u1", "u2", "u3"]
    assert any("1. 学来做什么？" in line for line in io.said)
    assert "第1课标为已掌握，节奏改为每天 2 个。" in io.said
    plans = Plans(store)
    plan = plans.list(["active"])[0]
    assert plan["new_per_day"] == 2 and plan["deadline"] == "2026-10-16"
    summary = plans.revisions(plan["id"])[1]["summary"]
    assert summary.startswith("每天 2 个新知识点") and "标为已掌握：《红黑树第1课》" in summary
    assert [u["state"] for u in plans.units(plan["id"])] == ["skipped", "pending", "pending"]
    assert io.cards[1]["discussing"] is True
    moves = [json.loads(r[0]) for r in store.db.execute("SELECT payload_json FROM events WHERE type='StateChanged'")]
    modes = [(m["from"], m["to"]) for m in moves if m["entity"] == "conversation"]
    assert ("chat", "planning") in modes and ("planning", "chat") in modes


def test_discussion_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr("zhiqiu.plan_flow.DISCUSS_TURNS", 1)
    io = ConfirmIO("say:改一下", "say:再改", "later")
    store, model, _, agent = agent_with(tmp_path, [], [plan_answer(plan_units(2)), plan_answer(plan_units(2), reply="好")],
                                        io=io)
    agent.handle("/plan 红黑树")
    assert any("已经讨论了 1 轮" in line for line in io.said)
    rules = [r[0] for r in store.db.execute("SELECT json_extract(payload_json,'$.rule') FROM events WHERE type='GuardrailTriggered'")]
    assert "plan_limit" in rules


# ----- looking the outline up online -------------------------------------------------------------------

from zhiqiu.approvals import ApprovalPolicy  # noqa: E402
from zhiqiu.materials import Material  # noqa: E402

from test_runtime import FakeSearch  # noqa: E402


def found(url, title):
    return Material("web", url, f"h-{url}", f"{title} 的目录与章节", f"Exa Search: {title}")


def test_the_outline_is_looked_up_only_when_asked_and_only_the_topic_is_sent(tmp_path):
    search = FakeSearch([found("https://docs.example.org/rbtree", "红黑树官方文档"),
                         found("https://learn.example.com/path", "红黑树学习路线")])
    io = ConfirmIO("web", "later", confirms=[True, True])
    web_units = plan_units(2, lambda i: {"kind": "url", "ref": "https://docs.example.org/rbtree"})
    store, model, _, agent = agent_with(tmp_path, [], [plan_answer(plan_units(2), suggest_web=True,
                                                                   suggest_web_reason="主题较冷门"),
                                                       plan_answer(web_units)], io=io, search=search)
    agent.handle("/plan 红黑树")
    assert io.cards[0]["can_web"] is True and io.cards[0]["suggest_web"] is True
    assert search.queries == ["红黑树 官方文档 目录", "红黑树 学习路线"]
    asked = next(line for line in io.said if line.startswith("联网参考大纲"))
    assert "只发送查询词" in asked and "不发送你的资料" in asked
    web = model.payloads[1]["outline"]["web_outline"]
    assert [row["url"] for row in web] == ["https://docs.example.org/rbtree", "https://learn.example.com/path"]
    assert web[0]["title"] == "红黑树官方文档"
    plans = Plans(store)
    plan = plans.list(["draft"])[0]
    assert plan["basis"] == "web" and plan["basis_note"].startswith("联网参考：docs.example.org")
    assert [u["source_kind"] for u in plans.units(plan["id"])] == ["url", "url"]
    assert plans.revisions(plan["id"])[-1]["by"] == "web_outline"
    tools = [r[0] for r in store.db.execute("SELECT tool FROM tool_calls")]
    assert tools == ["web_search", "web_search"]


def test_declining_the_lookup_sends_nothing(tmp_path):
    search = FakeSearch([found("https://docs.example.org/x", "x")])
    io = ConfirmIO("web", "later", confirms=[True, False])
    store, model, _, agent = agent_with(tmp_path, [], [plan_answer(plan_units(2))], io=io, search=search)
    agent.handle("/plan 红黑树")
    assert search.queries == [] and "没有联网，计划保持不变。" in io.said


def test_no_lookup_offline_or_when_the_network_is_denied(tmp_path):
    search = FakeSearch([])
    io = CardIO("later")
    store, model, _, agent = agent_with(tmp_path, [], [plan_answer(plan_units(2))], io=io, search=search,
                                        policy=ApprovalPolicy({"network": "deny"}))
    agent.handle("/plan 红黑树")
    assert io.cards[0]["can_web"] is False
    io2 = CardIO("later")
    store2, _, _, agent2 = agent_with(tmp_path / "off", [], [plan_answer(plan_units(2))], io=io2, search=search,
                                      offline=True)
    agent2.handle("/plan 红黑树")
    assert io2.cards[0]["can_web"] is False and io2.cards[0]["units"][0]["note"] == "离线学不了"
