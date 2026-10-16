"""Direct manipulation of the calendar: put something at a time, move it, resize it, remove it.

- An EVENT (meeting, gym) is moved by changing its task's start/duration; the task keeps its own block in sync.
- A REMINDER is a point in time; moving it changes when it fires.
- A TASK placed by hand gets a *pinned* WORK block: the auto-planner keeps it where you put it and plans around it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Reminder, Task, TimeBlock
from app.domain.timeutil import clock
from app.services.tasks import TaskError, TaskService

MAX_MINUTES = 24 * 60


class BlockService:
    def __init__(self, session: AsyncSession, prefs: dict[str, Any]):
        self.s = session
        self.prefs = prefs
        self.tz = ZoneInfo(prefs["timezone"])
        self.tasks = TaskService(session, prefs)

    async def create_at(self, now: datetime, *, title: str, kind: str, start: datetime, end: datetime | None,
                        priority: str = "P3", notes: str | None = None, category: str | None = None,
                        remind_before_min: int | None = None) -> tuple[Task, TimeBlock | None]:
        kind = (kind or "TASK").upper()
        minutes = self._minutes(start, end) if kind != "REMINDER" else 5
        common = dict(now=now, title=title, priority=priority, notes=notes, category=category,
                      remind_before_min=remind_before_min)
        if kind == "EVENT":
            task = await self.tasks.create(kind="EVENT", due_at=start, estimate_min=minutes, **common)
            return task, await self._event_block(task)
        if kind == "REMINDER":
            return await self.tasks.create(kind="REMINDER", due_at=start, **common), None
        if kind != "TASK":
            raise TaskError("Type must be TASK, EVENT or REMINDER.")
        task = await self.tasks.create(kind="TASK", estimate_min=minutes, **common)
        block = TimeBlock(task_id=task.id, title=task.title, kind="WORK", start_at=start, end_at=start + timedelta(minutes=minutes),
                          status="PLANNED", pinned=True, reason="placed by you", created_at=now, updated_at=now)
        self.s.add(block)
        await self.s.flush()
        self._block_reminders(block, task, now)
        await self.s.flush()
        return task, block

    async def move(self, block: TimeBlock, now: datetime, start: datetime, end: datetime | None) -> TimeBlock:
        if block.status in ("CANCELLED", "DONE"):
            raise TaskError("That item is finished; it can't be moved.")
        minutes = self._minutes(start, end or start + (block.end_at - block.start_at))
        task = await self.s.get(Task, block.task_id) if block.task_id else None
        if block.kind == "EVENT" and task is not None:
            await self.tasks.update(task, now, {"due_at": start, "estimate_min": minutes})  # task re-syncs its block
            await self.s.refresh(block)
            return block
        block.start_at, block.end_at = start, start + timedelta(minutes=minutes)
        block.pinned, block.reason, block.updated_at = True, "placed by you", now
        await self._cancel_block_reminders(block)
        if task is not None:
            self._block_reminders(block, task, now)
        await self.s.flush()
        return block

    async def remove(self, block: TimeBlock, now: datetime) -> None:
        """Take it off the calendar. Events/reminders are deleted; a task goes back to the to-do list."""
        task = await self.s.get(Task, block.task_id) if block.task_id else None
        if block.kind == "EVENT" and task is not None:
            await self.tasks.delete(task, now)
            return
        block.status, block.updated_at = "CANCELLED", now
        await self._cancel_block_reminders(block)
        await self.s.flush()

    async def move_reminder(self, task: Task, now: datetime, when: datetime) -> Task:
        if task.kind != "REMINDER":
            raise TaskError("Only reminders can be moved this way.")
        return await self.tasks.update(task, now, {"due_at": when})

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _minutes(start: datetime, end: datetime | None) -> int:
        if end is None:
            return 30
        minutes = round((end - start).total_seconds() / 60)
        if minutes < 5:
            raise TaskError("An item must be at least 5 minutes long.")
        if minutes > MAX_MINUTES:
            raise TaskError("An item can't be longer than 24 hours.")
        return minutes

    async def _event_block(self, task: Task) -> TimeBlock | None:
        q = select(TimeBlock).where(TimeBlock.task_id == task.id, TimeBlock.kind == "EVENT", TimeBlock.status == "PLANNED")
        return (await self.s.execute(q)).scalars().first()

    async def _cancel_block_reminders(self, block: TimeBlock) -> None:
        await self.s.execute(update(Reminder).where(Reminder.block_id == block.id, Reminder.status == "PENDING")
                             .values(status="CANCELLED"))

    def _block_reminders(self, block: TimeBlock, task: Task, now: datetime) -> None:
        lead = timedelta(minutes=task.remind_before_min or 0)
        minutes = int((block.end_at - block.start_at).total_seconds() // 60)
        start_text = (f"▶️ In {task.remind_before_min} min: {block.title} ({clock(block.start_at, self.tz)}, {minutes} min)"
                      if lead else f"▶️ Time to start: {block.title} ({minutes} min, until {clock(block.end_at, self.tz)})")
        if block.start_at - lead > now - timedelta(minutes=1):
            self.s.add(Reminder(task_id=task.id, block_id=block.id, kind="BLOCK_START", fire_at=max(block.start_at - lead, now),
                                text=start_text, created_at=now))
        if block.end_at > now:
            self.s.add(Reminder(task_id=task.id, block_id=block.id, kind="BLOCK_END", fire_at=block.end_at,
                                text=f"⏱ Time's up for “{block.title}”. How did it go?", created_at=now))
