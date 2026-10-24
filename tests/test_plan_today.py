from datetime import date, timedelta

import pytest

from zhiqiu.agent import ChatAgent
from zhiqiu.clock import FixedClock
from zhiqiu.materials import Material
from zhiqiu.plan_flow import today_line, today_text
from zhiqiu.plans import Plans
from zhiqiu.store import Store

from test_flows import ScriptedIO
from test_plan_flow import CardIO, ConfirmIO, agent_with, local_source, plan_answer, plan_units
from test_runtime import FakeModel, FakeSearch, prepare_service

DAY = date(2026, 10, 13)


def active_plan(tmp_path, n=3, source=None):
    io = ConfirmIO("activate", confirms=[True, False])
    store, model, _, agent = agent_with(tmp_path, [], [plan_answer(plan_units(n, source))], io=io)
    agent.handle("/plan 红黑树 --from " + str(tmp_path / "notes"))
    return store, agent


def agent_on(tmp_path, agent, day, *inputs):
    store = Store(tmp_path / "state.sqlite", FixedClock(day))
    io = ScriptedIO(*inputs)
    later = ChatAgent(store, agent.service, io, read_roots=agent.read_roots)
    agent.service.store = store
    later.start()
    return store, io, later


def test_the_opening_line_says_what_today_holds(tmp_path):
    store, agent = active_plan(tmp_path)
    store2, io, later = agent_on(tmp_path, agent, DAY + timedelta(days=1))
    assert "今天：新学《红黑树第1课》约 30 分钟。输入 /today start 开始。" in io.said
    # Day one was missed: the rest moved back and day two holds the first unit.
    units = Plans(store2).units(Plans(store2).list(["active"])[0]["id"])
    assert [u["planned_date"] for u in units] == ["2026-10-14", "2026-10-15", "2026-10-16"]


def test_no_active_plan_keeps_the_old_opening(tmp_path):
    from test_agent import make_agent
    store, model, io, agent = make_agent(tmp_path, [], day=DAY)
    assert not any(line.startswith("今天") for line in io.said)


def test_today_start_learns_the_unit_with_its_material_and_marks_it_done(tmp_path):
    store, agent = active_plan(tmp_path, source=local_source(tmp_path / "notes"))
    store2, io, later = agent_on(tmp_path, agent, DAY, "/done", "答一", "答二", "答三")
    later.handle("/today start")
    plans = Plans(store2)
    plan = plans.list(["active"])[0]
    first = plans.units(plan["id"])[0]
    assert first["state"] == "done" and first["score"] is not None and first["done_date"] == DAY.isoformat()
    assert plan["recomputed_on"] is None or plans.get(plan["id"])["meta"].get("idle_since") is None
    view = plans.today(DAY)
    assert view["done_today"][0]["title"] == "红黑树第1课"
    # 30 minutes used of 45: another 30 does not fit, so nothing is offered ahead.
    assert view["ahead"] is None and today_line(view) == "今天的计划已经完成。"
    assert view["plans"][0]["done"] == 1


