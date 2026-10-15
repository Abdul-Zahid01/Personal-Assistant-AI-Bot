from __future__ import annotations

import calendar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.domain.types import RECURRENCES
from app.domain.timeutil import UTC


def _step(local: datetime, rule: str) -> datetime:
    """One occurrence forward, keeping the local wall-clock time (so DST doesn't shift it)."""
    if rule == "DAILY":
        return local + timedelta(days=1)
    if rule == "WEEKLY":
        return local + timedelta(days=7)
    if rule == "WEEKDAYS":
        nxt = local + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return nxt
    if rule == "MONTHLY":
        month = local.month % 12 + 1
        year = local.year + (1 if local.month == 12 else 0)
        day = min(local.day, calendar.monthrange(year, month)[1])
        return local.replace(year=year, month=month, day=day)
    raise ValueError(f"Unknown recurrence '{rule}'. Use one of {', '.join(RECURRENCES)}.")


def next_occurrence(rule: str, current_due: datetime, tz: ZoneInfo, after: datetime | None = None) -> datetime:
    """Next due time strictly after `current_due` (and after `after`, so late completion doesn't spawn past dates)."""
    local = current_due.astimezone(tz).replace(tzinfo=None)
    floor = after.astimezone(UTC) if after else current_due
    for _ in range(800):  # bounded: a daily item missed for two years still terminates
        local = _step(local, rule)
        candidate = local.replace(tzinfo=tz).astimezone(UTC)
        if candidate > current_due and candidate > floor:
            return candidate
    return candidate
