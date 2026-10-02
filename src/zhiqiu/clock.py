from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def today(self) -> date:
        return datetime.now().astimezone().date()

    def fsrs_datetime(self) -> datetime:
        # FSRS is day based here. Use a logical UTC noon for each local date so
        # daylight-saving changes cannot shift a task to the previous day.
        return datetime.combine(self.today(), time(12), timezone.utc)


@dataclass(frozen=True)
class FixedClock(Clock):
    local_date: date

    def now(self) -> datetime:
        return datetime.combine(self.local_date, time(12), timezone.utc)

    def today(self) -> date:
        return self.local_date
