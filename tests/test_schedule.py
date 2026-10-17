import json
import re
import threading
from datetime import date

import pytest

from zhiqiu import dashboard
from zhiqiu.clock import FixedClock
from zhiqiu.dashboard import DashboardServer
from zhiqiu.queries import schedule
from zhiqiu.store import DAILY_REVIEW_LIMIT, Store

from test_dashboard import request

TODAY = date(2026, 10, 16)  # a Friday


def seed(store, title, remind, *, state="pending", kp_state="active", deferrals=0, original=None,
         rule="fsrs-6.3.2", total=20):
    """One knowledge point with a finished first attempt, its review and an open review task."""
    db = store.db
    kp, attempt, review, task = (f"{title}-{part}" for part in ("kp", "attempt", "review", "task"))
    with store.transaction():
        db.execute("INSERT INTO knowledge_points(id,title,state,created_at) VALUES(?,?,?,?)",
                   (kp, title, kp_state, "2026-10-01T00:00:00+00:00"))
        db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)",
                   (attempt, kp, "first", "completed", "2026-10-10", "2026-10-10T01:00:00+00:00",
                    "2026-10-10T02:00:00+00:00", "finished"))
        db.execute("INSERT INTO reviews(id,attempt_id,kind,total,rating,created_at) VALUES(?,?,?,?,?,?)",
                   (review, attempt, "complete", total, 3, "2026-10-10T02:00:00+00:00"))
        db.execute("INSERT INTO review_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (task, kp, review, original or remind, original or remind, remind, state, deferrals, rule,
                    "2026-10-10T02:00:00+00:00", None, None))
    return kp


def done_review(store, kp, day, total):
    with store.transaction():
        store.db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)",
                         (f"{kp}-r{day}", kp, "review", "completed", day, f"{day}T01:00:00+00:00",
                          f"{day}T02:00:00+00:00", "finished"))
        store.db.execute("INSERT INTO reviews(id,attempt_id,kind,total,rating,created_at) VALUES(?,?,?,?,?,?)",
                         (f"{kp}-rv{day}", f"{kp}-r{day}", "complete", total, 3, f"{day}T02:00:00+00:00"))


@pytest.fixture()
def store(tmp_path):
    store = Store(tmp_path / "state.sqlite", FixedClock(TODAY))
    yield store
    store.close()


def test_tasks_are_grouped_by_reminder_day_with_overdue_and_relearn_apart(store):
    seed(store, "堆", "2026-10-14", deferrals=1, original="2026-10-13", rule="partial-next-day-v1", total=25)
    seed(store, "二叉树", "2026-10-16", state="started")
    seed(store, "图", "2026-10-17")
    seed(store, "红黑树", "2026-10-30")
    seed(store, "哈希", "2026-10-17", kp_state="needs_relearn")
    done = seed(store, "链表", "2026-11-20")
    done_review(store, done, "2026-10-12", 31)

    data = schedule(store.db, "2026-10-16", "2026-10-12", "2026-10-18")
    assert data["summary"] == {"overdue": 1, "today": 1, "next7": 1, "relearn": 1}
    [late] = data["overdue"]
    assert (late["title"], late["date"], late["original_due_date"], late["deferrals"], late["rule"],
            late["last_total"], late["due"]) == ("堆", "2026-10-14", "2026-10-13", 1, "次日补测", 25, True)
    days = {day["date"]: day for day in data["days"]}
    assert list(days) == [f"2026-10-{n}" for n in range(12, 19)]
    # An overdue task is listed once, under overdue, not again on its past day.
    assert days["2026-10-14"]["items"] == []
    [today] = days["2026-10-16"]["items"]
    assert (today["title"], today["state"], today["rule"], today["due"]) == ("二叉树", "started", "FSRS", True)
    [tomorrow] = days["2026-10-17"]["items"]
    assert tomorrow["title"] == "图" and tomorrow["due"] is False
    assert days["2026-10-12"]["done"] == [{"title": "链表", "total": 31}]
    assert data["relearn"] == ["哈希"]
    assert "哈希" not in json.dumps(data["days"], ensure_ascii=False)
    # Nothing that identifies a task leaves the query.
    assert not re.search(r"-task|task_id|review_id", json.dumps(data, ensure_ascii=False))


def test_the_forecast_covers_14_days_and_flags_days_over_the_daily_limit(store):
    seed(store, "堆", "2026-10-10")
    for n in range(DAILY_REVIEW_LIMIT + 1):
        seed(store, f"点{n}", "2026-10-20")
    seed(store, "图", "2026-10-29")
    seed(store, "远", "2026-10-30")
    data = schedule(store.db, "2026-10-16", "2026-10-12", "2026-10-18")
    forecast = data["forecast"]
    assert len(forecast) == 14 and forecast[0]["date"] == "2026-10-16" and forecast[-1]["date"] == "2026-10-29"
    assert forecast[0]["count"] == 0 and data["backlog"] == 1
    by_day = {day["date"]: day for day in forecast}
    assert by_day["2026-10-20"] == {"date": "2026-10-20", "count": DAILY_REVIEW_LIMIT + 1, "over_limit": True}
    assert by_day["2026-10-29"]["count"] == 1 and not by_day["2026-10-29"]["over_limit"]
    assert data["limit"] == DAILY_REVIEW_LIMIT


def test_done_today_counts_review_attempts_like_the_daily_limit(store):
    kp = seed(store, "堆", "2026-10-20")
    done_review(store, kp, "2026-10-16", 30)
    assert schedule(store.db, "2026-10-16", "2026-10-12", "2026-10-18")["done_today"] == 1


@pytest.fixture()
def served(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.sqlite", FixedClock(TODAY))
    seed(store, "图", "2026-10-17")
    seed(store, "堆", "2026-11-02")
    monkeypatch.setattr(dashboard, "Clock", lambda: FixedClock(TODAY))
    server = DashboardServer(tmp_path / "state.sqlite", port=0)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()
    store.close()


def test_the_week_runs_monday_to_sunday_and_the_month_grid_is_whole_weeks(served):
    status, _, body = request(served, "/api/schedule")
    week = json.loads(body)
    assert status == 200 and (week["view"], week["start"], week["end"]) == ("week", "2026-10-12", "2026-10-18")
    assert [item["title"] for day in week["days"] for item in day["items"]] == ["图"]
    status, _, body = request(served, "/api/schedule?view=month&date=2026-11-15")
    month = json.loads(body)
    # November 2026 starts on a Sunday and ends on a Monday.
    assert (month["start"], month["end"]) == ("2026-10-26", "2026-12-06") and len(month["days"]) == 42
    assert month["today"] == "2026-10-16"
    assert [item["title"] for day in month["days"] for item in day["items"]] == ["堆"]


@pytest.mark.parametrize("query", ["view=year", "date=2026-13-01", "date=tomorrow"])
def test_bad_schedule_parameters_are_refused(served, query):
    status, _, body = request(served, f"/api/schedule?{query}")
    assert status == 400 and json.loads(body)["error"]
