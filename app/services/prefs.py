"""User preferences: validated key/value settings stored in the database."""

from __future__ import annotations

import json
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Preference
from app.domain.planner import BlockedWindow, PlanPrefs
from app.domain.timeutil import get_tz, parse_hhmm


class PrefError(ValueError):
    pass


_TIME_KEYS = ("work_start", "work_end", "focus_start", "focus_end", "morning_brief", "evening_review")
_INT_RANGES = {
    "max_block_min": (15, 240),
    "min_block_min": (5, 120),
    "buffer_min": (0, 60),
    "nag_interval_min": (10, 720),
    "nag_max": (1, 48),
    "deadline_lead_min": (0, 1440),
    "event_lead_min": (0, 1440),
}
_BOOL_KEYS = ("auto_plan", "proactive")
INTERNAL_KEYS = ("last_brief_date", "last_review_date", "telegram_offset", "google_calendar_id")


def defaults(settings: Settings) -> dict[str, Any]:
    return {
        "timezone": settings.TIMEZONE,
        "work_start": "09:00",
        "work_end": "18:00",
        "focus_start": "09:00",
        "focus_end": "12:00",
        "max_block_min": 90,
        "min_block_min": 15,
        "buffer_min": 10,
        "morning_brief": "08:00",
        "evening_review": "21:00",
        "nag_interval_min": 120,
        "nag_max": 12,
        "deadline_lead_min": 10,
        "event_lead_min": 0,  # 0 = notify exactly when an event/routine starts; e.g. 10 = ten minutes before
        "auto_plan": True,  # plan the day automatically in the morning brief if you haven't
        "proactive": True,  # master switch for briefs, reviews and block reminders
        "blocked_windows": [],  # [{"label": "lunch", "start": "13:00", "end": "14:00"}]
    }


PUBLIC_KEYS = tuple(defaults(Settings(_env_file=None)).keys())


def normalize(key: str, value: Any) -> Any:
    if key not in PUBLIC_KEYS:
        raise PrefError(f"Unknown setting '{key}'. Known settings: {', '.join(PUBLIC_KEYS)}.")
    if isinstance(value, str) and key not in ("timezone",) + _TIME_KEYS:
        try:
            value = json.loads(value)  # the LLM often passes "90" or "true" as strings
        except ValueError:
            pass
    if key == "timezone":
        get_tz(str(value))
        return str(value)
    if key in _TIME_KEYS:
        t = parse_hhmm(str(value))
        return f"{t.hour:02d}:{t.minute:02d}"
    if key in _INT_RANGES:
        lo, hi = _INT_RANGES[key]
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise PrefError(f"{key} must be a whole number.") from None
        if not lo <= n <= hi:
            raise PrefError(f"{key} must be between {lo} and {hi}.")
        return n
    if key in _BOOL_KEYS:
        if isinstance(value, bool):
            return value
        if str(value).lower() in {"true", "yes", "on", "1"}:
            return True
        if str(value).lower() in {"false", "no", "off", "0"}:
            return False
        raise PrefError(f"{key} must be true or false.")
    if key == "blocked_windows":
        if not isinstance(value, list):
            raise PrefError("blocked_windows must be a list of {label, start, end}.")
        return [normalize_window(w) for w in value]
    raise PrefError(f"Cannot set '{key}'.")  # pragma: no cover


def normalize_window(w: Any) -> dict[str, str]:
    if not isinstance(w, dict) or not w.get("label"):
        raise PrefError("A blocked window needs a label, start and end (HH:MM).")
    start, end = parse_hhmm(str(w.get("start"))), parse_hhmm(str(w.get("end")))
    if start == end:
        raise PrefError("A blocked window's start and end must differ.")
    return {"label": str(w["label"])[:40], "start": f"{start:%H:%M}", "end": f"{end:%H:%M}"}


async def load_prefs(session: AsyncSession, settings: Settings) -> dict[str, Any]:
    prefs = defaults(settings)
    rows = (await session.execute(select(Preference))).scalars().all()
    for row in rows:
        if row.key in prefs:
            prefs[row.key] = row.value
        elif row.key in INTERNAL_KEYS:
            prefs[row.key] = row.value
    return prefs


async def set_pref(session: AsyncSession, key: str, value: Any) -> Any:
    clean = normalize(key, value)
    await put_internal(session, key, clean)
    return clean


async def put_internal(session: AsyncSession, key: str, value: Any) -> None:
    row = await session.get(Preference, key)
    if row is None:
        session.add(Preference(key=key, value=value))
    else:
        row.value = value
    await session.flush()


def to_plan_prefs(p: dict[str, Any]) -> PlanPrefs:
    return PlanPrefs(
        tz=ZoneInfo(p["timezone"]),
        work_start=parse_hhmm(p["work_start"]),
        work_end=parse_hhmm(p["work_end"]),
        focus_start=parse_hhmm(p["focus_start"]),
        focus_end=parse_hhmm(p["focus_end"]),
        max_block_min=int(p["max_block_min"]),
        min_block_min=int(p["min_block_min"]),
        buffer_min=int(p["buffer_min"]),
        blocked=tuple(
            BlockedWindow(w["label"], parse_hhmm(w["start"]), parse_hhmm(w["end"])) for w in p.get("blocked_windows", [])
        ),
    )


def hhmm(p: dict[str, Any], key: str) -> time:
    return parse_hhmm(p[key])
