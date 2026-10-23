"""Reminders at a set time: once or repeating, said when due (late ones marked), never run anything."""
import threading
from datetime import date, datetime, timedelta

import pytest

from zhiqiu.clock import FixedClock
from zhiqiu.reminders import Reminders, following, in_range, parse
from zhiqiu.store import Store
from zhiqiu.webio import Workbench

from test_agent import make_agent, tool
from test_webio import OPEN, next_event
from test_workbench_http import call, served  # noqa: F401  (fixture)

NOW = datetime(2026, 10, 23, 20, 0)  # a Friday
# Within the year ahead a reminder may be set for, whenever the tests run.
SOON = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d") + " 21:00"


@pytest.fixture
def reminders(tmp_path):
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 23)))
    yield Reminders(store)
    store.close()


@pytest.mark.parametrize("at, repeat, after, expected", [
    ("2026-10-23 21:00", "none", "2026-10-23 20:00", "2026-10-23 21:00"),
    ("2026-10-23 21:00", "none", "2026-10-23 21:00", None),
    ("2026-10-01 21:00", "daily", "2026-10-23 21:00", "2026-10-24 21:00"),
    ("2026-10-01 21:00", "daily", "2026-10-23 20:59", "2026-10-23 21:00"),
    ("2026-10-23 09:00", "weekdays", "2026-10-23 10:00", "2026-10-26 09:00"),  # Friday → Monday
    ("2026-10-02 14:00", "weekly", "2026-10-23 15:00", "2026-10-30 14:00"),
])
def test_the_next_time(at, repeat, after, expected):
    result = following(parse(at), repeat, parse(after))
    assert (result.strftime("%Y-%m-%d %H:%M") if result else None) == expected


def test_create_checks_and_fire_moves_on(reminders):
    with pytest.raises(ValueError, match="已经过了"):
        reminders.create("复习", "2026-10-23 19:00", now=NOW)
    with pytest.raises(ValueError, match="YYYY-MM-DD HH:MM"):
        reminders.create("复习", "明天晚上", now=NOW)
    with pytest.raises(ValueError, match="重复方式"):
        reminders.create("复习", "2026-10-23 21:00", "hourly", now=NOW)
    once = reminders.create("交报告", "2026-10-23 21:00", now=NOW)
    daily = reminders.create("复习红黑树", "2026-10-23 19:00", "daily", now=NOW)
    assert daily["next_at"] == "2026-10-24 19:00"  # today's 19:00 has passed
    assert reminders.fire(datetime(2026, 10, 23, 20, 59)) == []
    fired = reminders.fire(datetime(2026, 10, 23, 21, 1))
    assert [(row["title"], row["late"]) for row in fired] == [("交报告", False)]
    assert reminders.get(once["id"])["status"] == "done"
    # Missed while closed: said late on the next check, then on to the following day.
    fired = reminders.fire(datetime(2026, 10, 25, 8, 0))
    assert [(row["title"], row["late"], row["due_at"], row["next_at"]) for row in fired] == \
        [("复习红黑树", True, "2026-10-24 19:00", "2026-10-25 19:00")]


def test_snooze_cancel_and_the_calendar(reminders):
    daily = reminders.create("复习", "2026-10-23 21:00", "daily", now=NOW)
    reminders.fire(datetime(2026, 10, 23, 21, 0))
    later = reminders.snooze(daily["id"], 10, datetime(2026, 10, 23, 21, 0))
    assert later["next_at"] == "2026-10-23 21:10"
    with pytest.raises(ValueError):
        reminders.snooze(daily["id"], 7)
    reminders.fire(datetime(2026, 10, 23, 21, 10))
    days = [item["date"] for item in in_range(reminders.db, date(2026, 10, 23), date(2026, 10, 27))]
    assert days == ["2026-10-24", "2026-10-25", "2026-10-26", "2026-10-27"]
    reminders.cancel(daily["id"])
    assert in_range(reminders.db, date(2026, 10, 23), date(2026, 10, 27)) == []


