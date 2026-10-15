from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text

from app.api.deps import get_container, get_ctx, require_api_key
from app.api.schemas import (ActionIn, ChatIn, CompleteIn, PlanIn, ReplyOut, RoutineIn, RoutinePatch, SnoozeIn, TaskIn,
                             TaskPatch, routine_out, task_out)
from app.assistant.tools import ToolContext
from app.container import Container
from app.db.models import Message, Task
from app.domain.planner import PlanResult
from app.domain.timeutil import clock, fmt_day, parse_dt, resolve_day
from app.services import format as fmt
from app.services import prefs as prefs_svc
from app.services.review import run_review
from app.services.tasks import TaskError

public = APIRouter()
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


def _plan_out(r: PlanResult, ctx: ToolContext, committed: bool) -> dict[str, Any]:
    return {
        "day": r.day.isoformat(), "committed": committed, "text": fmt.plan_text(r, ctx.tz, committed=committed),
        "blocks": [{"task_id": b.task_id, "title": b.title, "start": b.start.isoformat(), "end": b.end.isoformat(),
                    "label": f"{clock(b.start, ctx.tz)}–{clock(b.end, ctx.tz)}", "priority": b.priority, "reason": b.reason}
                   for b in r.blocks],
        "deferred": [{"task_id": d.task_id, "title": d.title, "reason": d.reason} for d in r.deferred],
        "warnings": r.warnings, "planned_minutes": r.planned_minutes, "free_minutes_left": r.free_minutes_left,
    }


async def _task_or_404(ctx: ToolContext, task_id: str) -> Task:
    task = await ctx.tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "Task not found.")
    return task


# ---------------------------------------------------------------------- health
@public.get("/health")
async def health(container: Container = Depends(get_container)) -> dict[str, Any]:
    db_ok = True
    try:
        async with container.session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded", "database": db_ok,
        "llm": [getattr(p, "name", "?") for p in getattr(container.llm, "providers", [container.llm])],
        "telegram": bool(container.telegram and container.telegram.configured),
        "google_calendar": container.calendar is not None,
    }


# ---------------------------------------------------------------------- chat
@router.post("/chat", response_model=ReplyOut)
async def chat(body: ChatIn, container: Container = Depends(get_container)) -> ReplyOut:
    return ReplyOut.of(await container.assistant.handle(body.message, channel="web"))


@router.post("/actions", response_model=ReplyOut)
async def action(body: ActionIn, container: Container = Depends(get_container)) -> ReplyOut:
    return ReplyOut.of(await container.actions.run(body.data, channel="web"))


@router.get("/messages")
async def messages(after_id: int = 0, limit: int = Query(50, ge=1, le=200), ctx: ToolContext = Depends(get_ctx)) -> list[dict[str, Any]]:
    q = select(Message)
    if after_id > 0:
        rows = (await ctx.session.execute(q.where(Message.id > after_id).order_by(Message.id).limit(limit))).scalars().all()
    else:  # first load: the most recent `limit` messages, oldest first
        rows = list(reversed((await ctx.session.execute(q.order_by(Message.id.desc()).limit(limit))).scalars().all()))
    return [{"id": m.id, "role": m.role, "content": m.content, "channel": m.channel, "buttons": m.buttons or [],
             "created_at": m.created_at.isoformat()} for m in rows]


# ---------------------------------------------------------------------- tasks
@router.get("/tasks")
async def list_tasks(status: str = "open", kind: str | None = None, ctx: ToolContext = Depends(get_ctx)) -> list[dict[str, Any]]:
    if status not in {"open", "done", "all"}:
        raise HTTPException(400, "status must be open, done or all.")
    return [task_out(t) for t in await ctx.tasks.list_tasks(status=status, kind=kind)]


def _due(ctx: ToolContext, value: str | None):
    dt = parse_dt(value, ctx.tz) if value else None
    if value and dt is None:
        raise HTTPException(400, f"Can't read date/time '{value}'.")
    return dt


@router.post("/tasks", status_code=201)
async def create_task(body: TaskIn, ctx: ToolContext = Depends(get_ctx), container: Container = Depends(get_container)) -> dict[str, Any]:
    try:
        task = await ctx.tasks.create(
            now=ctx.now, title=body.title, kind=body.kind, due_at=_due(ctx, body.due_at), estimate_min=body.estimate_min,
            priority=body.priority, notes=body.notes, category=body.category, recurrence=body.recurrence)
        await ctx.session.commit()
    except TaskError as exc:
        raise HTTPException(400, str(exc)) from exc
    container.schedule_calendar_sync()
    return task_out(task)


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, ctx: ToolContext = Depends(get_ctx)) -> dict[str, Any]:
    return task_out(await _task_or_404(ctx, task_id))


