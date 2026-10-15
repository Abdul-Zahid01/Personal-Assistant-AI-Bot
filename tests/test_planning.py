from datetime import timedelta

from sqlalchemy import select

from app.db.models import Reminder, TimeBlock
from app.domain.planner import Interval
from app.services.calendar_sync import reconcile
from app.services.planning import PlanningService
from tests.conftest import TODAY, FakeCalendar, at

TOMORROW = TODAY + timedelta(days=1)


async def work_blocks(ctx, status="PLANNED"):
    q = select(TimeBlock).where(TimeBlock.kind == "WORK", TimeBlock.status == status).order_by(TimeBlock.start_at)
    return (await ctx.session.execute(q)).scalars().all()


async def test_build_does_not_save(ctx):
    await ctx.tasks.create(now=ctx.now, title="Report", priority="P1", estimate_min=60)
    result = await ctx.planning.build(TODAY, ctx.now)
    assert len(result.blocks) == 1
    assert await work_blocks(ctx) == []


async def test_commit_saves_blocks_and_reminders(ctx):
    t = await ctx.tasks.create(now=ctx.now, title="Report", priority="P1", estimate_min=60)
    result = await ctx.planning.commit(TODAY, ctx.now)
    (b,) = await work_blocks(ctx)
    assert (b.task_id, b.start_at, b.end_at) == (t.id, at(9), at(10))
    kinds = {r.kind: r for r in (await ctx.session.execute(select(Reminder).where(Reminder.block_id == b.id))).scalars()}
    assert kinds["BLOCK_START"].fire_at == at(9) and kinds["BLOCK_END"].fire_at == at(10)
    assert result.planned_minutes == 60


async def test_only_tasks_are_planned_not_reminders_or_events_or_done(ctx):
    await ctx.tasks.create(now=ctx.now, title="Ring mom", kind="REMINDER", due_at=at(19))
    done = await ctx.tasks.create(now=ctx.now, title="Finished")
    await ctx.tasks.complete(done, ctx.now)
    await ctx.tasks.create(now=ctx.now, title="Meeting", kind="EVENT", due_at=at(14))
    result = await ctx.planning.build(TODAY, ctx.now)
    assert result.blocks == [] and result.deferred == []


async def test_events_block_out_time(ctx):
    await ctx.tasks.create(now=ctx.now, title="Standup", kind="EVENT", due_at=at(9), estimate_min=120)
    await ctx.tasks.create(now=ctx.now, title="Report", priority="P1", estimate_min=60)
    result = await ctx.planning.build(TODAY, ctx.now)
    assert result.blocks[0].start >= at(11)


async def test_replanning_replaces_future_blocks_and_their_reminders(ctx):
    await ctx.tasks.create(now=ctx.now, title="Report", priority="P1", estimate_min=60)
    await ctx.planning.commit(TODAY, ctx.now)
    first = await work_blocks(ctx)
    await ctx.planning.commit(TODAY, ctx.now)
    assert len(await work_blocks(ctx)) == 1
    assert {b.id for b in first}.isdisjoint({b.id for b in await work_blocks(ctx)})
    assert {b.status for b in first} == {"CANCELLED"}
    old_pending = (await ctx.session.execute(
        select(Reminder).where(Reminder.block_id.in_([b.id for b in first]), Reminder.status == "PENDING"))).scalars().all()
    assert old_pending == []


async def test_replanning_keeps_blocks_that_already_ended(ctx, clock):
    await ctx.tasks.create(now=ctx.now, title="Report", priority="P1", estimate_min=60)
    await ctx.planning.commit(TODAY, ctx.now)  # 09:00-10:00
    clock.set(at(11))
    ctx.now = clock()
    await ctx.planning.commit(TODAY, ctx.now)
    past = await work_blocks(ctx)
    assert any(b.start_at == at(9) for b in past)  # the finished-hour block is history, not rewritten
    assert any(b.start_at >= at(11) for b in past)


async def test_deferred_tasks_are_reported(ctx):
    for i in range(8):
        await ctx.tasks.create(now=ctx.now, title=f"Big {i}", estimate_min=120, priority="P3")
    result = await ctx.planning.commit(TODAY, ctx.now)
    assert result.deferred and result.planned_minutes <= 9 * 60


async def test_learned_estimates_scale_the_plan(ctx):
    for i in range(3):
        t = await ctx.tasks.create(now=ctx.now, title=f"Essay {i}", estimate_min=30, category="writing")
        await ctx.tasks.complete(t, ctx.now, actual_min=60)
    await ctx.tasks.create(now=ctx.now, title="New essay", estimate_min=30, category="writing", priority="P1")
    result = await ctx.planning.build(TODAY, ctx.now)
    assert result.planned_minutes == 60  # 30 min estimate x 2.0 learned factor
    assert any("learned factor" in w for w in result.warnings)


