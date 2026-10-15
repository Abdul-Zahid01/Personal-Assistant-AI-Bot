from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Task(Base):
    """A task, a reminder or an event. `kind` decides how it behaves."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="TASK")  # TASK | REMINDER | EVENT
    status: Mapped[str] = mapped_column(String(16), default="TODO", index=True)  # TODO | IN_PROGRESS | DONE
    priority: Mapped[str] = mapped_column(String(2), default="P3")
    estimate_min: Mapped[int] = mapped_column(Integer, default=30)
    actual_min: Mapped[int] = mapped_column(Integer, default=0)
    # Deadline for a TASK, fire time for a REMINDER, start time for an EVENT.
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recurrence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)  # when you pressed Start
    # Set on the daily copies created from a routine (gym, lunch, call mom...).
    routine_id: Mapped[str | None] = mapped_column(ForeignKey("routines.id", ondelete="SET NULL"), nullable=True, index=True)


class Routine(Base):
    """Something you do on a fixed schedule (gym, meals, calling mom). A copy is created for each day automatically."""

    __tablename__ = "routines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16), default="EVENT")  # EVENT (blocks time) | REMINDER (just a nudge)
    time_of_day: Mapped[str] = mapped_column(String(5))  # HH:MM local time
    duration_min: Mapped[int] = mapped_column(Integer, default=30)
    days: Mapped[str] = mapped_column(String(7), default="0123456")  # weekdays it runs on, Monday = 0
    priority: Mapped[str] = mapped_column(String(2), default="P3")
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class RoutineRun(Base):
    """Marks that a routine's copy for a day was created, so deleting that copy doesn't bring it back."""

    __tablename__ = "routine_runs"

    routine_id: Mapped[str] = mapped_column(ForeignKey("routines.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD local


class TimeBlock(Base):
    """A slot on the calendar: planned work time, or a fixed event."""

    __tablename__ = "time_blocks"
    __table_args__ = (Index("ix_blocks_start", "start_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(8), default="WORK")  # WORK | EVENT
    start_at: Mapped[datetime] = mapped_column(UTCDateTime)
    end_at: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(12), default="PLANNED")  # PLANNED | DONE | SKIPPED | CANCELLED
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    calendar_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # A block needs a calendar write when synced_at is missing or older than updated_at.
    synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Reminder(Base):
    """Something to tell the user at `fire_at`. Persisted so it survives restarts."""

    __tablename__ = "reminders"
    __table_args__ = (Index("ix_reminders_due", "status", "fire_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True)
    block_id: Mapped[str | None] = mapped_column(ForeignKey("time_blocks.id", ondelete="CASCADE"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(16))  # REMINDER NAG DEADLINE EVENT_START BLOCK_START BLOCK_END
    fire_at: Mapped[datetime] = mapped_column(UTCDateTime)
    text: Mapped[str] = mapped_column(Text)
    repeat_every_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    repeats_left: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(12), default="PENDING")  # PENDING SENT CANCELLED EXPIRED FAILED
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Preference(Base):
    __tablename__ = "preferences"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)


class Message(Base):
    """Every message in the conversation, including proactive ones the assistant sends."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role: Mapped[str] = mapped_column(String(12))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(16), default="web")
    buttons: Mapped[Any] = mapped_column(JSON, nullable=True)  # [[label, data], ...]
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class DailyReview(Base):
    __tablename__ = "daily_reviews"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD in the user's timezone
    completed: Mapped[int] = mapped_column(Integer, default=0)
    carried_over: Mapped[int] = mapped_column(Integer, default=0)
    estimate_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PushSubscription(Base):
    """A browser/phone that asked for notifications (Web Push)."""

    __tablename__ = "push_subscriptions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    endpoint: Mapped[str] = mapped_column(String(1024), unique=True)
    p256dh: Mapped[str] = mapped_column(String(255))
    auth: Mapped[str] = mapped_column(String(255))
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    data_encrypted: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
