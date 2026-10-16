"""The heartbeat. Every tick (default 30 s) it: sends due reminders, sends the morning brief / evening review
when their time comes, and syncs the calendar. State lives in the database, so a restart loses nothing.

Single-process by design (personal assistant). Do not run several app workers against one database, or
reminders could be sent twice.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.models import Reminder, Task, TimeBlock
from app.domain.timeutil import day_bounds, fmt_day, parse_hhmm
from app.messaging import Button
from app.services import format as fmt
from app.services.planning import PlanningService
from app.services.prefs import load_prefs, put_internal
from app.services.review import run_review
from app.services.routines import RoutineService

log = logging.getLogger(__name__)

TASK_LINKED = ("REMINDER", "NAG", "DEADLINE", "EVENT_START", "BLOCK_START", "BLOCK_END")
EXPIRES_AFTER = {"DEADLINE": 30, "EVENT_START": 30, "BLOCK_START": 45, "BLOCK_END": 45}  # minutes; stale nudges are noise
MAX_ATTEMPTS = 8
BRIEF_WINDOW = timedelta(hours=4)  # the morning brief is only sent this long after its time


class Scheduler:
    def __init__(self, container):
        self.c = container

    async def run(self) -> None:
        interval = max(5, self.c.settings.SCHEDULER_TICK_SECONDS)
        log.info("Scheduler started (tick every %ss)", interval)
        while True:
            await self.tick()
            await asyncio.sleep(interval)

    async def tick(self, now: datetime | None = None) -> None:
        now = now or self.c.now()
        for step in (self.materialize_routines, self.dispatch_reminders, self.morning_brief, self.evening_review,
                     self._calendar):
            try:
                await step(now)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scheduler step %s failed", step.__name__)  # one failure must not stop the others

    async def materialize_routines(self, now: datetime) -> None:
        """Create today's and tomorrow's copies of your routines (gym, meals...) if they don't exist yet."""
        async with self.c.session_factory() as session:
            prefs = await load_prefs(session, self.c.settings)
            created = await RoutineService(session, prefs).materialize_upcoming(now)
            await session.commit()
        if created:
            self.c.after_change()

    async def _calendar(self, now: datetime) -> None:
        await self.c.sync_calendar()

    # ------------------------------------------------------------------ reminders
    async def dispatch_reminders(self, now: datetime) -> None:
        async with self.c.session_factory() as session:
            prefs = await load_prefs(session, self.c.settings)
            due = (await session.execute(
                select(Reminder).where(Reminder.status == "PENDING", Reminder.fire_at <= now)
                .order_by(Reminder.fire_at).limit(50)
            )).scalars().all()

            for r in due:
                task = await session.get(Task, r.task_id) if r.task_id else None
                block = await session.get(TimeBlock, r.block_id) if r.block_id else None

                if (r.task_id and (task is None or task.status == "DONE")) or (r.block_id and (block is None or block.status != "PLANNED")):
                    r.status = "CANCELLED"  # no longer relevant
                elif not prefs["proactive"] and r.kind in ("BLOCK_START", "BLOCK_END"):
                    r.status = "CANCELLED"
                elif r.kind in EXPIRES_AFTER and now - r.fire_at > timedelta(minutes=EXPIRES_AFTER[r.kind]):
                    r.status = "EXPIRED"  # e.g. the app was off; "start now" an hour late helps nobody
                else:
                    text = r.text
                    if r.kind == "REMINDER" and now - r.fire_at > timedelta(minutes=10):
                        text += " (sent late - the assistant was offline)"
                    ok = await self.c.notifier.send(text, fmt.task_buttons(task.id) if task else None)
                    if ok:
                        r.sent_at = now
                        r.attempts, r.last_error = 0, None
                        if r.repeat_every_min and r.repeats_left > 0:
                            r.fire_at, r.repeats_left = now + timedelta(minutes=r.repeat_every_min), r.repeats_left - 1
                        else:
                            r.status = "SENT"
                    else:
                        r.attempts += 1
                        r.last_error = "delivery failed"
                        if r.attempts >= MAX_ATTEMPTS:
                            r.status = "FAILED"
                        else:
                            r.fire_at = now + timedelta(seconds=min(900, 30 * 2 ** r.attempts))
                await session.commit()  # per reminder: a crash can't re-send the whole batch

    # ------------------------------------------------------------------ daily rhythm
    async def morning_brief(self, now: datetime) -> None:
        async with self.c.session_factory() as session:
            prefs = await load_prefs(session, self.c.settings)
            tz = ZoneInfo(prefs["timezone"])
            local = now.astimezone(tz)
            today = local.date()
            if not prefs["proactive"] or prefs.get("last_brief_date") == today.isoformat():
                return
            brief_at = datetime.combine(today, parse_hhmm(prefs["morning_brief"]), tzinfo=tz)
            latest = min(brief_at + BRIEF_WINDOW, datetime.combine(today, parse_hhmm(prefs["evening_review"]), tzinfo=tz))
            if not (brief_at <= local < latest):  # a "good morning" at 7pm (e.g. app started late) is just noise
                return

            planning = PlanningService(session, prefs, self.c.calendar)
            extra = ""
            if prefs["auto_plan"]:
                start, end = day_bounds(today, tz)
                planned_today = (await session.execute(
                    select(TimeBlock.id).where(TimeBlock.kind == "WORK", TimeBlock.status.in_(("PLANNED", "DONE")),
                                               TimeBlock.start_at >= start, TimeBlock.start_at < end).limit(1)
                )).first()
                has_open = (await session.execute(select(Task.id).where(Task.kind == "TASK", Task.status.in_(("TODO", "IN_PROGRESS"))).limit(1))).first()
                if planned_today is None and has_open is not None:
                    result = await planning.commit(today, now)
                    if result.deferred:
                        extra = "\nCouldn't fit today: " + "; ".join(f"{d.title} ({d.reason})" for d in result.deferred)
                    await session.commit()

            sched = await planning.day_schedule(today, now)
            text = fmt.schedule_text(sched, tz, header=f"☀️ Good morning! Here's {fmt_day(today)}:") + extra
            text += "\nTell me if you want to change anything."
            if await self.c.notifier.send(text, None):
                await put_internal(session, "last_brief_date", today.isoformat())
                await session.commit()
        self.c.after_change()

    async def evening_review(self, now: datetime) -> None:
        async with self.c.session_factory() as session:
            prefs = await load_prefs(session, self.c.settings)
            tz = ZoneInfo(prefs["timezone"])
            local = now.astimezone(tz)
            today = local.date()
            if not prefs["proactive"] or prefs.get("last_review_date") == today.isoformat():
                return
            if local.time() < parse_hhmm(prefs["evening_review"]):
                return

            planning = PlanningService(session, prefs, self.c.calendar)
            review = await run_review(session, prefs, planning, today, now)
            await session.commit()
            buttons: list[Button] = fmt.plan_buttons(today + timedelta(days=1)) if review.tomorrow and review.tomorrow.blocks else []
            if await self.c.notifier.send(review.text, buttons):
                await put_internal(session, "last_review_date", today.isoformat())
                await session.commit()
        self.c.after_change()
