from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.db.models import Reminder, Routine, RoutineRun, Task, TimeBlock
from app.jobs.scheduler import Scheduler
from app.main import create_app
from app.services.routines import RoutineError, describe_days, normalize_days
from tests.conftest import TODAY, ScriptedLLM, at, call

TOMORROW = TODAY + timedelta(days=1)  # TODAY is a Wednesday
API = "/api/v1"


async def copies(ctx, routine_id=None):
    q = select(Task).order_by(Task.due_at)
    if routine_id:
        q = q.where(Task.routine_id == routine_id)
    return (await ctx.session.execute(q)).scalars().all()


# ---------------------------------------------------------------------- days parsing
@pytest.mark.parametrize("value,expected", [
    (None, "0123456"), ("daily", "0123456"), ("every day", "0123456"), ("weekdays", "01234"), ("weekends", "56"),
    ("mon,wed,fri", "024"), ("Monday Wednesday", "02"), ([4, 0, 2], "024"), ("024", "024"), (["sat", "sun"], "56"),
])
def test_normalize_days(value, expected):
    assert normalize_days(value) == expected


@pytest.mark.parametrize("value", ["someday", "9", [7], "mon,funday"])
def test_bad_days(value):
    with pytest.raises(RoutineError):
        normalize_days(value)


def test_describe_days():
    assert describe_days("0123456") == "every day" and describe_days("01234") == "weekdays"
    assert describe_days("56") == "weekends" and describe_days("024") == "Mon, Wed, Fri"


# ---------------------------------------------------------------------- service
class TestRoutineService:
    async def test_create_makes_todays_and_tomorrows_copies(self, ctx):
        r = await ctx.routines.create(ctx.now, title="Gym", time_of_day="18:00", duration_min=60)
        made = await copies(ctx, r.id)
        assert [(t.kind, t.due_at, t.estimate_min, t.title) for t in made] == [
            ("EVENT", at(18), 60, "Gym"), ("EVENT", at(18, day=TOMORROW), 60, "Gym")]
        blocks = (await ctx.session.execute(select(TimeBlock).where(TimeBlock.kind == "EVENT"))).scalars().all()
        assert len(blocks) == 2  # they occupy time on the calendar, so the planner works around them

    async def test_notification_fires_at_start_time(self, ctx):
        r = await ctx.routines.create(ctx.now, title="Lunch", time_of_day="13:00", duration_min=45)
        today = (await copies(ctx, r.id))[0]
        (rem,) = (await ctx.session.execute(select(Reminder).where(Reminder.task_id == today.id))).scalars().all()
        assert rem.fire_at == at(13) and rem.text == "📅 Now: Lunch (1:00 PM–1:45 PM)"

    async def test_weekday_filter(self, ctx):
        r = await ctx.routines.create(ctx.now, title="Standup", time_of_day="10:00", days="mon,tue")  # Wed/Thu: none
        assert await copies(ctx, r.id) == []
        assert await ctx.routines.materialize(date(2026, 10, 19), ctx.now) == 1  # Monday

    async def test_materialize_is_idempotent(self, ctx):
        await ctx.routines.create(ctx.now, title="Gym", time_of_day="18:00")
        assert await ctx.routines.materialize_upcoming(ctx.now) == 0
        assert len(await copies(ctx)) == 2

    async def test_deleting_a_days_copy_does_not_bring_it_back(self, ctx):
        r = await ctx.routines.create(ctx.now, title="Gym", time_of_day="18:00")
        today = (await copies(ctx, r.id))[0]
        await ctx.tasks.delete(today, ctx.now)  # "skip gym today"
        assert await ctx.routines.materialize_upcoming(ctx.now) == 0
        assert [t.due_at for t in await copies(ctx, r.id)] == [at(18, day=TOMORROW)]

    async def test_past_reminder_routines_are_skipped_today_but_events_still_show(self, ctx, clock):
        clock.set(at(15))
        ctx.now = clock()
        ev = await ctx.routines.create(ctx.now, title="Breakfast", time_of_day="08:00", kind="EVENT")
        rem = await ctx.routines.create(ctx.now, title="Vitamins", time_of_day="08:00", kind="REMINDER")
        assert [t.due_at for t in await copies(ctx, ev.id)] == [at(8), at(8, day=TOMORROW)]
        assert [t.due_at for t in await copies(ctx, rem.id)] == [at(8, day=TOMORROW)]
        pending = (await ctx.session.execute(select(Reminder).where(Reminder.status == "PENDING"))).scalars().all()
        assert all(p.fire_at > ctx.now for p in pending)  # nothing stale fires immediately

    async def test_update_moves_upcoming_copies(self, ctx):
        r = await ctx.routines.create(ctx.now, title="Gym", time_of_day="18:00", duration_min=60)
        await ctx.routines.update(r, ctx.now, {"time_of_day": "19:30", "duration_min": 45, "title": "Workout"})
        made = await copies(ctx, r.id)
        assert [(t.title, t.due_at, t.estimate_min) for t in made] == [
            ("Workout", at(19, 30), 45), ("Workout", at(19, 30, day=TOMORROW), 45)]

    async def test_pause_removes_upcoming_and_resume_restores(self, ctx):
        r = await ctx.routines.create(ctx.now, title="Gym", time_of_day="18:00")
        await ctx.routines.update(r, ctx.now, {"active": False})
        assert await copies(ctx, r.id) == []
        await ctx.routines.update(r, ctx.now, {"active": True})
        assert len(await copies(ctx, r.id)) == 2

    async def test_changing_days_drops_copies_that_no_longer_fit(self, ctx):
        r = await ctx.routines.create(ctx.now, title="Gym", time_of_day="18:00")
        await ctx.routines.update(r, ctx.now, {"days": "thu"})  # TOMORROW is Thursday
        assert [t.due_at for t in await copies(ctx, r.id)] == [at(18, day=TOMORROW)]

    async def test_past_and_done_copies_are_history_and_survive_changes(self, ctx, clock):
        r = await ctx.routines.create(ctx.now, title="Breakfast", time_of_day="08:30")  # 08:30 < now (09:00)
        past = (await copies(ctx, r.id))[0]
        await ctx.routines.update(r, ctx.now, {"time_of_day": "07:30"})
        assert (await ctx.tasks.get(past.id)).due_at == at(8, 30)  # history isn't rewritten
        await ctx.routines.delete(r, ctx.now)
        remaining = await copies(ctx)
        assert [t.id for t in remaining] == [past.id] and remaining[0].routine_id is None

    async def test_validation(self, ctx):
        for kwargs, msg in [({"title": " ", "time_of_day": "08:00"}, "name"),
                            ({"title": "x", "time_of_day": "25:00"}, "HH:MM"),
                            ({"title": "x", "time_of_day": "08:00", "duration_min": 0}, "between"),
                            ({"title": "x", "time_of_day": "08:00", "kind": "TASK"}, "EVENT"),
                            ({"title": "x", "time_of_day": "08:00", "days": "nope"}, "day")]:
            with pytest.raises(RoutineError, match=msg):
                await ctx.routines.create(ctx.now, **kwargs)
        assert (await ctx.session.execute(select(Routine))).scalars().all() == []

    async def test_find_by_name(self, ctx):
        await ctx.routines.create(ctx.now, title="Gym", time_of_day="18:00")
        await ctx.routines.create(ctx.now, title="Call mom", time_of_day="19:30")
        assert (await ctx.routines.find("call")).title == "Call mom"
        with pytest.raises(RoutineError):
            await ctx.routines.find("swimming")

    async def test_planner_works_around_routines(self, ctx):
        await ctx.routines.create(ctx.now, title="Gym", time_of_day="09:00", duration_min=180)
        await ctx.tasks.create(now=ctx.now, title="Report", priority="P1", estimate_min=60)
        result = await ctx.planning.build(TODAY, ctx.now)
        assert result.blocks[0].start >= at(12)


