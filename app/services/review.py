"""End-of-day review: what got done, what carries over, and a draft plan for tomorrow."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DailyReview, Task, TimeBlock
from app.domain.planner import PlanResult
from app.domain.timeutil import day_bounds, fmt_day
from app.domain.types import OPEN_STATUSES
from app.services import format as fmt
from app.services.planning import PlanningService


@dataclass
class ReviewResult:
    day: date
    completed: list[Task] = field(default_factory=list)
    carried_over: list[Task] = field(default_factory=list)
    overdue: list[Task] = field(default_factory=list)
    estimate_ratio: float | None = None
    tomorrow: PlanResult | None = None
    text: str = ""


async def run_review(session: AsyncSession, prefs: dict[str, Any], planning: PlanningService, day: date, now: datetime) -> ReviewResult:
    tz = ZoneInfo(prefs["timezone"])
    start, end = day_bounds(day, tz)

    # Close out today's work blocks: finished tasks -> DONE, everything else -> SKIPPED.
    past_blocks = (await session.execute(
        select(TimeBlock).where(TimeBlock.kind == "WORK", TimeBlock.status == "PLANNED",
                                TimeBlock.start_at < end, TimeBlock.start_at >= start, TimeBlock.end_at <= now)
    )).scalars().all()
    skipped_task_ids: set[str] = set()
    for b in past_blocks:
        task = await session.get(Task, b.task_id) if b.task_id else None
        if task is not None and task.status == "DONE":
            b.status = "DONE"
        else:
            b.status = "SKIPPED"
            if task is not None:
                skipped_task_ids.add(task.id)
        b.updated_at = now

    completed = list((await session.execute(
        select(Task).where(Task.status == "DONE", Task.completed_at >= start, Task.completed_at < end)
        .order_by(Task.completed_at)
    )).scalars().all())

    open_tasks = (await session.execute(select(Task).where(Task.status.in_(OPEN_STATUSES), Task.kind == "TASK"))).scalars().all()
    carried = [t for t in open_tasks if t.id in skipped_task_ids or t.status == "IN_PROGRESS" or (t.due_at and t.due_at < end)]
    carried.sort(key=lambda t: (t.priority, t.due_at is None, t.due_at or t.created_at))
    overdue = [t for t in carried if t.due_at and t.due_at < now]

    timed = [t for t in completed if t.actual_min > 0 and t.estimate_min > 0]
    ratio = round(sum(t.actual_min for t in timed) / sum(t.estimate_min for t in timed), 2) if timed else None

    tomorrow = day + timedelta(days=1)
    plan = await planning.build(tomorrow, now)

    lines = [f"🌙 Day review — {fmt_day(day)}"]
    lines.append(f"Finished {len(completed)}" + (": " + ", ".join(t.title for t in completed[:8]) if completed else "."))
    if carried:
        lines.append(f"Carrying over {len(carried)}: " + ", ".join(f"{t.title} ({t.priority})" for t in carried[:8]))
    else:
        lines.append("Nothing carried over. 🎉")
    if overdue:
        lines.append("⚠️ Overdue: " + ", ".join(t.title for t in overdue[:5]))
    if ratio is not None:
        lines.append(f"⏱ You took about {ratio}× your estimates today.")
    lines.append("")
    lines.append(fmt.plan_text(plan, tz, committed=False, header=f"📝 Draft plan for {fmt_day(tomorrow)}"))
    text = "\n".join(lines)

    row = await session.get(DailyReview, day.isoformat())
    if row is None:
        row = DailyReview(day=day.isoformat(), created_at=now)
        session.add(row)
    row.completed, row.carried_over, row.estimate_ratio, row.summary = len(completed), len(carried), ratio, text
    await session.flush()

    return ReviewResult(day, completed, carried, overdue, ratio, plan, text)
