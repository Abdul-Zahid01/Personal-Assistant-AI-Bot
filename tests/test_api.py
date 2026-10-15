from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.container import build_container
from app.db.models import Task
from app.integrations.google_calendar import GoogleCalendar, make_state
from app.main import create_app
from tests.conftest import TODAY, FakeCalendar, ScriptedLLM, at, call, say

TOMORROW = TODAY + timedelta(days=1)


@pytest.fixture
async def client(container, settings):
    app = create_app(settings, container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


API = "/api/v1"


# ---------------------------------------------------------------------- auth & basics
async def test_health_is_public_and_reports_components(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": True, "llm": ["rules"], "telegram": False, "google_calendar": False}


async def test_index_page_is_served(client):
    r = await client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


async def test_api_key_is_enforced_when_configured(settings, clock, notifier):
    s = settings.model_copy(update={"API_KEY": "sekret"})
    c = build_container(s, clock=clock, notifier=notifier)
    await c.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(s, c)), base_url="http://t") as client:
            assert (await client.get(f"{API}/tasks")).status_code == 401
            assert (await client.get(f"{API}/tasks", headers={"X-API-Key": "wrong"})).status_code == 401
            assert (await client.get(f"{API}/tasks", headers={"X-API-Key": "sekret"})).status_code == 200
            assert (await client.post(f"{API}/chat", json={"message": "hi"})).status_code == 401
            assert (await client.get("/health")).status_code == 200  # health stays public
            assert (await client.post(f"{API}/google/connect")).status_code == 401
    finally:
        await c.close()


async def test_production_refuses_to_start_without_secrets():
    base = dict(_env_file=None, APP_ENV="production")
    with pytest.raises(RuntimeError, match="API_KEY"):
        create_app(Settings(**base, SECRET_KEY="x"))
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app(Settings(**base, API_KEY="k"))


async def test_production_without_key_rejects_requests(settings, clock, notifier):
    s = settings.model_copy(update={"APP_ENV": "production", "API_KEY": ""})
    c = build_container(s, clock=clock, notifier=notifier)
    await c.start()
    try:
        app = create_app(settings, c)  # built with dev settings, but container.settings is production
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            assert (await client.get(f"{API}/tasks")).status_code == 503
    finally:
        await c.close()


# ---------------------------------------------------------------------- chat
async def test_chat_roundtrip_and_message_feed(client, container):
    r = await client.post(f"{API}/chat", json={"message": "remind me to call mom tomorrow at 7pm"})
    assert r.status_code == 200 and r.json()["reply"].startswith("Created reminder: Call mom")
    feed = (await client.get(f"{API}/messages")).json()
    assert [(m["role"], m["channel"]) for m in feed] == [("user", "web"), ("assistant", "web")]
    newer = (await client.get(f"{API}/messages", params={"after_id": feed[0]["id"]})).json()
    assert [m["id"] for m in newer] == [feed[1]["id"]]
    assert (await client.get(f"{API}/messages", params={"after_id": feed[1]["id"]})).json() == []


async def test_chat_validation(client):
    assert (await client.post(f"{API}/chat", json={"message": ""})).status_code == 422
    assert (await client.post(f"{API}/chat", json={"message": "x" * 5000})).status_code == 422
    assert (await client.post(f"{API}/chat", json={})).status_code == 422


async def test_proactive_messages_appear_in_the_feed_with_buttons(client, container, notifier):
    await container.notifier.send("hello from the bot")  # FakeNotifier: not recorded -> use real one
    from app.channels.notifier import Notifier
    real = Notifier(container.session_factory, container.clock)
    from app.messaging import Button
    await real.send("Time to start: Report", [Button("✅ Done", "done:abc")])
    feed = (await client.get(f"{API}/messages")).json()
    assert feed[-1]["content"] == "Time to start: Report" and feed[-1]["buttons"] == [["✅ Done", "done:abc"]]