# ---------------------------------------------------------------------- start / progress tracking
class TestStartTracking:
    async def test_start_sets_the_clock_and_done_measures_actual_time(self, ctx, clock):
        t = await ctx.tasks.create(now=ctx.now, title="Report", estimate_min=60)
        await ctx.tasks.update(t, ctx.now, {"status": "IN_PROGRESS"})
        assert t.started_at == at(9)
        clock.advance(minutes=75)
        await ctx.tasks.complete(t, clock())
        assert t.actual_min == 75  # learned automatically

    async def test_explicit_actual_minutes_win(self, ctx, clock):
        t = await ctx.tasks.create(now=ctx.now, title="Report")
        await ctx.tasks.update(t, ctx.now, {"status": "IN_PROGRESS"})
        await ctx.tasks.complete(t, ctx.now + timedelta(minutes=90), actual_min=40)
        assert t.actual_min == 40

    async def test_pause_clears_and_restart_resets(self, ctx, clock):
        t = await ctx.tasks.create(now=ctx.now, title="Report")
        await ctx.tasks.update(t, ctx.now, {"status": "IN_PROGRESS"})
        await ctx.tasks.update(t, ctx.now, {"status": "TODO"})
        assert t.started_at is None
        await ctx.tasks.update(t, at(10), {"status": "IN_PROGRESS"})
        await ctx.tasks.update(t, at(11), {"status": "IN_PROGRESS"})  # already running: keep the original start
        assert t.started_at == at(10)


async def test_event_lead_preference(ctx):
    ctx.prefs["event_lead_min"] = 10
    from app.services.tasks import TaskService
    tasks = TaskService(ctx.session, ctx.prefs)
    t = await tasks.create(now=ctx.now, title="Dentist", kind="EVENT", due_at=at(15))
    (rem,) = (await ctx.session.execute(select(Reminder).where(Reminder.task_id == t.id))).scalars().all()
    assert rem.fire_at == at(14, 50) and "Starting soon" in rem.text