def test_the_model_proposes_and_the_user_decides(tmp_path):
    args = {"title": "复习红黑树", "at": SOON, "repeat": "daily", "reason": "你说每天晚上复习"}
    store, model, io, agent = make_agent(tmp_path, [tool("propose_reminder", args)], confirm=True)
    agent.start()
    agent.handle("每天晚上九点提醒我复习红黑树")
    assert any(text.startswith("建议设一个提醒：每天 21:00  复习红黑树") for text in io.said)
    row = store.db.execute("SELECT title,repeat,next_at FROM reminders").fetchone()
    assert tuple(row) == ("复习红黑树", "daily", SOON)
    store2, model2, io2, agent2 = make_agent(tmp_path / "no", [tool("propose_reminder", args)], confirm=False)
    agent2.start()
    agent2.handle("提醒我")
    assert store2.db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0 and "好的，不设。" in io2.said


def test_a_past_time_is_refused_before_asking(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [tool("propose_reminder", {"title": "x", "at": "2001-01-01 21:00"})])
    agent.start()
    agent.handle("提醒我")
    row = store.db.execute("SELECT status,error_kind FROM tool_calls WHERE tool='propose_reminder'").fetchone()
    assert tuple(row) == ("invalid_args", "invalid_args")
    assert store.db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


def test_status_tells_the_model_the_time_and_todays_reminders(tmp_path):
    from zhiqiu.queries import reminders_today
    store, *_ = make_agent(tmp_path, [])
    Reminders(store).create("交报告", "2026-10-23 21:00", now=NOW)
    data = reminders_today(store.db, NOW)
    assert data["now"] == "2026-10-23 20:00"
    assert data["reminders_today"] == [{"title": "交报告", "note": "", "repeat": "none", "at": "2026-10-23 21:00", "said": False}]


def test_the_workbench_says_due_reminders_even_mid_turn(tmp_path):
    from test_agent import ChatModel
    from zhiqiu.agent import ChatAgent
    from zhiqiu.runtime import LearningRuntime
    clock = {"now": datetime(2026, 10, 23, 20, 0)}
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 23)))
    Reminders(store).create("交报告", "2026-10-23 20:30", now=clock["now"])
    store.close()
    work = Workbench(tmp_path / "state.sqlite", lambda s, io: ChatAgent(s, LearningRuntime(s, ChatModel([])), io),
                     clock=FixedClock(date(2026, 10, 23)), remind_every=0.05, local_now=lambda: clock["now"])
    OPEN.append(work)
    work.start()
    after = work.io.last_seq
    clock["now"] = datetime(2026, 10, 23, 20, 31)
    event = next_event(work.io, after, ("reminder",))
    assert (event["title"], event["late"], event["next_at"]) == ("交报告", False, None)
    assert work.close()


def test_reminders_over_http_and_in_the_calendar(served):
    server, bench, _ = served
    status, data = call(server, "/api/reminders/create", "POST", {"title": "复习", "at": SOON, "repeat": "weekly"})
    assert status == 200
    rid = data["reminder"]["id"]
    assert call(server, "/api/reminders/create", "POST", {"title": "", "at": SOON})[0] == 400
    month = call(server, f"/api/schedule?view=month&date={SOON[:10]}")[1]
    found = [(day["date"], item["time"]) for day in month["days"] for item in day["reminders"]]
    assert found[0] == (SOON[:10], "21:00") and all(time == "21:00" for _, time in found)
    assert call(server, "/api/reminders/snooze", "POST", {"id": rid, "minutes": 3})[0] == 400
    assert call(server, "/api/reminders/cancel", "POST", {"id": rid})[1]["reminder"]["status"] == "cancelled"
    assert call(server, "/api/reminders/cancel", "POST", {"id": "nope"})[0] == 404
    assert call(server, "/api/reminders/create", "POST", {"title": "x", "at": SOON}, token=False)[0] == 403
