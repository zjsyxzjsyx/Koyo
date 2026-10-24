"""Study plans: a topic split into knowledge points (units) spread over days.

The model proposes the units (plan_drafts.py checks them); planner.py dates them; this module keeps them.
A plan is a draft until the user confirms it: a draft is edited by replacing its units wholesale from a
snapshot, and nothing about it is real yet (no knowledge points, no StateChanged events for its units).
Confirming creates the points and records each unit's first state; from then on a unit only moves through
flow.transition, and it is done only when its point's first self-test is.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from . import flow, planner, queries, reminders, tracing
from .planner import Facts, Lane, Pace
from .store import Store, new_id

PLAN_STATES = ("draft", "active", "paused", "completed", "abandoned")
OPEN_STATES = ("draft", "active", "paused")
SOURCE_KINDS = ("local", "url", "notion", "lesson_web")
REVISION_BY = ("model", "user_card", "web_outline", "reschedule")
# Units whose lesson has begun or ended; a later revision keeps them as they are.
LOCKED_UNITS = ("learning", "done")
# Why dates moved, as the plan's history says it.
REASONS = {"missed": "漏学顺延", "review_day": "今天复习多，新内容顺延", "blocked": "前置知识未过关",
           "unblocked": "前置知识已过关", "ahead": "提前学", "user": "用户调整", "new_plan": "新计划加入，轮流安排",
           "shared_day": "与其他计划轮流"}
_PLAN_FIELDS = ("title", "goal", "basis", "basis_note", "new_per_day", "daily_minutes", "rest_days", "remind_at",
                "preset", "start_date", "deadline")
_UNIT_FIELDS = ("key", "title", "goal", "requires", "minutes", "source_kind", "source_ref", "state", "skip_reason",
                "planned_date", "block_reason")


def _unit(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["requires"] = json.loads(item.pop("requires_json") or "[]")
    return item


def _plan(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["rest_days"] = json.loads(item["rest_days"] or "[]")
    item["sources"] = json.loads(item.pop("sources_json") or "{}")
    item["meta"] = json.loads(item.pop("meta_json") or "{}")
    return item


def calendar(db: Any, start: date, end: date) -> list[dict[str, Any]]:
    """Units of active plans dated between two days (inclusive), for the calendar (any connection, a
    read-only one is enough); [] for a database from before plans."""
    try:
        rows = db.execute("""SELECT u.title,COALESCE(u.done_date,u.planned_date) planned_date,u.state,u.minutes,
            p.title plan FROM plan_units u
            JOIN study_plans p ON p.id=u.plan_id WHERE p.state='active' AND u.state IN ('pending','learning','done')
            AND COALESCE(u.done_date,u.planned_date) BETWEEN ? AND ? ORDER BY u.planned_date,p.priority,u.seq""",
            (start.isoformat(), end.isoformat())).fetchall()
    except Exception:
        return []
    return [{"date": row["planned_date"], "title": row["title"], "plan": row["plan"], "state": row["state"],
             "minutes": row["minutes"]} for row in rows]


def actual_minutes(db: Any, kp_id: str) -> float | None:
    """Lesson plus first self-test of a point, from its messages, breaks left out (any connection)."""
    attempt = db.execute("""SELECT id,ended_at FROM attempts WHERE kp_id=? AND kind='first'
        AND state='completed' ORDER BY started_at LIMIT 1""", (kp_id,)).fetchone()
    if not attempt:
        return None
    stamps = [datetime.fromisoformat(row[0]) for row in db.execute("""SELECT m.created_at FROM messages m
        JOIN sessions s ON s.id=m.session_id WHERE s.kp_id=? AND (s.attempt_id=? OR m.attempt_id=?)""",
        (kp_id, attempt["id"], attempt["id"]))]
    if attempt["ended_at"]:
        stamps.append(datetime.fromisoformat(attempt["ended_at"]))
    return planner.active_minutes(stamps) or None


def listing(db: Any, states: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Plans by state, active first, then by the order they take turns in (any connection)."""
    states = tuple(states or PLAN_STATES)
    marks = ",".join("?" * len(states))
    rows = db.execute(f"""SELECT * FROM study_plans WHERE state IN ({marks})
        ORDER BY CASE state WHEN 'active' THEN 0 WHEN 'paused' THEN 1 WHEN 'draft' THEN 2 ELSE 3 END,
        priority, COALESCE(activated_at, created_at)""", states).fetchall()
    return [_plan(row) for row in rows]


