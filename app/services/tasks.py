"""Task lifecycle: create, update, complete, snooze, delete — and the reminders/blocks that follow from each."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Reminder, Task, TimeBlock
from app.domain.recurrence import next_occurrence
from app.domain.timeutil import clock, fmt_dt
from app.domain.types import KINDS, OPEN_STATUSES, PRIORITIES, RECURRENCES

SCHEDULE_REMINDER_KINDS = ("REMINDER", "NAG", "DEADLINE", "EVENT_START")
_LATEST_WORDS = {"", "latest", "last", "it", "that", "this", "current", "recent", "most recent"}


class TaskError(ValueError):
    """A problem the user can understand and fix (shown to them as-is)."""


@dataclass
class CompleteResult:
    task: Task
    next_task: Task | None = None


def short_id(task_id: str) -> str:
    return task_id[:8]


class TaskService:
    def __init__(self, session: AsyncSession, prefs: dict[str, Any]):
        self.s = session
        self.prefs = prefs
        self.tz = ZoneInfo(prefs["timezone"])

    # ------------------------------------------------------------------ create / update
    async def create(
        self,
        *,
        now: datetime,
        title: str,
        kind: str = "TASK",
        due_at: datetime | None = None,
        estimate_min: int | None = None,
        priority: str = "P3",
        notes: str | None = None,
        category: str | None = None,
        recurrence: str | None = None,
        routine_id: str | None = None,
    ) -> Task:
        title = (title or "").strip()
        if not title:
            raise TaskError("A task needs a title.")
        kind = self._choose(kind, KINDS, "kind", "TASK")
        priority = self._choose(priority, PRIORITIES, "priority", "P3")
        recurrence = self._recurrence(recurrence)
        if kind == "EVENT" and due_at is None:
            raise TaskError("An event needs a start time.")
        if recurrence and due_at is None:
            raise TaskError("A repeating item needs a time or date to repeat from.")
        if estimate_min is None:
            estimate_min = {"EVENT": 60, "REMINDER": 5}.get(kind, 30)
        estimate_min = self._minutes(estimate_min, 1440 if kind == "EVENT" else 720)

        task = Task(
            title=title[:255], notes=notes or None, kind=kind, status="TODO", priority=priority,
            estimate_min=estimate_min, due_at=due_at, category=(category or None), recurrence=recurrence,
            created_at=now, updated_at=now, routine_id=routine_id,
        )
        self.s.add(task)
        await self.s.flush()
        await self._sync_schedule(task, now)
        return task

    async def update(self, task: Task, now: datetime, changes: dict[str, Any]) -> Task:
        resched = False
        for key, value in changes.items():
            if key == "title":
                if not str(value or "").strip():
                    raise TaskError("The title can't be empty.")
                task.title, resched = str(value).strip()[:255], True
            elif key == "notes":
                task.notes = value or None
            elif key == "due_at":
                task.due_at, resched = value, True
            elif key == "estimate_min":
                task.estimate_min = self._minutes(value, 1440 if task.kind == "EVENT" else 720)
                resched = True
            elif key == "priority":
                task.priority = self._choose(value, PRIORITIES, "priority", "P3")
            elif key == "category":
                task.category = value or None
            elif key == "kind":
                task.kind, resched = self._choose(value, KINDS, "kind", "TASK"), True
            elif key == "recurrence":
                task.recurrence = None if str(value or "NONE").upper() == "NONE" else self._recurrence(value)
            elif key == "status":
                status = str(value).upper()
                if status not in OPEN_STATUSES:
                    raise TaskError("Use complete to mark a task done; status can only be TODO or IN_PROGRESS.")
                if status == "IN_PROGRESS" and task.status != "IN_PROGRESS":
                    task.started_at = now  # the clock for "how far along am I" starts now
                elif status == "TODO":
                    task.started_at = None
                task.status = status
            else:
                raise TaskError(f"Can't change '{key}'.")
        if task.kind == "EVENT" and task.due_at is None:
            raise TaskError("An event needs a start time.")
        if task.recurrence and task.due_at is None:
            raise TaskError("A repeating item needs a time or date to repeat from.")
        task.updated_at = now
        if resched:
            await self._sync_schedule(task, now)
        await self.s.flush()
        return task

    async def complete(self, task: Task, now: datetime, actual_min: int | None = None) -> CompleteResult:
        if task.status == "DONE":
            raise TaskError(f"'{task.title}' is already done.")
        task.status, task.completed_at, task.updated_at = "DONE", now, now
        if actual_min is not None:
            task.actual_min = self._minutes(actual_min, 1440)
        elif task.started_at is not None and task.actual_min == 0:
            # you pressed Start, so we know how long it really took (feeds the estimate learning)
            task.actual_min = max(1, min(1440, round((now - task.started_at).total_seconds() / 60)))

        await self._cancel_reminders(task.id)
        for block in await self._blocks(task.id, statuses=("PLANNED",)):
            if block.kind == "EVENT" or block.end_at <= now:
                block.status = "DONE"
            else:  # work time that's no longer needed
                block.status = "CANCELLED"
            block.updated_at = now

        next_task = None
        if task.recurrence and task.due_at:
            next_due = next_occurrence(task.recurrence, task.due_at, self.tz, after=now)
            next_task = await self.create(
                now=now, title=task.title, kind=task.kind, due_at=next_due, estimate_min=task.estimate_min,
                priority=task.priority, notes=task.notes, category=task.category, recurrence=task.recurrence,
            )
        await self.s.flush()
        return CompleteResult(task, next_task)

    async def snooze(self, task: Task, now: datetime, minutes: int) -> Task:
        minutes = self._minutes(minutes, 7 * 24 * 60)
        base = max(task.due_at, now) if task.due_at else now
        return await self.update(task, now, {"due_at": base + timedelta(minutes=minutes)})

    async def delete(self, task: Task, now: datetime) -> None:
        for block in await self._blocks(task.id, statuses=("PLANNED", "DONE", "SKIPPED")):
            block.status, block.task_id, block.updated_at = "CANCELLED", None, now  # lets calendar sync remove the event
        await self.s.flush()
        await self.s.delete(task)
        await self.s.flush()

    # ------------------------------------------------------------------ queries
    async def get(self, task_id: str) -> Task | None:
        return await self.s.get(Task, task_id)

    async def list_tasks(self, *, status: str = "open", kind: str | None = None, limit: int = 100) -> list[Task]:
        q = select(Task)
        if status == "open":
            q = q.where(Task.status.in_(OPEN_STATUSES))
        elif status == "done":
            q = q.where(Task.status == "DONE")
        if kind:
            q = q.where(Task.kind == kind.upper())
        rows = list((await self.s.execute(q.limit(1000))).scalars().all())
        rows.sort(key=lambda t: (t.due_at is None, t.due_at or t.created_at, t.priority))
        if status == "done":
            rows.sort(key=lambda t: t.completed_at or t.created_at, reverse=True)
        return rows[:limit]

    async def resolve(self, ref: str | None, *, include_done: bool = False) -> Task:
        """Find a task from what a person would say: 'latest', an id, or part of the title."""
        text = (ref or "").strip()
        open_q = select(Task).where(Task.status.in_(OPEN_STATUSES))

        if text.lower() in _LATEST_WORDS:
            task = (await self.s.execute(open_q.order_by(Task.updated_at.desc()).limit(1))).scalars().first()
            if task is None:
                raise TaskError("You have no open tasks yet.")
            return task

        if re.fullmatch(r"[0-9a-f]{4,32}", text.lower()):
            hits = (await self.s.execute(select(Task).where(Task.id.like(text.lower() + "%")).limit(5))).scalars().all()
            if len(hits) == 1:
                return hits[0]

        pools = [open_q] + ([select(Task).where(Task.status == "DONE")] if include_done else [])
        needle = text.lower()
        tokens = [t for t in re.findall(r"\w+", needle) if len(t) > 1]
        for q in pools:
            candidates = (await self.s.execute(q.limit(2000))).scalars().all()
            tiers: dict[int, list[Task]] = {}
            for t in candidates:
                title = t.title.lower()
                if title == needle:
                    tier = 4
                elif title.startswith(needle):
                    tier = 3
                elif needle in title:
                    tier = 2
                elif tokens and all(tok in title for tok in tokens):
                    tier = 1
                else:
                    continue
                tiers.setdefault(tier, []).append(t)
            if tiers:
                best = tiers[max(tiers)]
                if len(best) == 1:
                    return best[0]
                options = "; ".join(f"{short_id(t.id)}: {t.title}" for t in best[:5])
                raise TaskError(f"More than one task matches '{text}' ({options}). Tell me which one (use the id).")
        raise TaskError(f"I couldn't find a task matching '{text}'.")

    async def event_conflicts(self, start: datetime, end: datetime, exclude_task_id: str | None = None) -> list[TimeBlock]:
        q = select(TimeBlock).where(
            TimeBlock.kind == "EVENT", TimeBlock.status == "PLANNED", TimeBlock.start_at < end, TimeBlock.end_at > start
        )
        blocks = (await self.s.execute(q)).scalars().all()
        return [b for b in blocks if b.task_id != exclude_task_id]

    # ------------------------------------------------------------------ internals
    async def _blocks(self, task_id: str, statuses: tuple[str, ...]) -> list[TimeBlock]:
        q = select(TimeBlock).where(TimeBlock.task_id == task_id, TimeBlock.status.in_(statuses))
        return list((await self.s.execute(q)).scalars().all())

    async def _cancel_reminders(self, task_id: str, kinds: tuple[str, ...] | None = None) -> None:
        cond = [Reminder.task_id == task_id, Reminder.status == "PENDING"]
        if kinds:
            cond.append(Reminder.kind.in_(kinds))
        await self.s.execute(update(Reminder).where(*cond).values(status="CANCELLED"))

    async def _sync_schedule(self, task: Task, now: datetime) -> None:
        """(Re)build the reminders and the calendar slot implied by the task's current kind/time."""
        await self._cancel_reminders(task.id, SCHEDULE_REMINDER_KINDS)
        lead = timedelta(minutes=int(self.prefs["deadline_lead_min"]))
        due = task.due_at

        def add(kind: str, fire_at: datetime, text: str, every: int | None = None, repeats: int = 0) -> None:
            self.s.add(Reminder(task_id=task.id, kind=kind, fire_at=fire_at, text=text, repeat_every_min=every,
                                repeats_left=repeats, created_at=now))

        if task.kind == "REMINDER":
            if due is not None:
                add("REMINDER", max(due, now), f"⏰ Reminder: {task.title}")
            else:
                every = int(self.prefs["nag_interval_min"])
                add("NAG", now + timedelta(minutes=every), f"🔔 Still pending: {task.title}", every,
                    int(self.prefs["nag_max"]) - 1)
        elif task.kind == "EVENT" and due is not None:
            event_lead = timedelta(minutes=int(self.prefs.get("event_lead_min", 0)))
            end = due + timedelta(minutes=task.estimate_min)
            if event_lead and due - event_lead > now:
                add("EVENT_START", due - event_lead, f"📅 Starting soon: {task.title} at {clock(due, self.tz)}")
            elif not event_lead and due > now - timedelta(minutes=1):
                add("EVENT_START", max(due, now), f"📅 Now: {task.title} ({clock(due, self.tz)}–{clock(end, self.tz)})")
        elif task.kind == "TASK" and due is not None and due - lead > now:
            add("DEADLINE", due - lead, f"⏳ Due soon: {task.title} ({fmt_dt(due, self.tz)})")

        # calendar slot for events
        existing = await self._blocks(task.id, statuses=("PLANNED",))
        event_block = next((b for b in existing if b.kind == "EVENT"), None)
        if task.kind == "EVENT" and due is not None:
            end = due + timedelta(minutes=task.estimate_min)
            if event_block is None:
                self.s.add(TimeBlock(task_id=task.id, title=task.title, kind="EVENT", start_at=due, end_at=end,
                                     status="PLANNED", created_at=now, updated_at=now))
            else:
                event_block.title, event_block.start_at, event_block.end_at, event_block.updated_at = task.title, due, end, now
        elif event_block is not None:
            event_block.status, event_block.updated_at = "CANCELLED", now
        await self.s.flush()

    @staticmethod
    def _choose(value: Any, allowed: tuple[str, ...], label: str, default: str) -> str:
        if value is None or str(value).strip() == "":
            return default
        v = str(value).strip().upper()
        if v not in allowed:
            raise TaskError(f"{label} must be one of {', '.join(allowed)}.")
        return v

    @staticmethod
    def _recurrence(value: Any) -> str | None:
        if value is None or str(value).strip().upper() in {"", "NONE"}:
            return None
        v = str(value).strip().upper()
        if v not in RECURRENCES:
            raise TaskError(f"Repeat must be one of {', '.join(RECURRENCES)}.")
        return v

    @staticmethod
    def _minutes(value: Any, maximum: int) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise TaskError("Minutes must be a whole number.") from None
        if not 1 <= n <= maximum:
            raise TaskError(f"Minutes must be between 1 and {maximum}.")
        return n
