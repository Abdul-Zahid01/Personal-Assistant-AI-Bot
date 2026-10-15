"""The offline fallback, exercised end-to-end through the real agent loop, tools and database."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models import Task
from tests.conftest import TODAY, at

TOMORROW = TODAY + timedelta(days=1)


async def say(container, text):
    return (await container.assistant.handle(text)).text


async def all_tasks(container):
    async with container.session_factory() as s:
        return (await s.execute(select(Task).order_by(Task.created_at, Task.title))).scalars().all()


async def test_remind_me_tomorrow_at_time(container):
    reply = await say(container, "remind me to call mom tomorrow at 7pm")
    (t,) = await all_tasks(container)
    assert (t.kind, t.title, t.due_at) == ("REMINDER", "Call mom", at(19, day=TOMORROW))
    assert reply.startswith("Created reminder: Call mom")


async def test_task_with_estimate_priority_and_weekday(container):
    await say(container, "add task write quarterly report by friday 3pm, 2 hours, urgent")
    (t,) = await all_tasks(container)
    assert (t.kind, t.priority, t.estimate_min, t.due_at) == ("TASK", "P1", 120, at(15, day=TODAY + timedelta(days=2)))
    assert t.title == "Write quarterly report"


async def test_timed_activity_becomes_an_event(container):
    await say(container, "meeting with Sara tomorrow at 2pm for 45 minutes")
    (t,) = await all_tasks(container)
    assert (t.kind, t.estimate_min, t.due_at) == ("EVENT", 45, at(14, day=TOMORROW))
    assert "Sara" in t.title


async def test_relative_time(container):
    await say(container, "remind me to stretch in 20 minutes")
    (t,) = await all_tasks(container)
    assert t.due_at == container.now() + timedelta(minutes=20) and t.title == "Stretch"


async def test_recurring(container):
    await say(container, "remind me to take vitamins every day at 8am")
    (t,) = await all_tasks(container)
    assert t.recurrence == "DAILY" and t.due_at == at(8, day=TOMORROW)  # 8am already passed today


async def test_recurring_without_a_time_asks_for_one(container):
    reply = await say(container, "add task water plants daily")
    assert reply.startswith("Sorry") and "repeat" in reply and await all_tasks(container) == []


async def test_reminder_without_time_nags(container):
    reply = await say(container, "remind me to pay rent")
    assert "every 2h until you mark it done" in reply


async def test_done_snooze_and_list(container):
    await say(container, "add task buy milk")
    container.clock.advance(minutes=1)  # the test clock is frozen; real messages arrive at different times
    await say(container, "add task call plumber")
    assert "buy milk" in (await say(container, "show tasks")).lower()
    assert (await say(container, "done")).startswith("✅ Done: Call plumber")  # latest
    assert (await say(container, "done with milk")).startswith("✅ Done: Buy milk")
    assert "no open tasks" in (await say(container, "done"))
    assert all(t.status == "DONE" for t in await all_tasks(container))


async def test_snooze(container):
    await say(container, "remind me to stretch tomorrow at 9am")
    reply = await say(container, "snooze 2 hours")
    (t,) = await all_tasks(container)
    assert t.due_at == at(11, day=TOMORROW) and "Snoozed" in reply


async def test_plan_day_flow(container):
    await say(container, "add task write report 60 minutes urgent")
    draft = await container.assistant.handle("plan my day")
    assert "Draft plan" in draft.text and "Write report" in draft.text and draft.buttons
    saved = await container.assistant.handle("plan my day and save it")
    assert "Plan saved" in saved.text


async def test_schedule_and_review_commands(container):
    await say(container, "remind me to call mom today at 7pm")
    assert "Call mom" in await say(container, "what's on today?")
    assert "Day review" in await say(container, "end of day review")


async def test_delete(container):
    await say(container, "add task buy milk")
    assert (await say(container, "delete buy milk")).startswith("Deleted")
    assert await all_tasks(container) == []


async def test_unknown_input_shows_help_and_changes_nothing(container):
    reply = await say(container, "what is the meaning of life")
    assert "simple commands" in reply and await all_tasks(container) == []


@pytest.mark.parametrize("text", ["done", "snooze", "plan my day", "show tasks", "end of day review"])
async def test_commands_on_empty_database_do_not_crash(container, text):
    assert await say(container, text)


@pytest.mark.parametrize("text,actual", [
    ("I finished the client emails, took 20 minutes", 20),
    ("i've completed client emails", 0),
    ("just finished with my client emails - it took about 2 hours", 120),
    ("Done with the client emails", 0),
])
async def test_natural_completion_phrases(container, text, actual):
    await say(container, "add task reply to client emails")
    reply = await say(container, text)
    (t,) = await all_tasks(container)
    assert reply.startswith("✅ Done: Reply to client emails") and t.status == "DONE" and t.actual_min == actual