# ---------------------------------------------------------------------- scheduler
async def test_scheduler_creates_copies_each_day_and_notifies(container, notifier):
    async with container.session_factory() as s:
        ctx = await container.make_context(s)
        await ctx.routines.create(ctx.now, title="Lunch", time_of_day="13:00", duration_min=45)
        await s.commit()
    sched = Scheduler(container)
    await sched.materialize_routines(at(0, 5, day=TOMORROW + timedelta(days=1)))  # day after tomorrow, just past midnight
    async with container.session_factory() as s:
        days = sorted(r.day for r in (await s.execute(select(RoutineRun))).scalars())
    assert days == [TODAY.isoformat(), TOMORROW.isoformat(), (TOMORROW + timedelta(days=1)).isoformat(),
                    (TOMORROW + timedelta(days=2)).isoformat()]
    await sched.dispatch_reminders(at(13, 0))
    assert notifier.texts == ["📅 Now: Lunch (1:00 PM–1:45 PM)"]


# ---------------------------------------------------------------------- chat
async def test_chat_creates_and_manages_routines(container):
    container.llm = ScriptedLLM([
        call("create_routine", title="Gym", time="18:00", duration_min=60, days="weekdays"),
        call("list_routines"),
        call("update_routine", routine_ref="gym", active=False),
        call("delete_routine", routine_ref="gym"),
    ])
    container.settings = container.settings.model_copy(update={"FAST_REPLIES": True})
    assert (await container.assistant.handle("gym every weekday at 6pm for an hour")).text.startswith("Routine added: Gym at 18:00 for 60 min, weekdays")
    assert "18:00 Gym (60 min, weekdays)" in (await container.assistant.handle("what are my routines?")).text
    assert "(paused)" in (await container.assistant.handle("pause gym")).text
    assert (await container.assistant.handle("delete gym")).text.startswith("Deleted the routine")


# ---------------------------------------------------------------------- HTTP API
@pytest.fixture
async def client(container, settings):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, container)), base_url="http://t") as c:
        yield c


async def test_routines_api(client):
    r = await client.post(f"{API}/routines", json={"title": "Call mom", "time_of_day": "19:30", "duration_min": 15, "days": ["mon", "wed"]})
    assert r.status_code == 201
    body = r.json()
    assert body["days"] == [0, 2] and body["days_label"] == "Mon, Wed" and body["today"]["due_at"] == at(19, 30).isoformat()

    listed = (await client.get(f"{API}/routines")).json()
    assert [x["title"] for x in listed] == ["Call mom"]
    patched = (await client.patch(f"{API}/routines/{body['id']}", json={"time_of_day": "20:00", "days": "daily"})).json()
    assert patched["time_of_day"] == "20:00" and patched["days_label"] == "every day"
    assert (await client.patch(f"{API}/routines/{body['id']}", json={"time_of_day": "99:00"})).status_code == 400
    assert (await client.post(f"{API}/routines", json={"title": "x", "time_of_day": "nope"})).status_code == 400
    assert (await client.delete(f"{API}/routines/{body['id']}")).json()["status"] == "deleted"
    assert (await client.delete(f"{API}/routines/{body['id']}")).status_code == 404
    assert (await client.get(f"{API}/routines")).json() == []


async def test_start_pause_and_schedule_data_for_the_map(client, container, clock):
    t = (await client.post(f"{API}/tasks", json={"title": "Report", "priority": "P1", "estimate_min": 60})).json()
    await client.post(f"{API}/routines", json={"title": "Lunch", "time_of_day": "13:00", "duration_min": 45})
    await client.post(f"{API}/plan", json={"date": "today"})
    started = (await client.post(f"{API}/tasks/{t['id']}/start")).json()
    assert started["status"] == "IN_PROGRESS" and started["started_at"] == at(9).isoformat()

    sched = (await client.get(f"{API}/schedule")).json()
    assert sched["window"] == {"start": "09:00", "end": "18:00"} and sched["now"] == at(9).isoformat()
    by_title = {b["title"]: b for b in sched["blocks"]}
    assert by_title["Lunch"]["routine"] is True and by_title["Lunch"]["kind"] == "EVENT"
    assert by_title["Report"]["task_status"] == "IN_PROGRESS" and by_title["Report"]["priority"] == "P1"
    assert [x["title"] for x in sched["in_progress"]] == ["Report"]

    paused = (await client.post(f"{API}/tasks/{t['id']}/pause")).json()
    assert paused["status"] == "TODO" and paused["started_at"] is None
    await client.post(f"{API}/tasks/{t['id']}/complete")
    assert (await client.post(f"{API}/tasks/{t['id']}/start")).status_code == 400
    assert (await client.post(f"{API}/tasks/nope/start")).status_code == 404
