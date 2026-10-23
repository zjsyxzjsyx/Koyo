"""Reminders at a set time: "every day at 21:00, review", "Friday 14:00, hand in the report".

Times are local wall-clock minutes written "YYYY-MM-DD HH:MM", so they sort as text and mean what the
user said. A reminder is due when next_at has come; firing it records when, then moves next_at to the
following occurrence (or ends a one-off). A reminder never runs anything by itself: it is said to the
user, who decides what to do.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .store import Store, new_id

REPEATS = {"none": "一次", "daily": "每天", "weekdays": "工作日", "weekly": "每周"}
FORMAT = "%Y-%m-%d %H:%M"
TITLE_CHARS = 60
NOTE_CHARS = 200
# How far ahead a one-off may be set, and how long ago a due one still counts as just due (not missed).
AHEAD_DAYS = 366
LATE = timedelta(minutes=10)
SNOOZE_MINUTES = (5, 10, 30, 60)


def parse(text: Any) -> datetime:
    if not isinstance(text, str):
        raise ValueError("时间应写成 YYYY-MM-DD HH:MM")
    try:
        return datetime.strptime(text.strip().replace("T", " ")[:16], FORMAT)
    except ValueError:
        raise ValueError(f"时间应写成 YYYY-MM-DD HH:MM：{text}") from None


def stamp(moment: datetime) -> str:
    return moment.strftime(FORMAT)


def local_now() -> datetime:
    return datetime.now().replace(second=0, microsecond=0)


def following(at: datetime, repeat: str, after: datetime) -> datetime | None:
    """The first occurrence of a reminder set for `at` that is later than `after` (None for a one-off)."""
    if repeat == "none":
        return at if at > after else None
    moment = at
    if moment <= after:
        # Jump close in one step, then walk the last few days.
        days = (after.date() - at.date()).days
        step = 7 if repeat == "weekly" else 1
        moment = at + timedelta(days=max(0, days // step * step - step))
    while moment <= after or (repeat == "weekdays" and moment.isoweekday() > 5):
        moment += timedelta(days=7 if repeat == "weekly" else 1)
    return moment


def occurrences(row: dict[str, Any], start: date, end: date, limit: int = 62) -> list[datetime]:
    """The times an active reminder falls on between two dates (inclusive), for the calendar."""
    if row["status"] != "active" or not row["next_at"]:
        return []
    moment = parse(row["next_at"])
    result = []
    while moment.date() <= end and len(result) < limit:
        if moment.date() >= start:
            result.append(moment)
        nxt = following(parse(row["at"]), row["repeat"], moment)
        if nxt is None:
            break
        moment = nxt
    return result


def describe(row: dict[str, Any]) -> str:
    when = parse(row["next_at"] or row["at"])
    repeat = row["repeat"]
    clock = when.strftime("%H:%M")
    if repeat == "daily":
        return f"每天 {clock}"
    if repeat == "weekdays":
        return f"工作日 {clock}"
    if repeat == "weekly":
        return f"每周{'一二三四五六日'[when.weekday()]} {clock}"
    return when.strftime("%m-%d %H:%M")


class Reminders:
    def __init__(self, store: Store):
        self.store = store

    @property
    def db(self):
        return self.store.db

    def get(self, reminder_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,)).fetchone()
        if not row:
            raise LookupError(f"没有这个提醒：{reminder_id}")
        return dict(row)

    @staticmethod
    def check(title: Any, at: Any, repeat: Any, note: Any = "", now: datetime | None = None) -> tuple[str, datetime, str, str]:
        """The cleaned values, or ValueError saying what is wrong (used before asking the user, too)."""
        title = " ".join(title.split()) if isinstance(title, str) else ""
        if not title or len(title) > TITLE_CHARS:
            raise ValueError(f"提醒内容须为 1 到 {TITLE_CHARS} 字")
        note = note.strip() if isinstance(note, str) else ""
        if len(note) > NOTE_CHARS:
            raise ValueError(f"备注最多 {NOTE_CHARS} 字")
        if repeat not in REPEATS:
            raise ValueError(f"重复方式只能是 {'、'.join(REPEATS)}")
        moment = parse(at)
        now = now or local_now()
        if repeat == "none" and moment <= now:
            raise ValueError(f"提醒时间已经过了：{stamp(moment)}（现在 {stamp(now)}）")
        if moment > now + timedelta(days=AHEAD_DAYS):
            raise ValueError("提醒时间最多设在一年以内")
        return title, moment, repeat, note

    def create(self, title: Any, at: Any, repeat: Any = "none", note: Any = "", *,
               conversation_id: str | None = None, now: datetime | None = None) -> dict[str, Any]:
        now = now or local_now()
        title, moment, repeat, note = self.check(title, at, repeat, note, now)
        first = following(moment, repeat, now - timedelta(minutes=1))
        reminder_id = new_id()
        with self.store.transaction():
            self.db.execute("""INSERT INTO reminders(id,title,note,at,repeat,next_at,status,conversation_id,created_at)
                VALUES(?,?,?,?,?,?,'active',?,?)""", (reminder_id, title, note, stamp(moment), repeat,
                                                      stamp(first) if first else None, conversation_id, self.store._ts()))
        return self.get(reminder_id)

    def active(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM reminders WHERE status='active' ORDER BY next_at, created_at")]

    def today(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Active reminders still to come today, plus those already said today."""
        now = now or local_now()
        day = now.date()
        rows = [row for row in self.active() if row["next_at"] and parse(row["next_at"]).date() == day]
        said = [dict(row) for row in self.db.execute("SELECT * FROM reminders WHERE fired_at LIKE ? AND status!='cancelled'",
                                                     (f"{day.isoformat()}%",))]
        return rows + [row for row in said if row["id"] not in {item["id"] for item in rows}]

    def due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or local_now()
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM reminders WHERE status='active' AND next_at<=? ORDER BY next_at", (stamp(now),))]

    def fire(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Mark every due reminder as said and move it on; returns them, each with `late` when it came due
        more than a few minutes ago (the workbench was not running then)."""
        now = now or local_now()
        fired = []
        with self.store.transaction():
            for row in self.due(now):
                nxt = following(parse(row["at"]), row["repeat"], now)
                self.db.execute("""UPDATE reminders SET fired_at=?,fired_count=fired_count+1,next_at=?,status=?
                    WHERE id=?""", (stamp(now), stamp(nxt) if nxt else None, "active" if nxt else "done", row["id"]))
                fired.append({**row, "late": now - parse(row["next_at"]) > LATE, "due_at": row["next_at"],
                              "next_at": stamp(nxt) if nxt else None})
        return fired

    def snooze(self, reminder_id: str, minutes: int, now: datetime | None = None) -> dict[str, Any]:
        if minutes not in SNOOZE_MINUTES:
            raise ValueError(f"稍后提醒只能是 {'、'.join(map(str, SNOOZE_MINUTES))} 分钟")
        now = now or local_now()
        row = self.get(reminder_id)
        if row["status"] == "cancelled":
            raise ValueError("这个提醒已经取消")
        later = now + timedelta(minutes=minutes)
        # A repeating one keeps its next regular time when that comes first.
        nxt = later if not row["next_at"] or parse(row["next_at"]) > later else parse(row["next_at"])
        with self.store.transaction():
            self.db.execute("UPDATE reminders SET next_at=?,status='active' WHERE id=?", (stamp(nxt), reminder_id))
        return self.get(reminder_id)

    def cancel(self, reminder_id: str) -> dict[str, Any]:
        self.get(reminder_id)
        with self.store.transaction():
            self.db.execute("UPDATE reminders SET status='cancelled',next_at=NULL WHERE id=?", (reminder_id,))
        return self.get(reminder_id)

    def in_range(self, start: date, end: date) -> list[dict[str, Any]]:
        return in_range(self.db, start, end)


def in_range(db: Any, start: date, end: date) -> list[dict[str, Any]]:
    """Each occurrence of an active reminder between two dates, for the calendar (any connection, read-only
    is enough): {id, title, note, repeat, at, date, time}. [] for a database from before reminders."""
    try:
        rows = [dict(row) for row in db.execute("SELECT * FROM reminders WHERE status='active' ORDER BY next_at")]
    except Exception:
        return []
    result = []
    for row in rows:
        for moment in occurrences(row, start, end):
            result.append({"id": row["id"], "title": row["title"], "note": row["note"], "repeat": row["repeat"],
                           "at": stamp(moment), "date": moment.date().isoformat(), "time": moment.strftime("%H:%M")})
    return sorted(result, key=lambda item: item["at"])