def overview(db: Any) -> dict[str, Any]:
    """Every plan with its progress, for the plans page (read-only)."""
    try:
        plans = listing(db)
    except Exception:
        return {"plans": []}
    items = []
    for plan in plans:
        units = [_unit(row) for row in db.execute("SELECT * FROM plan_units WHERE plan_id=? ORDER BY seq", (plan["id"],))]
        live = [u for u in units if u["state"] != "skipped"]
        upcoming = [u for u in live if u["state"] in ("pending", "learning") and u["planned_date"]]
        items.append({"id": plan["id"], "title": plan["title"], "topic": plan["topic"], "state": plan["state"],
                      "basis_note": plan["basis_note"], "done": sum(u["state"] == "done" for u in live),
                      "total": len(live), "blocked": sum(u["state"] == "blocked" for u in live),
                      "next": min(upcoming, key=lambda u: u["planned_date"])["title"] if upcoming else None,
                      "finish": planner.finish_date(units), "deadline": plan["deadline"],
                      "late_days": planner.late_days(units, plan["deadline"]),
                      "pace": planner.pace(plan["preset"] if plan["preset"] in planner.PRESETS else "standard",
                                           new_per_day=plan["new_per_day"], daily_minutes=plan["daily_minutes"],
                                           rest_days=plan["rest_days"]).describe(),
                      "created_at": plan["created_at"], "activated_at": plan["activated_at"]})
    return {"plans": items}


def detail(db: Any, plan_id: str) -> dict[str, Any]:
    """One plan's units and versions (read-only)."""
    row = db.execute("SELECT * FROM study_plans WHERE id=?", (plan_id,)).fetchone()
    if not row:
        raise LookupError(f"没有这个学习计划：{plan_id}")
    plan = _plan(row)
    plan["units"] = [_unit(r) for r in db.execute("SELECT * FROM plan_units WHERE plan_id=? ORDER BY seq", (plan_id,))]
    plan["revisions"] = [dict(r) for r in db.execute(
        "SELECT revision,by,summary,created_at FROM plan_revisions WHERE plan_id=? ORDER BY revision DESC", (plan_id,))]
    return plan