async def test_calendar_busy_time_is_respected(ctx, container):
    cal = FakeCalendar()
    cal.busy_intervals = [Interval(at(9), at(13))]
    planning = PlanningService(ctx.session, ctx.prefs, cal)
    await ctx.tasks.create(now=ctx.now, title="Report", priority="P1", estimate_min=60)
    result = await planning.build(TODAY, ctx.now)
    assert result.blocks[0].start >= at(13)


async def test_calendar_failure_degrades_gracefully(ctx):
    cal = FakeCalendar()
    cal.fail_busy = True
    planning = PlanningService(ctx.session, ctx.prefs, cal)
    await ctx.tasks.create(now=ctx.now, title="Report", priority="P1", estimate_min=60)
    result = await planning.build(TODAY, ctx.now)
    assert len(result.blocks) == 1 and any("Google Calendar" in w for w in result.warnings)


async def test_blocked_windows_from_preferences_are_honoured(ctx):
    ctx.prefs["blocked_windows"] = [{"label": "lunch", "start": "09:00", "end": "13:00"}]
    planning = PlanningService(ctx.session, ctx.prefs)
    await ctx.tasks.create(now=ctx.now, title="Report", priority="P1", estimate_min=60)
    assert (await planning.build(TODAY, ctx.now)).blocks[0].start >= at(13)


async def test_plan_tomorrow_uses_full_working_day(ctx):
    await ctx.tasks.create(now=ctx.now, title="Report", priority="P1", estimate_min=60)
    result = await ctx.planning.build(TOMORROW, ctx.now)
    assert result.blocks[0].start == at(9, day=TOMORROW)


async def test_day_schedule_groups_items(ctx):
    await ctx.tasks.create(now=ctx.now, title="Report", priority="P1", estimate_min=60, due_at=at(17))
    await ctx.tasks.create(now=ctx.now, title="Ring mom", kind="REMINDER", due_at=at(19))
    await ctx.tasks.create(now=ctx.now, title="Standup", kind="EVENT", due_at=at(11))
    await ctx.tasks.create(now=ctx.now, title="Old", due_at=at(10, day=TODAY - timedelta(days=2)))
    await ctx.tasks.create(now=ctx.now, title="Taxes", due_at=at(18))
    await ctx.planning.commit(TODAY, ctx.now)
    sched = await ctx.planning.day_schedule(TODAY, ctx.now)
    assert [b.title for b in sched.blocks if b.kind == "EVENT"] == ["Standup"]
    assert {b.title for b in sched.blocks if b.kind == "WORK"} >= {"Report"}
    assert [t.title for t in sched.reminders] == ["Ring mom"]
    assert [t.title for t in sched.overdue] == ["Old"]


# ---------------------------------------------------------------------- calendar reconcile
async def test_reconcile_creates_updates_and_deletes_events(ctx):
    cal = FakeCalendar()
    t = await ctx.tasks.create(now=ctx.now, title="Standup", kind="EVENT", due_at=at(11))
    await ctx.session.commit()

    assert await reconcile(ctx.session, cal, ctx.now) == 1
    (block,) = (await ctx.session.execute(select(TimeBlock))).scalars().all()
    assert block.calendar_event_id and cal.events[block.calendar_event_id]["title"] == "Standup"
    assert await reconcile(ctx.session, cal, ctx.now) == 0  # nothing changed -> no writes

    await ctx.tasks.update(t, ctx.now + timedelta(minutes=1), {"due_at": at(13)})
    await ctx.session.commit()
    assert await reconcile(ctx.session, cal, ctx.now) == 1
    assert cal.events[block.calendar_event_id]["start"] == at(13)

    await ctx.tasks.delete(t, ctx.now + timedelta(minutes=2))
    await ctx.session.commit()
    assert await reconcile(ctx.session, cal, ctx.now) == 1
    assert cal.events == {}
    assert await reconcile(ctx.session, cal, ctx.now) == 0


async def test_reconcile_failure_is_retried_later(ctx):
    cal = FakeCalendar()
    await ctx.tasks.create(now=ctx.now, title="Standup", kind="EVENT", due_at=at(11))
    await ctx.session.commit()
    cal.fail_writes = True
    assert await reconcile(ctx.session, cal, ctx.now) == 0 and cal.events == {}
    cal.fail_writes = False
    assert await reconcile(ctx.session, cal, ctx.now) == 1 and len(cal.events) == 1


async def test_reconcile_noop_when_disconnected(ctx):
    cal = FakeCalendar()
    cal.connected = False
    await ctx.tasks.create(now=ctx.now, title="Standup", kind="EVENT", due_at=at(11))
    await ctx.session.commit()
    assert await reconcile(ctx.session, cal, ctx.now) == 0
    assert await reconcile(ctx.session, None, ctx.now) == 0
