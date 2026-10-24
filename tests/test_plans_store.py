import json
import sqlite3
from datetime import date

import pytest

from zhiqiu import contracts
from zhiqiu.clock import FixedClock
from zhiqiu.flow import IllegalTransition
from zhiqiu.plans import Plans
from zhiqiu.store import SCHEMA_VERSION, Store

TODAY = date(2026, 10, 23)


def store_at(tmp_path):
    return Store(tmp_path / "state.sqlite", FixedClock(TODAY))


def unit(key, title, day=None, *, requires=(), state="pending", minutes=30, kind="local", ref=""):
    return {"key": key, "title": title, "goal": f"学会{title}", "requires": list(requires), "minutes": minutes,
            "source_kind": kind, "source_ref": ref, "state": state, "skip_reason": None if state != "skipped" else "known",
            "planned_date": day, "block_reason": None}


def snapshot(units, **extra):
    return {"title": "Redis 入门", "goal": "", "basis": "materials", "basis_note": "你的笔记目录", "new_per_day": 1,
            "daily_minutes": 45, "rest_days": [], "remind_at": "20:00", "preset": "standard",
            "start_date": TODAY.isoformat(), "deadline": None, "meta": {"summary": "s"}, "units": units, **extra}


def two_units():
    return [unit("u1", "Redis 数据结构", "2026-10-23"), unit("u2", "Redis 持久化", "2026-10-24", requires=["u1"])]


def changes(store, entity):
    rows = [json.loads(r[0]) for r in store.db.execute(
        "SELECT payload_json FROM events WHERE type='StateChanged' ORDER BY id")]
    return [(r["from"], r["to"], r["cause"]) for r in rows if r["entity"] == entity]


def test_schema_has_plan_tables(tmp_path):
    store = store_at(tmp_path)
    assert SCHEMA_VERSION == 26
    tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"study_plans", "plan_units", "plan_revisions"} <= tables
    assert "plan_id" in {row["name"] for row in store.db.execute("PRAGMA table_info(reminders)")}


def test_v25_database_migrates_without_touching_old_rows(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path, FixedClock(TODAY))
    kp = store.ensure_knowledge_point("旧知识点")
    # Pretend it was written by 9.2: drop what 26 adds and step the version back.
    store.db.executescript("""DROP TABLE plan_revisions; DROP TABLE plan_units; DROP TABLE study_plans;
        ALTER TABLE reminders DROP COLUMN plan_id; PRAGMA user_version=25;""")
    store.close()
    reopened = Store(path, FixedClock(TODAY))
    assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == 26
    assert reopened.get_knowledge_point("旧知识点")["id"] == kp
    assert list(tmp_path.glob("state.sqlite.pre-v26.bak"))


def test_draft_is_not_real_until_confirmed(tmp_path):
    store = store_at(tmp_path)
    plans = Plans(store)
    plan_id = plans.create(snapshot(two_units()), topic="Redis")
    plan = plans.get(plan_id)
    assert plan["state"] == "draft" and plan["revision"] == 1
    assert [u["key"] for u in plan["units"]] == ["u1", "u2"]
    assert plan["units"][1]["requires"] == ["u1"]
    # No knowledge points and no unit states before confirming.
    assert store.get_knowledge_point("Redis 数据结构") is None
    assert changes(store, "plan_unit") == []
    assert changes(store, "plan") == [(None, "draft", "drafted")]


def test_revising_a_draft_rewrites_units_and_undo_restores(tmp_path):
    store = store_at(tmp_path)
    plans = Plans(store)
    plan_id = plans.create(snapshot(two_units()), topic="Redis")
    revised = snapshot([unit("u2", "Redis 持久化", "2026-10-23"), unit("u3", "Redis 主从复制", "2026-10-24")])
    assert plans.save(plan_id, revised, by="model", summary="去掉数据结构") == 2
    assert [u["title"] for u in plans.units(plan_id)] == ["Redis 持久化", "Redis 主从复制"]
    assert plans.undo(plan_id) == 3
    assert [u["title"] for u in plans.units(plan_id)] == ["Redis 数据结构", "Redis 持久化"]
    assert [r["by"] for r in plans.revisions(plan_id)] == ["model", "model", "user_card"]
    with pytest.raises(ValueError):
        Plans(store).save(plan_id, snapshot([unit("a", "x"), unit("a", "y")]), by="model", summary="重复")


