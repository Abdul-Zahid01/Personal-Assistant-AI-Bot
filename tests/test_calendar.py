import asyncio
import json
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.db.models import Reminder, TimeBlock
from app.events import EventBus, sse_stream
from app.main import create_app
from app.services.blocks import BlockService
from app.services.calendar_view import calendar_range
from app.services.tasks import TaskError
from tests.conftest import TODAY, at

TOMORROW = TODAY + timedelta(days=1)
API = "/api/v1"


@pytest.fixture
def blocks(ctx):
    return BlockService(ctx.session, ctx.prefs)


async def pending(ctx, **where):
    q = select(Reminder).where(Reminder.status == "PENDING")
    for k, v in where.items():
        q = q.where(getattr(Reminder, k) == v)
    return (await ctx.session.execute(q.order_by(Reminder.fire_at))).scalars().all()


# ---------------------------------------------------------------------- placing things at a time
class TestPlace:
    async def test_task_gets_a_pinned_work_block_with_start_and_end_alerts(self, ctx, blocks):
        task, block = await blocks.create_at(ctx.now, title="Write report", kind="TASK", start=at(14), end=at(15, 30),
                                             priority="P1", notes="Use Q3 numbers")
        assert (task.kind, task.estimate_min, task.priority, task.notes) == ("TASK", 90, "P1", "Use Q3 numbers")
        assert (block.kind, block.pinned, block.start_at, block.end_at) == ("WORK", True, at(14), at(15, 30))
        assert [(r.kind, r.fire_at) for r in await pending(ctx, block_id=block.id)] == [("BLOCK_START", at(14)), ("BLOCK_END", at(15, 30))]

    async def test_remind_before_moves_the_start_alert_earlier(self, ctx, blocks):
        _, block = await blocks.create_at(ctx.now, title="Call", kind="TASK", start=at(14), end=at(14, 30), remind_before_min=15)
        start = (await pending(ctx, block_id=block.id))[0]
        assert start.fire_at == at(13, 45) and "In 15 min" in start.text

    async def test_event_and_reminder(self, ctx, blocks):
        ev, ev_block = await blocks.create_at(ctx.now, title="Dentist", kind="EVENT", start=at(16), end=at(17), remind_before_min=30)
        assert ev_block is not None and ev_block.kind == "EVENT" and ev.estimate_min == 60
        assert [r.fire_at for r in await pending(ctx, task_id=ev.id)] == [at(15, 30)]
        rem, none = await blocks.create_at(ctx.now, title="Water plants", kind="REMINDER", start=at(18), end=None, remind_before_min=5)
        assert none is None and [r.fire_at for r in await pending(ctx, task_id=rem.id)] == [at(17, 55)]

    @pytest.mark.parametrize("start,end,msg", [((14, 0), (14, 2), "at least 5"), ((14, 0), (13, 0), "at least 5")])
    async def test_bad_ranges(self, ctx, blocks, start, end, msg):
        with pytest.raises(TaskError, match=msg):
            await blocks.create_at(ctx.now, title="x", kind="TASK", start=at(*start), end=at(*end))
        with pytest.raises(TaskError, match="24 hours"):
            await blocks.create_at(ctx.now, title="x", kind="EVENT", start=at(9), end=at(9) + timedelta(hours=25))
        with pytest.raises(TaskError, match="Type"):
            await blocks.create_at(ctx.now, title="x", kind="PARTY", start=at(9), end=at(10))


