import asyncio
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.schemas import WhatsAppWebhookEvent, WhatsAppVerificationQuery
from app.services.llm_engine import route_intent


def test_verification_query_validates() -> None:
    query = WhatsAppVerificationQuery(
        mode="subscribe",
        challenge="123",
        verify_token="secret-token",
    )

    assert query.mode == "subscribe"
    assert query.challenge == "123"
    assert query.verify_token == "secret-token"


def test_whatsapp_payload_parsing() -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "15551234567"},
                            "contacts": [{"profile": {"name": "Jane"}, "wa_id": "111"}],
                            "messages": [
                                {
                                    "from": "111",
                                    "id": "msg_1",
                                    "timestamp": "1700000000",
                                    "text": {"body": "Schedule my deep work"},
                                    "type": "text",
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }

    event = WhatsAppWebhookEvent.model_validate(payload)
    message = event.entry[0].changes[0].value.messages[0]

    assert message.from_number == "111"
    assert message.text.body == "Schedule my deep work"


def test_signature_generation_matches() -> None:
    payload = b'{"object":"whatsapp_business_account"}'
    secret = "test-secret"
    signature = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    assert signature.startswith("sha256=")
    assert len(signature) > 10


def test_webhook_verification_endpoint() -> None:
    client = TestClient(app)
    response = client.get(
        "/api/v1/webhook/whatsapp",
        params={"hub.mode": "subscribe", "hub.challenge": "123", "hub.verify_token": "secret-token"},
    )

    assert response.status_code == 200
    assert response.json()["challenge"] == 123


def test_webhook_accepts_empty_post_body() -> None:
    client = TestClient(app)
    response = client.post("/api/v1/webhook/whatsapp")

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_create_task_endpoint() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/v1/tasks",
        json={
            "title": "Write AI recap",
            "description": "Prepare the end-of-day summary",
            "estimated_minutes": 45,
            "priority": "P1",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["title"] == "Write AI recap"
    assert payload["status"] == "QUEUED"


def test_create_task_schedules_deadline_reminder(monkeypatch) -> None:
    import app.main as main_module

    captured: dict[str, object] = {}

    def fake_send_task(name, args=None, countdown=None):
        if name == "app.workers.tasks.deadline_task_reminder":
            captured["name"] = name
            captured["args"] = args
            captured["countdown"] = countdown
        return None

    monkeypatch.setattr("app.main.celery_app.send_task", fake_send_task)

    future_deadline = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    client = TestClient(app)
    response = client.post(
        "/api/v1/tasks",
        json={
            "title": "Send report",
            "description": "Email the weekly report",
            "estimated_minutes": 20,
            "priority": "P2",
            "deadline": future_deadline,
        },
    )

    assert response.status_code == 201
    assert captured["name"] == "app.workers.tasks.deadline_task_reminder"
    assert isinstance(captured["args"], list)
    assert captured["args"][1] == "Send report"
    assert int(captured["countdown"]) > 0


def test_create_task_without_deadline_does_not_schedule_deadline_reminder(monkeypatch) -> None:
    calls: list[str] = []

    def fake_send_task(name, args=None, countdown=None):
        calls.append(name)
        return None

    monkeypatch.setattr("app.main.celery_app.send_task", fake_send_task)

    client = TestClient(app)
    response = client.post(
        "/api/v1/tasks",
        json={
            "title": "Clean inbox",
            "description": "Triage email",
            "estimated_minutes": 15,
            "priority": "P3",
        },
    )

    assert response.status_code == 201
    assert "app.workers.tasks.deadline_task_reminder" not in calls


def test_task_creation_creates_google_calendar_event(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_post(url, json=None, headers=None, timeout=None):
        assert url == "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        assert headers["Authorization"] == "Bearer google-access-token"
        return FakeResponse({"id": "event-123"})

    import app.main as main_module
    main_module._google_tokens.clear()
    main_module._google_tokens["access_token"] = "google-access-token"
    monkeypatch.setattr("app.main.httpx.post", fake_post)

    client = TestClient(app)
    response = client.post(
        "/api/v1/tasks",
        json={
            "title": "Plan sprint review",
            "description": "Prepare the quarterly sprint review",
            "estimated_minutes": 30,
            "priority": "P2",
            "sync_to_calendar": True,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["google_event_id"] == "event-123"
    main_module._google_tokens.clear()


def test_chat_no_time_remind_message_schedules_recurring_nudges(monkeypatch) -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    captured: dict[str, Any] = {"tasks": []}

    def fake_send_task(name, args=None, countdown=None):
        captured["tasks"].append((name, args, countdown))
        return None

    monkeypatch.setattr("app.main.celery_app.send_task", fake_send_task)

    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"message": "remind me to drink water"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert "every 2 hours" in payload["reply"].lower()
    assert any(name == "app.workers.tasks.recurring_task_reminder" and countdown == 7200 for name, _, countdown in captured["tasks"])


def test_chat_timed_reminder_is_not_calendar_event(monkeypatch) -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    called = {"calendar_post": 0}

    def fake_post(url, json=None, headers=None, timeout=None):
        if "calendar" in url:
            called["calendar_post"] += 1
        return type("Resp", (), {"raise_for_status": lambda self: None, "json": lambda self: {"id": "event-x"}})()

    monkeypatch.setattr("app.main.httpx.post", fake_post)

    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"message": "remind me about the meeting with rahul at 3pm today"},
    )

    assert response.status_code == 200
    assert called["calendar_post"] == 0


def test_chat_explicit_calendar_event_syncs(monkeypatch) -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    main_module._google_tokens.clear()
    main_module._google_tokens["access_token"] = "google-access-token"
    called = {"calendar_post": 0}

    def fake_post(url, json=None, headers=None, timeout=None):
        if "calendar" in url:
            called["calendar_post"] += 1
        return type("Resp", (), {"raise_for_status": lambda self: None, "json": lambda self: {"id": "event-evt"}})()

    monkeypatch.setattr("app.main.httpx.post", fake_post)

    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"message": "create event for design review at 4pm today in calendar"},
    )

    assert response.status_code == 200
    assert called["calendar_post"] >= 1


def test_chat_infers_event_for_timed_activity_without_calendar_keyword(monkeypatch) -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    main_module._google_tokens.clear()
    main_module._google_tokens["access_token"] = "google-access-token"
    called = {"calendar_post": 0}

    def fake_post(url, json=None, headers=None, timeout=None):
        if "calendar" in url:
            called["calendar_post"] += 1
        return type("Resp", (), {"raise_for_status": lambda self: None, "json": lambda self: {"id": "event-auto"}})()

    monkeypatch.setattr("app.main.httpx.post", fake_post)
    async def fake_classify(_text, _has_time):
        return None

    monkeypatch.setattr("app.main.classify_entry_intent_with_llm", fake_classify)

    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"message": "create a gym time today at 7pm"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert called["calendar_post"] >= 1
    assert "Synced to Google Calendar" in payload["reply"]


def test_chat_timed_activity_event_override_when_llm_says_task(monkeypatch) -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    main_module._google_tokens.clear()
    main_module._google_tokens["access_token"] = "google-access-token"
    called = {"calendar_post": 0}

    def fake_post(url, json=None, headers=None, timeout=None):
        if "calendar" in url:
            called["calendar_post"] += 1
        return type("Resp", (), {"raise_for_status": lambda self: None, "json": lambda self: {"id": "event-override"}})()

    async def fake_classify(_text, _has_time):
        return {"entry_type": "TASK", "sync_to_calendar": False}

    monkeypatch.setattr("app.main.httpx.post", fake_post)
    monkeypatch.setattr("app.main.classify_entry_intent_with_llm", fake_classify)

    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"message": "create a gym time today at 7pm"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert called["calendar_post"] >= 1
    assert "Synced to Google Calendar" in payload["reply"]


def test_telemetry_endpoint_accepts_payload() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/v1/telemetry/distraction",
        json={"app_name": "YouTube", "timestamp": "2026-08-17T15:30:00Z"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


def test_rule_endpoint_creates_constraint() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/v1/rules",
        json={"rule_key": "EATING_CURFEW", "start_time": "19:00:00", "end_time": "20:00:00", "is_hard_constraint": True},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["rule_key"] == "EATING_CURFEW"
    assert payload["is_hard_constraint"] is True


def test_schedule_endpoint_returns_plan() -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    client = TestClient(app)
    client.post(
        "/api/v1/tasks",
        json={
            "title": "Deep work session",
            "description": "Build the assistant engine",
            "estimated_minutes": 90,
            "priority": "P1",
        },
    )
    response = client.get("/api/v1/schedule/today")

    assert response.status_code == 200
    payload = response.json()
    assert "schedule_table" in payload
    assert len(payload["schedule_table"]) > 0
    assert payload["assistant_message"]


def test_schedule_endpoint_returns_today_tasks_only() -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    now = datetime.now(timezone.utc)

    client = TestClient(app)
    client.post(
        "/api/v1/tasks",
        json={
            "title": "Today task",
            "description": "Must do today",
            "estimated_minutes": 30,
            "priority": "P2",
            "deadline": (now + timedelta(hours=2)).isoformat(),
        },
    )
    client.post(
        "/api/v1/tasks",
        json={
            "title": "Tomorrow task",
            "description": "Do tomorrow",
            "estimated_minutes": 30,
            "priority": "P1",
            "deadline": (now + timedelta(days=1, hours=2)).isoformat(),
        },
    )

    response = client.get("/api/v1/schedule/today")
    assert response.status_code == 200
    payload = response.json()
    task_titles = [row["task"] for row in payload["schedule_table"]]
    assert "Today task" in task_titles
    assert "Tomorrow task" not in task_titles


def test_schedule_endpoint_empty_day_message() -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    client = TestClient(app)
    response = client.get("/api/v1/schedule/today")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schedule_table"][0]["task"] == "No task scheduled"


def test_schedule_endpoint_prioritizes_today_tasks() -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    now = datetime.now(timezone.utc)
    common_deadline = (now + timedelta(hours=3)).isoformat()

    client = TestClient(app)
    client.post(
        "/api/v1/tasks",
        json={
            "title": "Priority low",
            "description": "Low priority",
            "estimated_minutes": 25,
            "priority": "P4",
            "deadline": common_deadline,
        },
    )
    client.post(
        "/api/v1/tasks",
        json={
            "title": "Priority high",
            "description": "High priority",
            "estimated_minutes": 25,
            "priority": "P1",
            "deadline": common_deadline,
        },
    )
    client.post(
        "/api/v1/tasks",
        json={
            "title": "Priority medium",
            "description": "Medium priority",
            "estimated_minutes": 25,
            "priority": "P2",
            "deadline": common_deadline,
        },
    )

    response = client.get("/api/v1/schedule/today")
    assert response.status_code == 200
    payload = response.json()
    priorities = [row["priority"] for row in payload["schedule_table"]]
    assert priorities[:3] == ["P1", "P2", "P4"]


def test_start_task_endpoint_sets_active_state() -> None:
    client = TestClient(app)
    create_response = client.post(
        "/api/v1/tasks",
        json={
            "title": "Write project summary",
            "description": "Draft summary for the team",
            "estimated_minutes": 30,
            "priority": "P2",
        },
    )
    task_id = create_response.json()["id"]

    response = client.post(f"/api/v1/tasks/{task_id}/start")

    assert response.status_code == 200
    assert response.json()["status"] == "IN_PROGRESS"


def test_update_task_status_endpoint() -> None:
    client = TestClient(app)
    create_response = client.post(
        "/api/v1/tasks",
        json={
            "title": "Prepare milestone readout",
            "description": "Create readout deck",
            "estimated_minutes": 60,
            "priority": "P3",
        },
    )
    task_id = create_response.json()["id"]

    response = client.patch(
        f"/api/v1/tasks/{task_id}/status",
        json={"status": "DONE", "actual_minutes": 45},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "DONE"
    assert payload["actual_minutes"] == 45


def test_update_task_endpoint() -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    client = TestClient(app)
    create_response = client.post(
        "/api/v1/tasks",
        json={
            "title": "Draft mail",
            "description": "Send draft",
            "estimated_minutes": 20,
            "priority": "P3",
        },
    )
    task_id = create_response.json()["id"]

    response = client.put(
        f"/api/v1/tasks/{task_id}",
        json={
            "title": "Draft client mail",
            "estimated_minutes": 35,
            "priority": "P2",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["title"] == "Draft client mail"
    assert payload["estimated_minutes"] == 35
    assert payload["priority"] == "P2"


def test_delete_task_endpoint() -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    client = TestClient(app)
    create_response = client.post(
        "/api/v1/tasks",
        json={
            "title": "Delete me",
            "description": "To be removed",
            "estimated_minutes": 10,
            "priority": "P4",
        },
    )
    task_id = create_response.json()["id"]

    delete_response = client.delete(f"/api/v1/tasks/{task_id}")
    assert delete_response.status_code == 200
    assert delete_response.json()["status"] == "deleted"

    tasks_response = client.get("/api/v1/tasks")
    tasks_payload = tasks_response.json()
    assert all(task["id"] != task_id for task in tasks_payload)


def test_update_task_syncs_google_calendar_event(monkeypatch) -> None:
    class FakeResponse:
        def raise_for_status(self):
            return None

    import app.main as main_module

    main_module._in_memory_tasks.clear()
    main_module._task_calendar_events.clear()
    main_module._google_tokens.clear()
    main_module._google_tokens["access_token"] = "google-access-token"

    captured: dict[str, Any] = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        return type("Resp", (), {"raise_for_status": lambda self: None, "json": lambda self: {"id": "event-999"}})()

    def fake_patch(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr("app.main.httpx.post", fake_post)
    monkeypatch.setattr("app.main.httpx.patch", fake_patch)

    client = TestClient(app)
    create_response = client.post(
        "/api/v1/tasks",
        json={
            "title": "Prepare notes",
            "description": "Initial",
            "estimated_minutes": 25,
            "priority": "P3",
            "sync_to_calendar": True,
            "entry_type": "EVENT",
        },
    )
    task_id = create_response.json()["id"]

    update_response = client.put(
        f"/api/v1/tasks/{task_id}",
        json={"title": "Prepare final notes", "estimated_minutes": 40},
    )
    assert update_response.status_code == 200
    assert captured["url"].endswith("/events/event-999")
    assert captured["json"]["summary"] == "Prepare final notes"


def test_delete_task_syncs_google_calendar_event(monkeypatch) -> None:
    class FakeResponse:
        def raise_for_status(self):
            return None

    import app.main as main_module

    main_module._in_memory_tasks.clear()
    main_module._task_calendar_events.clear()
    main_module._google_tokens.clear()
    main_module._google_tokens["access_token"] = "google-access-token"

    captured: dict[str, str] = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        return type("Resp", (), {"raise_for_status": lambda self: None, "json": lambda self: {"id": "event-del"}})()

    def fake_delete(url, headers=None, timeout=None):
        captured["url"] = url
        return FakeResponse()

    monkeypatch.setattr("app.main.httpx.post", fake_post)
    monkeypatch.setattr("app.main.httpx.delete", fake_delete)

    client = TestClient(app)
    create_response = client.post(
        "/api/v1/tasks",
        json={
            "title": "Remove later",
            "description": "Cleanup",
            "estimated_minutes": 15,
            "priority": "P4",
            "sync_to_calendar": True,
            "entry_type": "EVENT",
        },
    )
    task_id = create_response.json()["id"]

    delete_response = client.delete(f"/api/v1/tasks/{task_id}")
    assert delete_response.status_code == 200
    assert captured["url"].endswith("/events/event-del")


def test_update_task_without_event_mapping_is_still_successful() -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    main_module._task_calendar_events.clear()

    client = TestClient(app)
    create_response = client.post(
        "/api/v1/tasks",
        json={
            "title": "No calendar map",
            "description": "Local only",
            "estimated_minutes": 20,
            "priority": "P2",
        },
    )
    task_id = create_response.json()["id"]

    update_response = client.put(
        f"/api/v1/tasks/{task_id}",
        json={"title": "No calendar map updated"},
    )
    assert update_response.status_code == 200
    assert update_response.json()["title"] == "No calendar map updated"


def test_intent_routing_uses_start_task_action() -> None:
    intent = asyncio.run(route_intent("Start the product brief and remind me in 30 minutes"))

    assert intent.action == "START_TASK"
    assert intent.confidence > 0.5


def test_intent_routing_uses_rule_action() -> None:
    intent = asyncio.run(route_intent("Set a curfew at 7 PM and workout at 6 AM"))

    assert intent.action == "SET_RULE"
    assert intent.confidence > 0.5


def test_chat_endpoint_handles_task_request() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"message": "Create a task to finish the project brief in 45 minutes"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert "reply" in payload
    assert "Created task" in payload["reply"]
    assert "project brief" in payload["reply"].lower()
    assert payload["parser_used"] == "fallback"


def test_chat_update_status_marks_latest_task_done() -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    client = TestClient(app)
    client.post(
        "/api/v1/tasks",
        json={
            "title": "Finish summary",
            "description": "Complete daily summary",
            "estimated_minutes": 30,
            "priority": "P2",
        },
    )

    response = client.post(
        "/api/v1/chat",
        json={"message": "done with it"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert "Marked task as DONE" in payload["reply"]

    tasks_response = client.get("/api/v1/tasks")
    tasks_payload = tasks_response.json()
    assert tasks_payload[-1]["status"] == "DONE"


def test_chat_done_creates_next_recurring_task() -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    main_module._task_recurrence_rules.clear()

    client = TestClient(app)
    create_response = client.post(
        "/api/v1/chat",
        json={"message": "create a task to practice coding daily at 7pm"},
    )
    assert create_response.status_code == 200
    assert "Recurs daily" in create_response.json()["reply"]

    done_response = client.post("/api/v1/chat", json={"message": "done"})
    assert done_response.status_code == 200
    assert "Created next recurring task" in done_response.json()["reply"]

    tasks_response = client.get("/api/v1/tasks")
    tasks_payload = tasks_response.json()
    assert len(tasks_payload) >= 2


def test_chat_snooze_latest_task() -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    client = TestClient(app)
    client.post(
        "/api/v1/tasks",
        json={
            "title": "Write notes",
            "description": "Study",
            "estimated_minutes": 30,
            "priority": "P2",
            "deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
    )

    response = client.post("/api/v1/chat", json={"message": "snooze 20 minutes"})
    assert response.status_code == 200
    payload = response.json()
    assert "Snoozed latest task" in payload["reply"]


def test_chat_reschedule_latest_task() -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    client = TestClient(app)
    client.post(
        "/api/v1/tasks",
        json={
            "title": "Client call",
            "description": "Call",
            "estimated_minutes": 30,
            "priority": "P2",
            "deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
    )

    response = client.post("/api/v1/chat", json={"message": "reschedule to tomorrow at 9am"})
    assert response.status_code == 200
    payload = response.json()
    assert "Rescheduled latest task" in payload["reply"]


def test_chat_cleanup_duplicates_archives_today_duplicates() -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    now = datetime.now(timezone.utc)
    deadline = (now + timedelta(hours=3)).isoformat()

    client = TestClient(app)
    client.post(
        "/api/v1/tasks",
        json={
            "title": "Review docs",
            "description": "A",
            "estimated_minutes": 20,
            "priority": "P2",
            "deadline": deadline,
        },
    )
    client.post(
        "/api/v1/tasks",
        json={
            "title": "Review docs",
            "description": "B",
            "estimated_minutes": 25,
            "priority": "P3",
            "deadline": deadline,
        },
    )

    response = client.post("/api/v1/chat", json={"message": "cleanup duplicates"})
    assert response.status_code == 200
    assert "Archived" in response.json()["reply"]

    schedule_response = client.get("/api/v1/schedule/today")
    schedule_payload = schedule_response.json()
    titles = [row["task"] for row in schedule_payload["schedule_table"]]
    assert titles.count("Review docs") == 1


def test_chat_shows_conflict_suggestion_for_overlapping_time() -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    client = TestClient(app)
    client.post(
        "/api/v1/chat",
        json={"message": "create event for design review at 7pm today in calendar"},
    )

    response = client.post(
        "/api/v1/chat",
        json={"message": "create event for gym time today at 7pm"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert "Potential conflict with:" in payload["reply"]
    assert "Suggestion:" in payload["reply"]


def test_chat_end_of_day_analysis_summary() -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    now = datetime.now(timezone.utc)
    client = TestClient(app)

    first = client.post(
        "/api/v1/tasks",
        json={
            "title": "Wrap report",
            "description": "complete summary",
            "estimated_minutes": 30,
            "priority": "P1",
            "deadline": (now + timedelta(hours=1)).isoformat(),
        },
    )
    task_id = first.json()["id"]

    client.patch(f"/api/v1/tasks/{task_id}/status", json={"status": "DONE", "actual_minutes": 20})

    client.post(
        "/api/v1/tasks",
        json={
            "title": "Prepare tomorrow notes",
            "description": "pending item",
            "estimated_minutes": 20,
            "priority": "P2",
            "deadline": (now + timedelta(hours=2)).isoformat(),
        },
    )

    response = client.post("/api/v1/chat", json={"message": "end of day analysis"})
    assert response.status_code == 200
    payload = response.json()
    assert "End-of-day analysis:" in payload["reply"]
    assert "completed" in payload["reply"]
    assert "Suggested move to tomorrow:" in payload["reply"]


def test_chat_endpoint_parses_tomorrow_time() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"message": "Create a task to gym session tomorrow at 7pm for 60 minutes"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert "reply" in payload
    assert "scheduled for" in payload["reply"].lower()


def test_chat_task_confirmation_mentions_local_time() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"message": "Create a task to gym session tomorrow at 7pm for 60 minutes"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert "(local time)" in payload["reply"]


def test_chat_endpoint_parses_tomorrw_typo_time() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"message": "Create a interview session for tomorrw at 2pm"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert "reply" in payload
    assert "scheduled for" in payload["reply"].lower()


def test_chat_endpoint_parses_relative_in_minutes() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"message": "Create a task to finish project brief in 45 minutes"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert "scheduled for" in payload["reply"].lower()


def test_chat_endpoint_parses_by_tomorrow_without_time() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"message": "I need to submit my resume to Sharan by tomorrow remind me"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert "scheduled for" in payload["reply"].lower()


def test_chat_schedule_reply_lists_today_plan() -> None:
    import app.main as main_module

    main_module._in_memory_tasks.clear()
    now = datetime.now(timezone.utc)

    client = TestClient(app)
    client.post(
        "/api/v1/tasks",
        json={
            "title": "Review roadmap",
            "description": "Review today's plan",
            "estimated_minutes": 40,
            "priority": "P1",
            "deadline": (now + timedelta(hours=2)).isoformat(),
        },
    )

    response = client.post("/api/v1/chat", json={"message": "give me tasks for today"})
    assert response.status_code == 200
    payload = response.json()
    assert "Today plan:" in payload["reply"]
    assert "Review roadmap" in payload["reply"]


def test_chat_calendar_event_uses_parsed_deadline(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    captured_payload: dict[str, object] = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured_payload["event"] = json
        return FakeResponse({"id": "event-456"})

    import app.main as main_module

    message = "Create event for gym session tomorrow at 7pm for 60 minutes in calendar"
    expected_start = main_module.parse_deadline_from_text(message).isoformat()

    main_module._google_tokens.clear()
    main_module._google_tokens["access_token"] = "google-access-token"
    monkeypatch.setattr("app.main.httpx.post", fake_post)

    client = TestClient(app)
    response = client.post("/api/v1/chat", json={"message": message})

    assert response.status_code == 200
    assert captured_payload["event"]["start"]["dateTime"] == expected_start
    main_module._google_tokens.clear()


def test_google_oauth_login_redirects_to_google() -> None:
    client = TestClient(app)
    response = client.get("/auth/google/login", follow_redirects=False)

    assert response.status_code == 302
    location = response.headers.get("location", "")
    assert "accounts.google.com" in location
    assert "client_id=" in location
    assert "redirect_uri=" in location


def test_google_oauth_callback_exchange_and_store_tokens(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_post(url, data=None, headers=None, timeout=None):
        assert url == "https://oauth2.googleapis.com/token"
        assert data["code"] == "test-code"
        assert data["grant_type"] == "authorization_code"
        return FakeResponse({
            "access_token": "google-access-token",
            "refresh_token": "google-refresh-token",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "https://www.googleapis.com/auth/calendar",
        })

    monkeypatch.setattr("app.main.httpx.post", fake_post)

    client = TestClient(app)
    response = client.get("/auth/google/callback?code=test-code&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcalendar")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "google_oauth_connected"
    assert payload["access_token"] == "google-access-token"
    assert payload["refresh_token"] == "google-refresh-token"


def test_google_oauth_status_endpoint() -> None:
    import app.main as main_module

    main_module._google_tokens.clear()
    main_module._google_tokens.update({
        "access_token": "token",
        "refresh_token": "refresh",
        "expires_at": "2099-01-01T00:00:00+00:00",
    })

    client = TestClient(app)
    response = client.get("/auth/google/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["connected"] is True
    assert payload["access_token_present"] is True
    assert payload["refresh_token_present"] is True


def test_google_oauth_callback_invalid_grant_with_existing_tokens(monkeypatch) -> None:
    class FailingResponse:
        def raise_for_status(self):
            request = httpx.Request("POST", "https://oauth2.googleapis.com/token")
            response = httpx.Response(
                400,
                request=request,
                json={"error": "invalid_grant", "error_description": "Bad Request"},
            )
            raise httpx.HTTPStatusError("Bad Request", request=request, response=response)

    def fake_post(url, data=None, timeout=None):
        return FailingResponse()

    import app.main as main_module

    main_module._google_tokens.clear()
    main_module._google_tokens.update({
        "access_token": "existing-access",
        "refresh_token": "existing-refresh",
    })
    monkeypatch.setattr("app.main.httpx.post", fake_post)

    client = TestClient(app)
    response = client.get("/auth/google/callback?code=already-used-code")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "google_oauth_already_connected"


def test_chat_calendar_request_without_token_shows_hint() -> None:
    import app.main as main_module

    main_module._google_tokens.clear()
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"message": "add a meeting with friend tomorrow at 3pm to calendar"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert "calendar sync is not active" in payload["reply"].lower()


def test_chat_endpoint_uses_llm_extracted_task_details(monkeypatch) -> None:
    import app.main as main_module

    async def fake_extract(_: str):
        return {
            "title": "Interview Prep Session",
            "estimated_minutes": 50,
            "priority": "P1",
            "deadline": None,
        }

    monkeypatch.setattr("app.main.extract_task_details_with_llm", fake_extract)

    client = TestClient(app)
    response = client.post(
        "/api/v1/chat",
        json={"message": "plz add intreview prep tomrw evening for 50 mins"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert "Interview Prep Session" in payload["reply"]
    assert "P1" in payload["reply"]
    assert payload["parser_used"] == "llm"


def test_intent_routing_create_task_with_today_is_task_action() -> None:
    intent = asyncio.run(route_intent("create a task to talk to anna catherene at 7pm today"))

    assert intent.action == "ADD_OR_MODIFY_TASK"


def test_intent_routing_give_me_tasks_for_today_is_schedule_action() -> None:
    intent = asyncio.run(route_intent("give me the task fro today"))

    assert intent.action == "GET_TODAY_SCHEDULE"
