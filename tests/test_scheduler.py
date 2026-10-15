from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models import DailyReview, Message, Preference, Reminder, Task, TimeBlock
from app.jobs.scheduler import MAX_ATTEMPTS, Scheduler
from app.services.prefs import set_pref
from tests.conftest import TODAY, at

TOMORROW = TODAY + timedelta(days=1)


@pytest.fixture
def sched(container):
    return Scheduler(container)


async def create(container, **kw):
    async with container.session_factory() as s:
        ctx = await container.make_context(s)
        t = await ctx.tasks.create(now=container.now(), **kw)
        await s.commit()
        return t


async def all_reminders(container):
    async with container.session_factory() as s:
        return (await s.execute(select(Reminder).order_by(Reminder.fire_at))).scalars().all()


# ---------------------------------------------------------------------- reminders
async def test_due_reminder_is_sent_once_with_buttons(container, sched, clock, notifier):
    t = await create(container, title="Call mom", kind="REMINDER", due_at=at(10))
    await sched.dispatch_reminders(at(9, 59))
    assert notifier.sent == []  # not yet
    clock.set(at(10, 1))
    await sched.dispatch_reminders(clock.now)
    assert notifier.texts == ["⏰ Reminder: Call mom"]
    assert [b.data.split(":")[0] for b in notifier.sent[0][1]] == ["done", "snz", "tmr"]
    assert notifier.sent[0][1][0].data == f"done:{t.id}"
    await sched.dispatch_reminders(at(10, 2))
    assert len(notifier.sent) == 1  # never twice
    assert [r.status for r in await all_reminders(container)] == ["SENT"]


async def test_nag_repeats_then_stops_after_done(container, sched, notifier):
    t = await create(container, title="Pay rent", kind="REMINDER")
    await sched.dispatch_reminders(at(11, 1))  # first nag due at 11:00 (every 2h)
    await sched.dispatch_reminders(at(13, 2))
    assert notifier.texts == ["🔔 Still pending: Pay rent"] * 2
    async with container.session_factory() as s:
        ctx = await container.make_context(s)
        await ctx.tasks.complete(await ctx.tasks.get(t.id), at(13, 3))
        await s.commit()
    await sched.dispatch_reminders(at(15, 5))
    assert len(notifier.sent) == 2


async def test_nag_is_capped(container, sched, notifier, settings):
    async with container.session_factory() as s:
        await set_pref(s, "nag_max", 3)
        await s.commit()
    await create(container, title="Pay rent", kind="REMINDER")
    for h in range(11, 11 + 24, 2):
        await sched.dispatch_reminders(at(0, day=TODAY) + timedelta(hours=h))
    assert len(notifier.sent) == 3


async def test_stale_deadline_reminders_expire_instead_of_sending(container, sched, notifier):
    await create(container, title="Report", due_at=at(15))  # heads-up at 14:50
    await sched.dispatch_reminders(at(17))  # app was offline all afternoon
    assert notifier.sent == []
    assert [r.status for r in await all_reminders(container)] == ["EXPIRED"]


async def test_late_plain_reminder_still_sent_with_note(container, sched, notifier):
    await create(container, title="Call mom", kind="REMINDER", due_at=at(10))
    await sched.dispatch_reminders(at(15))
    assert len(notifier.sent) == 1 and "sent late" in notifier.texts[0]


async def test_reminder_for_completed_task_is_cancelled(container, sched, notifier):
    t = await create(container, title="Report", due_at=at(15))
    async with container.session_factory() as s:
        # simulate a completion that didn't go through the service (reminder left pending)
        (await s.get(Task, t.id)).status = "DONE"
        await s.commit()
    await sched.dispatch_reminders(at(14, 55))
    assert notifier.sent == [] and [r.status for r in await all_reminders(container)] == ["CANCELLED"]


async def test_failed_delivery_is_retried_with_backoff_then_gives_up(container, sched, notifier):
    await create(container, title="Call mom", kind="REMINDER", due_at=at(10))
    notifier.fail = True
    now = at(10, 1)
    gaps = []
    for _ in range(MAX_ATTEMPTS):
        await sched.dispatch_reminders(now)
        (r,) = await all_reminders(container)
        if r.status == "FAILED":
            break
        gaps.append(r.fire_at - now)
        now = r.fire_at
    assert gaps == sorted(gaps) and gaps[0] == timedelta(seconds=60)  # exponential backoff
    assert r.status == "FAILED" and r.attempts == MAX_ATTEMPTS

    notifier.fail = False
    await sched.dispatch_reminders(now + timedelta(hours=1))
    assert notifier.sent == []  # failed reminders are not resurrected


async def test_delivery_recovers_after_transient_failure(container, sched, notifier):
    await create(container, title="Call mom", kind="REMINDER", due_at=at(10))
    notifier.fail = True
    await sched.dispatch_reminders(at(10, 1))
    notifier.fail = False
    await sched.dispatch_reminders(at(10, 3))
    assert len(notifier.sent) == 1
    assert [r.status for r in await all_reminders(container)] == ["SENT"]


async def test_one_failing_step_does_not_block_the_others(container, sched, notifier, monkeypatch):
    await create(container, title="Call mom", kind="REMINDER", due_at=at(10))

    async def boom(now):
        raise RuntimeError("brief exploded")

    monkeypatch.setattr(sched, "morning_brief", boom)
    await sched.tick(at(10, 1))
    assert len(notifier.sent) == 1