class Plans:
    def __init__(self, store: Store):
        self.store = store

    @property
    def db(self):
        return self.store.db

    # ----- reading --------------------------------------------------------------------------------

    def get(self, plan_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM study_plans WHERE id=?", (plan_id,)).fetchone()
        if not row:
            raise LookupError(f"没有这个学习计划：{plan_id}")
        plan = _plan(row)
        plan["units"] = self.units(plan_id)
        return plan

    def units(self, plan_id: str) -> list[dict[str, Any]]:
        return [_unit(row) for row in self.db.execute(
            "SELECT * FROM plan_units WHERE plan_id=? ORDER BY seq", (plan_id,))]

    def unit(self, unit_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM plan_units WHERE id=?", (unit_id,)).fetchone()
        if not row:
            raise LookupError(f"没有这个计划单元：{unit_id}")
        return _unit(row)

    def list(self, states: Iterable[str] | None = None) -> list[dict[str, Any]]:
        return listing(self.db, states)

    def revisions(self, plan_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute(
            "SELECT id,plan_id,revision,by,summary,created_at FROM plan_revisions WHERE plan_id=? ORDER BY revision",
            (plan_id,))]

    def snapshot(self, plan_id: str) -> dict[str, Any]:
        """What a revision stores: the plan's own fields and every unit, enough to restore a draft."""
        plan = self.get(plan_id)
        return {**{field: plan[field] for field in _PLAN_FIELDS}, "meta": plan["meta"],
                "units": [{field: unit[field] for field in _UNIT_FIELDS} | {"kp_id": unit["kp_id"]}
                          for unit in plan["units"]]}

    def revision_snapshot(self, plan_id: str, revision: int) -> dict[str, Any]:
        row = self.db.execute("SELECT snapshot_json FROM plan_revisions WHERE plan_id=? AND revision=?",
                              (plan_id, revision)).fetchone()
        if not row:
            raise LookupError(f"没有第 {revision} 版")
        return json.loads(row["snapshot_json"])

    # ----- writing --------------------------------------------------------------------------------

    def create(self, snapshot: dict[str, Any], *, topic: str, sources: dict[str, Any] | None = None,
               conversation_id: str | None = None, by: str = "model", summary: str = "初稿") -> str:
        """A new draft from a checked and dated snapshot (plan_drafts.check, then planner.schedule)."""
        plan_id = new_id()
        with self.store.transaction():
            now = self.store._ts()
            self.db.execute("""INSERT INTO study_plans(id,title,topic,goal,state,basis,basis_note,new_per_day,
                daily_minutes,rest_days,remind_at,preset,start_date,deadline,sources_json,meta_json,conversation_id,
                revision,created_at) VALUES(?,?,?,?,'draft',?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
                (plan_id, snapshot["title"], topic.strip(), snapshot.get("goal") or "", snapshot["basis"],
                 snapshot.get("basis_note") or "", snapshot["new_per_day"], snapshot["daily_minutes"],
                 json.dumps(snapshot.get("rest_days") or []), snapshot.get("remind_at"),
                 snapshot.get("preset") or "standard", snapshot["start_date"], snapshot.get("deadline"),
                 json.dumps(sources or {}, ensure_ascii=False), json.dumps(snapshot.get("meta") or {}, ensure_ascii=False),
                 conversation_id, now))
            flow.enter(self.store, "plan", plan_id, "draft", "drafted")
            self._write_units(plan_id, snapshot["units"], record=False)
            revision = self._record_revision(plan_id, by, summary)
            self.store._event(None, None, "PlanDrafted", {"plan_id": plan_id, "title": snapshot["title"],
                                                          "units": len(snapshot["units"]), "basis": snapshot["basis"],
                                                          "revision": revision})
        return plan_id

    def save(self, plan_id: str, snapshot: dict[str, Any], *, by: str, summary: str) -> int:
        """Replace a plan's fields and units with a revised snapshot; returns the new revision number.

        Units already being learned or done stay as they are. In a draft the other units are rewritten;
        in a confirmed plan a unit left out is skipped (removed) rather than deleted, so its history stays.
        """
        with self.store.transaction():
            plan = self._row(plan_id)
            if plan["state"] not in OPEN_STATES:
                raise ValueError("已结束的学习计划不能再修改")
            self._update_plan(plan_id, snapshot)
            self._write_units(plan_id, snapshot["units"], record=plan["state"] != "draft")
            revision = self._record_revision(plan_id, by, summary)
            self.store._event(None, None, "PlanRevised" if by != "reschedule" else "PlanRescheduled",
                              {"plan_id": plan_id, "by": by, "summary": summary, "revision": revision})
            return revision

    def undo(self, plan_id: str) -> int:
        """Back to the previous version. In a confirmed plan the units already begun or done stay as they are."""
        plan = self._row(plan_id)
        if plan["state"] not in OPEN_STATES:
            raise ValueError("已结束的学习计划不能撤销")
        if plan["revision"] < 2:
            raise ValueError("已经是第一版，没有可撤销的修改")
        return self.save(plan_id, self.revision_snapshot(plan_id, plan["revision"] - 1), by="user_card",
                         summary=f"撤销到第 {plan['revision'] - 1} 版")

    def activate(self, plan_id: str) -> dict[str, Any]:
        """Confirm a draft in one transaction: the topic point, one point per unit (an existing point of the
        same title is reused), each unit's first state, and the plan itself. Returns the plan."""
        with self.store.transaction():
            plan = self._row(plan_id)
            if plan["state"] != "draft":
                raise flow.IllegalTransition("plan", plan["state"], "active")
            topic_kp = self.store._ensure_knowledge_point(plan["topic"])
            for unit in self.units(plan_id):
                kp_id = unit["kp_id"] or self.store._ensure_knowledge_point(unit["title"], parent_id=topic_kp)
                self.db.execute("""UPDATE plan_units SET kp_id=?,first_planned_date=COALESCE(first_planned_date,planned_date)
                    WHERE id=?""", (kp_id, unit["id"]))
                flow.enter(self.store, "plan_unit", unit["id"], unit["state"], "plan_activated", kp_id=kp_id)
            self.db.execute("UPDATE study_plans SET topic_kp_id=?,activated_at=? WHERE id=?",
                            (topic_kp, self.store._ts(), plan_id))
            flow.transition(self.store, "plan", plan_id, "active", "confirmed", kp_id=topic_kp)
            self.store._event(topic_kp, None, "PlanActivated", {"plan_id": plan_id, "title": plan["title"]})
        return self.get(plan_id)

    def set_state(self, plan_id: str, target: str, cause: str) -> dict[str, Any]:
        """Pause, resume, complete or abandon. Points and review tasks always stay."""
        with self.store.transaction():
            self._set_state(plan_id, target, cause)
        return self.get(plan_id)

    def _set_state(self, plan_id: str, target: str, cause: str) -> None:
        plan = self._row(plan_id)
        previous = flow.transition(self.store, "plan", plan_id, target, cause, kp_id=plan["topic_kp_id"])
        if previous == target:
            return
        if target in ("completed", "abandoned"):
            self.db.execute("UPDATE study_plans SET ended_at=? WHERE id=?", (self.store._ts(), plan_id))
        reminders.follow_plan(self.store, plan_id, target == "active")
        kind = {"paused": "PlanPaused", "active": "PlanResumed", "completed": "PlanCompleted",
                "abandoned": "PlanAbandoned"}[target]
        self.store._event(plan["topic_kp_id"], None, kind, {"plan_id": plan_id, "cause": cause})

    def unit_to(self, unit_id: str, target: str, cause: str, **columns: Any) -> None:
        """Move one unit of a confirmed plan (and set columns such as score or block_reason with it)."""
        with self.store.transaction():
            self._unit_to(unit_id, target, cause, **columns)

    def _unit_to(self, unit_id: str, target: str, cause: str, **columns: Any) -> None:
        unit = self.db.execute("SELECT * FROM plan_units WHERE id=?", (unit_id,)).fetchone()
        if not unit:
            raise LookupError(f"没有这个计划单元：{unit_id}")
        previous = flow.transition(self.store, "plan_unit", unit_id, target, cause, kp_id=unit["kp_id"])
        if columns:
            names = ",".join(f"{name}=?" for name in columns)
            self.db.execute(f"UPDATE plan_units SET {names} WHERE id=?", (*columns.values(), unit_id))
        if previous != target:
            kind = {"blocked": "UnitBlocked", "done": "UnitDone", "skipped": "UnitSkipped"}.get(target)
            if previous == "blocked" and target == "pending":
                kind = "UnitUnblocked"
            if kind:
                self.store._event(unit["kp_id"], None, kind, {"plan_id": unit["plan_id"], "unit_id": unit_id,
                                                               "cause": cause})
        if target in ("done", "skipped"):
            self._complete_if_finished(unit["plan_id"])

    def _complete_if_finished(self, plan_id: str) -> None:
        plan = self._row(plan_id)
        if plan["state"] != "active":
            return
        if not self.db.execute("""SELECT 1 FROM plan_units WHERE plan_id=? AND state NOT IN ('done','skipped')""",
                               (plan_id,)).fetchone():
            self._set_state(plan_id, "completed", "all_units_finished")

    # ----- dates ----------------------------------------------------------------------------------

    def forecast(self, today: date) -> dict[str, int]:
        """Due reviews per day for the next two weeks; today counts the overdue ones too."""
        data = queries.schedule(self.db, today.isoformat(), today.isoformat(), today.isoformat())
        forecast = {item["date"]: item["count"] for item in data["forecast"]}
        forecast[today.isoformat()] = self.due_today(today)
        return forecast

    def due_today(self, today: date) -> int:
        return len({row["kp_id"] for row in queries.due_tasks(self.db, today.isoformat())})

    def facts(self, units: Iterable[dict[str, Any]]) -> Facts:
        """First self-test scores, the latest review since, and points needing relearning, by unit key."""
        facts = Facts()
        for unit in units:
            if not unit.get("kp_id"):
                continue
            point = self.db.execute("SELECT state FROM knowledge_points WHERE id=?", (unit["kp_id"],)).fetchone()
            if point and point["state"] == "needs_relearn":
                facts.relearn.add(unit["key"])
            first = self.db.execute("""SELECT r.total,r.created_at FROM reviews r JOIN attempts a ON a.id=r.attempt_id
                WHERE a.kp_id=? AND a.kind='first' AND r.total IS NOT NULL ORDER BY r.created_at DESC LIMIT 1""",
                (unit["kp_id"],)).fetchone()
            if first is None:
                continue
            facts.scores[unit["key"]] = first["total"]
            latest = self.db.execute("""SELECT r.total FROM reviews r JOIN attempts a ON a.id=r.attempt_id
                WHERE a.kp_id=? AND a.kind='review' AND r.total IS NOT NULL AND r.created_at>?
                ORDER BY r.created_at DESC LIMIT 1""", (unit["kp_id"], first["created_at"])).fetchone()
            facts.latest_reviews[unit["key"]] = latest["total"] if latest else None
        return facts

    def calibration(self) -> float:
        """How long the user's planned lessons really took against their estimates (planner.calibration)."""
        pairs = []
        for row in self.db.execute("""SELECT u.minutes,u.kp_id FROM plan_units u WHERE u.state='done'
                AND u.kp_id IS NOT NULL ORDER BY u.done_date"""):
            minutes = self.actual_minutes(row["kp_id"])
            if minutes:
                pairs.append((minutes, row["minutes"]))
        return planner.calibration(pairs)

    def actual_minutes(self, kp_id: str) -> float | None:
        return actual_minutes(self.db, kp_id)

    def lanes(self, base: Pace | None = None, *, floors_from: date | None = None,
              allow_earlier: bool = False) -> list[Lane]:
        """Every active plan by priority, ready for planner.schedule; pending units keep at least their date."""
        lanes = []
        for plan in self.list(["active"]):
            units = self.units(plan["id"])
            floors = {}
            if floors_from is not None:
                for unit in units:
                    if unit["state"] == "pending":
                        old = unit["planned_date"] and date.fromisoformat(unit["planned_date"])
                        floors[unit["key"]] = floors_from if allow_earlier or not old else max(floors_from, old)
            lanes.append(Lane(plan["id"], units, planner.plan_pace(plan, base), floors))
        return lanes

    def schedule_draft(self, snapshot: dict[str, Any], today: date, *, plan_id: str = "draft",
                       base: Pace | None = None) -> tuple[list[dict[str, Any]], list[str]]:
        """Dates for a draft as if it were confirmed now, sharing days with the active plans after them.

        Returns the dated units and the titles of active plans whose days would move later.
        """
        start = max(today, date.fromisoformat(snapshot["start_date"]))
        # A confirmed plan being revised takes its own lane's place.
        lanes = [lane for lane in self.lanes(base, floors_from=today) if lane.plan_id != plan_id]
        draft = Lane(plan_id, snapshot["units"], planner.plan_pace(snapshot, base))
        result = planner.schedule(lanes + [draft], start, self.forecast(today), factor=self.calibration())
        moved = []
        for lane in lanes:
            dates = result[lane.plan_id]
            if any(unit["state"] == "pending" and dates.get(unit["key"]) != unit["planned_date"] for unit in lane.units):
                moved.append(self._row(lane.plan_id)["title"])
        return planner.apply(snapshot["units"], result[plan_id]), moved

    def recompute(self, today: date, *, force: bool = False, cause: str | None = None,
                  allow_earlier: bool = False, base: Pace | None = None) -> list[dict[str, Any]]:
        """The daily pass over the active plans (once a day unless forced): hold back units whose
        prerequisite is weak, release them when it is not, and give new dates when days were missed, today
        turned into a review day or a unit has no date. Returns what changed, plan by plan."""
        plans = [plan for plan in self.list(["active"]) if force or plan["recomputed_on"] != today.isoformat()]
        if not plans:
            return []
        forecast = self.forecast(today)
        due = forecast.get(today.isoformat(), 0)
        revised: dict[str, list[dict[str, Any]]] = {}
        reasons: dict[str, list[str]] = {}
        for plan in plans:
            units = self.units(plan["id"])
            weak = planner.weak_units(units, self.facts(units))
            blocks = planner.blocking(units, weak)
            changed = []
            new_units = []
            for unit in units:
                if unit["state"] == "pending" and unit["key"] in blocks:
                    unit = {**unit, "state": "blocked", "block_reason": blocks[unit["key"]], "planned_date": None}
                    changed.append("blocked")
                elif unit["state"] == "blocked" and unit["key"] not in blocks:
                    unit = {**unit, "state": "pending", "block_reason": None}
                    changed.append("unblocked")
                elif unit["state"] == "blocked" and unit["block_reason"] != blocks[unit["key"]]:
                    unit = {**unit, "block_reason": blocks[unit["key"]]}
                new_units.append(unit)
            found = planner.triggers(new_units, today, due, planner.plan_pace(plan, base))
            if "missed" in found and not plan["meta"].get("idle_since"):
                missed = [u["planned_date"] for u in units if u["state"] == "pending" and u["planned_date"]
                          and u["planned_date"] < today.isoformat()]
                plan["meta"]["idle_since"] = min(missed)
            revised[plan["id"]] = new_units
            reasons[plan["id"]] = sorted(set(changed + found))
        if cause:
            for plan_id in reasons:
                reasons[plan_id] = sorted(set(reasons[plan_id] + [cause]))
        changes: list[dict[str, Any]] = []
        if any(reasons.values()):
            with tracing.span(self.store, "flow", "plan.recompute") as span:
                changes = self._reschedule(today, revised, reasons, forecast, base, allow_earlier)
                span.set(changed=sum(len(c["units"]) for c in changes),
                         reasons=sorted({r for c in changes for r in c["reasons"]}))
        with self.store.transaction():
            for plan in plans:
                meta = json.loads(self._row(plan["id"])["meta_json"] or "{}")
                if plan["meta"].get("idle_since") and not meta.get("idle_since"):
                    meta["idle_since"] = plan["meta"]["idle_since"]
                self.db.execute("UPDATE study_plans SET recomputed_on=?,meta_json=? WHERE id=?",
                                (today.isoformat(), json.dumps(meta, ensure_ascii=False), plan["id"]))
        return changes

    def _reschedule(self, today: date, revised: dict[str, list[dict[str, Any]]], reasons: dict[str, list[str]],
                    forecast: dict[str, int], base: Pace | None, allow_earlier: bool) -> list[dict[str, Any]]:
        """New dates for every active plan together; each plan whose units changed gets a version."""
        changes: list[dict[str, Any]] = []
        lanes = []
        for plan in self.list(["active"]):
            units = revised.get(plan["id"]) or self.units(plan["id"])
            floors = {}
            for unit in units:
                if unit["state"] == "pending":
                    old = unit["planned_date"] and date.fromisoformat(unit["planned_date"])
                    floors[unit["key"]] = today if allow_earlier or not old else max(today, old)
            lanes.append(Lane(plan["id"], units, planner.plan_pace(plan, base), floors))
        result = planner.schedule(lanes, today, forecast, factor=self.calibration())
        for lane in lanes:
            plan = self.get(lane.plan_id)
            units = planner.apply(lane.units, result[lane.plan_id])
            before = {u["key"]: (u["state"], u["planned_date"], u["block_reason"]) for u in plan["units"]}
            after = {u["key"]: (u["state"], u["planned_date"], u["block_reason"]) for u in units}
            if before == after:
                continue
            why = reasons.get(lane.plan_id) or ["shared_day"]
            moved = [{"title": u["title"], "from": before[u["key"]][1], "to": u["planned_date"],
                      "state": u["state"], "reason": u.get("block_reason")}
                     for u in units if before[u["key"]] != after[u["key"]]]
            snapshot = {**self.snapshot(lane.plan_id), "units": units}
            self.save(lane.plan_id, snapshot, by="reschedule", summary="、".join(REASONS.get(r, r) for r in why))
            changes.append({"plan_id": lane.plan_id, "title": plan["title"], "reasons": why, "units": moved})
        return changes

    # ----- today ----------------------------------------------------------------------------------

    def today(self, today: date, *, base: Pace | None = None) -> dict[str, Any]:
        """What today holds across the active plans and the due reviews (after the daily pass).

        One answer for every place that shows it: the opening line, the status panel, the calendar,
        `zhiqiu status`, /today and the chat context."""
        self.recompute(today, base=base)
        base = base or planner.configured_pace()
        iso = today.isoformat()
        due = queries.due_tasks(self.db, iso)
        reviews = list(dict.fromkeys(row["title"] for row in due))
        factor = self.calibration()
        units, going, blocked, late, slow, later = [], [], [], [], [], []
        plans = self.list(["active"])
        for plan in plans:
            pace = planner.plan_pace(plan, base)
            plan_units = self.units(plan["id"])
            for unit in plan_units:
                item = {"plan_id": plan["id"], "plan": plan["title"], "unit_id": unit["id"], "key": unit["key"],
                        "title": unit["title"], "minutes": round(unit["minutes"] * factor),
                        "source_kind": unit["source_kind"], "source_ref": unit["source_ref"],
                        "planned_date": unit["planned_date"]}
                if unit["state"] == "learning":
                    going.append(item)
                elif unit["state"] == "pending" and unit["planned_date"] == iso:
                    units.append(item)
                elif unit["state"] == "pending" and unit["planned_date"] and unit["planned_date"] > iso:
                    later.append(item)
                elif unit["state"] == "blocked":
                    blocked.append({**item, "reason": unit["block_reason"]})
            days = planner.late_days(plan_units, plan["deadline"])
            if days:
                late.append({"plan_id": plan["id"], "title": plan["title"], "days": days, "deadline": plan["deadline"]})
            idle = plan["meta"].get("idle_since")
            if idle:
                missed = planner.learning_days(date.fromisoformat(idle), today, pace)
                asked = plan["missed_asked_on"]
                if missed >= planner.configured("missed_prompt_days", 3) and (
                        not asked or (today - date.fromisoformat(asked)).days >= planner.MISSED_PROMPT_EVERY):
                    slow.append({"plan_id": plan["id"], "title": plan["title"], "days": missed})
        review_day = bool(plans) and len(reviews) >= base.review_day_at
        done_today = [dict(row) for row in self.db.execute("""SELECT u.title,u.minutes,u.score FROM plan_units u
            JOIN study_plans p ON p.id=u.plan_id WHERE u.state='done' AND u.done_date=?""", (iso,))]
        minutes = sum(item["minutes"] for item in units + going) + len(reviews) * base.review_minutes
        ahead = None
        if not units and not going and done_today and later:
            used = sum(round(row["minutes"] * factor) for row in done_today) + len(reviews) * base.review_minutes
            first = min(later, key=lambda item: item["planned_date"])
            if used + first["minutes"] <= base.daily_minutes:
                ahead = first
        tomorrow = (today + timedelta(days=1)).isoformat()
        return {"date": iso, "active": len(plans), "reviews": reviews, "review_day": review_day,
                "units": units, "continuing": going, "blocked": blocked, "late": late, "slow_down": slow,
                "minutes": minutes, "done_today": done_today, "ahead": ahead,
                "tomorrow": [item for item in later if item["planned_date"] == tomorrow],
                "plans": [{"id": plan["id"], "title": plan["title"],
                           "done": sum(u["state"] == "done" for u in self.units(plan["id"])),
                           "total": sum(u["state"] != "skipped" for u in self.units(plan["id"]))} for plan in plans]}

    def move_priority(self, plan_id: str, direction: str) -> list[dict[str, Any]]:
        """Up or down among the open plans: the order plans take turns in when they share a day."""
        if direction not in ("up", "down"):
            raise ValueError("方向只能是 up 或 down")
        plans = [plan for plan in self.list(["active", "paused"])]
        index = next((i for i, plan in enumerate(plans) if plan["id"] == plan_id), None)
        if index is None:
            raise LookupError("只有进行中或暂停的计划可以调整先后")
        target = index - 1 if direction == "up" else index + 1
        if 0 <= target < len(plans):
            plans[index], plans[target] = plans[target], plans[index]
        with self.store.transaction():
            for number, plan in enumerate(plans):
                self.db.execute("UPDATE study_plans SET priority=? WHERE id=?", (number, plan["id"]))
        return self.list(["active", "paused"])

    def asked_to_slow_down(self, plan_id: str, today: date) -> None:
        with self.store.transaction():
            self.db.execute("UPDATE study_plans SET missed_asked_on=? WHERE id=?", (today.isoformat(), plan_id))

    def calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        return calendar(self.db, start, end)

    # ----- helpers --------------------------------------------------------------------------------

    def _row(self, plan_id: str) -> Any:
        row = self.db.execute("SELECT * FROM study_plans WHERE id=?", (plan_id,)).fetchone()
        if not row:
            raise LookupError(f"没有这个学习计划：{plan_id}")
        return row

    def _update_plan(self, plan_id: str, snapshot: dict[str, Any]) -> None:
        values = {field: snapshot[field] for field in _PLAN_FIELDS if field in snapshot}
        if "rest_days" in values:
            values["rest_days"] = json.dumps(values["rest_days"] or [])
        if "meta" in snapshot:
            values["meta_json"] = json.dumps(snapshot["meta"] or {}, ensure_ascii=False)
        if values:
            names = ",".join(f"{name}=?" for name in values)
            self.db.execute(f"UPDATE study_plans SET {names} WHERE id=?", (*values.values(), plan_id))

    def _write_units(self, plan_id: str, units: list[dict[str, Any]], *, record: bool) -> None:
        keys = [unit["key"] for unit in units]
        if len(set(keys)) != len(keys):
            raise ValueError("计划单元的 key 重复")
        existing = {row["key"]: row for row in self.db.execute("SELECT * FROM plan_units WHERE plan_id=?", (plan_id,))}
        for seq, unit in enumerate(units):
            old = existing.pop(unit["key"], None)
            if old is not None and old["state"] in LOCKED_UNITS:
                self.db.execute("UPDATE plan_units SET seq=? WHERE id=?", (seq, old["id"]))
                continue
            if old is not None and record:
                self._update_confirmed_unit(old, unit, seq)
                continue
            if old is not None:
                self.db.execute("DELETE FROM plan_units WHERE id=?", (old["id"],))
            self._insert_unit(plan_id, seq, unit, record=record)
        for old in existing.values():
            if old["state"] in LOCKED_UNITS:
                # Kept, after the units the revision lists.
                self.db.execute("UPDATE plan_units SET seq=? WHERE id=?", (len(units) + old["seq"], old["id"]))
            elif record:
                if old["state"] != "skipped":
                    self._unit_to(old["id"], "skipped", "removed", skip_reason="removed", planned_date=None)
            else:
                self.db.execute("DELETE FROM plan_units WHERE id=?", (old["id"],))

    def _insert_unit(self, plan_id: str, seq: int, unit: dict[str, Any], *, record: bool) -> None:
        if unit["source_kind"] not in SOURCE_KINDS:
            raise ValueError(f"未知的资料类型：{unit['source_kind']}")
        unit_id = new_id()
        state = unit.get("state") or "pending"
        self.db.execute("""INSERT INTO plan_units(id,plan_id,seq,key,title,goal,requires_json,minutes,source_kind,
            source_ref,kp_id,state,skip_reason,planned_date,first_planned_date,block_reason)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (unit_id, plan_id, seq, unit["key"], unit["title"], unit.get("goal") or "",
             json.dumps(list(unit.get("requires") or []), ensure_ascii=False), int(unit["minutes"]),
             unit["source_kind"], unit.get("source_ref") or "", unit.get("kp_id"), state, unit.get("skip_reason"),
             unit.get("planned_date"), unit.get("planned_date") if record else None, unit.get("block_reason")))
        if record:
            kp_id = unit.get("kp_id")
            if not kp_id:
                plan = self._row(plan_id)
                kp_id = self.store._ensure_knowledge_point(unit["title"], parent_id=plan["topic_kp_id"])
                self.db.execute("UPDATE plan_units SET kp_id=? WHERE id=?", (kp_id, unit_id))
            flow.enter(self.store, "plan_unit", unit_id, state, "plan_revised", kp_id=kp_id)

    def _update_confirmed_unit(self, old: Any, unit: dict[str, Any], seq: int) -> None:
        # A confirmed unit keeps its point, so its title stays; the rest follows the revision.
        self.db.execute("""UPDATE plan_units SET seq=?,goal=?,requires_json=?,minutes=?,source_kind=?,source_ref=?,
            planned_date=?,first_planned_date=COALESCE(first_planned_date,?),block_reason=? WHERE id=?""",
            (seq, unit.get("goal") or "", json.dumps(list(unit.get("requires") or []), ensure_ascii=False),
             int(unit["minutes"]), unit["source_kind"], unit.get("source_ref") or "", unit.get("planned_date"),
             unit.get("planned_date"), unit.get("block_reason"), old["id"]))
        target = unit.get("state") or "pending"
        if target != old["state"]:
            self._unit_to(old["id"], target, "plan_revised", skip_reason=unit.get("skip_reason"))

    def _record_revision(self, plan_id: str, by: str, summary: str) -> int:
        if by not in REVISION_BY:
            raise ValueError(f"unknown revision source: {by}")
        revision = self.db.execute("SELECT revision FROM study_plans WHERE id=?", (plan_id,)).fetchone()[0] + 1
        self.db.execute("UPDATE study_plans SET revision=? WHERE id=?", (revision, plan_id))
        self.db.execute("INSERT INTO plan_revisions VALUES(?,?,?,?,?,?,?)",
                        (new_id(), plan_id, revision, by, summary, json.dumps(self.snapshot(plan_id), ensure_ascii=False),
                         self.store._ts()))
        return revision
