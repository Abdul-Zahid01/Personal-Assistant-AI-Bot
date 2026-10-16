"""Everything the calendar UI shows for a date range, as one flat list of items.

Item types
  block     a time block on the calendar (planned/pinned work, an event, a routine's copy for the day)
  reminder  a point-in-time reminder
  deadline  a task's due time when it isn't scheduled as a block
  routine   a routine occurrence on a future day whose copy doesn't exist yet (read-only preview)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Routine, RoutineRun, Task, TimeBlock
from app.domain.timeutil import day_bounds, local_dt, parse_hhmm

MAX_DAYS = 62


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


async def calendar_range(session: AsyncSession, prefs: dict[str, Any], first: date, last: date) -> list[dict[str, Any]]:
    if last < first:
        raise ValueError("The end date is before the start date.")
    if (last - first).days >= MAX_DAYS:
        raise ValueError(f"Ask for at most {MAX_DAYS} days at a time.")
    tz = ZoneInfo(prefs["timezone"])
    start, _ = day_bounds(first, tz)
    _, end = day_bounds(last, tz)
    items: list[dict[str, Any]] = []

    blocks = (await session.execute(
        select(TimeBlock).where(TimeBlock.status.in_(("PLANNED", "DONE", "SKIPPED")), TimeBlock.start_at < end, TimeBlock.end_at > start)
    )).scalars().all()
    task_ids = {b.task_id for b in blocks if b.task_id}
    tasks = {t.id: t for t in (await session.execute(select(Task).where(Task.id.in_(task_ids)))).scalars()} if task_ids else {}
    for b in blocks:
        t = tasks.get(b.task_id)
        items.append({
            "type": "block", "id": b.id, "block_id": b.id, "task_id": b.task_id, "title": b.title,
            "kind": b.kind, "start": _iso(b.start_at), "end": _iso(b.end_at), "status": b.status, "pinned": b.pinned,
            "reason": b.reason, "priority": t.priority if t else "P3", "task_status": t.status if t else None,
            "task_kind": t.kind if t else None, "notes": t.notes if t else None, "started_at": _iso(t.started_at) if t else None,
            "routine_id": t.routine_id if t else None, "remind_before_min": t.remind_before_min if t else None,
            "estimate_min": t.estimate_min if t else None, "category": t.category if t else None,
        })

    blocked = task_ids
    dated = (await session.execute(
        select(Task).where(Task.due_at >= start, Task.due_at < end, Task.kind.in_(("REMINDER", "TASK")))
    )).scalars().all()
    for t in dated:
        if t.kind == "TASK" and (t.id in blocked or t.status == "DONE"):
            continue
        items.append({
            "type": "reminder" if t.kind == "REMINDER" else "deadline", "id": t.id, "block_id": None, "task_id": t.id,
            "title": t.title, "kind": t.kind, "start": _iso(t.due_at), "end": _iso(t.due_at), "status": t.status,
            "pinned": False, "reason": None, "priority": t.priority, "task_status": t.status, "task_kind": t.kind,
            "notes": t.notes, "started_at": _iso(t.started_at), "routine_id": t.routine_id,
            "remind_before_min": t.remind_before_min, "estimate_min": t.estimate_min, "category": t.category,
        })

    routines = (await session.execute(select(Routine).where(Routine.active.is_(True)))).scalars().all()
    if routines:
        runs = {(r.routine_id, r.day) for r in (await session.execute(
            select(RoutineRun).where(RoutineRun.day >= first.isoformat(), RoutineRun.day <= last.isoformat())
        )).scalars()}
        day = first
        while day <= last:
            for r in routines:
                if str(day.weekday()) in r.days and (r.id, day.isoformat()) not in runs:
                    s = local_dt(day, parse_hhmm(r.time_of_day), tz)
                    items.append({
                        "type": "routine", "id": f"{r.id}:{day.isoformat()}", "block_id": None, "task_id": None,
                        "title": r.title, "kind": r.kind, "start": _iso(s), "end": _iso(s + timedelta(minutes=r.duration_min)),
                        "status": "PROJECTED", "pinned": False, "reason": None, "priority": r.priority, "task_status": None,
                        "task_kind": r.kind, "notes": None, "started_at": None, "routine_id": r.id,
                        "remind_before_min": None, "estimate_min": r.duration_min, "category": r.category,
                    })
            day += timedelta(days=1)

    items.sort(key=lambda i: (i["start"] or "", i["end"] or ""))
    return items
