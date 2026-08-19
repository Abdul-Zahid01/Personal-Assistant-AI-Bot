from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx

from app.config import settings
from app.schemas import LLMIntent, ScheduleResponse


def _normalize_extracted_task(extracted: dict[str, Any]) -> dict[str, Any] | None:
    title = str(extracted.get("title") or "").strip()
    if not title:
        return None

    try:
        estimated_minutes = int(extracted.get("estimated_minutes") or 30)
    except (TypeError, ValueError):
        estimated_minutes = 30
    estimated_minutes = max(1, min(estimated_minutes, 720))

    priority = str(extracted.get("priority") or "P3").upper()
    if priority not in {"P1", "P2", "P3", "P4"}:
        priority = "P3"

    deadline = None
    deadline_iso = extracted.get("deadline_iso")
    if isinstance(deadline_iso, str) and deadline_iso.strip():
        try:
            deadline = datetime.fromisoformat(deadline_iso.replace("Z", "+00:00"))
        except ValueError:
            deadline = None

    return {
        "title": title,
        "estimated_minutes": estimated_minutes,
        "priority": priority,
        "deadline": deadline,
        "parser_used": str(extracted.get("__provider") or "llm"),
    }


async def _extract_with_openai(message: str, prompt: str) -> dict[str, Any] | None:
    if not settings.OPENAI_API_KEY:
        return None

    try:
        response = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.OPENAI_MODEL,
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": message},
                ],
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        extracted = json.loads(content)
        if not isinstance(extracted, dict):
            return None
        extracted["__provider"] = "openai"
        return extracted
    except Exception:
        return None


