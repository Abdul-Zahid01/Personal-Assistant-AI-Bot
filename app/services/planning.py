"""Builds and saves day plans: gathers tasks, events and calendar busy-time, runs the pure planner."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Reminder, Task, TimeBlock
from app.domain.estimates import apply_factor, calibration_factors
from app.domain.planner import Interval, PlanResult, plan_day
from app.domain.prioritizer import PlanTask
from app.domain.timeutil import clock, day_bounds
from app.domain.types import OPEN_STATUSES
from app.services.prefs import to_plan_prefs

log = logging.getLogger(__name__)


class CalendarPort(Protocol):
    async def is_connected(self) -> bool: ...
    async def busy(self, start: datetime, end: datetime) -> list[Interval]: ...
    async def upsert_event(self, *, event_id: str | None, title: str, start: datetime, end: datetime, block_id: str) -> str: ...
    async def delete_event(self, event_id: str) -> None: ...


@dataclass
class DaySchedule:
    day: date
    blocks: list[TimeBlock] = field(default_factory=list)  # work blocks + events, sorted
    reminders: list[Task] = field(default_factory=list)  # reminders that fire this day
    unscheduled: list[Task] = field(default_factory=list)  # tasks due this day that have no block
    overdue: list[Task] = field(default_factory=list)  # open tasks past their deadline


class PlanningService:
    def __init__(self, session: AsyncSession, prefs: dict[str, Any], calendar: CalendarPort | None = None):
        self.s = session
        self.prefs = prefs
        self.tz = ZoneInfo(prefs["timezone"])
        self.plan_prefs = to_plan_prefs(prefs)
        self.calendar = calendar

    # ------------------------------------------------------------------ plan
    async def build(self, day: date, now: datetime) -> PlanResult:
        """Compute a plan without saving it."""
        start, end = day_bounds(day, self.tz)
        tasks = (await self.s.execute(
            select(Task).where(Task.kind == "TASK", Task.status.in_(OPEN_STATUSES))
        )).scalars().all()

        factors = await self._calibration()
        plan_tasks = [
            PlanTask(
                id=t.id, title=t.title, priority=t.priority,
                remaining_min=apply_factor(max(t.estimate_min - t.actual_min, 15), t.category, factors),
                due_at=t.due_at, created_at=t.created_at, category=t.category,
            )
            for t in tasks
        ]

        busy, warnings = await self._busy(start, end)
        result = plan_day(day, plan_tasks, busy, self.plan_prefs, now)
        result.warnings.extend(warnings)
        if factors.get("*") and abs(factors["*"] - 1) >= 0.15:
            result.warnings.append(f"Estimates adjusted by learned factor x{factors['*']} (you usually take that much longer/shorter).")
        return result

    async def commit(self, day: date, now: datetime) -> PlanResult:
        """Compute the plan and save it, replacing any not-yet-finished plan for that day."""
        result = await self.build(day, now)
        start, end = day_bounds(day, self.tz)

        old = (await self.s.execute(
            select(TimeBlock).where(
                TimeBlock.kind == "WORK", TimeBlock.status == "PLANNED",
                TimeBlock.start_at < end, TimeBlock.end_at > start, TimeBlock.end_at > now,
            )
        )).scalars().all()
        for b in old:
            b.status, b.updated_at = "CANCELLED", now
        if old:
            await self.s.execute(
                update(Reminder).where(Reminder.block_id.in_([b.id for b in old]), Reminder.status == "PENDING")
                .values(status="CANCELLED")
            )

        for pb in result.blocks:
            block = TimeBlock(task_id=pb.task_id, title=pb.title, kind="WORK", start_at=pb.start, end_at=pb.end,
                              status="PLANNED", reason=pb.reason, created_at=now, updated_at=now)
            self.s.add(block)
            await self.s.flush()
            minutes = int((pb.end - pb.start).total_seconds() // 60)
            self.s.add(Reminder(
                task_id=pb.task_id, block_id=block.id, kind="BLOCK_START", fire_at=max(pb.start, now),
                text=f"▶️ Time to start: {pb.title} ({minutes} min, until {clock(pb.end, self.tz)})", created_at=now))
            self.s.add(Reminder(
                task_id=pb.task_id, block_id=block.id, kind="BLOCK_END", fire_at=pb.end,
                text=f"⏱ Your time block for “{pb.title}” just ended. How did it go?", created_at=now))
        await self.s.flush()
        return result

    # ------------------------------------------------------------------ views
    async def day_schedule(self, day: date, now: datetime) -> DaySchedule:
        start, end = day_bounds(day, self.tz)
        blocks = (await self.s.execute(
            select(TimeBlock).where(TimeBlock.status.in_(("PLANNED", "DONE")), TimeBlock.start_at < end, TimeBlock.end_at > start)
            .order_by(TimeBlock.start_at)
        )).scalars().all()

        open_tasks = (await self.s.execute(select(Task).where(Task.status.in_(OPEN_STATUSES)))).scalars().all()
        blocked_task_ids = {b.task_id for b in blocks if b.task_id}
        reminders = sorted(
            (t for t in open_tasks if t.kind == "REMINDER" and (t.due_at is None or start <= t.due_at < end)),
            key=lambda t: (t.due_at is None, t.due_at or t.created_at),
        )
        unscheduled = sorted(
            (t for t in open_tasks if t.kind == "TASK" and t.due_at and start <= t.due_at < end and t.id not in blocked_task_ids),
            key=lambda t: t.due_at,
        )
        overdue = sorted(
            (t for t in open_tasks if t.kind == "TASK" and t.due_at and t.due_at < min(start, now)),
            key=lambda t: t.due_at,
        )
        return DaySchedule(day, list(blocks), reminders, unscheduled, overdue)

    # ------------------------------------------------------------------ internals
    async def _busy(self, start: datetime, end: datetime) -> tuple[list[Interval], list[str]]:
        events = (await self.s.execute(
            select(TimeBlock).where(TimeBlock.kind == "EVENT", TimeBlock.status == "PLANNED",
                                    TimeBlock.start_at < end, TimeBlock.end_at > start)
        )).scalars().all()
        busy = [Interval(e.start_at, e.end_at) for e in events]
        warnings: list[str] = []
        if self.calendar is not None:
            try:
                if await self.calendar.is_connected():
                    busy.extend(await self.calendar.busy(start, end))
            except Exception as exc:  # planning must still work when Google is down
                log.warning("Calendar busy-time lookup failed: %s", exc)
                warnings.append("I couldn't read your Google Calendar, so this plan ignores it.")
        return busy, warnings

    async def _calibration(self) -> dict[str, float]:
        rows = (await self.s.execute(
            select(Task.category, Task.estimate_min, Task.actual_min)
            .where(Task.status == "DONE", Task.kind == "TASK", Task.actual_min > 0)
            .order_by(Task.completed_at.desc()).limit(200)
        )).all()
        return calibration_factors([(c, e, a) for c, e, a in rows])
