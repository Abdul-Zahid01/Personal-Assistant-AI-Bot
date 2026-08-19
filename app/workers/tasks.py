from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from celery import shared_task

from app.services.llm_engine import route_intent


@shared_task(ignore_result=True)
def process_whatsapp_message(payload: dict[str, Any]) -> None:
    message_text = ""
    try:
        entry = payload.get("entry", [{}])[0]
        changes = entry.get("changes", [])
        for change in changes:
            value = change.get("value", {})
            for message in value.get("messages", []):
                if "text" in message:
                    message_text = message["text"].get("body", "")
                    break
    except (IndexError, AttributeError, TypeError):
        message_text = ""

    if message_text:
        import asyncio

        asyncio.run(route_intent(message_text))


@shared_task(ignore_result=True)
def checkin_task_timer(task_id: str, minutes: int) -> None:
    scheduled_time = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    print(f"Task {task_id} check-in scheduled for {scheduled_time.isoformat()}")
    return {
        "task_id": task_id,
        "minutes": minutes,
        "scheduled_for": scheduled_time.isoformat(),
        "message": "Reminder trigger fired. Please confirm task status.",
    }


@shared_task(ignore_result=True)
def deadline_task_reminder(task_id: str, title: str, deadline_iso: str) -> dict[str, str]:
    now = datetime.now(timezone.utc).isoformat()
    print(f"Reminder: task '{title}' ({task_id}) is approaching deadline {deadline_iso}")
    return {
        "task_id": task_id,
        "title": title,
        "deadline": deadline_iso,
        "triggered_at": now,
        "message": "Task deadline reminder fired.",
    }


@shared_task(ignore_result=True)
def recurring_task_reminder(task_id: str, title: str, cycle: int = 1) -> dict[str, str]:
    now = datetime.now(timezone.utc).isoformat()
    print(f"Recurring reminder #{cycle}: complete task '{title}' ({task_id})")

    # Cap reminder loops for safety in this scaffolded setup.
    if cycle < 24:
        from app.workers.celery_app import celery_app

        celery_app.send_task(
            "app.workers.tasks.recurring_task_reminder",
            args=[task_id, title, cycle + 1],
            countdown=2 * 60 * 60,
        )

    return {
        "task_id": task_id,
        "title": title,
        "cycle": str(cycle),
        "triggered_at": now,
        "message": "Recurring task reminder fired.",
    }
