from datetime import date

from zhiqiu.clock import FixedClock
from zhiqiu.plans import Plans
from zhiqiu.store import Store

from test_plans_store import snapshot, unit
from test_workbench_http import call, served, wait_event  # noqa: F401  (fixture)

DAY = date(2026, 10, 15)


def a_plan(tmp_path, title="Redis 入门", activate=True):
    store = Store(tmp_path / "state.sqlite", FixedClock(DAY))
    plans = Plans(store)
    plan_id = plans.create(snapshot([unit("u1", f"{title}一", "2026-10-15"), unit("u2", f"{title}二", "2026-10-16")],
                                    title=title, start_date=DAY.isoformat()), topic=title)
    if activate:
        plans.activate(plan_id)
    store.close()
    return plan_id


def test_plans_are_listed_and_shown(served, tmp_path):
    server, bench, model = served
    plan_id = a_plan(tmp_path)
    status, data = call(server, "/api/plans")
    assert status == 200
    item = data["plans"][0]
    assert (item["title"], item["state"], item["done"], item["total"], item["next"]) == (
        "Redis 入门", "active", 0, 2, "Redis 入门一")
    status, detail = call(server, f"/api/plans/{plan_id}")
    assert status == 200 and [u["key"] for u in detail["units"]] == ["u1", "u2"]
    assert detail["revisions"][0]["by"] == "model"
    assert call(server, "/api/plans/nope")[0] == 404
    month = call(server, "/api/schedule?view=month&date=2026-10-15")[1]
    planned = [(day["date"], item["title"]) for day in month["days"] for item in day["plan"]]
    assert planned == [("2026-10-15", "Redis 入门一"), ("2026-10-16", "Redis 入门二")]
    assert isinstance(month["summary"]["plan_today"], int)


def test_plans_are_paused_resumed_reordered_and_abandoned_over_http(served, tmp_path):
    server, bench, model = served
    first = a_plan(tmp_path)
    second = a_plan(tmp_path, "Rust")
    act = lambda plan_id, action, **kw: call(server, "/api/plans/action", "POST", {"plan_id": plan_id, "action": action}, **kw)  # noqa: E731
    status, data = act(first, "pause")
    assert status == 200 and {p["id"]: p["state"] for p in data["plans"]}[first] == "paused"
    assert act(first, "resume")[0] == 200
    order = [p["id"] for p in act(second, "up")[1]["plans"]]
    assert order[:2] == [second, first]
    assert act(first, "abandon")[0] == 200
    assert act(first, "resume")[0] == 409
    assert act(first, "explode")[0] == 400
    assert act("nope", "pause")[0] == 404
    assert act(second, "pause", token=False)[0] == 403


def test_opening_a_plan_shows_its_card_in_the_chat(served, tmp_path):
    server, bench, model = served
    plan_id = a_plan(tmp_path, activate=False)
    after = bench.io.last_seq
    status, data = call(server, "/api/plans/action", "POST", {"plan_id": plan_id, "action": "open"})
    assert status == 202
    event, _ = wait_event(server, ("plan",), after)
    assert event["card"]["plan_id"] == plan_id and event["card"]["state"] == "draft"
    # While the card waits, the page may not change that plan behind its back.
    assert call(server, "/api/plans/action", "POST", {"plan_id": plan_id, "action": "abandon"})[0] == 409
    assert call(server, "/api/chat/respond", "POST", {"request_id": event["request_id"], "value": "later"})[0] == 200
    state = None
    for _ in range(100):
        state = call(server, "/api/chat/state")[1]
        if not state["busy"]:
            break
    assert "today" in state


def test_the_plan_commands(tmp_path):
    from typer.testing import CliRunner
    from zhiqiu.cli import app
    a_plan(tmp_path)
    db = str(tmp_path / "state.sqlite")
    result = CliRunner().invoke(app, ["--db", db, "plan", "list"])
    assert result.exit_code == 0 and "1. 《Redis 入门》 进行中  0/2" in result.output
    result = CliRunner().invoke(app, ["--db", db, "plan", "show", "1"])
    assert result.exit_code == 0 and "学习计划《Redis 入门》" in result.output and "第 1 版：初稿" in result.output
    result = CliRunner().invoke(app, ["--db", db, "plan", "pause", "1"])
    assert result.exit_code == 0 and "已暂停" in result.output
    result = CliRunner().invoke(app, ["--db", db, "plan", "pause", "1"])
    assert result.exit_code == 1
    result = CliRunner().invoke(app, ["--db", db, "plan", "resume", "9"])
    assert result.exit_code != 0
    result = CliRunner().invoke(app, ["--db", db, "status"])
    assert result.exit_code == 0