def test_confirming_creates_points_under_the_topic_and_records_unit_states(tmp_path):
    store = store_at(tmp_path)
    existing = store.ensure_knowledge_point("Redis 持久化")
    plans = Plans(store)
    plan_id = plans.create(snapshot(two_units()), topic="Redis")
    plan = plans.activate(plan_id)
    topic = store.get_knowledge_point("Redis")
    assert plan["state"] == "active" and plan["topic_kp_id"] == topic["id"]
    first, second = plan["units"]
    assert store.get_knowledge_point("Redis 数据结构")["parent_id"] == topic["id"]
    # The point of the same title is reused, not duplicated.
    assert second["kp_id"] == existing
    assert first["first_planned_date"] == "2026-10-23"
    assert changes(store, "plan_unit") == [(None, "pending", "plan_activated")] * 2
    assert changes(store, "plan")[-1] == ("draft", "active", "confirmed")
    with pytest.raises(IllegalTransition):
        plans.activate(plan_id)


def test_a_failed_activation_leaves_nothing_half_made(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    plans = Plans(store)
    plan_id = plans.create(snapshot(two_units()), topic="Redis")
    calls = []
    real = store._ensure_knowledge_point

    def flaky(title, parent_id=None):
        calls.append(title)
        if len(calls) == 3:
            raise sqlite3.OperationalError("disk full")
        return real(title, parent_id)

    monkeypatch.setattr(store, "_ensure_knowledge_point", flaky)
    with pytest.raises(sqlite3.OperationalError):
        plans.activate(plan_id)
    assert plans.get(plan_id)["state"] == "draft"
    assert store.get_knowledge_point("Redis") is None
    assert all(u["kp_id"] is None for u in plans.units(plan_id))


def test_revising_a_confirmed_plan_keeps_learned_units_and_skips_removed_ones(tmp_path):
    store = store_at(tmp_path)
    plans = Plans(store)
    plan_id = plans.create(snapshot(two_units() + [unit("u3", "Redis 主从复制", "2026-10-25")]), topic="Redis")
    plan = plans.activate(plan_id)
    first, second, third = plan["units"]
    plans.unit_to(first["id"], "learning", "lesson_started")
    # The revision drops u1 (locked: kept) and u3 (removed: skipped), renames u2 (title kept), adds u4.
    revised = snapshot([unit("u2", "改名的持久化", "2026-10-26", minutes=40), unit("u4", "Redis 集群", "2026-10-27")])
    plans.save(plan_id, revised, by="model", summary="调整")
    units = {u["key"]: u for u in plans.units(plan_id)}
    assert units["u1"]["state"] == "learning"
    assert units["u2"]["title"] == "Redis 持久化" and units["u2"]["minutes"] == 40
    assert units["u3"]["state"] == "skipped" and units["u3"]["skip_reason"] == "removed"
    assert units["u4"]["kp_id"] == store.get_knowledge_point("Redis 集群")["id"]
    assert ("pending", "skipped", "removed") in changes(store, "plan_unit")
    # Undoing brings u3 back; the unit being learned stays as it is.
    plans.undo(plan_id)
    units = {u["key"]: u for u in plans.units(plan_id)}
    assert units["u3"]["state"] == "pending" and units["u1"]["state"] == "learning"


def test_plan_completes_when_every_unit_is_done_or_skipped(tmp_path):
    store = store_at(tmp_path)
    plans = Plans(store)
    plan_id = plans.create(snapshot(two_units()), topic="Redis")
    first, second = plans.activate(plan_id)["units"]
    plans.unit_to(first["id"], "learning", "lesson_started")
    plans.unit_to(first["id"], "done", "first_test_completed", score=80, done_date="2026-10-23")
    assert plans.get(plan_id)["state"] == "active"
    plans.unit_to(second["id"], "skipped", "user_known", skip_reason="user_known")
    plan = plans.get(plan_id)
    assert plan["state"] == "completed" and plan["ended_at"]
    assert plan["units"][0]["score"] == 80
    with pytest.raises(IllegalTransition):
        plans.unit_to(first["id"], "pending", "nope")
    kinds = [r[0] for r in store.db.execute("SELECT type FROM events WHERE type LIKE 'Plan%' OR type LIKE 'Unit%'")]
    assert kinds == ["PlanDrafted", "PlanActivated", "UnitDone", "UnitSkipped", "PlanCompleted"]


def test_pause_resume_and_abandon(tmp_path):
    store = store_at(tmp_path)
    plans = Plans(store)
    plan_id = plans.create(snapshot(two_units()), topic="Redis")
    plans.activate(plan_id)
    assert plans.set_state(plan_id, "paused", "user")["state"] == "paused"
    assert plans.set_state(plan_id, "active", "user")["state"] == "active"
    assert plans.set_state(plan_id, "abandoned", "user")["state"] == "abandoned"
    with pytest.raises(ValueError):
        plans.save(plan_id, snapshot(two_units()), by="model", summary="x")
    # Abandoning keeps the points.
    assert store.get_knowledge_point("Redis 数据结构")
    assert [p["id"] for p in plans.list(["abandoned"])] == [plan_id]


def test_plan_events_replay_cleanly(tmp_path):
    store = store_at(tmp_path)
    plans = Plans(store)
    plan_id = plans.create(snapshot(two_units()), topic="Redis")
    plans.save(plan_id, snapshot(two_units()[:1]), by="user_card", summary="删一个")
    first, = plans.activate(plan_id)["units"]
    plans.unit_to(first["id"], "blocked", "prerequisite_low", block_reason="原因")
    plans.unit_to(first["id"], "pending", "prerequisite_passed", block_reason=None)
    result = contracts.state_replay(store.db, 0, "")
    assert result["evidence"] == [] and result["status"] == "pass" and result["checked"] >= 3


# ----- daily recompute ------------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402

from zhiqiu.scoring import DimensionGrade  # noqa: E402


def grade(store, attempt, score):
    grades = []
    for dimension in ("concept", "mechanism", "application"):
        question_id, _ = store.add_question(attempt, dimension, f"解释{dimension}")
        answer_id = store.add_answer(question_id, f"{dimension} 的原始回答")
        grades.append(DimensionGrade(dimension, score, question_id, answer_id, f"{dimension} 的原始回答", "依据"))
    return store.finalize(attempt, grades)


def at(tmp_path, day):
    return Store(tmp_path / "state.sqlite", FixedClock(day))


def three_day_plan(tmp_path):
    store = store_at(tmp_path)
    plans = Plans(store)
    units = [unit("u1", "主从复制", "2026-10-23"), unit("u2", "哨兵", "2026-10-24", requires=["u1"]),
             unit("u3", "集群", "2026-10-25", requires=["u2"])]
    plan_id = plans.create(snapshot(units), topic="Redis")
    plans.activate(plan_id)
    return store, plans, plan_id


def dated(plans, plan_id):
    return [(u["key"], u["state"], u["planned_date"]) for u in plans.units(plan_id)]


def test_missed_days_move_the_rest_back_once_a_day(tmp_path):
    store, plans, plan_id = three_day_plan(tmp_path)
    later = Plans(at(tmp_path, date(2026, 10, 25)))
    changes = later.recompute(date(2026, 10, 25))
    assert changes[0]["reasons"] == ["missed"]
    assert dated(later, plan_id) == [("u1", "pending", "2026-10-25"), ("u2", "pending", "2026-10-26"),
                                     ("u3", "pending", "2026-10-27")]
    assert later.get(plan_id)["meta"]["idle_since"] == "2026-10-23"
    assert later.revisions(plan_id)[-1]["by"] == "reschedule"
    assert later.revisions(plan_id)[-1]["summary"] == "漏学顺延"
    # The same day again changes nothing.
    assert later.recompute(date(2026, 10, 25)) == []
    # first_planned_date keeps the original day for punctuality.
    assert later.units(plan_id)[0]["first_planned_date"] == "2026-10-23"


def test_a_low_first_score_blocks_what_depends_on_it_until_a_review_passes(tmp_path):
    store, plans, plan_id = three_day_plan(tmp_path)
    u1 = plans.units(plan_id)[0]
    plans.unit_to(u1["id"], "learning", "lesson_started")
    attempt = store.start_attempt(u1["kp_id"], "first")
    first = grade(store, attempt, 1)
    # Finishing the first self-test marks the unit done, with its score.
    done = plans.units(plan_id)[0]
    assert (done["state"], done["score"], done["done_date"]) == ("done", first["total"], "2026-10-23")
    assert first["total"] < 50
    plans.recompute(date(2026, 10, 23), force=True)
    states = {u["key"]: u for u in plans.units(plan_id)}
    assert states["u2"]["state"] == "blocked" and states["u3"]["state"] == "blocked"
    assert states["u2"]["block_reason"].startswith("《主从复制》自测")
    assert states["u2"]["planned_date"] is None
    # The next review passes: the units come back with new dates from that day.
    review_day = date.fromisoformat(first["due_date"])
    later_store = at(tmp_path, review_day)
    review = later_store.start_attempt(u1["kp_id"], "review", [first["task_id"]])
    grade(later_store, review, 3)
    later = Plans(later_store)
    changes = later.recompute(review_day)
    assert "unblocked" in changes[0]["reasons"]
    assert dated(later, plan_id)[1:] == [("u2", "pending", review_day.isoformat()),
                                         ("u3", "pending", (review_day + timedelta(days=1)).isoformat())]
    kinds = [r[0] for r in later_store.db.execute("SELECT type FROM events WHERE type LIKE 'Unit%'")]
    assert kinds.count("UnitBlocked") == 2 and kinds.count("UnitUnblocked") == 2


def test_a_new_plan_takes_turns_with_an_active_one(tmp_path):
    store, plans, plan_id = three_day_plan(tmp_path)
    other = snapshot([unit("a", "Rust 所有权"), unit("b", "Rust 借用", requires=["a"])], title="Rust")
    units, moved = plans.schedule_draft(other, TODAY)
    assert [u["planned_date"] for u in units] == ["2026-10-24", "2026-10-26"]
    assert moved == ["Redis 入门"]


def test_calibration_uses_real_lesson_time(tmp_path):
    store, plans, plan_id = three_day_plan(tmp_path)
    assert plans.calibration() == 1.0


# ----- the run contract and the metrics ----------------------------------------------------------------

from zhiqiu import metrics  # noqa: E402


def test_plans_consistent_passes_and_catches_each_kind_of_violation(tmp_path):
    store = store_at(tmp_path)
    assert contracts.plans_consistent(store.db, 0, "")["status"] == "insufficient"
    plans = Plans(store)
    future = (date.today().replace(year=date.today().year + 1)).isoformat()
    later = (date.fromisoformat(future) + timedelta(days=1)).isoformat()
    third = (date.fromisoformat(later) + timedelta(days=1)).isoformat()
    plan_id = plans.create(snapshot([unit("u1", "甲", future), unit("u2", "乙", later, requires=["u1"]),
                                     unit("u3", "丙", third)]), topic="主题")
    plans.activate(plan_id)
    assert contracts.plans_consistent(store.db, 0, "")["status"] == "pass"
    db = store.db
    # Two units on one day (limit 1), a prerequisite after its dependent, a done unit without a test,
    # a blocked unit without its reason.
    db.execute("UPDATE plan_units SET planned_date=? WHERE key='u1'", ((date.fromisoformat(later) + timedelta(days=1)).isoformat(),))
    db.execute("UPDATE plan_units SET state='done' WHERE key='u3'")
    plan2 = plans.create(snapshot([unit("v1", "丁", None), unit("v2", "戊", None)]), topic="二")
    plans.activate(plan2)
    db.execute("UPDATE plan_units SET state='blocked',block_reason=NULL WHERE key='v2'")
    db.execute("UPDATE plan_units SET planned_date=? WHERE key='v1'", (later,))
    result = contracts.plans_consistent(db, 0, "")
    problems = [item["problem"] for item in result["evidence"]]
    assert result["status"] == "fail"
    assert any("排在前置《甲》之前" in p for p in problems)
    assert any("没有完成的首次自测" in p for p in problems)
    assert any("推迟但没有写原因" in p for p in problems)
    assert any("超过每天 1 个" in p for p in problems)


def test_plan_metrics(tmp_path):
    store, plans, plan_id = three_day_plan(tmp_path)
    u1 = plans.units(plan_id)[0]
    attempt = store.start_attempt(u1["kp_id"], "first")
    grade(store, attempt, 3)
    data = metrics.collect(store.db, 30)["plans"]
    assert data["by_state"] == {"active": 1} and data["units_done"] == 1
    assert data["on_time_rate"] == 1.0 and data["average_delay_days"] == 0
