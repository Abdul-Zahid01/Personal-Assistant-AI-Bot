from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WhatsAppVerificationQuery(BaseModel):
    mode: str
    challenge: str
    verify_token: str


class WhatsAppText(BaseModel):
    body: str


class WhatsAppContact(BaseModel):
    profile: dict[str, Any] | None = None
    wa_id: str | None = None


class WhatsAppMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_number: str = Field(alias="from")
    id: str
    timestamp: str
    type: str
    text: WhatsAppText | None = None

    @field_validator("from_number", mode="before")
    @classmethod
    def extract_from_number(cls, value: Any) -> Any:
        return value


class WhatsAppValue(BaseModel):
    messaging_product: str
    metadata: dict[str, Any] | None = None
    contacts: list[WhatsAppContact] = Field(default_factory=list)
    messages: list[WhatsAppMessage] = Field(default_factory=list)


class WhatsAppChange(BaseModel):
    field: str
    value: WhatsAppValue


class WhatsAppWebhookEntry(BaseModel):
    id: str
    changes: list[WhatsAppChange]


class WhatsAppWebhookEvent(BaseModel):
    object: str
    entry: list[WhatsAppWebhookEntry]


class TelemetryRequest(BaseModel):
    app_name: str
    timestamp: datetime


class LLMIntent(BaseModel):
    action: Literal[
        "GET_TODAY_SCHEDULE",
        "START_TASK",
        "UPDATE_TASK_STATUS",
        "ADD_OR_MODIFY_TASK",
        "SET_RULE",
        "GENERAL_QUERY",
    ]
    confidence: float = 0.0
    raw_text: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class ScheduleWindow(BaseModel):
    time_window: str
    task: str
    priority: str
    notes: str


class ScheduleResponse(BaseModel):
    schedule_table: list[ScheduleWindow] = Field(default_factory=list)
    changes_made: list[str] = Field(default_factory=list)
    dropped_or_deferred_tasks: list[str] = Field(default_factory=list)
    assistant_message: str = ""


class RuleCreate(BaseModel):
    rule_key: str
    start_time: str
    end_time: str | None = None
    is_hard_constraint: bool = True
    metadata_json: dict[str, Any] | None = None


class RuleRead(RuleCreate):
    id: str


class TaskCreate(BaseModel):
    title: str
    description: str | None = None
    estimated_minutes: int
    priority: str = "P2"
    deadline: datetime | None = None
    recurrence: Literal["DAILY", "WEEKLY"] | None = None
    entry_type: Literal["TASK", "REMINDER", "EVENT"] = "TASK"
    sync_to_calendar: bool = False


class TaskRead(TaskCreate):
    id: str
    status: str = "QUEUED"
    actual_minutes: int = 0


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    estimated_minutes: int | None = None
    priority: str | None = None
    deadline: datetime | None = None