def test_learning_ahead_when_time_is_left(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHIQIU_PLAN_DAILY_MINUTES", "90")
    store, agent = active_plan(tmp_path, source=lambda i: {"kind": "local", "ref": str(tmp_path / "notes" / "rbtree.md")})
    store2, io, later = agent_on(tmp_path, agent, DAY, "/done", "答一", "答二", "答三", "/end")
    later.handle("/today start")
    plans = Plans(store2)
    view = plans.today(DAY)
    assert view["ahead"]["title"] == "红黑树第2课"
    assert "可以提前学《红黑树第2课》（/today ahead）" in today_line(view)
    later.handle("/today ahead")
    plan = plans.list(["active"])[0]
    units = plans.units(plan["id"])
    assert units[1]["state"] == "learning"
    # The rest moved earlier: the third unit is now tomorrow.
    assert units[2]["planned_date"] == (DAY + timedelta(days=1)).isoformat()


def test_a_review_day_holds_new_units_back(tmp_path):
    store, agent = active_plan(tmp_path)
    plans = Plans(store)
    view = plans.today(DAY)
    view["reviews"], view["review_day"], view["units"] = ["甲", "乙", "丙"], True, []
    assert today_line(view) == "今天复习 3 项，是复习日；计划里的新内容顺延。"


def test_an_idle_plan_is_asked_about_once(tmp_path):
    store, agent = active_plan(tmp_path)
    store2, io, later = agent_on(tmp_path, agent, DAY + timedelta(days=4))
    assert any("已经连续 4 个学习日没学了" in line for line in io.said)
    store3, io3, again = agent_on(tmp_path, agent, DAY + timedelta(days=5))
    assert not any("个学习日没学了" in line for line in io3.said)


def test_the_chat_context_carries_the_plans(tmp_path):
    store, agent = active_plan(tmp_path)
    store2, io, later = agent_on(tmp_path, agent, DAY)
    context = later._context()
    assert "《红黑树》0/3" in context and "今天：《红黑树第1课》" in context and "明天：《红黑树第2课》" in context


def test_today_text_lists_numbered_units(tmp_path):
    store, agent = active_plan(tmp_path)
    text = today_text(Plans(store).today(DAY))
    assert "  1. 新学《红黑树第1课》（红黑树，约 30 分钟，资料：学习时联网找）" in text
    assert "  明天：《红黑树第2课》" in text


def test_a_unit_without_material_is_found_online_or_refused_offline(tmp_path):
    search = FakeSearch([Material("web", "https://example.org/rb", "h1", "红黑树插入后染红再旋转", "Exa Search: 红黑树")])
    store, service, _ = prepare_service(tmp_path, FakeModel(enough=False), search)
    kp, lesson, materials = service.prepare("红黑树", None, lambda *_: True, allow_empty=True)
    assert [m.source for m in materials] == ["https://example.org/rb"] and search.queries
    with pytest.raises(ValueError, match="离线时学不了"):
        service.prepare("红黑树", None, lambda *_: True, offline=True, allow_empty=True)
    with pytest.raises(ValueError, match="请用 --from"):
        service.prepare("红黑树", None, lambda *_: True)


# ----- reminders follow the plan ---------------------------------------------------------------------

from datetime import datetime  # noqa: E402

from zhiqiu.reminders import Reminders  # noqa: E402


def planned_with_reminder(tmp_path):
    io = ConfirmIO("activate", confirms=[True, True])
    store, model, _, agent = agent_with(tmp_path, [], [plan_answer(plan_units(2))], io=io)
    agent.handle("/plan 红黑树")
    plans = Plans(store)
    return store, plans, plans.list(["active"])[0]["id"]


def test_the_daily_reminder_says_what_today_holds(tmp_path):
    store, plans, plan_id = planned_with_reminder(tmp_path)
    row = store.db.execute("SELECT * FROM reminders").fetchone()
    assert row["plan_id"] == plan_id and row["repeat"] == "daily"
    fired = Reminders(store).fire(datetime.strptime(row["next_at"], "%Y-%m-%d %H:%M"))
    assert fired[0]["title"].startswith("学习计划 · 今天：新学《红黑树第1课》")
    # The stored title stays the plan's name.
    assert store.db.execute("SELECT title FROM reminders").fetchone()[0] == "学习计划：红黑树"


def test_pausing_stops_the_reminder_and_resuming_brings_it_back(tmp_path):
    store, plans, plan_id = planned_with_reminder(tmp_path)
    plans.set_state(plan_id, "paused", "user")
    assert store.db.execute("SELECT status,next_at FROM reminders").fetchone()[:] == ("cancelled", None)
    plans.set_state(plan_id, "active", "user")
    status, next_at = store.db.execute("SELECT status,next_at FROM reminders").fetchone()
    assert status == "active" and next_at.endswith("20:00")
    plans.set_state(plan_id, "abandoned", "user")
    assert store.db.execute("SELECT status FROM reminders").fetchone()[0] == "cancelled"


def test_finishing_every_unit_completes_the_plan_and_ends_the_reminder(tmp_path):
    store, plans, plan_id = planned_with_reminder(tmp_path)
    for unit in plans.units(plan_id):
        attempt = store.start_attempt(unit["kp_id"], "first")
        from test_plans_store import grade
        grade(store, attempt, 3)
    plan = plans.get(plan_id)
    assert plan["state"] == "completed"
    assert store.db.execute("SELECT status FROM reminders").fetchone()[0] == "cancelled"