# ---------------------------------------------------------------------- moving / resizing
class TestMove:
    async def test_moving_a_planned_block_pins_it_and_moves_its_alerts(self, ctx, blocks):
        await ctx.tasks.create(now=ctx.now, title="Report", priority="P1", estimate_min=60)
        await ctx.planning.commit(TODAY, ctx.now)
        (block,) = (await ctx.session.execute(select(TimeBlock).where(TimeBlock.kind == "WORK"))).scalars().all()
        assert not block.pinned
        await blocks.move(block, ctx.now, at(16), at(17, 30))  # moved and stretched
        assert (block.start_at, block.end_at, block.pinned) == (at(16), at(17, 30), True)
        assert [r.fire_at for r in await pending(ctx, block_id=block.id)] == [at(16), at(17, 30)]

    async def test_moving_an_event_moves_the_event_itself(self, ctx, blocks):
        ev, ev_block = await blocks.create_at(ctx.now, title="Gym", kind="EVENT", start=at(18), end=at(19))
        await blocks.move(ev_block, ctx.now, at(19), at(20, 30))
        assert ev.due_at == at(19) and ev.estimate_min == 90
        assert (ev_block.start_at, ev_block.end_at) == (at(19), at(20, 30))
        assert [r.fire_at for r in await pending(ctx, task_id=ev.id)] == [at(19)]

    async def test_move_without_end_keeps_the_duration(self, ctx, blocks):
        _, block = await blocks.create_at(ctx.now, title="x", kind="TASK", start=at(10), end=at(10, 45))
        await blocks.move(block, ctx.now, at(15), None)
        assert block.end_at - block.start_at == timedelta(minutes=45)

    async def test_done_items_cannot_move(self, ctx, blocks):
        task, block = await blocks.create_at(ctx.now, title="x", kind="TASK", start=at(8), end=at(8, 30))
        await ctx.tasks.complete(task, ctx.now)
        with pytest.raises(TaskError):
            await blocks.move(block, ctx.now, at(15), None)

    async def test_remove_event_deletes_it_but_removing_work_keeps_the_task(self, ctx, blocks):
        ev, ev_block = await blocks.create_at(ctx.now, title="Gym", kind="EVENT", start=at(18), end=at(19))
        await blocks.remove(ev_block, ctx.now)
        assert await ctx.tasks.get(ev.id) is None
        task, block = await blocks.create_at(ctx.now, title="Report", kind="TASK", start=at(14), end=at(15))
        await blocks.remove(block, ctx.now)
        assert (await ctx.tasks.get(task.id)).status == "TODO" and block.status == "CANCELLED"
        assert await pending(ctx, block_id=block.id) == []

    async def test_moving_a_reminder(self, ctx, blocks):
        rem, _ = await blocks.create_at(ctx.now, title="Water plants", kind="REMINDER", start=at(18), end=None)
        await blocks.move_reminder(rem, ctx.now, at(20))
        assert [r.fire_at for r in await pending(ctx, task_id=rem.id)] == [at(20)]


# ---------------------------------------------------------------------- the planner respects what you placed
class TestPinnedAndPlanner:
    async def test_replanning_keeps_pinned_blocks_and_plans_around_them(self, ctx, blocks):
        report, pinned = await blocks.create_at(ctx.now, title="Report", kind="TASK", start=at(9), end=at(11), priority="P1")
        await ctx.tasks.create(now=ctx.now, title="Emails", priority="P1", estimate_min=60)
        result = await ctx.planning.commit(TODAY, ctx.now)
        assert pinned.status == "PLANNED" and pinned.start_at == at(9)  # untouched
        assert [b.title for b in result.blocks] == ["Emails"]  # the pinned task isn't planned twice
        assert result.blocks[0].start >= at(11)  # ...and nothing is put on top of it

    async def test_a_finished_pinned_slot_lets_the_task_be_planned_again(self, ctx, blocks, clock):
        await blocks.create_at(ctx.now, title="Report", kind="TASK", start=at(9), end=at(10))
        clock.set(at(11))
        ctx.now = clock()
        assert [b.title for b in (await ctx.planning.build(TODAY, ctx.now)).blocks] == ["Report"]  # not done -> plan the rest


