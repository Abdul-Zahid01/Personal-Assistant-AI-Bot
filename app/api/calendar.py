from __future__ import annotations

import time
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import get_container, get_ctx, require_api_key
from app.api.schemas import task_out
from app.assistant.tools import ToolContext
from app.container import Container
from app.db.models import TimeBlock
from app.domain.timeutil import parse_dt
from app.events import sse_stream
from app.security import sign, verify
from app.services.blocks import BlockService
from app.services.calendar_view import calendar_range
from app.services.tasks import TaskError

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])
stream_router = APIRouter()  # EventSource can't send headers: authorised by a short-lived signed token instead

STREAM_TOKEN_TTL = 12 * 3600


class BlockIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    kind: str = "TASK"  # TASK (pinned work time) | EVENT | REMINDER
    start: str
    end: str | None = None
    priority: str = "P3"
    notes: str | None = Field(default=None, max_length=4000)
    category: str | None = None
    remind_before_min: int | None = None


class BlockMove(BaseModel):
    start: str
    end: str | None = None


def _dt(ctx: ToolContext, value: str | None, field: str):
    if value is None:
        return None
    dt = parse_dt(value, ctx.tz)
    if dt is None:
        raise HTTPException(400, f"Can't read {field} '{value}'.")
    return dt


@router.get("/calendar")
async def get_calendar(start: date, end: date, ctx: ToolContext = Depends(get_ctx)) -> dict[str, Any]:
    """Everything to draw for a date range (inclusive), in one call. Max 62 days."""
    try:
        items = await calendar_range(ctx.session, ctx.prefs, start, end)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"timezone": ctx.tz.key, "now": ctx.now.isoformat(), "start": start.isoformat(), "end": end.isoformat(),
            "window": {"start": ctx.prefs["work_start"], "end": ctx.prefs["work_end"]}, "items": items}


@router.post("/blocks", status_code=201)
async def place_item(body: BlockIn, ctx: ToolContext = Depends(get_ctx), container: Container = Depends(get_container)) -> dict[str, Any]:
    """Put something on the calendar at a time (what drag-to-create uses)."""
    start, end = _dt(ctx, body.start, "start"), _dt(ctx, body.end, "end")
    try:
        task, block = await BlockService(ctx.session, ctx.prefs).create_at(
            ctx.now, title=body.title, kind=body.kind, start=start, end=end, priority=body.priority, notes=body.notes,
            category=body.category, remind_before_min=body.remind_before_min)
        await ctx.session.commit()
    except TaskError as exc:
        await ctx.session.rollback()
        raise HTTPException(400, str(exc)) from exc
    container.after_change()
    return {"task": task_out(task), "block_id": block.id if block else None}


async def _block_or_404(ctx: ToolContext, block_id: str) -> TimeBlock:
    block = await ctx.session.get(TimeBlock, block_id)
    if block is None or block.status == "CANCELLED":
        raise HTTPException(404, "That calendar item no longer exists.")
    return block


@router.patch("/blocks/{block_id}")
async def move_block(block_id: str, body: BlockMove, ctx: ToolContext = Depends(get_ctx),
                     container: Container = Depends(get_container)) -> dict[str, Any]:
    """Move and/or resize (drag and drop). A moved work block becomes pinned."""
    block = await _block_or_404(ctx, block_id)
    try:
        await BlockService(ctx.session, ctx.prefs).move(block, ctx.now, _dt(ctx, body.start, "start"), _dt(ctx, body.end, "end"))
        await ctx.session.commit()
    except TaskError as exc:
        await ctx.session.rollback()
        raise HTTPException(400, str(exc)) from exc
    container.after_change()
    return {"id": block.id, "start": block.start_at.isoformat(), "end": block.end_at.isoformat(), "pinned": block.pinned}


@router.delete("/blocks/{block_id}")
async def remove_block(block_id: str, ctx: ToolContext = Depends(get_ctx), container: Container = Depends(get_container)) -> dict[str, str]:
    block = await _block_or_404(ctx, block_id)
    await BlockService(ctx.session, ctx.prefs).remove(block, ctx.now)
    await ctx.session.commit()
    container.after_change()
    return {"status": "removed", "id": block_id}


@router.patch("/reminders/{task_id}")
async def move_reminder(task_id: str, body: BlockMove, ctx: ToolContext = Depends(get_ctx),
                        container: Container = Depends(get_container)) -> dict[str, Any]:
    task = await ctx.tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "Reminder not found.")
    try:
        await BlockService(ctx.session, ctx.prefs).move_reminder(task, ctx.now, _dt(ctx, body.start, "start"))
        await ctx.session.commit()
    except TaskError as exc:
        await ctx.session.rollback()
        raise HTTPException(400, str(exc)) from exc
    container.after_change()
    return task_out(task)


# ---------------------------------------------------------------------- live stream
@router.post("/stream/token")
async def stream_token(container: Container = Depends(get_container)) -> dict[str, Any]:
    exp = int(time.time()) + STREAM_TOKEN_TTL
    return {"token": f"{exp}.{sign(container.settings.SECRET_KEY, 'stream', str(exp))}", "expires": exp}


def _stream_allowed(container: Container, token: str | None) -> bool:
    settings = container.settings
    if not settings.API_KEY and settings.is_dev:
        return True
    try:
        exp, sig = (token or "").split(".")
        return int(exp) > time.time() and verify(settings.SECRET_KEY, "stream", exp, sig)
    except ValueError:
        return False


@stream_router.get("/api/v1/stream")
async def stream(request: Request, token: str | None = Query(default=None), container: Container = Depends(get_container)):
    """Server-Sent Events: 'message' when the assistant sends something, 'changed' when data changed."""
    if not _stream_allowed(container, token):
        raise HTTPException(401, "Invalid or expired stream token.")
    return StreamingResponse(sse_stream(container.bus, request), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
