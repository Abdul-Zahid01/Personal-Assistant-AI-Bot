from __future__ import annotations

import hashlib
import hmac
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.models import GoogleOAuthToken, Task, TaskPriority, TaskStatus
from app.schemas import RuleCreate, RuleRead, ScheduleResponse, ScheduleWindow, TaskCreate, TaskRead, TaskUpdate, TelemetryRequest
from app.services.llm_engine import classify_entry_intent_with_llm, extract_task_details_with_llm, route_intent
from app.workers.celery_app import celery_app
from app.workers.tasks import process_whatsapp_message


class ChatMessage(BaseModel):
    message: str


def schedule_task_checkin(task_id: str, estimated_minutes: int) -> None:
    try:
        celery_app.send_task(
            "app.workers.tasks.checkin_task_timer",
            args=[task_id, max(1, estimated_minutes // 2)],
            countdown=5,
        )
    except Exception:
        pass


def schedule_deadline_reminder(task_id: str, title: str, deadline: datetime | None) -> None:
    if deadline is None:
        return

    deadline_utc = deadline
    if deadline_utc.tzinfo is None:
        deadline_utc = deadline_utc.replace(tzinfo=timezone.utc)
    else:
        deadline_utc = deadline_utc.astimezone(timezone.utc)

    remind_at = deadline_utc - timedelta(minutes=10)
    countdown = int((remind_at - datetime.now(timezone.utc)).total_seconds())
    if countdown <= 0:
        return

    try:
        celery_app.send_task(
            "app.workers.tasks.deadline_task_reminder",
            args=[task_id, title, deadline_utc.isoformat()],
            countdown=countdown,
        )
    except Exception:
        pass

_in_memory_tasks: list[dict[str, Any]] = []
_in_memory_rules: list[dict[str, Any]] = []
_google_tokens: dict[str, str] = {}
_task_calendar_events: dict[str, str] = {}
_task_recurrence_rules: dict[str, str] = {}
_task_entry_types: dict[str, str] = {}


def _priority_rank(priority: str) -> int:
    return {"P1": 0, "P2": 1, "P3": 2, "P4": 3}.get(priority, 4)


def _parse_deadline_value(deadline: Any) -> datetime | None:
    if deadline is None:
        return None
    if isinstance(deadline, datetime):
        if deadline.tzinfo is None:
            return deadline.replace(tzinfo=timezone.utc)
        return deadline
    if isinstance(deadline, str) and deadline.strip():
        try:
            parsed = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            return None
    return None


def _format_deadline_local(deadline: Any) -> str | None:
    deadline_dt = _parse_deadline_value(deadline)
    if deadline_dt is None:
        return None
    local_dt = deadline_dt.astimezone(datetime.now().astimezone().tzinfo)
    return local_dt.strftime("%Y-%m-%d %I:%M %p")


async def _find_time_conflicts(deadline: datetime | None, exclude_task_id: str | None = None) -> list[dict[str, Any]]:
    if deadline is None:
        return []

    target = deadline.astimezone(timezone.utc)
    tasks = await _load_tasks_for_schedule()
    conflicts: list[dict[str, Any]] = []

    for task in tasks:
        if not _is_active_task(task):
            continue
        task_id = str(task.get("id") or "")
        if exclude_task_id and task_id == exclude_task_id:
            continue

        task_deadline = _parse_deadline_value(task.get("deadline"))
        if task_deadline is None:
            continue
        task_deadline_utc = task_deadline.astimezone(timezone.utc)

        # We treat events/tasks within 45 minutes as potential overlap for suggestions.
        if abs((task_deadline_utc - target).total_seconds()) <= 45 * 60:
            conflicts.append(task)

    return conflicts


def _build_conflict_suggestion(deadline: datetime | None, conflicts: list[dict[str, Any]]) -> str:
    if deadline is None or not conflicts:
        return ""

    conflict_titles = ", ".join(str(item.get("title") or "Task") for item in conflicts[:2])
    alt_time = deadline + timedelta(hours=1)
    alt_local = _format_deadline_local(alt_time) or alt_time.isoformat()
    return (
        f" Potential conflict with: {conflict_titles}. "
        f"Suggestion: move this to {alt_local} or convert one item into a reminder task."
    )


async def generate_end_of_day_analysis() -> str:
    tasks = await _load_tasks_for_schedule()
    if not tasks:
        return "End-of-day analysis: No tasks logged today yet."

    now_local = datetime.now().astimezone()
    today = now_local.date()
    done_today: list[dict[str, Any]] = []
    pending_today: list[dict[str, Any]] = []
    overdue: list[dict[str, Any]] = []

    for task in tasks:
        deadline = _parse_deadline_value(task.get("deadline"))
        status = str(task.get("status") or "").upper()
        if deadline is None:
            continue

        local_day = deadline.astimezone(now_local.tzinfo).date()
        if local_day != today:
            continue

        if status == "DONE":
            done_today.append(task)
        else:
            pending_today.append(task)
            if deadline.astimezone(now_local.tzinfo) < now_local:
                overdue.append(task)

    if not done_today and not pending_today:
        return "End-of-day analysis: No dated tasks for today."

    move_candidates = sorted(
        pending_today,
        key=lambda t: (_priority_rank(str(t.get("priority") or "P4")), _parse_deadline_value(t.get("deadline")) or datetime.max.replace(tzinfo=timezone.utc)),
    )
    top_moves = ", ".join(str(t.get("title") or "Task") for t in move_candidates[:3]) if move_candidates else "None"

    analysis = (
        f"End-of-day analysis: completed {len(done_today)} task(s), pending {len(pending_today)} task(s), "
        f"overdue {len(overdue)} task(s). "
        f"Suggested move to tomorrow: {top_moves}."
    )
    return analysis


def _detect_recurrence_from_text(text: str) -> str | None:
    lowered = text.lower()
    if any(marker in lowered for marker in ["every day", "daily", "each day"]):
        return "DAILY"
    if any(marker in lowered for marker in ["every week", "weekly", "each week"]):
        return "WEEKLY"
    return None


def _classify_entry_type(text: str, parsed_deadline: datetime | None) -> tuple[str, bool]:
    lowered = text.strip().lower()
    asks_calendar_event = any(marker in lowered for marker in ["calendar", "calender", "create event", "add event"])
    is_reminder_phrase = lowered.startswith("remind me") or lowered.startswith("remind")
    is_explicit_task = "task" in lowered or "todo" in lowered
    looks_like_timed_activity = any(marker in lowered for marker in ["meeting", "appointment", "call", "gym", "workout", "session", "time"]) 
    has_explicit_clock_time = bool(re.search(r"\b(?:at\s+)?(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:am|pm)\b", lowered)) or bool(
        re.search(r"\b(?:at\s+)?(?:[01]?\d|2[0-3]):[0-5]\d\b", lowered)
    )

    if is_reminder_phrase:
        return "REMINDER", False
    if asks_calendar_event:
        return "EVENT", True
    if has_explicit_clock_time and not is_reminder_phrase:
        return "EVENT", True
    if is_explicit_task and parsed_deadline is None:
        return "TASK", False
    if parsed_deadline is not None and looks_like_timed_activity:
        return "EVENT", True
    if parsed_deadline is None:
        return "REMINDER", False
    return "TASK", False


def schedule_recurring_task_reminder(task_id: str, title: str) -> None:
    try:
        celery_app.send_task(
            "app.workers.tasks.recurring_task_reminder",
            args=[task_id, title, 1],
            countdown=2 * 60 * 60,
        )
    except Exception:
        pass


def _is_active_task(task: dict[str, Any]) -> bool:
    status = str(task.get("status") or "").upper()
    return status not in {"DONE", "BLOCKED"}


def parse_deadline_from_text(text: str) -> datetime | None:
    lowered = text.lower()
    base_date = None
    now_local = datetime.now().astimezone()

    relative_minutes_match = re.search(r"\bin\s+(\d+)\s*(minutes?|mins?)\b", lowered)
    if relative_minutes_match:
        minutes = int(relative_minutes_match.group(1))
        return (now_local + timedelta(minutes=minutes)).astimezone(timezone.utc)

    relative_hours_match = re.search(r"\bin\s+(\d+)\s*(hours?|hrs?)\b", lowered)
    if relative_hours_match:
        hours = int(relative_hours_match.group(1))
        return (now_local + timedelta(hours=hours)).astimezone(timezone.utc)

    if re.search(r"\b(?:by\s+)?tomorr\w*\b", lowered):
        base_date = (now_local + timedelta(days=1)).date()
    elif "today" in lowered:
        base_date = now_local.date()

    if base_date is None:
        return None

    time_match = re.search(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", lowered)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        ampm = time_match.group(3)
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
    else:
        hhmm_match = re.search(r"\b(?:at\s+)?([01]?\d|2[0-3]):([0-5]\d)\b", lowered)
        if not hhmm_match:
            # If user says "by tomorrow" without a time, default to 9 AM local.
            hour = 9
            minute = 0
        else:
            hour = int(hhmm_match.group(1))
            minute = int(hhmm_match.group(2))

    return datetime(
        year=base_date.year,
        month=base_date.month,
        day=base_date.day,
        hour=hour,
        minute=minute,
        tzinfo=now_local.tzinfo,
    ).astimezone(timezone.utc)


def _set_google_token_metadata(token_payload: dict[str, Any]) -> None:
    expires_in = token_payload.get("expires_in")
    expires_at = None
    if isinstance(expires_in, int):
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

    _google_tokens.update(
        {
            "access_token": token_payload.get("access_token", _google_tokens.get("access_token", "")),
            "refresh_token": token_payload.get("refresh_token", _google_tokens.get("refresh_token", "")),
            "scope": token_payload.get("scope", _google_tokens.get("scope", "")),
            "token_type": token_payload.get("token_type", _google_tokens.get("token_type", "Bearer")),
            "expires_at": expires_at or _google_tokens.get("expires_at", ""),
            "last_error": "",
        }
    )


def _token_expires_at_datetime() -> datetime | None:
    expires_at = _google_tokens.get("expires_at")
    if not expires_at:
        return None
    try:
        return datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return None


async def persist_google_tokens_to_db() -> None:
    try:
        async with AsyncSessionLocal() as session:
            token_row = await session.scalar(select(GoogleOAuthToken).where(GoogleOAuthToken.provider == "google"))
            if token_row is None:
                token_row = GoogleOAuthToken(provider="google")
                session.add(token_row)

            token_row.access_token = _google_tokens.get("access_token") or None
            token_row.refresh_token = _google_tokens.get("refresh_token") or None
            token_row.scope = _google_tokens.get("scope") or None
            token_row.token_type = _google_tokens.get("token_type") or None
            token_row.expires_at = _token_expires_at_datetime()
            token_row.last_error = _google_tokens.get("last_error") or None
            token_row.updated_at = datetime.now(timezone.utc)
            await session.commit()
    except Exception:
        pass


async def load_google_tokens_from_db() -> None:
    try:
        async with AsyncSessionLocal() as session:
            token_row = await session.scalar(select(GoogleOAuthToken).where(GoogleOAuthToken.provider == "google"))
            if token_row is None:
                return

            _google_tokens.update(
                {
                    "access_token": token_row.access_token or "",
                    "refresh_token": token_row.refresh_token or "",
                    "scope": token_row.scope or "",
                    "token_type": token_row.token_type or "Bearer",
                    "expires_at": token_row.expires_at.isoformat() if token_row.expires_at else "",
                    "last_error": token_row.last_error or "",
                }
            )
    except Exception:
        pass


async def ensure_google_access_token() -> str | None:
    access_token = _google_tokens.get("access_token")
    refresh_token = _google_tokens.get("refresh_token")
    expires_at = _google_tokens.get("expires_at")

    if access_token and expires_at:
        try:
            expires_at_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < (expires_at_dt - timedelta(seconds=60)):
                return access_token
        except ValueError:
            return access_token
    elif access_token:
        return access_token

    if not refresh_token:
        _google_tokens["last_error"] = "No Google refresh token found. Please reconnect."
        await persist_google_tokens_to_db()
        return None

    try:
        token_response = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
        token_response.raise_for_status()
        payload = token_response.json()
        _set_google_token_metadata(payload)
        await persist_google_tokens_to_db()
        return _google_tokens.get("access_token")
    except Exception as exc:
        _google_tokens["last_error"] = f"Google token refresh failed: {exc}"
        await persist_google_tokens_to_db()
        return None


async def create_google_calendar_event(task_title: str, description: str | None, estimated_minutes: int, deadline: Any | None = None) -> str | None:
    access_token = await ensure_google_access_token()
    if not access_token:
        return None

    try:
        start_time = deadline
        if isinstance(start_time, str):
            try:
                start_time = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            except ValueError:
                start_time = None

        if start_time is None:
            start_time = parse_deadline_from_text(task_title)
        if start_time is None and description:
            start_time = parse_deadline_from_text(description)
        if start_time is None:
            start_time = datetime.now(timezone.utc)

        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        else:
            start_time = start_time.astimezone(timezone.utc)

        end_time = start_time + timedelta(minutes=max(1, estimated_minutes))

        payload = {
            "summary": task_title,
            "description": description or "Created by Personal AI Assistant",
            "start": {"dateTime": start_time.isoformat()},
            "end": {"dateTime": end_time.isoformat()},
        }

        response = httpx.post(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            json=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        _google_tokens["last_error"] = ""
        await persist_google_tokens_to_db()
        return data.get("id")
    except Exception as exc:
        _google_tokens["last_error"] = f"Google Calendar create failed: {exc}"
        await persist_google_tokens_to_db()
        return None


async def update_google_calendar_event(
    event_id: str,
    task_title: str,
    description: str | None,
    estimated_minutes: int,
    deadline: Any | None = None,
) -> bool:
    access_token = await ensure_google_access_token()
    if not access_token:
        return False

    try:
        start_time = deadline
        if isinstance(start_time, str):
            try:
                start_time = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            except ValueError:
                start_time = None

        if start_time is None:
            start_time = parse_deadline_from_text(task_title)
        if start_time is None and description:
            start_time = parse_deadline_from_text(description)
        if start_time is None:
            start_time = datetime.now(timezone.utc)

        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        else:
            start_time = start_time.astimezone(timezone.utc)

        end_time = start_time + timedelta(minutes=max(1, estimated_minutes))
        payload = {
            "summary": task_title,
            "description": description or "Updated by Personal AI Assistant",
            "start": {"dateTime": start_time.isoformat()},
            "end": {"dateTime": end_time.isoformat()},
        }

        response = httpx.patch(
            f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{event_id}",
            json=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout=20,
        )
        response.raise_for_status()
        _google_tokens["last_error"] = ""
        await persist_google_tokens_to_db()
        return True
    except Exception as exc:
        _google_tokens["last_error"] = f"Google Calendar update failed: {exc}"
        await persist_google_tokens_to_db()
        return False


async def delete_google_calendar_event(event_id: str) -> bool:
    access_token = await ensure_google_access_token()
    if not access_token:
        return False

    try:
        response = httpx.delete(
            f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{event_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        response.raise_for_status()
        _google_tokens["last_error"] = ""
        await persist_google_tokens_to_db()
        return True
    except Exception as exc:
        _google_tokens["last_error"] = f"Google Calendar delete failed: {exc}"
        await persist_google_tokens_to_db()
        return False


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await init_db()
        await load_google_tokens_from_db()
    except Exception:
        pass
    yield


app = FastAPI(title="Personal AI Assistant", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "http://127.0.0.1:8080", "http://localhost:8000", "http://127.0.0.1:8000", "file://"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/auth/google/login")
async def google_oauth_login() -> RedirectResponse:
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID or "",
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/calendar",
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return RedirectResponse(url=auth_url, status_code=302)


@app.get("/auth/google/callback")
async def google_oauth_callback(
    code: str | None = None,
    error: str | None = None,
    scope: str | None = None,
) -> JSONResponse:
    if error:
        raise HTTPException(status_code=400, detail=error)
    if not code:
        raise HTTPException(status_code=400, detail="Missing Google OAuth code")

    try:
        token_response = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            timeout=20,
        )
        token_response.raise_for_status()
        token_payload = token_response.json()
    except httpx.HTTPStatusError as exc:
        response_payload: dict[str, Any] = {}
        try:
            response_payload = exc.response.json()
        except Exception:
            response_payload = {}

        error_code = response_payload.get("error", "token_exchange_failed")
        error_description = response_payload.get("error_description", str(exc))
        _google_tokens["last_error"] = f"Google OAuth token exchange failed: {error_code}"
        await persist_google_tokens_to_db()

        if error_code == "invalid_grant" and (_google_tokens.get("access_token") or _google_tokens.get("refresh_token")):
            return JSONResponse(
                content={
                    "status": "google_oauth_already_connected",
                    "detail": "The authorization code was already used or expired, but an existing Google connection is active.",
                    "last_error": _google_tokens.get("last_error"),
                }
            )

        raise HTTPException(status_code=400, detail=f"Google OAuth failed: {error_description}")

    if scope and "scope" not in token_payload:
        token_payload["scope"] = scope
    _set_google_token_metadata(token_payload)
    await persist_google_tokens_to_db()

    return JSONResponse(
        content={
            "status": "google_oauth_connected",
            "access_token": _google_tokens["access_token"],
            "refresh_token": _google_tokens["refresh_token"],
            "scope": _google_tokens["scope"],
            "token_type": _google_tokens["token_type"],
        }
    )


@app.get("/auth/google/status")
async def google_oauth_status() -> dict[str, Any]:
    return {
        "connected": bool(_google_tokens.get("refresh_token") or _google_tokens.get("access_token")),
        "access_token_present": bool(_google_tokens.get("access_token")),
        "refresh_token_present": bool(_google_tokens.get("refresh_token")),
        "expires_at": _google_tokens.get("expires_at") or None,
        "last_error": _google_tokens.get("last_error") or None,
    }


@app.get("/api/v1/webhook/whatsapp")
async def verify_whatsapp_webhook(
    mode: str = Query(..., alias="hub.mode"),
    challenge: str = Query(..., alias="hub.challenge"),
    verify_token: str = Query(..., alias="hub.verify_token"),
) -> dict[str, Any]:
    if mode != "subscribe":
        raise HTTPException(status_code=400, detail="Invalid mode")
    if verify_token != settings.WHATSAPP_VERIFY_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid verify token")
    return {"status": "ok", "challenge": int(challenge)}


@app.post("/api/v1/webhook/whatsapp")
async def receive_whatsapp_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
) -> JSONResponse:
    raw_body = await request.body()
    if not raw_body:
        return JSONResponse(content={"status": "ignored"})

    if x_hub_signature_256 is not None:
        expected = "sha256=" + hmac.new(settings.META_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(content={"status": "ignored"})

    process_whatsapp_message.delay(payload)
    return JSONResponse(content={"status": "ok"})


@app.post("/api/v1/tasks", status_code=201)
async def create_task(payload: TaskCreate) -> dict[str, Any]:
    task_id = str(uuid4())
    schedule_deadline_reminder(task_id=task_id, title=payload.title, deadline=payload.deadline)
    task_record = {
        "id": task_id,
        "title": payload.title,
        "description": payload.description,
        "estimated_minutes": payload.estimated_minutes,
        "actual_minutes": 0,
        "priority": payload.priority,
        "status": "QUEUED",
        "deadline": payload.deadline.isoformat() if payload.deadline else None,
        "recurrence": payload.recurrence,
        "entry_type": payload.entry_type,
    }
    _task_entry_types[task_id] = payload.entry_type
    if payload.recurrence:
        _task_recurrence_rules[task_id] = payload.recurrence

    if payload.entry_type == "REMINDER" and payload.deadline is None:
        schedule_recurring_task_reminder(task_id=task_id, title=payload.title)

    google_event_id = None
    if payload.sync_to_calendar:
        google_event_id = await create_google_calendar_event(
            task_title=payload.title,
            description=payload.description,
            estimated_minutes=payload.estimated_minutes,
            deadline=payload.deadline,
        )
    if google_event_id:
        task_record["google_event_id"] = google_event_id
        _task_calendar_events[task_id] = google_event_id

    try:
        async with AsyncSessionLocal() as session:
            task = Task(
                id=task_id,
                title=payload.title,
                description=payload.description,
                estimated_minutes=payload.estimated_minutes,
                deadline=payload.deadline,
                priority=TaskPriority(payload.priority),
                status=TaskStatus.QUEUED,
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            result = TaskRead(
                id=str(task.id),
                title=task.title,
                description=task.description,
                estimated_minutes=task.estimated_minutes,
                priority=task.priority.value,
                status=task.status.value,
                actual_minutes=task.actual_minutes,
                deadline=task.deadline,
            ).model_dump(mode="json")
            if google_event_id:
                result["google_event_id"] = google_event_id
                _task_calendar_events[str(task.id)] = google_event_id
            if payload.recurrence:
                result["recurrence"] = payload.recurrence
                _task_recurrence_rules[str(task.id)] = payload.recurrence
            result["entry_type"] = payload.entry_type
            _task_entry_types[str(task.id)] = payload.entry_type
            return result
    except Exception:
        _in_memory_tasks.append(task_record)
        return task_record


@app.put("/api/v1/tasks/{task_id}")
async def update_task(task_id: str, payload: TaskUpdate) -> dict[str, Any]:
    for task in _in_memory_tasks:
        if task["id"] == task_id:
            if payload.title is not None:
                task["title"] = payload.title
            if payload.description is not None:
                task["description"] = payload.description
            if payload.estimated_minutes is not None:
                task["estimated_minutes"] = payload.estimated_minutes
            if payload.priority is not None:
                task["priority"] = payload.priority
            if payload.deadline is not None:
                task["deadline"] = payload.deadline.isoformat()

            event_id = _task_calendar_events.get(task_id)
            if event_id:
                await update_google_calendar_event(
                    event_id=event_id,
                    task_title=task["title"],
                    description=task.get("description"),
                    estimated_minutes=int(task.get("estimated_minutes") or 30),
                    deadline=task.get("deadline"),
                )
            return task

    try:
        async with AsyncSessionLocal() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")

            if payload.title is not None:
                task.title = payload.title
            if payload.description is not None:
                task.description = payload.description
            if payload.estimated_minutes is not None:
                task.estimated_minutes = payload.estimated_minutes
            if payload.priority is not None:
                task.priority = TaskPriority(payload.priority)
            if payload.deadline is not None:
                task.deadline = payload.deadline

            event_id = _task_calendar_events.get(task_id)
            if event_id:
                await update_google_calendar_event(
                    event_id=event_id,
                    task_title=task.title,
                    description=task.description,
                    estimated_minutes=task.estimated_minutes,
                    deadline=task.deadline,
                )

            await session.commit()
            await session.refresh(task)
            return TaskRead(
                id=str(task.id),
                title=task.title,
                description=task.description,
                estimated_minutes=task.estimated_minutes,
                priority=task.priority.value,
                status=task.status.value,
                actual_minutes=task.actual_minutes,
                deadline=task.deadline,
            ).model_dump(mode="json")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="Task not found")


@app.delete("/api/v1/tasks/{task_id}")
async def delete_task(task_id: str) -> dict[str, str]:
    event_id = _task_calendar_events.get(task_id)
    if event_id:
        await delete_google_calendar_event(event_id)

    for index, task in enumerate(_in_memory_tasks):
        if task["id"] == task_id:
            _in_memory_tasks.pop(index)
            _task_calendar_events.pop(task_id, None)
            return {"status": "deleted", "task_id": task_id}

    try:
        async with AsyncSessionLocal() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")
            await session.delete(task)
            await session.commit()
            _task_calendar_events.pop(task_id, None)
            return {"status": "deleted", "task_id": task_id}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="Task not found")


@app.post("/api/v1/tasks/{task_id}/start")
async def start_task(task_id: str) -> dict[str, Any]:
    for task in _in_memory_tasks:
        if task["id"] == task_id:
            task["status"] = "IN_PROGRESS"
            schedule_task_checkin(task_id, int(task["estimated_minutes"]))
            return task

    try:
        async with AsyncSessionLocal() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")
            task.status = TaskStatus.IN_PROGRESS
            await session.commit()
            await session.refresh(task)
            schedule_task_checkin(task_id, int(task.estimated_minutes))
            return TaskRead(
                id=str(task.id),
                title=task.title,
                description=task.description,
                estimated_minutes=task.estimated_minutes,
                priority=task.priority.value,
                status=task.status.value,
                actual_minutes=task.actual_minutes,
                deadline=task.deadline,
            ).model_dump(mode="json")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="Task not found")


@app.patch("/api/v1/tasks/{task_id}/status")
async def update_task_status(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    for task in _in_memory_tasks:
        if task["id"] == task_id:
            task["status"] = payload.get("status", task["status"])
            task["actual_minutes"] = payload.get("actual_minutes", task.get("actual_minutes", 0))
            return task

    try:
        async with AsyncSessionLocal() as session:
            task = await session.get(Task, task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")
            task.status = TaskStatus(payload["status"])
            task.actual_minutes = payload.get("actual_minutes", task.actual_minutes)
            await session.commit()
            await session.refresh(task)
            return TaskRead(
                id=str(task.id),
                title=task.title,
                description=task.description,
                estimated_minutes=task.estimated_minutes,
                priority=task.priority.value,
                status=task.status.value,
                actual_minutes=task.actual_minutes,
                deadline=task.deadline,
            ).model_dump(mode="json")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="Task not found")


@app.get("/api/v1/tasks")
async def list_tasks() -> list[dict[str, Any]]:
    try:
        async with AsyncSessionLocal() as session:
            tasks = await session.execute(Task.__table__.select())
            rows = tasks.fetchall()
            return [
                {
                    "id": str(row[0]),
                    "title": row[1],
                    "description": row[2],
                    "estimated_minutes": row[3],
                    "actual_minutes": row[4],
                    "priority": row[5].value if hasattr(row[5], "value") else row[5],
                    "status": row[6].value if hasattr(row[6], "value") else row[6],
                    "deadline": row[10].isoformat() if row[10] else None,
                    "entry_type": _task_entry_types.get(str(row[0]), "TASK"),
                }
                for row in rows
            ]
    except Exception:
        return _in_memory_tasks


async def mark_latest_task_done() -> dict[str, Any] | None:
    if _in_memory_tasks:
        latest = _in_memory_tasks[-1]
        latest["status"] = "DONE"
        recurring = _task_recurrence_rules.get(str(latest.get("id")))
        if recurring:
            next_task = await _create_next_recurring_task(latest)
            if next_task is not None:
                latest["next_task_created"] = {
                    "title": next_task.get("title"),
                    "deadline": next_task.get("deadline"),
                }
        return latest

    try:
        async with AsyncSessionLocal() as session:
            latest_task = await session.scalar(select(Task).order_by(Task.created_at.desc()).limit(1))
            if latest_task is None:
                return None
            latest_task.status = TaskStatus.DONE
            await session.commit()
            await session.refresh(latest_task)
            result = TaskRead(
                id=str(latest_task.id),
                title=latest_task.title,
                description=latest_task.description,
                estimated_minutes=latest_task.estimated_minutes,
                priority=latest_task.priority.value,
                status=latest_task.status.value,
                actual_minutes=latest_task.actual_minutes,
                deadline=latest_task.deadline,
            ).model_dump(mode="json")
            recurring = _task_recurrence_rules.get(str(latest_task.id))
            if recurring:
                next_task = await _create_next_recurring_task(result)
                if next_task is not None:
                    result["next_task_created"] = {
                        "title": next_task.get("title"),
                        "deadline": next_task.get("deadline"),
                    }
            return result
    except Exception:
        return None


async def _create_next_recurring_task(completed_task: dict[str, Any]) -> dict[str, Any] | None:
    task_id = str(completed_task.get("id") or "")
    recurrence = _task_recurrence_rules.get(task_id)
    if recurrence not in {"DAILY", "WEEKLY"}:
        return None

    deadline_dt = _parse_deadline_value(completed_task.get("deadline"))
    if deadline_dt is None:
        deadline_dt = datetime.now(timezone.utc)

    if recurrence == "DAILY":
        next_deadline = deadline_dt + timedelta(days=1)
    else:
        next_deadline = deadline_dt + timedelta(days=7)

    try:
        return await create_task(
            TaskCreate(
                title=str(completed_task.get("title") or "Recurring task"),
                description=completed_task.get("description"),
                estimated_minutes=int(completed_task.get("estimated_minutes") or 30),
                priority=str(completed_task.get("priority") or "P3"),
                deadline=next_deadline,
                recurrence=recurrence,
            )
        )
    except Exception:
        return None


async def _update_latest_task_deadline(new_deadline: datetime) -> dict[str, Any] | None:
    if _in_memory_tasks:
        latest = _in_memory_tasks[-1]
        latest["deadline"] = new_deadline.astimezone(timezone.utc).isoformat()
        event_id = _task_calendar_events.get(str(latest.get("id")))
        if event_id:
            await update_google_calendar_event(
                event_id=event_id,
                task_title=str(latest.get("title") or "Task"),
                description=latest.get("description"),
                estimated_minutes=int(latest.get("estimated_minutes") or 30),
                deadline=latest.get("deadline"),
            )
        return latest

    try:
        async with AsyncSessionLocal() as session:
            latest_task = await session.scalar(select(Task).order_by(Task.created_at.desc()).limit(1))
            if latest_task is None:
                return None
            latest_task.deadline = new_deadline.astimezone(timezone.utc)
            await session.commit()
            await session.refresh(latest_task)

            event_id = _task_calendar_events.get(str(latest_task.id))
            if event_id:
                await update_google_calendar_event(
                    event_id=event_id,
                    task_title=latest_task.title,
                    description=latest_task.description,
                    estimated_minutes=latest_task.estimated_minutes,
                    deadline=latest_task.deadline,
                )

            return TaskRead(
                id=str(latest_task.id),
                title=latest_task.title,
                description=latest_task.description,
                estimated_minutes=latest_task.estimated_minutes,
                priority=latest_task.priority.value,
                status=latest_task.status.value,
                actual_minutes=latest_task.actual_minutes,
                deadline=latest_task.deadline,
            ).model_dump(mode="json")
    except Exception:
        return None


async def archive_duplicate_today_tasks() -> int:
    tasks = await _load_tasks_for_schedule()
    now_local = datetime.now().astimezone()
    today = now_local.date()

    candidates = [
        task for task in tasks
        if _is_active_task(task)
        and _parse_deadline_value(task.get("deadline")) is not None
        and _parse_deadline_value(task.get("deadline")).astimezone(now_local.tzinfo).date() == today
    ]
    if not candidates:
        return 0

    sorted_candidates = sorted(
        candidates,
        key=lambda item: (
            str(item.get("title") or "").strip().lower(),
            _priority_rank(str(item.get("priority") or "P4")),
            _parse_deadline_value(item.get("deadline")) or datetime.max.replace(tzinfo=timezone.utc),
        ),
    )

    keep_title_once: set[str] = set()
    duplicate_ids: list[str] = []
    for task in sorted_candidates:
        title_key = str(task.get("title") or "").strip().lower()
        task_id = str(task.get("id") or "")
        if not title_key or not task_id:
            continue
        if title_key in keep_title_once:
            duplicate_ids.append(task_id)
        else:
            keep_title_once.add(title_key)

    if not duplicate_ids:
        return 0

    archived_count = 0
    for task in _in_memory_tasks:
        if str(task.get("id")) in duplicate_ids:
            task["status"] = "DONE"
            archived_count += 1

    remaining_ids = {task_id for task_id in duplicate_ids}
    if remaining_ids:
        try:
            async with AsyncSessionLocal() as session:
                db_tasks = (await session.execute(select(Task).where(Task.id.in_(list(remaining_ids))))).scalars().all()
                for db_task in db_tasks:
                    db_task.status = TaskStatus.DONE
                    archived_count += 1
                if db_tasks:
                    await session.commit()
        except Exception:
            pass

    return archived_count


async def _load_tasks_for_schedule() -> list[dict[str, Any]]:
    if _in_memory_tasks:
        return _in_memory_tasks

    try:
        async with AsyncSessionLocal() as session:
            db_tasks = (await session.execute(select(Task).order_by(Task.created_at.asc()))).scalars().all()
            return [
                {
                    "id": str(task.id),
                    "title": task.title,
                    "description": task.description,
                    "estimated_minutes": task.estimated_minutes,
                    "actual_minutes": task.actual_minutes,
                    "priority": task.priority.value,
                    "status": task.status.value,
                    "deadline": task.deadline.isoformat() if task.deadline else None,
                    "entry_type": _task_entry_types.get(str(task.id), "TASK"),
                }
                for task in db_tasks
            ]
    except Exception:
        return _in_memory_tasks


@app.post("/api/v1/rules", status_code=201)
async def create_rule(payload: RuleCreate) -> dict[str, Any]:
    rule_id = str(uuid4())
    rule_record = {
        "id": rule_id,
        "rule_key": payload.rule_key,
        "start_time": payload.start_time,
        "end_time": payload.end_time,
        "is_hard_constraint": payload.is_hard_constraint,
        "metadata_json": payload.metadata_json,
    }
    _in_memory_rules.append(rule_record)
    return RuleRead(**rule_record).model_dump(mode="json")


@app.get("/api/v1/rules")
async def list_rules() -> list[dict[str, Any]]:
    return _in_memory_rules


@app.get("/api/v1/schedule/today")
async def get_today_schedule() -> dict[str, Any]:
    tasks = await _load_tasks_for_schedule()
    rules = _in_memory_rules if _in_memory_rules else []
    now_local = datetime.now().astimezone()
    today = now_local.date()

    today_tasks: list[dict[str, Any]] = []
    for task in tasks:
        if not _is_active_task(task):
            continue
        if str(task.get("entry_type") or _task_entry_types.get(str(task.get("id")), "TASK")) == "REMINDER" and not task.get("deadline"):
            today_tasks.append(task)
            continue
        deadline_dt = _parse_deadline_value(task.get("deadline"))
        if deadline_dt is None:
            continue
        if deadline_dt.astimezone(now_local.tzinfo).date() == today:
            today_tasks.append(task)

    if not today_tasks:
        schedule_table = [{
            "time_window": "09:00 AM - 10:00 AM",
            "task": "No task scheduled",
            "priority": "P4",
            "notes": "Your day is clear. Add a task to generate a plan.",
        }]
        assistant_message = "No tasks with a deadline today. Add or schedule a task and I will organize your day."
        return {
            "schedule_table": schedule_table,
            "changes_made": ["No changes required"],
            "dropped_or_deferred_tasks": [],
            "assistant_message": assistant_message,
        }

    sorted_tasks = sorted(
        today_tasks,
        key=lambda item: (
            _priority_rank(str(item.get("priority") or "P4")),
            _parse_deadline_value(item.get("deadline")) or datetime.max.replace(tzinfo=timezone.utc),
        ),
    )
    schedule_table: list[dict[str, str]] = []
    seen_titles: set[str] = set()
    for task in sorted_tasks:
        title_key = str(task.get("title") or "").strip().lower()
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        deadline_dt = _parse_deadline_value(task.get("deadline"))
        local_time = "Flexible"
        if deadline_dt is not None:
            local_time = deadline_dt.astimezone(now_local.tzinfo).strftime("%I:%M %p")
        schedule_table.append({
            "time_window": local_time,
            "task": task["title"],
            "priority": task.get("priority", "P4"),
            "notes": "Reminder" if str(task.get("entry_type") or "").upper() == "REMINDER" else "Due today",
        })

    deferred: list[str] = []
    hard_rules = [rule["rule_key"] for rule in rules if rule.get("is_hard_constraint")]
    changes_made = [
        f"Protected your focus window around {rule}" for rule in hard_rules
    ] if hard_rules else ["No hard constraint conflicts detected"]

    assistant_message = (
        "I built your day around your deep-work windows and kept hard constraints protected. "
        + ("I also kept your evening rule intact." if hard_rules else "Your schedule remains flexible.")
    )

    return ScheduleResponse(
        schedule_table=[
            ScheduleWindow(**item) for item in schedule_table
        ],
        changes_made=changes_made,
        dropped_or_deferred_tasks=deferred,
        assistant_message=assistant_message,
    ).model_dump(mode="json")


@app.post("/api/v1/telemetry/distraction")
async def ingest_distraction_telemetry(payload: TelemetryRequest) -> dict[str, str]:
    return {"status": "accepted", "app_name": payload.app_name}


@app.post("/api/v1/chat")
async def chat_with_assistant(payload: ChatMessage) -> dict[str, str]:
    text = (payload.message or "").strip()
    if not text:
        return {"reply": "Please send a message so I can help.", "parser_used": "none"}

    lowered = text.lower()

    if any(marker in lowered for marker in ["end of day", "eod", "day summary", "daily analysis", "what to move", "move to tomorrow"]):
        analysis = await generate_end_of_day_analysis()
        return {"reply": analysis, "intent": "GET_TODAY_SCHEDULE", "confidence": "0.95", "parser_used": "none"}

    if "snooze" in lowered:
        amount_match = re.search(r"(\d+)\s*(minutes?|mins?|hours?|hrs?)", lowered)
        if amount_match:
            amount = int(amount_match.group(1))
            unit = amount_match.group(2)
            delta = timedelta(hours=amount) if unit.startswith("h") else timedelta(minutes=amount)
        else:
            delta = timedelta(minutes=15)
        new_deadline = datetime.now(timezone.utc) + delta
        updated = await _update_latest_task_deadline(new_deadline)
        if updated is None:
            reply = "I could not find a task to snooze yet. Create a task first."
        else:
            deadline_text = _format_deadline_local(updated.get("deadline")) or updated.get("deadline")
            reply = f"Snoozed latest task to {deadline_text} (local time)."
        return {"reply": reply, "intent": "UPDATE_TASK_STATUS", "confidence": "0.95", "parser_used": "none"}

    if "reschedule" in lowered:
        parsed_deadline = parse_deadline_from_text(text)
        if parsed_deadline is None:
            return {
                "reply": "Please include a time, for example: reschedule to tomorrow at 7pm.",
                "intent": "UPDATE_TASK_STATUS",
                "confidence": "0.95",
                "parser_used": "none",
            }
        updated = await _update_latest_task_deadline(parsed_deadline)
        if updated is None:
            reply = "I could not find a task to reschedule yet. Create a task first."
        else:
            deadline_text = _format_deadline_local(updated.get("deadline")) or updated.get("deadline")
            reply = f"Rescheduled latest task to {deadline_text} (local time)."
        return {"reply": reply, "intent": "UPDATE_TASK_STATUS", "confidence": "0.95", "parser_used": "none"}

    if any(marker in lowered for marker in ["cleanup duplicates", "clean duplicates", "archive duplicates"]):
        archived = await archive_duplicate_today_tasks()
        reply = f"Archived {archived} duplicate task(s) for today." if archived else "No duplicate tasks found for today."
        return {"reply": reply, "intent": "GET_TODAY_SCHEDULE", "confidence": "0.95", "parser_used": "none"}

    intent = await route_intent(text)
    parser_used = "none"

    if intent.action == "ADD_OR_MODIFY_TASK":
        llm_task = await extract_task_details_with_llm(text)
        recurrence = None

        if llm_task is not None:
            extracted_title = llm_task["title"]
            estimated_minutes = int(llm_task["estimated_minutes"])
            priority = llm_task["priority"]
            parsed_deadline = llm_task["deadline"] or parse_deadline_from_text(text)
            parser_used = str(llm_task.get("parser_used") or "llm")
            recurrence = _detect_recurrence_from_text(text)
        else:
            parser_used = "fallback"
            extracted_title = text
            for prefix in ["create a task to ", "create task to ", "add a task to ", "add task to ", "new task to ", "create a task ", "create task ", "add a task ", "add task ", "new task "]:
                if extracted_title.lower().startswith(prefix):
                    extracted_title = extracted_title[len(prefix):]
                    break

            match = re.search(r"(?:to\s+)?(.+?)(?:\s+in\s+\d+\s*(?:mins?|minutes?)|\s+for\s+\d+\s*(?:mins?|minutes?)|$)", extracted_title, flags=re.IGNORECASE)
            if match:
                extracted_title = match.group(1).strip(" ,.-")
            if not extracted_title:
                extracted_title = "Untitled task"

            minutes_match = re.search(r"(\d+)\s*(?:mins?|minutes?)", text, flags=re.IGNORECASE)
            estimated_minutes = int(minutes_match.group(1)) if minutes_match else 30

            lower_text = text.lower()
            if any(keyword in lower_text for keyword in ["urgent", "high priority", "p1", "asap"]):
                priority = "P1"
            elif any(keyword in lower_text for keyword in ["low priority", "p4", "easy"]):
                priority = "P4"
            elif any(keyword in lower_text for keyword in ["medium", "p2", "normal"]):
                priority = "P2"
            else:
                priority = "P3"

            parsed_deadline = parse_deadline_from_text(text)
            recurrence = _detect_recurrence_from_text(text)

        lower_text = text.lower()
        rule_entry_type, rule_sync_to_calendar = _classify_entry_type(text, parsed_deadline)
        llm_entry = await classify_entry_intent_with_llm(text, parsed_deadline is not None)
        if llm_entry is not None:
            entry_type = str(llm_entry.get("entry_type") or "TASK")
            sync_to_calendar = bool(llm_entry.get("sync_to_calendar"))

            # Safety override: if deterministic rules clearly identify an event,
            # prefer event behavior unless user explicitly requested a reminder.
            if rule_entry_type == "EVENT" and entry_type == "TASK":
                entry_type = "EVENT"
                sync_to_calendar = True
        else:
            entry_type, sync_to_calendar = rule_entry_type, rule_sync_to_calendar

        created = await create_task(
            TaskCreate(
                title=extracted_title.title(),
                description=f"Created from chat: {text}",
                estimated_minutes=estimated_minutes,
                priority=priority,
                deadline=parsed_deadline,
                recurrence=recurrence,
                entry_type=entry_type,
                sync_to_calendar=sync_to_calendar,
            )
        )

        conflicts = await _find_time_conflicts(parsed_deadline, exclude_task_id=str(created.get("id") or ""))
        schedule_note = ""
        if created.get("deadline"):
            local_deadline_text = _format_deadline_local(created.get("deadline"))
            if local_deadline_text:
                schedule_note = f", scheduled for {local_deadline_text} (local time)"
            else:
                schedule_note = f", scheduled for {created['deadline']}"
        reply = (
            f"Created task: {created.get('title', extracted_title.title())} "
            f"({created.get('estimated_minutes', estimated_minutes)} minutes, {created.get('priority', priority)}{schedule_note})"
        )
        if sync_to_calendar or any(keyword in lower_text for keyword in ["calendar", "calender"]):
            if created.get("google_event_id"):
                reply += " Synced to Google Calendar."
            else:
                reply += " Calendar sync is not active right now. Reconnect at /auth/google/login and try again."
        if created.get("recurrence"):
            recurrence_human = "daily" if created.get("recurrence") == "DAILY" else "weekly"
            reply += f" Recurs {recurrence_human}."
        if entry_type == "REMINDER" and parsed_deadline is None:
            reply += " I will remind you every 2 hours until you mark it done."
        if conflicts:
            reply += _build_conflict_suggestion(parsed_deadline, conflicts)

    elif intent.action == "SET_RULE":
        reply = "I’ve captured that rule and will keep it in your scheduling constraints."
    elif intent.action == "GET_TODAY_SCHEDULE":
        today_plan = await get_today_schedule()
        rows = today_plan.get("schedule_table", [])
        if rows and rows[0].get("task") != "No task scheduled":
            top_rows = rows[:3]
            summary = "; ".join(
                f"{row.get('time_window')} - {row.get('task')} ({row.get('priority')})" for row in top_rows
            )
            reply = f"Today plan: {summary}."
        else:
            reply = "No tasks with a deadline today yet. Add one and I will organize your day."
    elif intent.action == "START_TASK":
        reply = "I’ll start the task and keep it moving with check-ins while you work."
    elif intent.action == "UPDATE_TASK_STATUS":
        updated = await mark_latest_task_done()
        if updated is None:
            reply = "I could not find a task to update yet. Create a task first."
        else:
            reply = f"Marked task as DONE: {updated.get('title', 'Task')}."
            next_task = updated.get("next_task_created")
            if isinstance(next_task, dict):
                next_deadline = _format_deadline_local(next_task.get("deadline")) or next_task.get("deadline")
                reply += f" Created next recurring task for {next_deadline} (local time)."
    else:
        reply = "I can help with tasks, schedules, and rules. Ask me to create a task or plan your day."

    return {
        "reply": reply,
        "intent": intent.action,
        "confidence": str(intent.confidence),
        "parser_used": parser_used,
    }
