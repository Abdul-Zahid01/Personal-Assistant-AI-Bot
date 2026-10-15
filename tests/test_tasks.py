from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models import Reminder, Task, TimeBlock
from app.services.tasks import TaskError
from tests.conftest import TODAY, at


async def reminders(ctx, **filters):
    q = select(Reminder).order_by(Reminder.fire_at)
    for k, v in filters.items():
        q = q.where(getattr(Reminder, k) == v)
    return (await ctx.session.execute(q)).scalars().all()


async def blocks(ctx):
    return (await ctx.session.execute(select(TimeBlock))).scalars().all()


class TestCreate:
    async def test_basic_task_defaults(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="  Write report  ")
        assert (t.title, t.kind, t.status, t.priority, t.estimate_min) == ("Write report", "TASK", "TODO", "P3", 30)

    @pytest.mark.parametrize("kwargs,msg", [
        ({"title": "  "}, "title"),
        ({"title": "x", "kind": "MEETING"}, "kind"),
        ({"title": "x", "priority": "P9"}, "priority"),
        ({"title": "x", "estimate_min": 0}, "Minutes"),
        ({"title": "x", "estimate_min": 99999}, "Minutes"),
        ({"title": "x", "estimate_min": "abc"}, "whole number"),
        ({"title": "x", "kind": "EVENT"}, "start time"),
        ({"title": "x", "recurrence": "DAILY"}, "repeat"),
        ({"title": "x", "recurrence": "HOURLY", "due_at": at(10)}, "Repeat"),
    ])
    async def test_validation(self, ctx, kwargs, msg):
        with pytest.raises(TaskError, match=msg):
            await ctx.tasks.create(now=ctx.now, **kwargs)

    async def test_deadline_reminder_is_scheduled_before_due(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="Report", due_at=at(15))
        (r,) = await reminders(ctx, task_id=t.id)
        assert r.kind == "DEADLINE" and r.fire_at == at(14, 50) and r.status == "PENDING"

    async def test_no_deadline_reminder_when_lead_time_already_passed(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="Soon", due_at=ctx.now + timedelta(minutes=5))
        assert await reminders(ctx, task_id=t.id) == []

    async def test_reminder_with_time(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="Call mom", kind="REMINDER", due_at=at(19))
        (r,) = await reminders(ctx, task_id=t.id)
        assert r.kind == "REMINDER" and r.fire_at == at(19) and "Call mom" in r.text

    async def test_reminder_without_time_nags_until_done(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="Pay rent", kind="REMINDER")
        (r,) = await reminders(ctx, task_id=t.id)
        assert r.kind == "NAG" and r.repeat_every_min == 120 and r.repeats_left == 11
        assert r.fire_at == ctx.now + timedelta(hours=2)

    async def test_event_creates_calendar_block_and_heads_up(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="Standup", kind="EVENT", due_at=at(11), estimate_min=30)
        (b,) = await blocks(ctx)
        assert (b.kind, b.start_at, b.end_at, b.task_id) == ("EVENT", at(11), at(11, 30), t.id)
        (r,) = await reminders(ctx, task_id=t.id)
        assert r.kind == "EVENT_START" and r.fire_at == at(11)  # notifies at the start time by default (event_lead_min=0)

    async def test_event_default_duration_is_an_hour(self, ctx):
        await ctx.tasks.create(now=ctx.now, title="Dentist", kind="EVENT", due_at=at(16))
        (b,) = await blocks(ctx)
        assert b.end_at - b.start_at == timedelta(hours=1)


