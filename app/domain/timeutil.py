"""Time helpers. Everything stored or compared is timezone-aware UTC; local time is only for humans."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}


def get_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError(f"Unknown timezone '{name}'. Use an IANA name like 'Asia/Karachi'.") from exc


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_hhmm(value: str) -> time:
    m = re.fullmatch(r"\s*([01]?\d|2[0-3]):([0-5]\d)\s*", str(value))
    if not m:
        raise ValueError(f"'{value}' is not a time in HH:MM format.")
    return time(int(m.group(1)), int(m.group(2)))


def parse_dt(value: str | datetime | None, tz: ZoneInfo) -> datetime | None:
    """Parse an ISO-8601 string. Values without an offset are read in `tz`. Returns UTC or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC)


def local_dt(day: date, t: time, tz: ZoneInfo) -> datetime:
    """Wall-clock time on a local day, as UTC."""
    return datetime.combine(day, t, tzinfo=tz).astimezone(UTC)


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    return local_dt(day, time.min, tz), local_dt(day + timedelta(days=1), time.min, tz)


def local_day(dt: datetime, tz: ZoneInfo) -> date:
    return dt.astimezone(tz).date()


def clock(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%I:%M %p").lstrip("0")


def fmt_dt(dt: datetime, tz: ZoneInfo) -> str:
    return f"{dt.astimezone(tz).strftime('%a %d %b')}, {clock(dt, tz)}"


def fmt_day(day: date) -> str:
    return day.strftime("%A %d %B")


def fmt_minutes(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h}h {m}m"
    return f"{h}h" if h else f"{m}m"


def resolve_day(spec: str | None, now: datetime, tz: ZoneInfo) -> date:
    """'today' | 'tomorrow' | 'YYYY-MM-DD' | weekday name -> a local date."""
    today = now.astimezone(tz).date()
    s = (spec or "today").strip().lower()
    if s in {"", "today"}:
        return today
    if s == "tomorrow":
        return today + timedelta(days=1)
    if s == "yesterday":
        return today - timedelta(days=1)
    if s in _WEEKDAYS:
        return today + timedelta(days=(_WEEKDAYS[s] - today.weekday()) % 7 or 7)
    try:
        return date.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"I couldn't understand the date '{spec}'. Use today, tomorrow or YYYY-MM-DD.") from exc


def _parse_clock(text: str) -> tuple[int, int] | None:
    m = re.search(r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*(am|pm)\b", text)
    if m:
        hour, minute, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ap == "pm" and hour != 12:
            hour += 12
        if ap == "am" and hour == 12:
            hour = 0
        return hour, minute
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    if re.search(r"\bnoon\b", text):
        return 12, 0
    return None


def parse_when(text: str, now: datetime, tz: ZoneInfo) -> datetime | None:
    """Best-effort natural-language time ("tomorrow at 7pm", "in 20 mins", "friday 3pm"). UTC or None.

    This is the offline fallback; with an LLM the model emits ISO timestamps instead.
    """
    t = text.lower()
    local_now = now.astimezone(tz)

    m = re.search(r"\bin\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?)\b", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(days=n) if unit.startswith("d") else timedelta(hours=n) if unit.startswith("h") else timedelta(minutes=n)
        return (now + delta).astimezone(UTC)

    day: date | None = None
    if re.search(r"\btomorr\w*\b", t):
        day = local_now.date() + timedelta(days=1)
    elif re.search(r"\b(today|tonight)\b", t):
        day = local_now.date()
    else:
        for name, idx in _WEEKDAYS.items():
            if re.search(rf"\b{name}\b", t):
                day = local_now.date() + timedelta(days=(idx - local_now.weekday()) % 7 or 7)
                break

    hm = _parse_clock(t)
    if day is None and hm is None:
        return None
    if day is None:  # only a clock time: today if still ahead, else tomorrow
        day = local_now.date()
        if datetime.combine(day, time(*hm), tzinfo=tz) <= local_now:
            day += timedelta(days=1)
    if hm is None:
        hm = (20, 0) if "tonight" in t else (9, 0)
    return local_dt(day, time(*hm), tz)