async def test_button_actions_via_api(client, container):
    t = (await client.post(f"{API}/tasks", json={"title": "Call mom", "kind": "REMINDER", "due_at": "2026-10-14T10:00:00"})).json()
    r = await client.post(f"{API}/actions", json={"data": f"done:{t['id']}"})
    assert r.json()["reply"].startswith("✅ Done: Call mom")
    assert (await client.get(f"{API}/tasks/{t['id']}")).json()["status"] == "DONE"
    assert (await client.post(f"{API}/actions", json={"data": ""})).status_code == 422


# ---------------------------------------------------------------------- tasks CRUD
async def test_task_crud_lifecycle(client):
    r = await client.post(f"{API}/tasks", json={"title": "Write report", "priority": "P1", "estimate_min": 90,
                                                "due_at": "2026-10-16T17:00:00", "category": "writing"})
    assert r.status_code == 201
    t = r.json()
    assert t["due_at"] == at(17, day=TODAY + timedelta(days=2)).isoformat() and t["status"] == "TODO"

    assert [x["id"] for x in (await client.get(f"{API}/tasks")).json()] == [t["id"]]
    patched = (await client.patch(f"{API}/tasks/{t['id']}", json={"priority": "P2", "due_at": None})).json()
    assert patched["priority"] == "P2" and patched["due_at"] is None and patched["estimate_min"] == 90  # untouched fields stay

    snoozed = (await client.post(f"{API}/tasks/{t['id']}/snooze", json={"minutes": 45})).json()
    assert snoozed["due_at"] == (at(9) + timedelta(minutes=45)).isoformat()

    done = (await client.post(f"{API}/tasks/{t['id']}/complete", json={"actual_min": 100})).json()
    assert done["task"]["status"] == "DONE" and done["task"]["actual_min"] == 100 and done["next_task"] is None
    assert (await client.get(f"{API}/tasks")).json() == []
    assert len((await client.get(f"{API}/tasks", params={"status": "done"})).json()) == 1

    assert (await client.delete(f"{API}/tasks/{t['id']}")).json()["status"] == "deleted"
    assert (await client.get(f"{API}/tasks/{t['id']}")).status_code == 404


async def test_task_errors_map_to_http_codes(client):
    assert (await client.post(f"{API}/tasks", json={"title": "x", "priority": "P9"})).status_code == 400
    assert (await client.post(f"{API}/tasks", json={"title": "x", "due_at": "banana"})).status_code == 400
    assert (await client.post(f"{API}/tasks", json={"title": "x", "kind": "EVENT"})).status_code == 400
    assert (await client.post(f"{API}/tasks", json={})).status_code == 422
    assert (await client.patch(f"{API}/tasks/nope", json={"title": "y"})).status_code == 404
    assert (await client.post(f"{API}/tasks/nope/complete")).status_code == 404
    assert (await client.delete(f"{API}/tasks/nope")).status_code == 404
    assert (await client.get(f"{API}/tasks", params={"status": "weird"})).status_code == 400
    t = (await client.post(f"{API}/tasks", json={"title": "x"})).json()
    assert (await client.post(f"{API}/tasks/{t['id']}/complete")).status_code == 200
    assert (await client.post(f"{API}/tasks/{t['id']}/complete")).status_code == 400  # already done


async def test_completing_a_recurring_task_returns_the_next_one(client):
    t = (await client.post(f"{API}/tasks", json={"title": "Gym", "kind": "EVENT", "due_at": "2026-10-14T18:00:00", "recurrence": "DAILY"})).json()
    done = (await client.post(f"{API}/tasks/{t['id']}/complete")).json()
    assert done["next_task"]["due_at"] == at(18, day=TOMORROW).isoformat()


# ---------------------------------------------------------------------- planning
async def test_plan_preview_then_save(client):
    await client.post(f"{API}/tasks", json={"title": "Report", "priority": "P1", "estimate_min": 60})
    preview = (await client.get(f"{API}/plan")).json()
    assert not preview["committed"] and preview["blocks"][0]["label"] == "9:00 AM–10:00 AM"
    assert (await client.get(f"{API}/schedule")).json()["blocks"] == []  # nothing saved yet

    saved = (await client.post(f"{API}/plan", json={"date": "today"})).json()
    assert saved["committed"] and len(saved["blocks"]) == 1
    sched = (await client.get(f"{API}/schedule")).json()
    assert [b["title"] for b in sched["blocks"]] == ["Report"] and sched["timezone"] == "Asia/Karachi"
    assert "9:00 AM–10:00 AM  Report" in sched["text"]


