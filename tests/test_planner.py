from datetime import date, datetime, timedelta

import pytest

from zhiqiu import planner
from zhiqiu.planner import Facts, Lane

MON = date(2026, 10, 26)  # a Monday


def unit(key, *, requires=(), minutes=30, state="pending", planned=None, title=None):
    return {"key": key, "title": title or key, "requires": list(requires), "minutes": minutes, "state": state,
            "planned_date": planned}


def chain(n, **kw):
    return [unit(f"u{i}", requires=[f"u{i - 1}"] if i > 1 else [], **kw) for i in range(1, n + 1)]


def dates(result, plan="p"):
    return result[plan]


def test_presets_match_the_reviewed_defaults():
    standard = planner.pace()
    assert (standard.new_per_day, standard.daily_minutes, standard.rest_days, standard.review_day_at) == (1, 45, (), 3)
    relaxed = planner.pace("relaxed")
    assert (relaxed.daily_minutes, relaxed.rest_days, relaxed.review_day_at) == (30, ("sat", "sun"), 2)
    assert planner.pace("intense").new_per_day == 2
    assert planner.pace("standard", new_per_day=2, rest_days=["Sun"]).rest_days == ("sun",)


@pytest.mark.parametrize("kwargs", [{"new_per_day": 4}, {"daily_minutes": 5}, {"rest_days": ["xyz"]},
                                    {"rest_days": list(planner.WEEKDAYS)}, {"new_per_day": "1"}])
def test_bad_pace_values_are_refused(kwargs):
    with pytest.raises(ValueError):
        planner.pace(**kwargs)
    with pytest.raises(ValueError):
        planner.pace("fast")


def test_configured_pace_reads_the_plan_settings(monkeypatch):
    monkeypatch.setenv("ZHIQIU_PLAN_PRESET", "relaxed")
    monkeypatch.setenv("ZHIQIU_PLAN_DAILY_MINUTES", "40")
    monkeypatch.setenv("ZHIQIU_PLAN_REST_DAYS", "sun")
    pace = planner.configured_pace()
    assert (pace.preset, pace.daily_minutes, pace.rest_days, pace.new_per_day) == ("relaxed", 40, ("sun",), 1)
    monkeypatch.setenv("ZHIQIU_PLAN_REMIND_AT", "off")
    assert planner.configured_remind_at() is None
    with pytest.raises(ValueError):
        planner.remind_time("25:00")


def test_one_new_unit_a_day_in_order():
    result = planner.schedule([Lane("p", chain(3), planner.pace())], MON)
    assert dates(result) == {"u1": "2026-10-26", "u2": "2026-10-27", "u3": "2026-10-28"}


def test_rest_days_are_skipped():
    result = planner.schedule([Lane("p", chain(3), planner.pace("relaxed"))], date(2026, 10, 30))  # a Friday
    assert dates(result) == {"u1": "2026-10-30", "u2": "2026-11-02", "u3": "2026-11-03"}


def test_review_days_hold_no_new_unit():
    forecast = {"2026-10-27": 3, "2026-10-28": 2}
    result = planner.schedule([Lane("p", chain(3), planner.pace())], MON, forecast)
    assert dates(result) == {"u1": "2026-10-26", "u2": "2026-10-28", "u3": "2026-10-29"}
    # The relaxed preset already calls two reviews a review day.
    result = planner.schedule([Lane("p", chain(2), planner.pace("relaxed"))], MON, forecast)
    assert dates(result) == {"u1": "2026-10-26", "u2": "2026-10-29"}


def test_a_second_unit_fits_only_within_the_minutes_but_the_first_always_fits():
    pace = planner.pace("intense", daily_minutes=60)
    units = [unit("a", minutes=40), unit("b", minutes=30), unit("c", minutes=15), unit("d", minutes=90)]
    result = planner.schedule([Lane("p", units, pace)], MON)
    # a+b = 70 > 60, so b waits a day; b+c = 45 fits; d is long but first on its day.
    assert dates(result) == {"a": "2026-10-26", "b": "2026-10-27", "c": "2026-10-27", "d": "2026-10-28"}
    # Reviews count against the minutes: two reviews (16 min) + 40 leaves no room for 15 more... 16+40+15=71>60.
    result = planner.schedule([Lane("p", [unit("a", minutes=40), unit("c", minutes=15)], pace)], MON,
                              {"2026-10-26": 2})
    assert dates(result) == {"a": "2026-10-26", "c": "2026-10-27"}


def test_prerequisites_may_share_a_day_only_when_earlier():
    pace = planner.pace("intense")
    units = [unit("a", minutes=20), unit("b", requires=["a"], minutes=20)]
    assert dates(planner.schedule([Lane("p", units, pace)], MON)) == {"a": "2026-10-26", "b": "2026-10-26"}