async def _extract_with_gemini(message: str, prompt: str) -> dict[str, Any] | None:
    if not settings.GEMINI_API_KEY:
        return None

    model = settings.GEMINI_MODEL or "gemini-1.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    try:
        response = httpx.post(
            url,
            params={"key": settings.GEMINI_API_KEY},
            headers={"Content-Type": "application/json"},
            json={
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                },
                "contents": [
                    {
                        "parts": [
                            {
                                "text": (
                                    f"{prompt}\n"
                                    "Return only JSON with keys: title, estimated_minutes, priority, deadline_iso.\n"
                                    f"User message: {message}"
                                )
                            }
                        ]
                    }
                ],
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        text = (
            payload.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )
        extracted = json.loads(text)
        if not isinstance(extracted, dict):
            return None
        extracted["__provider"] = "gemini"
        return extracted
    except Exception:
        return None


async def extract_task_details_with_llm(message: str) -> dict[str, Any] | None:
    prompt = (
        "Extract task fields from the user message. Return JSON only with keys: "
        "title (string), estimated_minutes (int), priority (P1|P2|P3|P4), "
        "deadline_iso (ISO8601 string or null). "
        "If the message has no clear duration, use 30. "
        "If no clear priority, use P3. "
        "If no clear time, set deadline_iso to null."
    )
    provider = (settings.LLM_PROVIDER or "openai").strip().lower()

    extracted: dict[str, Any] | None = None
    if provider == "gemini":
        extracted = await _extract_with_gemini(message, prompt)
        if extracted is None:
            extracted = await _extract_with_openai(message, prompt)
    else:
        extracted = await _extract_with_openai(message, prompt)
        if extracted is None:
            extracted = await _extract_with_gemini(message, prompt)

    if not isinstance(extracted, dict):
        return None

    return _normalize_extracted_task(extracted)


async def classify_entry_intent_with_llm(message: str, has_time: bool) -> dict[str, Any] | None:
    prompt = (
        "Classify whether the user intends a TASK, REMINDER, or EVENT. "
        "Return JSON only with keys: entry_type, sync_to_calendar. "
        "entry_type must be one of TASK, REMINDER, EVENT. "
        "sync_to_calendar must be true only for EVENT. "
        "Rules: "
        "1) If user says 'remind me' or clearly asks reminder behavior, classify as REMINDER and sync_to_calendar=false. "
        "2) If user is creating/scheduling a timed activity like meeting/gym/call/appointment and intent sounds like calendar placement, classify EVENT and sync_to_calendar=true. "
        "3) If user explicitly mentions calendar/event, classify EVENT and sync_to_calendar=true. "
        "4) Plain task tracking intent should be TASK and sync_to_calendar=false. "
    )

    provider = (settings.LLM_PROVIDER or "openai").strip().lower()
    extracted: dict[str, Any] | None = None
    user_message = f"has_time={has_time}; message={message}"

    if provider == "gemini":
        extracted = await _extract_with_gemini(user_message, prompt)
        if extracted is None:
            extracted = await _extract_with_openai(user_message, prompt)
    else:
        extracted = await _extract_with_openai(user_message, prompt)
        if extracted is None:
            extracted = await _extract_with_gemini(user_message, prompt)

    if not isinstance(extracted, dict):
        return None

    entry_type = str(extracted.get("entry_type") or "").upper().strip()
    if entry_type not in {"TASK", "REMINDER", "EVENT"}:
        return None

    sync_to_calendar = bool(extracted.get("sync_to_calendar"))
    if entry_type != "EVENT":
        sync_to_calendar = False

    return {
        "entry_type": entry_type,
        "sync_to_calendar": sync_to_calendar,
    }


async def route_intent(message: str) -> LLMIntent:
    normalized = message.strip().lower()
    compact = " ".join(normalized.split())

    create_markers = [
        "create a task",
        "create task",
        "add a task",
        "add task",
        "new task",
        "remind me to",
    ]
    schedule_markers = ["schedule", "plan my day", "what's on my schedule", "what is on my schedule", "today"]
    query_markers = ["give", "show", "what", "list", "get"]

    is_task_create = any(marker in compact for marker in create_markers)
    is_schedule_query = any(marker in compact for marker in schedule_markers) and any(
        marker in compact for marker in query_markers
    )

    # Handle "task ... today" ask phrasing (including common typo "fro" for "for").
    if not is_task_create and not is_schedule_query:
        if ("task" in compact or "tasks" in compact) and (" today" in compact or " fro today" in compact or " for today" in compact):
            if any(marker in compact for marker in query_markers):
                is_schedule_query = True

    if any(keyword in normalized for keyword in ["curfew", "workout", "rule", "no eating", "eating curfew"]):
        action = "SET_RULE"
    elif is_task_create:
        action = "ADD_OR_MODIFY_TASK"
    elif is_schedule_query or any(keyword in normalized for keyword in ["schedule", "plan my day", "what's on my schedule"]):
        action = "GET_TODAY_SCHEDULE"
    elif any(keyword in normalized for keyword in ["start", "begin", "launch", "go ahead with"]):
        action = "START_TASK"
    elif any(keyword in normalized for keyword in ["done", "progress", "percent", "%", "completed", "finished"]):
        action = "UPDATE_TASK_STATUS"
    elif any(keyword in normalized for keyword in ["task", "add", "remind", "create", "new task", "todo"]):
        action = "ADD_OR_MODIFY_TASK"
    else:
        action = "GENERAL_QUERY"

    confidence = 0.95 if action != "GENERAL_QUERY" else 0.7
    return LLMIntent(action=action, confidence=confidence, raw_text=message, details={"message": message})


async def generate_schedule_context() -> ScheduleResponse:
    return ScheduleResponse(
        schedule_table=[
            {
                "time_window": "09:15 AM - 10:30 AM",
                "task": "Deep work block",
                "priority": "P1",
                "notes": "High cognitive load",
            }
        ],
        changes_made=["Protected the morning focus block"],
        dropped_or_deferred_tasks=["Optional admin cleanup pushed to tomorrow"],
        assistant_message="I preserved your highest-value work in the morning and moved lower-priority admin work to tomorrow.",
    )
