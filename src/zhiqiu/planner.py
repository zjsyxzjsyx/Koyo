"""Dating a study plan's units: how much new material a day may hold, and moving things when life happens.

Pure functions over plain data: no database, no model, no clock (callers pass `today`), so the rules can be
tested day by day. The rules (PRD 10.0 §5.4):

- A day holds at most `new_per_day` new units, counted across every active plan; plans take turns.
- A day whose due reviews reach `review_day_at` is a review day: no new unit (reviews come first).
- A unit comes after its prerequisites (an earlier day, or earlier on the same day).
- The first new unit of a day always fits; a further one only within `daily_minutes`, reviews included.
- Missed days push the rest back as a whole; nothing piles up on one day. Dates only move later, except
  when the user chose to learn ahead.
- A unit whose prerequisite scored below 50 on its first self-test waits (blocked) until that point's
  next review reaches 50.
"""
from __future__ import annotations

import os
import re
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_NAMES = dict(zip(WEEKDAYS, "一二三四五六日"))
PRESETS = ("relaxed", "standard", "intense")
PRESET_NAMES = {"relaxed": "轻松", "standard": "标准", "intense": "紧凑"}
# PRD 10.0 §9 decision 1 (reviewed 2026-10-23): "standard" is the default.
PRESET_VALUES: dict[str, dict[str, Any]] = {
    "relaxed": {"new_per_day": 1, "daily_minutes": 30, "rest_days": ("sat", "sun"), "review_day_at": 2},
    "standard": {"new_per_day": 1, "daily_minutes": 45, "rest_days": (), "review_day_at": 3},
    "intense": {"new_per_day": 2, "daily_minutes": 90, "rest_days": (), "review_day_at": 3},
}
LIMITS = {"new_per_day": (1, 3), "daily_minutes": (15, 180), "review_minutes": (3, 30), "unit_minutes": (15, 60),
          "max_units": (2, 50), "missed_prompt_days": (1, 14)}
UNIT_MINUTES = (15, 60)
# A first self-test below this (FSRS rating 1) holds back the units that depend on it.
PASS_SCORE = 50
# The ratio of actual to estimated minutes is taken from the latest few lessons once there are enough.
CALIBRATION_MIN, CALIBRATION_LAST, CALIBRATION_RANGE = 3, 5, (0.5, 2.0)
# Breaks longer than this inside a lesson are not counted as study time.
IDLE_GAP_MINUTES = 30
# Asked once at most this often whether to slow a plan down.
MISSED_PROMPT_EVERY = 7
HORIZON_DAYS = 3650
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class Pace:
    new_per_day: int = 1
    daily_minutes: int = 45
    rest_days: tuple[str, ...] = ()
    review_day_at: int = 3
    review_minutes: int = 8
    unit_minutes: int = 30
    preset: str = "standard"

    def as_dict(self) -> dict[str, Any]:
        return {"new_per_day": self.new_per_day, "daily_minutes": self.daily_minutes,
                "rest_days": list(self.rest_days), "preset": self.preset}

    def describe(self) -> str:
        rest = f"，{'、'.join('周' + WEEKDAY_NAMES[d] for d in self.rest_days)}休息" if self.rest_days else ""
        return f"每天 {self.new_per_day} 个新知识点，约 {self.daily_minutes} 分钟（含复习）{rest}"


def _bounded(name: str, value: Any) -> int:
    low, high = LIMITS[name]
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} 应为 {low} 到 {high} 的整数：{value!r}")
    return value


def rest_days(value: Iterable[Any]) -> tuple[str, ...]:
    days = []
    for item in value:
        day = str(item).strip().lower()[:3]
        if day not in WEEKDAYS:
            raise ValueError(f"休息日只能是 {', '.join(WEEKDAYS)}：{item!r}")
        if day not in days:
            days.append(day)
    if len(days) == len(WEEKDAYS):
        raise ValueError("一周至少要有一天学习")
    return tuple(sorted(days, key=WEEKDAYS.index))