class TestUpdate:
    async def test_moving_a_deadline_reschedules_reminders(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="Report", due_at=at(15))
        await ctx.tasks.update(t, ctx.now, {"due_at": at(17)})
        rows = await reminders(ctx, task_id=t.id)
        pending = [r for r in rows if r.status == "PENDING"]
        assert [r.fire_at for r in pending] == [at(16, 50)]
        assert [r.status for r in rows if r.fire_at == at(14, 50)] == ["CANCELLED"]

    async def test_moving_an_event_moves_its_block(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="Call", kind="EVENT", due_at=at(11))
        await ctx.tasks.update(t, ctx.now, {"due_at": at(13)})
        (b,) = await blocks(ctx)
        assert b.start_at == at(13) and b.status == "PLANNED"

    async def test_turning_event_into_task_cancels_block(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="Call", kind="EVENT", due_at=at(11))
        await ctx.tasks.update(t, ctx.now, {"kind": "TASK"})
        (b,) = await blocks(ctx)
        assert b.status == "CANCELLED"

    async def test_clear_deadline_and_recurrence(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="Gym", due_at=at(18), recurrence="DAILY")
        with pytest.raises(TaskError, match="repeating"):
            await ctx.tasks.update(t, ctx.now, {"due_at": None})
        await ctx.tasks.update(t, ctx.now, {"recurrence": "NONE"})
        await ctx.tasks.update(t, ctx.now, {"due_at": None})
        assert t.recurrence is None and t.due_at is None

    async def test_cannot_set_done_via_update_or_unknown_field(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="x")
        with pytest.raises(TaskError):
            await ctx.tasks.update(t, ctx.now, {"status": "DONE"})
        with pytest.raises(TaskError):
            await ctx.tasks.update(t, ctx.now, {"bogus": 1})
        with pytest.raises(TaskError):
            await ctx.tasks.update(t, ctx.now, {"title": " "})


class TestComplete:
    async def test_complete_cancels_pending_reminders(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="Pay rent", kind="REMINDER")
        await ctx.tasks.complete(t, ctx.now)
        assert t.status == "DONE" and t.completed_at == ctx.now
        assert {r.status for r in await reminders(ctx)} == {"CANCELLED"}

    async def test_complete_twice_is_an_error(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="x")
        await ctx.tasks.complete(t, ctx.now)
        with pytest.raises(TaskError, match="already done"):
            await ctx.tasks.complete(t, ctx.now)

    async def test_records_actual_minutes(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="x")
        await ctx.tasks.complete(t, ctx.now, actual_min=45)
        assert t.actual_min == 45

    async def test_future_work_blocks_are_cancelled_past_ones_kept(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="Report")
        ctx.session.add_all([
            TimeBlock(task_id=t.id, title="Report", kind="WORK", start_at=at(8), end_at=at(8, 30), created_at=ctx.now, updated_at=ctx.now),
            TimeBlock(task_id=t.id, title="Report", kind="WORK", start_at=at(14), end_at=at(15), created_at=ctx.now, updated_at=ctx.now),
        ])
        await ctx.session.flush()
        await ctx.tasks.complete(t, ctx.now)
        by_start = {b.start_at: b.status for b in await blocks(ctx)}
        assert by_start == {at(8): "DONE", at(14): "CANCELLED"}

    async def test_recurring_task_spawns_next_occurrence(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="Gym", kind="EVENT", due_at=at(18), recurrence="DAILY")
        result = await ctx.tasks.complete(t, ctx.now)
        nxt = result.next_task
        assert nxt is not None and nxt.id != t.id and nxt.status == "TODO"
        assert nxt.due_at == at(18, day=TODAY + timedelta(days=1)) and nxt.recurrence == "DAILY"
        assert any(b.task_id == nxt.id and b.kind == "EVENT" for b in await blocks(ctx))

    async def test_late_completion_skips_past_dates(self, ctx):
        t = await ctx.tasks.create(now=at(8, day=TODAY - timedelta(days=5)), title="Vitamins", kind="REMINDER",
                                   due_at=at(8, day=TODAY - timedelta(days=5)), recurrence="DAILY")
        result = await ctx.tasks.complete(t, ctx.now)
        assert result.next_task.due_at == at(8, day=TODAY + timedelta(days=1))

    async def test_non_recurring_has_no_next(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="x", due_at=at(15))
        assert (await ctx.tasks.complete(t, ctx.now)).next_task is None


