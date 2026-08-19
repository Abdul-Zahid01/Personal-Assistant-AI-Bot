from __future__ import annotations

import uuid
from datetime import datetime, time, timezone
from enum import Enum

from sqlalchemy import Boolean, Column, DateTime, Enum as SQLAlchemyEnum, Integer, String, Text, Time
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.database import Base


class TaskPriority(str, Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class TaskStatus(str, Enum):
    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    DONE = "DONE"


class TaskComplexity(str, Enum):
    EASY = "EASY"
    MEDIUM = "MEDIUM"
    HARD = "HARD"


class ConversationRole(str, Enum):
    user = "user"
    assistant = "assistant"
    system = "system"


class Task(Base):
    __tablename__ = "tasks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    estimated_minutes = Column(Integer, nullable=False)
    actual_minutes = Column(Integer, default=0, nullable=False)
    priority = Column(SQLAlchemyEnum(TaskPriority), default=TaskPriority.P2, nullable=False)
    status = Column(SQLAlchemyEnum(TaskStatus), default=TaskStatus.QUEUED, nullable=False)
    complexity = Column(SQLAlchemyEnum(TaskComplexity), default=TaskComplexity.MEDIUM, nullable=False)
    scheduled_start = Column(DateTime(timezone=True), nullable=True)
    scheduled_end = Column(DateTime(timezone=True), nullable=True)
    deadline = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class UserRule(Base):
    __tablename__ = "user_rules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rule_key = Column(String(50), unique=True, nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=True)
    is_hard_constraint = Column(Boolean, default=True, nullable=False)
    metadata_json = Column(JSONB, nullable=True)


class TelemetryEvent(Base):
    __tablename__ = "telemetry_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_app = Column(String(100), nullable=False)
    event_type = Column(String(50), nullable=False)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class ConversationHistory(Base):
    __tablename__ = "conversation_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    role = Column(SQLAlchemyEnum(ConversationRole), nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class GoogleOAuthToken(Base):
    __tablename__ = "google_oauth_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(32), unique=True, nullable=False, default="google")
    access_token = Column(Text, nullable=True)
    refresh_token = Column(Text, nullable=True)
    scope = Column(Text, nullable=True)
    token_type = Column(String(32), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
