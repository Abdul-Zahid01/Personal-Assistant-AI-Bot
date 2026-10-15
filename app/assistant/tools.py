"""The assistant's tools. The LLM decides *which* tool to call; these functions do the real work.

Each handler returns a dict with a human-readable "message" (the rule-based fallback echoes it; an LLM
paraphrases it). Problems the user can fix raise ToolError/TaskError/PrefError/ValueError and are reported back
to the model as {"ok": false, "error": ...} so it can ask a better question.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant.llm.base import ToolSpec
from app.domain.timeutil import fmt_dt, parse_dt, resolve_day
from app.services import format as fmt
from app.services import prefs as prefs_svc
from app.services.planning import PlanningService
from app.services.review import run_review
from app.services.routines import RoutineService, describe_days
from app.services.tasks import TaskService

if TYPE_CHECKING:  # pragma: no cover
    from app.container import Container


class ToolError(ValueError):
    pass


@dataclass
class ToolContext:
    session: AsyncSession
    container: "Container"
    prefs: dict[str, Any]
    now: datetime
    tz: ZoneInfo
    tasks: TaskService
    planning: PlanningService
    routines: RoutineService


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------- helpers
def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        spec["required"] = required
    return spec


S = lambda desc, **kw: {"type": "string", "description": desc, **kw}  # noqa: E731
I = lambda desc: {"type": "integer", "description": desc}  # noqa: E731
B = lambda desc: {"type": "boolean", "description": desc}  # noqa: E731

DUE = S("Date-time in ISO 8601, e.g. 2026-10-15T19:00:00. Without an offset it is read in the user's timezone. "
        "Resolve words like 'tomorrow' or 'friday' using the current time given in the system prompt.")
REF = S("Which task: its id, part of its title, or 'latest' for the most recently touched one.")


def _int(args: dict[str, Any], key: str) -> int | None:
    v = args.get(key)
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        raise ToolError(f"{key} must be a number.") from None


def _due(ctx: ToolContext, value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    dt = parse_dt(value, ctx.tz)
    if dt is None:
        raise ToolError(f"I couldn't read the date/time '{value}'. Use ISO format like 2026-10-15T19:00:00.")
    return dt


async def _reload_prefs(ctx: ToolContext) -> None:
    """After a settings change, later tool calls in the same turn (e.g. plan_day) must see the new values."""
    ctx.prefs = await prefs_svc.load_prefs(ctx.session, ctx.container.settings)
    ctx.tz = ZoneInfo(ctx.prefs["timezone"])
    ctx.container.tz = ctx.tz
    ctx.tasks = TaskService(ctx.session, ctx.prefs)
    ctx.planning = PlanningService(ctx.session, ctx.prefs, ctx.container.calendar)
    ctx.routines = RoutineService(ctx.session, ctx.prefs)


def _ok(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "message": message, **extra}


# ---------------------------------------------------------------------- handlers
async def create_task(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    due = _due(ctx, a.get("due_at"))
    task = await ctx.tasks.create(
        now=ctx.now, title=a.get("title") or "", kind=a.get("kind") or "TASK", due_at=due,
        estimate_min=_int(a, "estimate_min"), priority=a.get("priority") or "P3", notes=a.get("notes"),
        category=a.get("category"), recurrence=a.get("recurrence"),
    )
    msg = f"Created {task.kind.lower()}: {task.title}"
    if task.kind == "TASK":
        msg += f" [{task.priority}, {task.estimate_min} min]"
    if due:
        msg += f", {'at' if task.kind != 'TASK' else 'due'} {fmt_dt(due, ctx.tz)}"
    if task.recurrence:
        msg += f", repeats {task.recurrence.lower()}"
    msg += "."
    if due and due < ctx.now:
        msg += " Note: that time has already passed."
    if task.kind == "REMINDER" and due is None:
        every = int(ctx.prefs["nag_interval_min"])
        msg += f" I'll nudge you every {f'{every // 60}h' if every % 60 == 0 else f'{every} min'} until you mark it done."
    if task.kind == "EVENT" and due:
        clash = await ctx.tasks.event_conflicts(due, due + timedelta(minutes=task.estimate_min), exclude_task_id=task.id)
        if clash:
            alt = max(b.end_at for b in clash)
            msg += f" ⚠️ It overlaps with “{clash[0].title}”. A free start would be {fmt_dt(alt, ctx.tz)}."
    return _ok(msg, task_id=task.id)


async def list_tasks(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    status = (a.get("status") or "open").lower()
    if status not in {"open", "done", "all"}:
        raise ToolError("status must be open, done or all.")
    tasks = await ctx.tasks.list_tasks(status=status, kind=a.get("kind"), limit=_int(a, "limit") or 30)
    body = fmt.task_list(tasks, ctx.tz, empty="You have no open tasks. 🎉" if status == "open" else "Nothing found.")
    return _ok(f"{len(tasks)} {status} item(s):\n{body}" if tasks else body, count=len(tasks))


async def update_task(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    task = await ctx.tasks.resolve(a.get("task_ref"))
    changes: dict[str, Any] = {}
    for key in ("title", "notes", "priority", "category", "recurrence", "status", "kind"):
        if key in a and a[key] is not None:
            changes[key] = a[key]
    if "due_at" in a:
        changes["due_at"] = _due(ctx, a["due_at"])  # explicit null/"" clears the deadline
    if _int(a, "estimate_min") is not None:
        changes["estimate_min"] = _int(a, "estimate_min")
    if not changes:
        raise ToolError("Nothing to change. Tell me what to update (title, time, priority, estimate...).")
    await ctx.tasks.update(task, ctx.now, changes)
    return _ok(f"Updated: {fmt.task_line(task, ctx.tz)}", task_id=task.id)


async def complete_task(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    task = await ctx.tasks.resolve(a.get("task_ref"))
    result = await ctx.tasks.complete(task, ctx.now, actual_min=_int(a, "actual_min"))
    msg = f"✅ Done: {task.title}."
    if result.next_task:
        msg += f" Next one is scheduled for {fmt_dt(result.next_task.due_at, ctx.tz)}."
    return _ok(msg, task_id=task.id)


async def delete_task(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    task = await ctx.tasks.resolve(a.get("task_ref"), include_done=True)
    title = task.title
    await ctx.tasks.delete(task, ctx.now)
    return _ok(f"Deleted: {title}.")


async def snooze_task(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    task = await ctx.tasks.resolve(a.get("task_ref"))
    await ctx.tasks.snooze(task, ctx.now, _int(a, "minutes") or 30)
    return _ok(f"Snoozed “{task.title}” to {fmt_dt(task.due_at, ctx.tz)}.", task_id=task.id)


async def start_task(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    task = await ctx.tasks.resolve(a.get("task_ref"))
    await ctx.tasks.update(task, ctx.now, {"status": "IN_PROGRESS"})
    return _ok(f"▶️ Started: {task.title}. Tell me when it's done.", task_id=task.id)


async def plan_day(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    day = resolve_day(a.get("date"), ctx.now, ctx.tz)
    if day < ctx.now.astimezone(ctx.tz).date():
        raise ToolError("That day has already passed.")
    commit = bool(a.get("commit")) and str(a.get("commit")).lower() != "false"
    result = await (ctx.planning.commit if commit else ctx.planning.build)(day, ctx.now)
    out = _ok(fmt.plan_text(result, ctx.tz, committed=commit), committed=commit, blocks=len(result.blocks),
              deferred=[d.title for d in result.deferred])
    if not commit and result.blocks:
        out["buttons"] = fmt.plan_buttons(day)
    return out


async def get_schedule(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    day = resolve_day(a.get("date"), ctx.now, ctx.tz)
    sched = await ctx.planning.day_schedule(day, ctx.now)
    return _ok(fmt.schedule_text(sched, ctx.tz))


async def set_preference(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    key = str(a.get("key") or "")
    value = await prefs_svc.set_pref(ctx.session, key, a.get("value"))
    await _reload_prefs(ctx)
    return _ok(f"Saved: {key} = {value}. (Plans you've already saved are unchanged; ask me to re-plan.)")


async def add_blocked_window(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    window = prefs_svc.normalize_window({"label": a.get("label"), "start": a.get("start"), "end": a.get("end")})
    windows = [w for w in ctx.prefs.get("blocked_windows", []) if w["label"].lower() != window["label"].lower()]
    windows.append(window)
    await prefs_svc.set_pref(ctx.session, "blocked_windows", windows)
    await _reload_prefs(ctx)
    return _ok(f"I'll keep {window['start']}–{window['end']} free for “{window['label']}” when planning.")


async def remove_blocked_window(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    label = str(a.get("label") or "").lower()
    windows = ctx.prefs.get("blocked_windows", [])
    kept = [w for w in windows if w["label"].lower() != label]
    if len(kept) == len(windows):
        raise ToolError(f"No blocked window called '{a.get('label')}'.")
    await prefs_svc.set_pref(ctx.session, "blocked_windows", kept)
    await _reload_prefs(ctx)
    return _ok(f"Removed the “{a.get('label')}” window.")


async def end_of_day_review(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    day = ctx.now.astimezone(ctx.tz).date()
    review = await run_review(ctx.session, ctx.prefs, ctx.planning, day, ctx.now)
    out = _ok(review.text)
    if review.tomorrow and review.tomorrow.blocks:
        out["buttons"] = fmt.plan_buttons(day + timedelta(days=1))
    return out


async def create_routine(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    r = await ctx.routines.create(
        ctx.now, title=a.get("title") or "", time_of_day=a.get("time") or "", duration_min=_int(a, "duration_min") or 30,
        days=a.get("days"), kind=a.get("kind") or "EVENT", priority=a.get("priority") or "P3", category=a.get("category"))
    return _ok(f"Routine added: {r.title} at {r.time_of_day} for {r.duration_min} min, {describe_days(r.days)}. "
               f"It will appear on your day map and I'll notify you when it's time.", routine_id=r.id)


async def list_routines(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    routines = await ctx.routines.list()
    if not routines:
        return _ok("You have no routines yet. Try: gym every weekday at 18:00 for 60 minutes.")
    lines = [f"{'✅' if r.active else '⏸'} {r.time_of_day} {r.title} ({r.duration_min} min, {describe_days(r.days)})" for r in routines]
    return _ok("Your routines:\n" + "\n".join(lines), count=len(routines))


async def update_routine(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    r = await ctx.routines.find(a.get("routine_ref") or "")
    changes: dict[str, Any] = {}
    for src, dst in (("title", "title"), ("time", "time_of_day"), ("days", "days"), ("kind", "kind"),
                     ("priority", "priority"), ("category", "category"), ("active", "active")):
        if a.get(src) is not None:
            changes[dst] = a[src]
    if _int(a, "duration_min") is not None:
        changes["duration_min"] = _int(a, "duration_min")
    if not changes:
        raise ToolError("Nothing to change on that routine.")
    await ctx.routines.update(r, ctx.now, changes)
    state = "" if r.active else " (paused)"
    return _ok(f"Updated routine: {r.title} at {r.time_of_day}, {r.duration_min} min, {describe_days(r.days)}{state}.")


async def delete_routine(ctx: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    r = await ctx.routines.find(a.get("routine_ref") or "")
    title = r.title
    await ctx.routines.delete(r, ctx.now)
    return _ok(f"Deleted the routine “{title}” and its upcoming entries.")


# ---------------------------------------------------------------------- registry
TOOLS: dict[str, tuple[ToolSpec, Handler]] = {}


def _register(name: str, description: str, properties: dict[str, Any], required: list[str] | None, handler: Handler) -> None:
    TOOLS[name] = (ToolSpec(name, description, _obj(properties, required)), handler)


_register("create_task",
          "Create a task (work to do, may have a deadline), a reminder (a nudge at a time, or repeating until done) "
          "or an event (fixed time on the calendar, e.g. meeting, gym).",
          {"title": S("Short title, without time words."),
           "kind": S("TASK, REMINDER or EVENT. 'remind me' => REMINDER; fixed-time activity => EVENT; else TASK.",
                     enum=["TASK", "REMINDER", "EVENT"]),
           "due_at": DUE, "estimate_min": I("Expected minutes of work (events: duration). Default 30 (events 60)."),
           "priority": S("P1 urgent, P2 high, P3 normal, P4 low.", enum=["P1", "P2", "P3", "P4"]),
           "category": S("Optional kind of work (e.g. writing, email, coding) to learn how long it really takes."),
           "recurrence": S("Repeat rule; needs due_at.", enum=["DAILY", "WEEKDAYS", "WEEKLY", "MONTHLY"]),
           "notes": S("Optional details.")},
          ["title"], create_task)
_register("list_tasks", "List the user's tasks, reminders and events.",
          {"status": S("open (default), done or all.", enum=["open", "done", "all"]),
           "kind": S("Optional filter.", enum=["TASK", "REMINDER", "EVENT"]), "limit": I("Max items.")},
          None, list_tasks)
_register("update_task", "Change a task: title, time, priority, estimate, category, repeat, notes. Use due_at=null to clear the time.",
          {"task_ref": REF, "title": S("New title."), "due_at": DUE, "estimate_min": I("New estimate in minutes."),
           "priority": S("New priority.", enum=["P1", "P2", "P3", "P4"]), "category": S("Category."),
           "recurrence": S("Repeat rule, or NONE to stop repeating.", enum=["DAILY", "WEEKDAYS", "WEEKLY", "MONTHLY", "NONE"]),
           "notes": S("Notes."), "kind": S("Change the kind.", enum=["TASK", "REMINDER", "EVENT"])},
          ["task_ref"], update_task)
_register("complete_task", "Mark a task as done. Repeating tasks automatically get their next occurrence.",
          {"task_ref": REF, "actual_min": I("How many minutes it really took, if the user says.")}, None, complete_task)
_register("delete_task", "Permanently delete a task/reminder/event.", {"task_ref": REF}, ["task_ref"], delete_task)
_register("snooze_task", "Push a task or reminder later by some minutes.",
          {"task_ref": REF, "minutes": I("Minutes to push by. Default 30.")}, None, snooze_task)
_register("start_task", "Mark a task as in progress.", {"task_ref": REF}, None, start_task)
_register("plan_day",
          "Build the user's schedule for a day: ranks open tasks and fits them into free time around events and rules. "
          "commit=false returns a DRAFT to show the user; commit=true saves it and sets up reminders. "
          "Only commit after the user approves, or if they clearly ask to plan/schedule their day now.",
          {"date": S("today, tomorrow or YYYY-MM-DD. Default today."), "commit": B("Save the plan. Default false.")},
          None, plan_day)
_register("get_schedule", "Show what is already scheduled on a day (saved blocks, events, reminders, overdue).",
          {"date": S("today, tomorrow or YYYY-MM-DD. Default today.")}, None, get_schedule)
_register("set_preference",
          "Change a setting: timezone, work_start, work_end, focus_start, focus_end (HH:MM), max_block_min, buffer_min, "
          "morning_brief, evening_review (HH:MM), nag_interval_min, auto_plan, proactive.",
          {"key": S("Setting name."), "value": S("New value, e.g. 09:30, 90, true, Asia/Karachi.")}, ["key", "value"], set_preference)
_register("add_blocked_window", "Keep a daily time window free from planning (lunch, gym, prayer, family time...).",
          {"label": S("Name, e.g. lunch."), "start": S("HH:MM"), "end": S("HH:MM")}, ["label", "start", "end"], add_blocked_window)
_register("remove_blocked_window", "Remove a previously blocked window.", {"label": S("Its name.")}, ["label"], remove_blocked_window)
_register("end_of_day_review",
          "Review today: what got done, what carries over, and a draft plan for tomorrow.", {}, None, end_of_day_review)

_register("create_routine",
          "Create a ROUTINE: something done on a fixed schedule every day or on certain weekdays (gym, breakfast, lunch, "
          "dinner, calling mom, medication). A copy is created automatically for each day. Prefer this over a repeating "
          "task for daily habits.",
          {"title": S("Name, e.g. Gym."), "time": S("Start time HH:MM (24h), e.g. 18:00."),
           "duration_min": I("How long it takes. Default 30."),
           "days": S("daily (default), weekdays, weekends, or days like 'mon,wed,fri'."),
           "kind": S("EVENT blocks time on the calendar (default); REMINDER is only a nudge.", enum=["EVENT", "REMINDER"]),
           "priority": S("Priority.", enum=["P1", "P2", "P3", "P4"]), "category": S("Optional category.")},
          ["title", "time"], create_routine)
_register("list_routines", "List the user's routines.", {}, None, list_routines)
_register("update_routine", "Change a routine: time, duration, days, name, or pause it (active=false) / resume it.",
          {"routine_ref": S("Which routine: its name."), "title": S("New name."), "time": S("New time HH:MM."),
           "duration_min": I("New duration."), "days": S("New days."), "kind": S("EVENT or REMINDER.", enum=["EVENT", "REMINDER"]),
           "priority": S("Priority.", enum=["P1", "P2", "P3", "P4"]), "category": S("Category."),
           "active": B("false to pause, true to resume.")},
          ["routine_ref"], update_routine)
_register("delete_routine", "Delete a routine and its upcoming entries.", {"routine_ref": S("Which routine: its name.")},
          ["routine_ref"], delete_routine)


def tool_specs() -> list[ToolSpec]:
    return [spec for spec, _ in TOOLS.values()]