class TestSnoozeDelete:
    async def test_snooze_without_due_uses_now(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="x", kind="REMINDER")
        await ctx.tasks.snooze(t, ctx.now, 30)
        assert t.due_at == ctx.now + timedelta(minutes=30)
        assert [r.kind for r in await reminders(ctx, status="PENDING")] == ["REMINDER"]

    async def test_snooze_future_deadline_pushes_it_later(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="x", due_at=at(15))
        await ctx.tasks.snooze(t, ctx.now, 60)
        assert t.due_at == at(16)

    async def test_snooze_overdue_counts_from_now(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="x", due_at=at(8))
        await ctx.tasks.snooze(t, ctx.now, 15)
        assert t.due_at == ctx.now + timedelta(minutes=15)

    async def test_delete_keeps_a_cancelled_block_for_calendar_cleanup(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="Call", kind="EVENT", due_at=at(11))
        await ctx.session.commit()
        await ctx.tasks.delete(t, ctx.now)
        await ctx.session.commit()
        assert await ctx.tasks.get(t.id) is None
        (b,) = await blocks(ctx)
        assert b.status == "CANCELLED" and b.task_id is None
        assert await reminders(ctx) == []  # cascade


class TestResolve:
    async def seed(self, ctx):
        for i, title in enumerate(["Write quarterly report", "Write blog post", "Call mom", "Buy milk"]):
            await ctx.tasks.create(now=ctx.now + timedelta(minutes=i), title=title)

    async def test_latest_is_most_recently_touched_open_task(self, ctx):
        await self.seed(ctx)
        assert (await ctx.tasks.resolve("latest")).title == "Buy milk"
        assert (await ctx.tasks.resolve(None)).title == "Buy milk"
        assert (await ctx.tasks.resolve("it")).title == "Buy milk"

    async def test_by_title_fragment_and_case(self, ctx):
        await self.seed(ctx)
        assert (await ctx.tasks.resolve("MILK")).title == "Buy milk"
        assert (await ctx.tasks.resolve("call")).title == "Call mom"

    async def test_by_id_and_prefix(self, ctx):
        await self.seed(ctx)
        t = (await ctx.tasks.list_tasks())[0]
        assert (await ctx.tasks.resolve(t.id)).id == t.id
        assert (await ctx.tasks.resolve(t.id[:8])).id == t.id

    async def test_ambiguous_lists_candidates(self, ctx):
        await self.seed(ctx)
        with pytest.raises(TaskError, match="More than one") as e:
            await ctx.tasks.resolve("write")
        assert "quarterly" in str(e.value) and "blog" in str(e.value)

    async def test_exact_title_beats_partial_matches(self, ctx):
        await ctx.tasks.create(now=ctx.now, title="Report")
        await ctx.tasks.create(now=ctx.now, title="Report review")
        assert (await ctx.tasks.resolve("report")).title == "Report"

    async def test_not_found_and_no_tasks(self, ctx):
        with pytest.raises(TaskError, match="no open tasks"):
            await ctx.tasks.resolve("latest")
        await self.seed(ctx)
        with pytest.raises(TaskError, match="couldn't find"):
            await ctx.tasks.resolve("zebra")

    async def test_done_tasks_only_found_when_nothing_open_matches(self, ctx):
        t = await ctx.tasks.create(now=ctx.now, title="Old thing")
        await ctx.tasks.complete(t, ctx.now)
        with pytest.raises(TaskError):
            await ctx.tasks.resolve("old thing")
        assert (await ctx.tasks.resolve("old thing", include_done=True)).id == t.id


async def test_list_orders_by_deadline_then_no_deadline_last(ctx):
    await ctx.tasks.create(now=ctx.now, title="later", due_at=at(17))
    await ctx.tasks.create(now=ctx.now, title="nodue")
    await ctx.tasks.create(now=ctx.now, title="sooner", due_at=at(12))
    assert [t.title for t in await ctx.tasks.list_tasks()] == ["sooner", "later", "nodue"]


async def test_event_conflicts_detects_overlap(ctx):
    a = await ctx.tasks.create(now=ctx.now, title="A", kind="EVENT", due_at=at(11), estimate_min=60)
    assert await ctx.tasks.event_conflicts(at(11, 30), at(12, 30)) != []
    assert await ctx.tasks.event_conflicts(at(12), at(13)) == []  # touching is not overlapping
    assert await ctx.tasks.event_conflicts(at(11, 30), at(12, 30), exclude_task_id=a.id) == []
    assert isinstance(a, Task)
