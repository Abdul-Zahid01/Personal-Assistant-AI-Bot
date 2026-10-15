"""Turns plans and task lists into friendly text. Deterministic on purpose: proactive messages never need an LLM."""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from app.db.models import Task, TimeBlock
from app.domain.planner import PlanResult
from app.domain.timeutil import clock, fmt_day, fmt_dt, fmt_minutes
from app.messaging import Button
from app.services.planning import DaySchedule
from app.services.tasks import short_id

PRIORITY_ICON = {"P1": "🔴", "P2": "🟠", "P3": "🟡", "P4": "⚪"}


def task_line(t: Task, tz: ZoneInfo) -> str:
    when = f" — {fmt_dt(t.due_at, tz)}" if t.due_at else ""
    kind = {"REMINDER": "⏰", "EVENT": "📅"}.get(t.kind, PRIORITY_ICON.get(t.priority, "•"))
    extra = f" · {fmt_minutes(t.estimate_min)}" if t.kind == "TASK" else ""
    rep = f" · repeats {t.recurrence.lower()}" if t.recurrence else ""
    flag = " ▶" if t.status == "IN_PROGRESS" else ""
    return f"{kind} {t.title}{flag} [{t.priority}{extra}]{when}{rep} ·{short_id(t.id)}"


def task_list(tasks: list[Task], tz: ZoneInfo, empty: str = "Nothing here.") -> str:
    return "\n".join(task_line(t, tz) for t in tasks) if tasks else empty


def block_line(b: TimeBlock, tz: ZoneInfo) -> str:
    icon = "📅" if b.kind == "EVENT" else ("✅" if b.status == "DONE" else "🕘")
    return f"{icon} {clock(b.start_at, tz)}–{clock(b.end_at, tz)}  {b.title}"


def schedule_text(s: DaySchedule, tz: ZoneInfo, header: str | None = None) -> str:
    lines = [header or f"📆 {fmt_day(s.day)}"]
    if s.blocks:
        lines += [block_line(b, tz) for b in s.blocks]
    if s.reminders:
        lines.append("Reminders: " + "; ".join(
            f"{t.title}" + (f" ({clock(t.due_at, tz)})" if t.due_at else "") for t in s.reminders))
    if s.unscheduled:
        lines.append("Due today (not yet scheduled): " + "; ".join(
            f"{t.title} ({clock(t.due_at, tz)})" for t in s.unscheduled))
    if s.overdue:
        lines.append("⚠️ Overdue: " + "; ".join(t.title for t in s.overdue[:5]))
    if len(lines) == 1:
        lines.append("Nothing scheduled. Tell me what you need to do and I'll plan it.")
    return "\n".join(lines)


def plan_text(r: PlanResult, tz: ZoneInfo, *, committed: bool, header: str | None = None) -> str:
    head = header or ("✅ Plan saved for " if committed else "📝 Draft plan for ") + fmt_day(r.day)
    lines = [head]
    if r.blocks:
        for b in r.blocks:
            lines.append(f"🕘 {clock(b.start, tz)}–{clock(b.end, tz)}  {b.title}  ({b.reason})")
        lines.append(f"{fmt_minutes(r.planned_minutes)} planned, about {fmt_minutes(r.free_minutes_left)} free.")
    else:
        lines.append("No work blocks to schedule.")
    if r.deferred:
        lines.append("Couldn't fit: " + "; ".join(f"{d.title} ({d.reason})" for d in r.deferred))
    lines += [f"ℹ️ {w}" for w in r.warnings]
    return "\n".join(lines)


def task_buttons(task_id: str) -> list[Button]:
    return [
        Button("✅ Done", f"done:{task_id}"),
        Button("⏰ +30m", f"snz:{task_id}:30"),
        Button("➡️ Tomorrow", f"tmr:{task_id}"),
    ]


def plan_buttons(day: date) -> list[Button]:
    return [Button("✅ Approve plan", f"plan:{day.isoformat()}")]