def remind_time(value: Any) -> str | None:
    if value in (None, "") or (isinstance(value, str) and value.strip().lower() == "off"):
        return None
    if not isinstance(value, str) or not _TIME.match(value.strip()):
        raise ValueError(f"提醒时间应写成 HH:MM：{value!r}")
    return value.strip()


def pace(preset: str = "standard", **overrides: Any) -> Pace:
    """A preset with any explicit values on top; every value checked."""
    if preset not in PRESETS:
        raise ValueError(f"节奏档位只能是 {', '.join(PRESETS)}：{preset!r}")
    values = {**PRESET_VALUES[preset], **{k: v for k, v in overrides.items() if v is not None}}
    return Pace(new_per_day=_bounded("new_per_day", values["new_per_day"]),
                daily_minutes=_bounded("daily_minutes", values["daily_minutes"]),
                rest_days=rest_days(values["rest_days"]), review_day_at=values["review_day_at"],
                review_minutes=_bounded("review_minutes", values.get("review_minutes", 8)),
                unit_minutes=_bounded("unit_minutes", values.get("unit_minutes", 30)), preset=preset)


def _env_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} 应为整数：{raw!r}") from None


def configured_pace() -> Pace:
    """The default pace for new plans, from [plan] in config.toml (ZHIQIU_PLAN_*)."""
    raw_rest = os.getenv("ZHIQIU_PLAN_REST_DAYS")
    return pace(os.getenv("ZHIQIU_PLAN_PRESET", "").strip() or "standard",
                new_per_day=_env_int("ZHIQIU_PLAN_NEW_PER_DAY"),
                daily_minutes=_env_int("ZHIQIU_PLAN_DAILY_MINUTES"),
                review_minutes=_env_int("ZHIQIU_PLAN_REVIEW_MINUTES"),
                unit_minutes=_env_int("ZHIQIU_PLAN_UNIT_MINUTES"),
                rest_days=None if raw_rest is None else [d for d in raw_rest.split(",") if d.strip()])


def configured(name: str, default: int) -> int:
    value = _env_int(f"ZHIQIU_PLAN_{name.upper()}")
    return default if value is None else _bounded(name, value)