def test_blocked_units_and_what_follows_them_get_no_date():
    units = [unit("a", state="done", planned="2026-10-20"), unit("b", requires=["a"], state="blocked"),
             unit("c", requires=["b"]), unit("d")]
    result = planner.schedule([Lane("p", units, planner.pace())], MON)
    assert dates(result) == {"a": "2026-10-20", "b": None, "c": None, "d": "2026-10-26"}


def test_floors_keep_dates_from_moving_earlier():
    units = chain(2)
    lane = Lane("p", units, planner.pace(), floors={"u1": date(2026, 10, 28), "u2": date(2026, 10, 28)})
    assert dates(planner.schedule([lane], MON)) == {"u1": "2026-10-28", "u2": "2026-10-29"}


def test_two_plans_share_the_daily_new_unit_and_take_turns():
    lanes = [Lane("a", chain(2), planner.pace()), Lane("b", chain(2), planner.pace())]
    result = planner.schedule(lanes, MON)
    assert result["a"] == {"u1": "2026-10-26", "u2": "2026-10-28"}
    assert result["b"] == {"u1": "2026-10-27", "u2": "2026-10-29"}


def test_missed_days_push_everything_back_without_piling_up():
    units = [unit("u1", planned="2026-10-26"), unit("u2", requires=["u1"], planned="2026-10-27"),
             unit("u3", requires=["u2"], planned="2026-10-28")]
    today = date(2026, 10, 28)
    assert planner.triggers(units, today, 0, planner.pace()) == ["missed"]
    floors = {u["key"]: max(today, date.fromisoformat(u["planned_date"])) for u in units}
    result = planner.schedule([Lane("p", units, planner.pace(), floors)], today)
    assert dates(result) == {"u1": "2026-10-28", "u2": "2026-10-29", "u3": "2026-10-30"}
    assert planner.triggers(planner.apply(units, dates(result)), today, 0, planner.pace()) == []


def test_today_becomes_a_review_day_when_three_reviews_are_due():
    units = [unit("u1", planned="2026-10-26")]
    assert planner.triggers(units, MON, 3, planner.pace()) == ["review_day"]
    assert planner.triggers(units, MON, 2, planner.pace()) == []


def test_low_first_score_blocks_dependents_until_a_review_passes():
    units = [unit("a", state="done", title="主从复制"), unit("b", requires=["a"], title="哨兵"),
             unit("c", requires=["b"], title="集群"), unit("d", title="Lua")]
    weak = planner.weak_units(units, Facts(scores={"a": 42}))
    assert weak == {"a": "《主从复制》自测 42 分，等下次复习达到 50 分"}
    assert planner.blocking(units, weak) == {"b": weak["a"], "c": weak["a"]}
    assert planner.weak_units(units, Facts(scores={"a": 42}, latest_reviews={"a": 60})) == {}
    assert planner.weak_units(units, Facts(scores={"a": 80}, relearn={"a"}))["a"].startswith("《主从复制》需要补学")


def test_deadline_and_finish():
    units = [unit("u1", planned="2026-10-26"), unit("u2", planned="2026-11-02")]
    assert planner.late_days(units, "2026-10-30") == 3
    assert planner.late_days(units, "2026-11-05") == 0 == planner.late_days(units, None)
    assert planner.finish_date(units) == "2026-11-02"


def test_learning_days_leave_out_rest_days():
    assert planner.learning_days(date(2026, 10, 30), date(2026, 11, 3), planner.pace("relaxed")) == 2
    assert planner.learning_days(date(2026, 10, 30), date(2026, 11, 3), planner.pace()) == 4


def test_calibration_needs_three_lessons_and_is_bounded():
    assert planner.calibration([(40, 30), (40, 30)]) == 1.0
    assert planner.calibration([(40, 30), (45, 30), (39, 30)]) == 1.33
    assert planner.calibration([(10, 30)] * 6) == 0.5
    assert planner.calibration([(300, 30)] * 3) == 2.0
    # Only the latest five count.
    assert planner.calibration([(300, 30)] * 3 + [(30, 30)] * 5) == 1.0


def test_active_minutes_leave_out_long_breaks():
    start = datetime(2026, 10, 26, 20, 0)
    stamps = [start, start + timedelta(minutes=10), start + timedelta(minutes=100), start + timedelta(minutes=115)]
    assert planner.active_minutes(stamps) == 25


def test_unit_minutes_are_clamped():
    assert planner.unit_minutes(5, 30) == 15
    assert planner.unit_minutes(200, 30) == 60
    assert planner.unit_minutes("40", 30) == 30