async def test_proactive_switch_silences_block_reminders(container, sched, notifier):
    await create(container, title="Report", priority="P1", estimate_min=30)
    async with container.session_factory() as s:
        ctx = await container.make_context(s)
        await ctx.planning.commit(TODAY, ctx.now)
        await set_pref(s, "proactive", False)
        await s.commit()
    await sched.dispatch_reminders(at(9, 1))
    assert notifier.sent == []


# ---------------------------------------------------------------------- morning brief
async def test_morning_brief_auto_plans_and_sends_once_per_day(container, sched, notifier, clock):
    await create(container, title="Report", priority="P1", estimate_min=60)
    await sched.tick(at(7, 30))
    assert notifier.sent == []  # before 08:00

    await sched.tick(at(8, 5))
    brief = [t for t in notifier.texts if "Good morning" in t]
    assert len(brief) == 1 and "Report" in brief[0]
    async with container.session_factory() as s:
        blocks = (await s.execute(select(TimeBlock).where(TimeBlock.kind == "WORK"))).scalars().all()
        assert len(blocks) == 1  # it planned the day for you
    await sched.tick(at(8, 10))
    assert len([t for t in notifier.texts if "Good morning" in t]) == 1


async def test_morning_brief_does_not_replan_an_existing_plan(container, sched, notifier):
    await create(container, title="Report", priority="P1", estimate_min=60)
    async with container.session_factory() as s:
        ctx = await container.make_context(s)
        await ctx.planning.commit(TOMORROW, at(21))  # drafted last night
        await s.commit()
    async with container.session_factory() as s:
        before = {b.id for b in (await s.execute(select(TimeBlock))).scalars()}
    await sched.tick(at(8, 5, day=TOMORROW))
    async with container.session_factory() as s:
        after = {b.id for b in (await s.execute(select(TimeBlock).where(TimeBlock.status == "PLANNED"))).scalars()}
    assert before == after


async def test_morning_brief_retries_if_delivery_fails(container, sched, notifier):
    notifier.fail = True
    await sched.tick(at(8, 5))
    assert notifier.sent == []
    notifier.fail = False
    await sched.tick(at(8, 6))
    assert any("Good morning" in t for t in notifier.texts)


async def test_morning_brief_skipped_when_auto_plan_off_still_sends(container, sched, notifier):
    async with container.session_factory() as s:
        await set_pref(s, "auto_plan", False)
        await s.commit()
    await create(container, title="Report", priority="P1", estimate_min=60)
    await sched.tick(at(8, 5))
    async with container.session_factory() as s:
        assert (await s.execute(select(TimeBlock))).scalars().all() == []
    assert any("Good morning" in t for t in notifier.texts)


async def test_no_morning_brief_after_evening_review_time(container, sched, notifier):
    await sched.tick(at(22))
    assert not any("Good morning" in t for t in notifier.texts)


@pytest.mark.parametrize("h,m,sent", [(11, 59, True), (12, 0, False), (19, 39, False)])
async def test_morning_brief_only_within_four_hours(container, sched, notifier, h, m, sent):
    await sched.tick(at(h, m))
    assert any("Good morning" in t for t in notifier.texts) is sent


# ---------------------------------------------------------------------- evening review
async def test_evening_review_summarises_and_drafts_tomorrow(container, sched, notifier):
    done = await create(container, title="Email", estimate_min=30)
    await create(container, title="Report", priority="P1", estimate_min=60)
    async with container.session_factory() as s:
        ctx = await container.make_context(s)
        await ctx.planning.commit(TODAY, at(9))
        await ctx.tasks.complete(await ctx.tasks.get(done.id), at(12), actual_min=45)
        await s.commit()

    await sched.tick(at(20, 59))
    assert not any("Day review" in t for t in notifier.texts)
    await sched.tick(at(21, 1))
    (text, buttons) = next(x for x in notifier.sent if "Day review" in x[0])
    assert "Finished 1: Email" in text and "Carrying over 1: Report" in text
    assert "1.5×" in text  # 45 actual / 30 estimated
    assert "Draft plan for Thursday 15 October" in text
    assert buttons and buttons[0].data == f"plan:{TOMORROW.isoformat()}"

    async with container.session_factory() as s:
        row = await s.get(DailyReview, TODAY.isoformat())
        assert (row.completed, row.carried_over, row.estimate_ratio) == (1, 1, 1.5)
        statuses = {b.status for b in (await s.execute(select(TimeBlock).where(TimeBlock.kind == "WORK"))).scalars()}
        assert "PLANNED" not in statuses  # today's blocks are closed out (done / skipped / cancelled)
        assert (await s.get(Preference, "last_review_date")).value == TODAY.isoformat()

    await sched.tick(at(21, 30))
    assert len([t for t in notifier.texts if "Day review" in t]) == 1


async def test_evening_review_with_nothing_to_report(container, sched, notifier):
    await sched.tick(at(21, 1))
    (text,) = [t for t in notifier.texts if "Day review" in t]
    assert "Finished 0." in text and "Nothing carried over" in text


async def test_tomorrows_plan_approval_flow(container, sched, notifier):
    await create(container, title="Report", priority="P1", estimate_min=60)
    await sched.tick(at(21, 1))
    approve = next(b for t, bs in notifier.sent if "Day review" in t for b in bs)
    reply = await container.actions.run(approve.data)
    assert "Plan saved" in reply.text
    async with container.session_factory() as s:
        blocks = (await s.execute(select(TimeBlock).where(TimeBlock.status == "PLANNED"))).scalars().all()
        assert [b.start_at for b in blocks] == [at(9, day=TOMORROW)]
        msgs = (await s.execute(select(Message).where(Message.content.like("%Plan saved%")))).scalars().all()
        assert len(msgs) == 1  # replies to taps are recorded for the web chat too