def enabled() -> bool:
    return os.getenv("ZHIQIU_PLAN_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def configured_remind_at() -> str | None:
    return remind_time(os.getenv("ZHIQIU_PLAN_REMIND_AT", "20:00"))


def plan_pace(plan: dict[str, Any], base: Pace | None = None) -> Pace:
    """A stored plan's pace: its own per-day values over the preset; estimates from the configured pace."""
    base = base or configured_pace()
    preset = plan.get("preset") or base.preset
    return pace(preset if preset in PRESETS else "standard", new_per_day=plan["new_per_day"],
                daily_minutes=plan["daily_minutes"], rest_days=plan.get("rest_days") or [],
                review_minutes=base.review_minutes, unit_minutes=base.unit_minutes)


# ----- scheduling ----------------------------------------------------------------------------------


@dataclass
class Lane:
    """One plan in the joint schedule: its units in order, its pace, and the earliest day each may take."""
    plan_id: str
    units: Sequence[dict[str, Any]]
    pace: Pace
    floors: dict[str, date] = field(default_factory=dict)


def _day(value: Any) -> date | None:
    if value in (None, ""):
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def is_review_day(day: date, forecast: dict[str, int], pace: Pace) -> bool:
    return forecast.get(day.isoformat(), 0) >= pace.review_day_at


def schedule(lanes: Sequence[Lane], start: date, forecast: dict[str, int] | None = None, *,
             factor: float = 1.0) -> dict[str, dict[str, str | None]]:
    """Dates for every pending unit of every lane, {plan_id: {key: ISO date}}.

    Lanes are listed by priority; on each day they take turns, starting one further along each day, so two
    plans sharing one new unit a day alternate. Units not pending keep their date; a pending unit that
    cannot be placed (its prerequisite is blocked) gets None.
    """
    forecast = forecast or {}
    capacity = max((lane.pace.new_per_day for lane in lanes), default=0)
    result: dict[str, dict[str, str | None]] = {lane.plan_id: {} for lane in lanes}
    queues: list[list[dict[str, Any]]] = []
    placed: dict[tuple[str, str], tuple[date, int]] = {}
    stuck: set[tuple[str, str]] = set()
    for lane in lanes:
        queue = []
        for unit in lane.units:
            key = (lane.plan_id, unit["key"])
            state = unit.get("state") or "pending"
            if state in ("done", "learning"):
                placed[key] = (_day(unit.get("planned_date")) or start, -1)
                result[lane.plan_id][unit["key"]] = unit.get("planned_date")
            elif state == "pending":
                queue.append(unit)
            else:
                result[lane.plan_id][unit["key"]] = None if state == "blocked" else unit.get("planned_date")
                if state == "blocked":
                    stuck.add(key)
        # A pending unit after a blocked prerequisite cannot be dated either.
        ready = []
        for unit in queue:
            key = (lane.plan_id, unit["key"])
            if any((lane.plan_id, req) in stuck for req in unit.get("requires") or ()):
                stuck.add(key)
                result[lane.plan_id][unit["key"]] = None
            else:
                ready.append(unit)
        queues.append(ready)
    day, order = start, 0
    while any(queues):
        if (day - start).days > HORIZON_DAYS:
            raise ValueError("计划排不下：请减少单元或放宽节奏")
        count = 0
        lane_count = [0] * len(lanes)
        reviews = forecast.get(day.isoformat(), 0)
        lane_minutes = [reviews * lane.pace.review_minutes for lane in lanes]
        n = len(lanes)
        rotation = [(index + order) % n for index in range(n)]
        progress = True
        while progress and count < capacity:
            progress = False
            for index in rotation:
                if count >= capacity:
                    break
                lane, queue = lanes[index], queues[index]
                if not queue or WEEKDAYS[day.weekday()] in lane.pace.rest_days or is_review_day(day, forecast, lane.pace):
                    continue
                if lane_count[index] >= lane.pace.new_per_day:
                    continue
                unit = queue[0]
                floor = lane.floors.get(unit["key"])
                if floor and day < floor:
                    continue
                if any(placed.get((lane.plan_id, req), (date.max, 0))[0] > day for req in unit.get("requires") or ()
                       if (lane.plan_id, req) in placed):
                    continue
                minutes = round(unit["minutes"] * factor)
                if count and lane_minutes[index] + minutes > lane.pace.daily_minutes:
                    continue
                queue.pop(0)
                placed[(lane.plan_id, unit["key"])] = (day, count)
                result[lane.plan_id][unit["key"]] = day.isoformat()
                count += 1
                lane_count[index] += 1
                lane_minutes[index] += minutes
                progress = True
        if count:
            order += 1
        day += timedelta(days=1)
    return result


def apply(units: Sequence[dict[str, Any]], dates: dict[str, str | None]) -> list[dict[str, Any]]:
    return [{**unit, "planned_date": dates.get(unit["key"], unit.get("planned_date"))} for unit in units]


# ----- daily recompute -----------------------------------------------------------------------------


@dataclass
class Facts:
    """What happened, as the caller read it from the store, for one plan."""
    # key -> first self-test total of a done unit
    scores: dict[str, int] = field(default_factory=dict)
    # key -> total of the latest review after the first self-test (None if not reviewed since)
    latest_reviews: dict[str, int | None] = field(default_factory=dict)
    # keys whose knowledge point needs relearning
    relearn: set[str] = field(default_factory=set)


def weak_units(units: Sequence[dict[str, Any]], facts: Facts) -> dict[str, str]:
    """Done units whose point is not yet good enough for what builds on it: key -> reason."""
    weak = {}
    for unit in units:
        key = unit["key"]
        if unit.get("state") != "done":
            continue
        if key in facts.relearn:
            weak[key] = f"《{unit['title']}》需要补学，等补学后的自测达到 {PASS_SCORE} 分"
            continue
        score = facts.scores.get(key)
        latest = facts.latest_reviews.get(key)
        if score is not None and score < PASS_SCORE and (latest is None or latest < PASS_SCORE):
            weak[key] = f"《{unit['title']}》自测 {score} 分，等下次复习达到 {PASS_SCORE} 分"
    return weak


def blocking(units: Sequence[dict[str, Any]], weak: dict[str, str]) -> dict[str, str]:
    """Pending or blocked units that depend, directly or not, on a weak unit: key -> reason."""
    by_key = {unit["key"]: unit for unit in units}
    reasons: dict[str, str] = {}

    def reason(key: str, seen: frozenset[str] = frozenset()) -> str | None:
        if key in reasons:
            return reasons[key]
        for req in by_key.get(key, {}).get("requires") or ():
            if req in weak:
                return weak[req]
            if req not in seen and req in by_key and by_key[req].get("state") in ("pending", "blocked"):
                found = reason(req, seen | {key})
                if found:
                    return found
        return None

    for unit in units:
        if unit.get("state") in ("pending", "blocked"):
            found = reason(unit["key"])
            if found:
                reasons[unit["key"]] = found
    return reasons


def triggers(units: Sequence[dict[str, Any]], today: date, due_today: int, pace: Pace) -> list[str]:
    """Why the pending units need new dates today: missed, review_day or blocked (a unit without a date)."""
    found = []
    for unit in units:
        if unit.get("state") != "pending":
            continue
        planned = _day(unit.get("planned_date"))
        if planned is None:
            found.append("blocked")
        elif planned < today:
            found.append("missed")
        elif planned == today and due_today >= pace.review_day_at:
            found.append("review_day")
    return sorted(set(found), key=["missed", "review_day", "blocked"].index)


def learning_days(start: date, end: date, pace: Pace) -> int:
    """Days in [start, end) on which the pace would have the user learn (rest days excluded)."""
    days, day = 0, start
    while day < end:
        if WEEKDAYS[day.weekday()] not in pace.rest_days:
            days += 1
        day += timedelta(days=1)
    return days


def late_days(units: Sequence[dict[str, Any]], deadline: Any) -> int:
    """How many days the last unit falls after the deadline (0 when on time or without one)."""
    end = _day(deadline)
    dates = [_day(unit.get("planned_date")) for unit in units if unit.get("state") == "pending"]
    dates = [d for d in dates if d]
    if not end or not dates:
        return 0
    return max(0, (max(dates) - end).days)


def finish_date(units: Sequence[dict[str, Any]]) -> str | None:
    dates = [unit.get("planned_date") or unit.get("done_date") for unit in units if unit.get("state") != "skipped"]
    dates = [d for d in dates if d]
    return max(dates) if dates else None


# ----- estimates -----------------------------------------------------------------------------------


def calibration(pairs: Sequence[tuple[float, float]]) -> float:
    """Actual over estimated minutes for the latest lessons: the median ratio, bounded; 1 until enough."""
    pairs = [(actual, estimated) for actual, estimated in pairs if actual > 0 and estimated > 0]
    if len(pairs) < CALIBRATION_MIN:
        return 1.0
    ratios = [actual / estimated for actual, estimated in pairs[-CALIBRATION_LAST:]]
    low, high = CALIBRATION_RANGE
    return round(min(high, max(low, statistics.median(ratios))), 2)


def active_minutes(stamps: Sequence[Any]) -> float:
    """Minutes between the first and last moment, leaving out breaks longer than IDLE_GAP_MINUTES."""
    moments = sorted(stamps)
    total = 0.0
    for before, after in zip(moments, moments[1:]):
        gap = (after - before).total_seconds() / 60
        if gap <= IDLE_GAP_MINUTES:
            total += gap
    return round(total, 1)


def unit_minutes(value: Any, default: int) -> int:
    low, high = UNIT_MINUTES
    if type(value) is not int:
        return default
    return min(high, max(low, value))