@router.patch("/tasks/{task_id}")
async def update_task(task_id: str, body: TaskPatch, ctx: ToolContext = Depends(get_ctx),
                      container: Container = Depends(get_container)) -> dict[str, Any]:
    task = await _task_or_404(ctx, task_id)
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if "due_at" in changes:
        changes["due_at"] = _due(ctx, changes["due_at"])
    try:
        await ctx.tasks.update(task, ctx.now, changes)
        await ctx.session.commit()
    except TaskError as exc:
        raise HTTPException(400, str(exc)) from exc
    container.schedule_calendar_sync()
    return task_out(task)


@router.post("/tasks/{task_id}/complete")
async def complete_task(task_id: str, body: CompleteIn | None = None, ctx: ToolContext = Depends(get_ctx),
                        container: Container = Depends(get_container)) -> dict[str, Any]:
    task = await _task_or_404(ctx, task_id)
    try:
        result = await ctx.tasks.complete(task, ctx.now, actual_min=body.actual_min if body else None)
        await ctx.session.commit()
    except TaskError as exc:
        raise HTTPException(400, str(exc)) from exc
    container.schedule_calendar_sync()
    return {"task": task_out(result.task), "next_task": task_out(result.next_task) if result.next_task else None}


@router.post("/tasks/{task_id}/snooze")
async def snooze_task(task_id: str, body: SnoozeIn, ctx: ToolContext = Depends(get_ctx)) -> dict[str, Any]:
    task = await _task_or_404(ctx, task_id)
    try:
        await ctx.tasks.snooze(task, ctx.now, body.minutes)
        await ctx.session.commit()
    except TaskError as exc:
        raise HTTPException(400, str(exc)) from exc
    return task_out(task)


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, ctx: ToolContext = Depends(get_ctx), container: Container = Depends(get_container)) -> dict[str, str]:
    task = await _task_or_404(ctx, task_id)
    await ctx.tasks.delete(task, ctx.now)
    await ctx.session.commit()
    container.schedule_calendar_sync()
    return {"status": "deleted", "id": task_id}


# ---------------------------------------------------------------------- planning
@router.get("/plan")
async def preview_plan(date: str = "today", ctx: ToolContext = Depends(get_ctx)) -> dict[str, Any]:
    """Draft plan for a day; nothing is saved."""
    try:
        day = resolve_day(date, ctx.now, ctx.tz)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _plan_out(await ctx.planning.build(day, ctx.now), ctx, committed=False)


@router.post("/plan")
async def save_plan(body: PlanIn, ctx: ToolContext = Depends(get_ctx), container: Container = Depends(get_container)) -> dict[str, Any]:
    try:
        day = resolve_day(body.date, ctx.now, ctx.tz)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if day < ctx.now.astimezone(ctx.tz).date():
        raise HTTPException(400, "That day has already passed.")
    if not body.commit:
        return _plan_out(await ctx.planning.build(day, ctx.now), ctx, committed=False)
    result = await ctx.planning.commit(day, ctx.now)
    await ctx.session.commit()
    container.schedule_calendar_sync()
    return _plan_out(result, ctx, committed=True)


@router.get("/schedule")
async def schedule(date: str = "today", ctx: ToolContext = Depends(get_ctx)) -> dict[str, Any]:
    """What is saved for a day: work blocks, events, reminders, unscheduled and overdue items."""
    try:
        day = resolve_day(date, ctx.now, ctx.tz)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    s = await ctx.planning.day_schedule(day, ctx.now)
    task_ids = {b.task_id for b in s.blocks if b.task_id}
    tasks = {t.id: t for t in (await ctx.session.execute(select(Task).where(Task.id.in_(task_ids)))).scalars()} if task_ids else {}
    in_progress = (await ctx.session.execute(select(Task).where(Task.status == "IN_PROGRESS"))).scalars().all()

    def block_out(b) -> dict[str, Any]:
        t = tasks.get(b.task_id)
        return {
            "id": b.id, "task_id": b.task_id, "title": b.title, "kind": b.kind, "status": b.status,
            "start": b.start_at.isoformat(), "end": b.end_at.isoformat(), "reason": b.reason,
            "label": f"{clock(b.start_at, ctx.tz)}–{clock(b.end_at, ctx.tz)}",
            "task_status": t.status if t else None, "priority": t.priority if t else None,
            "routine": bool(t and t.routine_id), "started_at": t.started_at.isoformat() if t and t.started_at else None,
        }

    return {
        "day": day.isoformat(), "title": fmt_day(day), "timezone": ctx.tz.key, "text": fmt.schedule_text(s, ctx.tz),
        "now": ctx.now.isoformat(),
        "window": {"start": ctx.prefs["work_start"], "end": ctx.prefs["work_end"]},
        "blocks": [block_out(b) for b in s.blocks],
        "reminders": [task_out(t) for t in s.reminders], "unscheduled": [task_out(t) for t in s.unscheduled],
        "overdue": [task_out(t) for t in s.overdue], "in_progress": [task_out(t) for t in in_progress],
    }