async def test_plan_validation(client):
    assert (await client.get(f"{API}/plan", params={"date": "someday"})).status_code == 400
    assert (await client.post(f"{API}/plan", json={"date": "2020-01-01"})).status_code == 400
    r = await client.post(f"{API}/plan", json={"date": "tomorrow", "commit": False})
    assert r.status_code == 200 and r.json()["committed"] is False


async def test_review_endpoint(client):
    r = (await client.post(f"{API}/review")).json()
    assert "Day review" in r["text"] and r["completed"] == 0


# ---------------------------------------------------------------------- preferences
async def test_preferences_get_put_validate(client):
    prefs = (await client.get(f"{API}/preferences")).json()
    assert prefs["timezone"] == "Asia/Karachi" and prefs["work_start"] == "09:00" and "last_brief_date" not in prefs
    updated = (await client.put(f"{API}/preferences", json={"work_end": "17:30", "max_block_min": 60,
                                                             "blocked_windows": [{"label": "lunch", "start": "13:00", "end": "14:00"}]})).json()
    assert updated["work_end"] == "17:30" and updated["max_block_min"] == 60 and updated["blocked_windows"][0]["label"] == "lunch"
    r = await client.put(f"{API}/preferences", json={"work_start": "08:00", "work_end": "nonsense"})
    assert r.status_code == 400
    assert (await client.get(f"{API}/preferences")).json()["work_start"] == "09:00"  # all-or-nothing
    assert (await client.put(f"{API}/preferences", json={"last_brief_date": "x"})).status_code == 400  # internal keys are off limits
    assert (await client.put(f"{API}/preferences", json={"timezone": "Mars/Base"})).status_code == 400


async def test_preference_changes_affect_planning(client):
    await client.put(f"{API}/preferences", json={"work_start": "10:00", "focus_start": "10:00", "focus_end": "12:00"})
    await client.post(f"{API}/tasks", json={"title": "Report", "priority": "P1", "estimate_min": 60})
    assert (await client.get(f"{API}/plan")).json()["blocks"][0]["label"].startswith("10:00 AM")


# ---------------------------------------------------------------------- google
async def test_google_endpoints_without_configuration(client):
    assert (await client.get(f"{API}/google/status")).json() == {"configured": False, "connected": False}
    assert (await client.post(f"{API}/google/connect")).status_code == 400
    assert (await client.post(f"{API}/google/sync")).status_code == 400


async def test_google_callback_checks_state_and_never_prints_tokens(settings, clock, notifier, http_mock):
    s = settings.model_copy(update={"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "cs"})

    def google(request: httpx.Request):
        return httpx.Response(200, json={"access_token": "ACCESS-SECRET", "refresh_token": "REFRESH-SECRET", "expires_in": 3600})

    c = build_container(s, clock=clock, notifier=notifier)
    c.calendar = GoogleCalendar(s, c.session_factory, client=http_mock(google), clock=clock)
    await c.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(s, c)), base_url="http://t") as client:
            url = (await client.post(f"{API}/google/connect")).json()["url"]
            assert url.startswith("https://accounts.google.com/") and "state=" in url

            bad = await client.get("/auth/google/callback", params={"code": "x", "state": "forged"})
            assert bad.status_code == 400 and "expired or is invalid" in bad.text
            assert (await client.get("/auth/google/callback", params={"error": "access_denied"})).status_code == 400
            assert (await client.get("/auth/google/callback", params={"state": make_state(s.SECRET_KEY)})).status_code == 400

            ok = await client.get("/auth/google/callback", params={"code": "good", "state": make_state(s.SECRET_KEY)})
            assert ok.status_code == 200 and "connected" in ok.text
            assert "SECRET" not in ok.text
            assert (await client.get(f"{API}/google/status")).json()["connected"] is True
            assert (await client.post(f"{API}/google/disconnect")).json() == {"status": "disconnected"}
            assert (await client.get(f"{API}/google/status")).json()["connected"] is False
    finally:
        await c.close()


