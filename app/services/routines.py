"""Routines: things you do on a fixed schedule (gym, breakfast, call mom).

A routine is a template. For each day it runs, a normal task copy (an EVENT that blocks time on your calendar, or a
REMINDER) is created ahead of time, so it shows on the day map, gets a notification at its time, and the planner
schedules work around it. A RoutineRun row remembers which days were created, so deleting/skipping one day's copy
doesn't make it come back.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Routine, RoutineRun, Task
from app.domain.timeutil import day_bounds, local_dt, parse_hhmm
from app.domain.types import PRIORITIES
from app.services.tasks import TaskService

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ALL_DAYS = "0123456"


class RoutineError(ValueError):
    pass


def normalize_days(value: Any) -> str:
    """'daily' | 'weekdays' | 'weekends' | 'mon,wed,fri' | [0, 2, 4] | '024'  ->  '024' (Monday = 0)."""
    if value is None or value == "":
        return ALL_DAYS
    if isinstance(value, (list, tuple)):
        items = [str(v).strip().lower() for v in value]
    else:
        text = str(value).strip().lower()
        if text in {"daily", "every day", "everyday", "all"}:
            return ALL_DAYS
        if text in {"weekdays", "workdays"}:
            return "01234"
        if text in {"weekends", "weekend"}:
            return "56"
        items = [p for p in re.split(r"[\s,;/]+", text) if p] if not text.isdigit() else list(text)
    days: set[int] = set()
    for item in items:
        if item.isdigit() and 0 <= int(item) <= 6:
            days.add(int(item))
        elif item[:3] in DAY_NAMES:
            days.add(DAY_NAMES.index(item[:3]))
        else:
            raise RoutineError(f"I don't understand the day '{item}'. Use mon..sun, 'daily', 'weekdays' or 'weekends'.")
    if not days:
        raise RoutineError("A routine needs at least one day.")
    return "".join(str(d) for d in sorted(days))


def describe_days(days: str) -> str:
    if days == ALL_DAYS:
        return "every day"
    if days == "01234":
        return "weekdays"
    if days == "56":
        return "weekends"
    return ", ".join(DAY_NAMES[int(d)].title() for d in days)


class RoutineService:
    def __init__(self, session: AsyncSession, prefs: dict[str, Any]):
        self.s = session
        self.prefs = prefs
        self.tz = ZoneInfo(prefs["timezone"])
        self.tasks = TaskService(session, prefs)

    # ------------------------------------------------------------------ CRUD
    async def list(self) -> list[Routine]:
        rows = (await self.s.execute(select(Routine))).scalars().all()
        return sorted(rows, key=lambda r: (r.time_of_day, r.title.lower()))

    async def get(self, routine_id: str) -> Routine | None:
        return await self.s.get(Routine, routine_id)

    async def find(self, ref: str) -> Routine:
        text = (ref or "").strip().lower()
        routines = await self.list()
        hits = [r for r in routines if r.id == text or r.id.startswith(text) and len(text) >= 4] or \
               [r for r in routines if r.title.lower() == text] or [r for r in routines if text and text in r.title.lower()]
        if not hits:
            raise RoutineError(f"I couldn't find a routine matching '{ref}'.")
        if len(hits) > 1:
            raise RoutineError("More than one routine matches: " + ", ".join(f"{r.title} at {r.time_of_day}" for r in hits[:5]))
        return hits[0]

    async def create(self, now: datetime, *, title: str, time_of_day: str, duration_min: int = 30, days: Any = None,
                     kind: str = "EVENT", priority: str = "P3", category: str | None = None) -> Routine:
        r = Routine(created_at=now, updated_at=now)
        self._apply(r, {"title": title, "time_of_day": time_of_day, "duration_min": duration_min, "days": days,
                        "kind": kind, "priority": priority, "category": category})
        self.s.add(r)
        await self.s.flush()
        await self.materialize_upcoming(now, [r])
        return r

    async def update(self, r: Routine, now: datetime, changes: dict[str, Any]) -> Routine:
        self._apply(r, changes)
        r.updated_at = now
        await self.s.flush()
        # bring the upcoming copies in line: drop ones that no longer fit, update the rest
        for task in await self._future_copies(r, now):
            day = task.due_at.astimezone(self.tz).date()
            if not r.active or str(day.weekday()) not in r.days:
                await self.tasks.delete(task, now)
                run = await self.s.get(RoutineRun, (r.id, day.isoformat()))
                if run is not None:
                    await self.s.delete(run)
            else:
                await self.tasks.update(task, now, {
                    "title": r.title, "kind": r.kind, "priority": r.priority, "category": r.category,
                    "estimate_min": r.duration_min, "due_at": local_dt(day, parse_hhmm(r.time_of_day), self.tz)})
        await self.s.flush()
        await self.materialize_upcoming(now, [r])
        return r

    async def delete(self, r: Routine, now: datetime) -> None:
        for task in await self._future_copies(r, now):
            await self.tasks.delete(task, now)
        for task in (await self.s.execute(select(Task).where(Task.routine_id == r.id))).scalars():
            task.routine_id = None  # past/done copies stay as history, no longer tied to the routine
        await self.s.delete(r)
        await self.s.flush()

    # ------------------------------------------------------------------ daily copies
    async def materialize_upcoming(self, now: datetime, routines: list[Routine] | None = None) -> int:
        """Make sure today's and tomorrow's copies exist."""
        today = now.astimezone(self.tz).date()
        created = 0
        for day in (today, today + timedelta(days=1)):
            created += await self.materialize(day, now, routines)
        return created

    async def materialize(self, day: date, now: datetime, routines: list[Routine] | None = None) -> int:
        routines = routines if routines is not None else await self.list()
        created = 0
        for r in routines:
            if not r.active or str(day.weekday()) not in r.days:
                continue
            if await self.s.get(RoutineRun, (r.id, day.isoformat())) is not None:
                continue
            due = local_dt(day, parse_hhmm(r.time_of_day), self.tz)
            self.s.add(RoutineRun(routine_id=r.id, day=day.isoformat()))
            if r.kind == "REMINDER" and due < now:
                await self.s.flush()  # a reminder whose time already passed today would only be noise
                continue
            await self.tasks.create(
                now=now, title=r.title, kind=r.kind, due_at=due,
                estimate_min=r.duration_min, priority=r.priority, category=r.category, routine_id=r.id,
            )
            await self.s.flush()
            created += 1
        return created

    async def todays_copy(self, r: Routine, now: datetime) -> Task | None:
        start, end = day_bounds(now.astimezone(self.tz).date(), self.tz)
        q = select(Task).where(Task.routine_id == r.id, Task.due_at >= start, Task.due_at < end)
        return (await self.s.execute(q)).scalars().first()

    # ------------------------------------------------------------------ internals
    async def _future_copies(self, r: Routine, now: datetime) -> list[Task]:
        q = select(Task).where(Task.routine_id == r.id, Task.status != "DONE", Task.due_at > now)
        return list((await self.s.execute(q)).scalars().all())

    def _apply(self, r: Routine, changes: dict[str, Any]) -> None:
        for key, value in changes.items():
            if key == "title":
                if not str(value or "").strip():
                    raise RoutineError("A routine needs a name.")
                r.title = str(value).strip()[:255]
            elif key == "time_of_day":
                try:
                    t = parse_hhmm(str(value))
                except ValueError as exc:
                    raise RoutineError("Time must be HH:MM, e.g. 07:30 or 18:00.") from exc
                r.time_of_day = f"{t.hour:02d}:{t.minute:02d}"
            elif key == "duration_min":
                try:
                    n = int(value)
                except (TypeError, ValueError):
                    raise RoutineError("Duration must be a number of minutes.") from None
                if not 1 <= n <= 720:
                    raise RoutineError("Duration must be between 1 and 720 minutes.")
                r.duration_min = n
            elif key == "days":
                r.days = normalize_days(value)
            elif key == "kind":
                kind = str(value or "EVENT").upper()
                if kind not in ("EVENT", "REMINDER"):
                    raise RoutineError("A routine is either an EVENT (blocks time) or a REMINDER.")
                r.kind = kind
            elif key == "priority":
                p = str(value or "P3").upper()
                if p not in PRIORITIES:
                    raise RoutineError("Priority must be P1..P4.")
                r.priority = p
            elif key == "category":
                r.category = (str(value).strip()[:64] or None) if value else None
            elif key == "active":
                r.active = bool(value) if not isinstance(value, str) else value.lower() in {"true", "1", "yes", "on"}
            else:
                raise RoutineError(f"Can't change '{key}' on a routine.")
