from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.db.models import Task
from app.messaging import Reply


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class ActionIn(BaseModel):
    data: str = Field(min_length=1, max_length=64)


class ReplyOut(BaseModel):
    reply: str
    buttons: list[list[str]] = []

    @classmethod
    def of(cls, reply: Reply) -> "ReplyOut":
        return cls(reply=reply.text, buttons=[b.as_pair() for b in reply.buttons])


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    kind: str = "TASK"
    due_at: str | None = Field(default=None, description="ISO 8601; no offset = your timezone")
    estimate_min: int | None = None
    priority: str = "P3"
    notes: str | None = None
    category: str | None = None
    recurrence: str | None = None


class TaskPatch(BaseModel):
    title: str | None = None
    kind: str | None = None
    due_at: str | None = None
    estimate_min: int | None = None
    priority: str | None = None
    notes: str | None = None
    category: str | None = None
    recurrence: str | None = None
    status: str | None = None
    remind_before_min: int | None = None


class CompleteIn(BaseModel):
    actual_min: int | None = None


class SnoozeIn(BaseModel):
    minutes: int = 30


class PlanIn(BaseModel):
    date: str = "today"
    commit: bool = True


class RoutineIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    time_of_day: str = Field(description="HH:MM")
    duration_min: int = 30
    days: str | list[int] | list[str] | None = "daily"
    kind: str = "EVENT"
    priority: str = "P3"
    category: str | None = None


class RoutinePatch(BaseModel):
    title: str | None = None
    time_of_day: str | None = None
    duration_min: int | None = None
    days: str | list[int] | list[str] | None = None
    kind: str | None = None
    priority: str | None = None
    category: str | None = None
    active: bool | None = None


def routine_out(r: Any, today_copy: Task | None = None) -> dict[str, Any]:
    from app.services.routines import describe_days

    return {
        "id": r.id, "title": r.title, "kind": r.kind, "time_of_day": r.time_of_day, "duration_min": r.duration_min,
        "days": [int(d) for d in r.days], "days_label": describe_days(r.days), "priority": r.priority,
        "category": r.category, "active": r.active,
        "today": task_out(today_copy) if today_copy else None,
    }


def task_out(t: Task) -> dict[str, Any]:
    return {
        "id": t.id, "title": t.title, "notes": t.notes, "kind": t.kind, "status": t.status, "priority": t.priority,
        "estimate_min": t.estimate_min, "actual_min": t.actual_min, "category": t.category, "recurrence": t.recurrence,
        "due_at": t.due_at.isoformat() if t.due_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        "started_at": t.started_at.isoformat() if t.started_at else None,
        "routine_id": t.routine_id, "remind_before_min": t.remind_before_min,
    }