@router.post("/review")
async def review(ctx: ToolContext = Depends(get_ctx), container: Container = Depends(get_container)) -> dict[str, Any]:
    day = ctx.now.astimezone(ctx.tz).date()
    result = await run_review(ctx.session, ctx.prefs, ctx.planning, day, ctx.now)
    await ctx.session.commit()
    container.schedule_calendar_sync()
    return {"text": result.text, "completed": len(result.completed), "carried_over": len(result.carried_over),
            "tomorrow": _plan_out(result.tomorrow, ctx, committed=False) if result.tomorrow else None}


# ---------------------------------------------------------------------- preferences
@router.get("/preferences")
async def get_preferences(ctx: ToolContext = Depends(get_ctx)) -> dict[str, Any]:
    return {k: v for k, v in ctx.prefs.items() if k in prefs_svc.PUBLIC_KEYS}


@router.put("/preferences")
async def put_preferences(body: dict[str, Any], ctx: ToolContext = Depends(get_ctx)) -> dict[str, Any]:
    try:
        for key, value in body.items():  # validate everything first, so a bad value changes nothing
            prefs_svc.normalize(key, value)
        for key, value in body.items():
            await prefs_svc.set_pref(ctx.session, key, value)
        await ctx.session.commit()
    except ValueError as exc:
        await ctx.session.rollback()
        raise HTTPException(400, str(exc)) from exc
    fresh = await prefs_svc.load_prefs(ctx.session, ctx.container.settings)
    return {k: v for k, v in fresh.items() if k in prefs_svc.PUBLIC_KEYS}


# ---------------------------------------------------------------------- start / pause
@router.post("/tasks/{task_id}/start")
async def start_task(task_id: str, ctx: ToolContext = Depends(get_ctx)) -> dict[str, Any]:
    task = await _task_or_404(ctx, task_id)
    if task.status == "DONE":
        raise HTTPException(400, "That task is already done.")
    await ctx.tasks.update(task, ctx.now, {"status": "IN_PROGRESS"})
    await ctx.session.commit()
    return task_out(task)


@router.post("/tasks/{task_id}/pause")
async def pause_task(task_id: str, ctx: ToolContext = Depends(get_ctx)) -> dict[str, Any]:
    task = await _task_or_404(ctx, task_id)
    if task.status == "DONE":
        raise HTTPException(400, "That task is already done.")
    await ctx.tasks.update(task, ctx.now, {"status": "TODO"})
    await ctx.session.commit()
    return task_out(task)


# ---------------------------------------------------------------------- routines
async def _routine_or_404(ctx: ToolContext, routine_id: str):
    r = await ctx.routines.get(routine_id)
    if r is None:
        raise HTTPException(404, "Routine not found.")
    return r


@router.get("/routines")
async def list_routines(ctx: ToolContext = Depends(get_ctx)) -> list[dict[str, Any]]:
    return [routine_out(r, await ctx.routines.todays_copy(r, ctx.now)) for r in await ctx.routines.list()]


@router.post("/routines", status_code=201)
async def create_routine(body: RoutineIn, ctx: ToolContext = Depends(get_ctx),
                         container: Container = Depends(get_container)) -> dict[str, Any]:
    try:
        r = await ctx.routines.create(ctx.now, title=body.title, time_of_day=body.time_of_day, duration_min=body.duration_min,
                                      days=body.days, kind=body.kind, priority=body.priority, category=body.category)
        await ctx.session.commit()
    except ValueError as exc:
        await ctx.session.rollback()
        raise HTTPException(400, str(exc)) from exc
    container.schedule_calendar_sync()
    return routine_out(r, await ctx.routines.todays_copy(r, ctx.now))


@router.patch("/routines/{routine_id}")
async def update_routine(routine_id: str, body: RoutinePatch, ctx: ToolContext = Depends(get_ctx),
                         container: Container = Depends(get_container)) -> dict[str, Any]:
    r = await _routine_or_404(ctx, routine_id)
    try:
        await ctx.routines.update(r, ctx.now, body.model_dump(exclude_unset=True))
        await ctx.session.commit()
    except ValueError as exc:
        await ctx.session.rollback()
        raise HTTPException(400, str(exc)) from exc
    container.schedule_calendar_sync()
    return routine_out(r, await ctx.routines.todays_copy(r, ctx.now))


@router.delete("/routines/{routine_id}")
async def delete_routine(routine_id: str, ctx: ToolContext = Depends(get_ctx),
                         container: Container = Depends(get_container)) -> dict[str, str]:
    r = await _routine_or_404(ctx, routine_id)
    await ctx.routines.delete(r, ctx.now)
    await ctx.session.commit()
    container.schedule_calendar_sync()
    return {"status": "deleted", "id": routine_id}