async def test_per_task_reminder_lead_overrides_preferences(ctx):
    t = await ctx.tasks.create(now=ctx.now, title="Report", due_at=at(15), remind_before_min=60)
    assert [r.fire_at for r in await pending(ctx, task_id=t.id)] == [at(14)]
    await ctx.tasks.update(t, ctx.now, {"remind_before_min": None})  # back to the default (10 min)
    assert [r.fire_at for r in await pending(ctx, task_id=t.id)] == [at(14, 50)]
    with pytest.raises(TaskError):
        await ctx.tasks.update(t, ctx.now, {"remind_before_min": -5})


# ---------------------------------------------------------------------- range view
class TestCalendarRange:
    async def test_items_of_every_type(self, ctx, blocks):
        await blocks.create_at(ctx.now, title="Report", kind="TASK", start=at(14), end=at(15), priority="P1")
        await blocks.create_at(ctx.now, title="Dentist", kind="EVENT", start=at(16), end=at(17), priority="P2")
        await blocks.create_at(ctx.now, title="Plants", kind="REMINDER", start=at(18), end=None)
        await ctx.tasks.create(now=ctx.now, title="Taxes", due_at=at(17))
        await ctx.routines.create(ctx.now, title="Gym", time_of_day="07:00", duration_min=60)
        await ctx.session.commit()

        items = await calendar_range(ctx.session, ctx.prefs, TODAY, TODAY + timedelta(days=3))
        kinds = [(i["type"], i["title"]) for i in items]
        assert ("block", "Report") in kinds and ("block", "Dentist") in kinds
        assert ("reminder", "Plants") in kinds and ("deadline", "Taxes") in kinds
        gym = [i for i in items if i["title"] == "Gym"]
        assert [i["type"] for i in gym] == ["block", "block", "routine", "routine"]  # 2 real copies + 2 previews
        assert all(i["routine_id"] for i in gym)
        report = next(i for i in items if i["title"] == "Report")
        assert report["priority"] == "P1" and report["pinned"] is True
        assert [i["start"] for i in items] == sorted(i["start"] for i in items)

    async def test_paused_routines_have_no_previews_and_limits(self, ctx):
        r = await ctx.routines.create(ctx.now, title="Gym", time_of_day="07:00")
        await ctx.routines.update(r, ctx.now, {"active": False})
        assert [i for i in await calendar_range(ctx.session, ctx.prefs, TODAY, TODAY + timedelta(days=6)) if i["type"] == "routine"] == []
        with pytest.raises(ValueError):
            await calendar_range(ctx.session, ctx.prefs, TODAY, TODAY - timedelta(days=1))
        with pytest.raises(ValueError):
            await calendar_range(ctx.session, ctx.prefs, TODAY, TODAY + timedelta(days=70))


# ---------------------------------------------------------------------- live events
class FakeRequest:
    def __init__(self):
        self.gone = False

    async def is_disconnected(self):
        return self.gone


async def test_event_bus_delivers_to_every_subscriber_and_drops_when_full():
    bus = EventBus(max_queue=2)
    a, b = bus.subscribe(), bus.subscribe()
    for i in range(3):
        bus.publish("changed", n=i)
    assert [a.get_nowait()["n"], a.get_nowait()["n"]] == [1, 2]  # oldest dropped, newest kept
    assert b.qsize() == 2
    bus.unsubscribe(a)
    assert bus.subscribers == 1


async def test_sse_stream_format_heartbeat_and_close():
    bus, req = EventBus(), FakeRequest()
    gen = sse_stream(bus, req, heartbeat=0.05)
    assert await gen.__anext__() == "retry: 3000\n\n"
    assert "event: hello" in await gen.__anext__()
    bus.publish("message", id=7)
    chunk = await gen.__anext__()
    assert chunk.startswith("event: message\n") and json.loads(chunk.split("data: ")[1]) == {"type": "message", "id": 7}
    assert await gen.__anext__() == ": ping\n\n"  # nothing happened -> heartbeat
    bus.close()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(gen.__anext__(), 1)
    assert bus.subscribers == 0