async def test_changes_sync_to_the_calendar_in_the_background(settings, clock, notifier):
    cal = FakeCalendar()
    c = build_container(settings, clock=clock, notifier=notifier, calendar=cal)
    await c.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, c)), base_url="http://t") as client:
            await client.post(f"{API}/tasks", json={"title": "Standup", "kind": "EVENT", "due_at": "2026-10-14T11:00:00", "estimate_min": 30})
            await c.wait_for_sync()
            assert [e["title"] for e in cal.events.values()] == ["Standup"]
            await client.post(f"{API}/tasks", json={"title": "Report", "priority": "P1", "estimate_min": 60})
            await client.post(f"{API}/plan", json={"date": "today"})
            await c.wait_for_sync()
            assert sorted(e["title"] for e in cal.events.values()) == ["Report", "Standup"]
            t = (await client.get(f"{API}/tasks", params={"kind": "EVENT"})).json()[0]
            await client.delete(f"{API}/tasks/{t['id']}")
            await c.wait_for_sync()
            assert [e["title"] for e in cal.events.values()] == ["Report"]
    finally:
        await c.close()


# ---------------------------------------------------------------------- telegram webhook
async def test_telegram_webhook_requires_the_secret(settings, clock, notifier):
    s = settings.model_copy(update={"TELEGRAM_WEBHOOK_SECRET": "whsec"})
    c = build_container(s, clock=clock, notifier=notifier)
    await c.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(s, c)), base_url="http://t") as client:
            update = {"update_id": 1, "message": {"chat": {"id": 1}, "text": "hi"}}
            assert (await client.post(f"{API}/telegram/webhook", json=update)).status_code == 401
            assert (await client.post(f"{API}/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "no"})).status_code == 401
            assert (await client.post(f"{API}/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "whsec"})).status_code == 200
    finally:
        await c.close()


async def test_webhook_closed_when_no_secret_configured(client):
    r = await client.post(f"{API}/telegram/webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": ""})
    assert r.status_code == 401
    assert (await client.post(f"{API}/telegram/set-webhook", json={"url": "https://x.example/hook"})).status_code == 400


# ---------------------------------------------------------------------- end to end with a "smart" model
async def test_full_day_story(client, container):
    """Capture -> plan -> approve -> work -> finish -> review, all through the public API."""
    container.llm = ScriptedLLM([
        call("create_task", title="Quarterly report", estimate_min=120, priority="P1", due_at="2026-10-15T17:00:00"), say("Added."),
        call("create_task", title="Reply to emails", estimate_min=30), say("Added."),
        call("create_task", title="Dentist", kind="EVENT", due_at="2026-10-14T11:00:00", estimate_min=60), say("Added."),
        call("plan_day"), say("Here is your draft."),
    ])
    for text in ("report by tomorrow 5pm", "answer emails", "dentist at 11", "plan my day"):
        reply = (await client.post(f"{API}/chat", json={"message": text})).json()
    approve = reply["buttons"][0][1]
    saved = (await client.post(f"{API}/actions", json={"data": approve})).json()
    assert "Plan saved" in saved["reply"]

    sched = (await client.get(f"{API}/schedule")).json()
    work = [b for b in sched["blocks"] if b["kind"] == "WORK"]
    events = [b for b in sched["blocks"] if b["kind"] == "EVENT"]
    assert [b["title"] for b in events] == ["Dentist"]
    assert {b["title"] for b in work} == {"Quarterly report", "Reply to emails"}
    for b in work:  # nothing overlaps the dentist
        assert b["end"] <= events[0]["start"] or b["start"] >= events[0]["end"]

    tasks = {t["title"]: t for t in (await client.get(f"{API}/tasks")).json()}
    await client.post(f"{API}/tasks/{tasks['Reply to emails']['id']}/complete", json={"actual_min": 20})
    container.clock.set(at(21))  # end of the day
    review = (await client.post(f"{API}/review")).json()
    assert review["completed"] == 1 and review["carried_over"] == 1
    assert "Quarterly report" in review["text"]
    async with container.session_factory() as s:
        assert (await s.execute(select(Task).where(Task.status == "DONE"))).scalars().one().title == "Reply to emails"