async def test_reminders_and_changes_are_published(container):
    from app.channels.notifier import Notifier

    q = container.bus.subscribe()
    real = Notifier(container.session_factory, container.clock)
    real.bus = container.bus
    await real.send("⏰ Reminder: Stretch")
    container.after_change()
    events = [q.get_nowait()["type"], q.get_nowait()["type"]]
    assert events == ["message", "changed"]


# ---------------------------------------------------------------------- HTTP
@pytest.fixture
async def client(container, settings):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, container)), base_url="http://t") as c:
        yield c


async def test_calendar_api_roundtrip(client):
    r = await client.post(f"{API}/blocks", json={"title": "Design review", "kind": "TASK", "start": "2026-10-14T14:00:00",
                                                 "end": "2026-10-14T15:00:00", "priority": "P2", "notes": "bring mockups",
                                                 "remind_before_min": 10})
    assert r.status_code == 201
    block_id = r.json()["block_id"]
    cal = (await client.get(f"{API}/calendar", params={"start": TODAY.isoformat(), "end": TODAY.isoformat()})).json()
    item = next(i for i in cal["items"] if i["block_id"] == block_id)
    assert (item["priority"], item["notes"], item["remind_before_min"]) == ("P2", "bring mockups", 10)
    assert cal["timezone"] == "Asia/Karachi"

    moved = (await client.patch(f"{API}/blocks/{block_id}", json={"start": "2026-10-15T09:00:00"})).json()
    assert moved["start"] == at(9, day=TOMORROW).isoformat() and moved["end"] == at(10, day=TOMORROW).isoformat()
    assert (await client.patch(f"{API}/blocks/{block_id}", json={"start": "garbage"})).status_code == 400
    assert (await client.delete(f"{API}/blocks/{block_id}")).json()["status"] == "removed"
    assert (await client.patch(f"{API}/blocks/{block_id}", json={"start": "2026-10-15T09:00:00"})).status_code == 404


async def test_calendar_api_validation(client):
    assert (await client.get(f"{API}/calendar", params={"start": "2026-10-14", "end": "2026-12-31"})).status_code == 400
    assert (await client.get(f"{API}/calendar", params={"start": "nope", "end": "2026-10-14"})).status_code == 422
    assert (await client.post(f"{API}/blocks", json={"title": "x", "start": "2026-10-14T14:00:00", "end": "2026-10-14T14:01:00"})).status_code == 400
    assert (await client.post(f"{API}/blocks", json={"title": "", "start": "2026-10-14T14:00:00"})).status_code == 422


async def test_reminder_drag_endpoint(client):
    r = (await client.post(f"{API}/blocks", json={"title": "Plants", "kind": "REMINDER", "start": "2026-10-14T18:00:00"})).json()
    moved = (await client.patch(f"{API}/reminders/{r['task']['id']}", json={"start": "2026-10-14T19:15:00"})).json()
    assert moved["due_at"] == at(19, 15).isoformat()


async def test_stream_token_rules(settings, clock):
    from app.api.calendar import _stream_allowed
    from app.container import build_container

    open_dev = build_container(settings, clock=clock)
    assert _stream_allowed(open_dev, None)  # dev without API key: no token needed
    locked = build_container(settings.model_copy(update={"API_KEY": "k"}), clock=clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(locked.settings, locked)), base_url="http://t") as c:
        assert (await c.post(f"{API}/stream/token")).status_code == 401
        token = (await c.post(f"{API}/stream/token", headers={"X-API-Key": "k"})).json()["token"]
        assert (await c.get(f"{API}/stream", params={"token": "1.bad"})).status_code == 401
    assert _stream_allowed(locked, token) and not _stream_allowed(locked, "999.x") and not _stream_allowed(locked, "nonsense")
    exp, sig = token.split(".")
    assert not _stream_allowed(locked, f"{int(exp) + 1}.{sig}")  # tampered expiry
    await open_dev.close()
    await locked.close()
