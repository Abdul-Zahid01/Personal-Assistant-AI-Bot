"""The conversation loop and tools, driven by a scripted LLM so behaviour is deterministic."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.assistant.llm.base import LLMError, LLMResult, ToolCall
from app.db.models import Message, Preference, Reminder, Task, TimeBlock
from tests.conftest import TODAY, ScriptedLLM, at, call, say

TOMORROW = TODAY + timedelta(days=1)


async def tasks(container):
    async with container.session_factory() as s:
        return (await s.execute(select(Task).order_by(Task.created_at, Task.title))).scalars().all()


async def test_plain_answer_without_tools(container):
    container.llm = ScriptedLLM([say("Hello! What do you need?")])
    reply = await container.assistant.handle("hi")
    assert reply.text == "Hello! What do you need?"


async def test_empty_message(container):
    reply = await container.assistant.handle("   ")
    assert "Tell me" in reply.text


async def test_tool_call_creates_task_and_result_is_fed_back_to_the_model(container):
    llm = ScriptedLLM([
        call("create_task", title="Finish report", due_at="2026-10-16T17:00:00", priority="P1", estimate_min=90),
        say("Done - report is on your list for Friday 5pm."),
    ])
    container.llm = llm
    reply = await container.assistant.handle("finish the report by friday 5pm, urgent")
    assert reply.text.startswith("Done")

    (t,) = await tasks(container)
    assert (t.title, t.priority, t.estimate_min, t.due_at) == ("Finish report", "P1", 90, at(17, day=TODAY + timedelta(days=2)))

    second_call_messages = llm.calls[1][1]
    assert second_call_messages[-1].role == "tool" and second_call_messages[-1].name == "create_task"
    result = json.loads(second_call_messages[-1].content)
    assert result["ok"] is True and "Created task: Finish report" in result["message"]


async def test_system_prompt_has_current_time_and_open_tasks(container):
    async with container.session_factory() as s:
        ctx = await container.make_context(s)
        await ctx.tasks.create(now=ctx.now, title="Buy milk")
        await s.commit()
    llm = ScriptedLLM([say("ok")])
    container.llm = llm
    await container.assistant.handle("hello")
    system = llm.calls[0][0]
    assert "Wednesday 14 October 2026, 09:00 (Asia/Karachi)" in system and "Buy milk" in system
    assert "Working hours 09:00-18:00" in system


async def test_conversation_history_is_sent_and_stored(container):
    llm = ScriptedLLM([say("first answer"), say("second answer")])
    container.llm = llm
    await container.assistant.handle("one", channel="telegram")
    await container.assistant.handle("two")
    history = [(m.role, m.content) for m in llm.calls[1][1]]
    assert history == [("user", "one"), ("assistant", "first answer"), ("user", "two")]
    async with container.session_factory() as s:
        rows = (await s.execute(select(Message).order_by(Message.id))).scalars().all()
        assert [(m.role, m.channel) for m in rows] == [("user", "telegram"), ("assistant", "telegram"), ("user", "web"), ("assistant", "web")]


async def test_tool_errors_go_back_to_the_model_not_the_user(container):
    llm = ScriptedLLM([call("complete_task", task_ref="zebra"), say("I can't find a task called zebra - which one?")])
    container.llm = llm
    reply = await container.assistant.handle("done with zebra")
    result = json.loads(llm.calls[1][1][-1].content)
    assert result["ok"] is False and "couldn't find" in result["error"]
    assert reply.text.startswith("I can't find")


async def test_invalid_arguments_do_not_corrupt_state(container):
    llm = ScriptedLLM([call("create_task", title="x", priority="P9"), call("create_task", title="x", due_at="not a date"),
                       call("nope"), say("sorry")])
    container.llm = llm
    await container.assistant.handle("make a task")
    assert await tasks(container) == []
    errors = [json.loads(m.content)["error"] for _, msgs, _ in llm.calls[1:] for m in msgs[-1:] if m.role == "tool"]
    assert len(errors) == 3 and "priority" in errors[0] and "ISO" in errors[1] and "Unknown tool" in errors[2]


async def test_a_crashing_tool_is_contained(container, monkeypatch):
    from app.assistant import tools

    async def boom(ctx, args):
        raise RuntimeError("secret internal detail")

    spec, _ = tools.TOOLS["list_tasks"]
    monkeypatch.setitem(tools.TOOLS, "list_tasks", (spec, boom))
    llm = ScriptedLLM([call("list_tasks"), say("Something went wrong, try again.")])
    container.llm = llm
    reply = await container.assistant.handle("show tasks")
    result = json.loads(llm.calls[1][1][-1].content)
    assert "secret" not in json.dumps(result) and result["ok"] is False
    assert reply.text.startswith("Something went wrong")


async def test_all_llms_down_gives_a_friendly_message(container):
    container.llm = ScriptedLLM([LLMError("down")])
    reply = await container.assistant.handle("hello")
    assert "can't reach my language model" in reply.text
    async with container.session_factory() as s:  # the user's message is still saved
        assert (await s.execute(select(Message).where(Message.role == "user"))).scalars().one().content == "hello"


async def test_runaway_tool_loop_is_stopped(container):
    container.llm = ScriptedLLM([call("list_tasks")] * 10)
    reply = await container.assistant.handle("loop")
    assert "stuck" in reply.text


async def test_parallel_tool_calls_are_all_executed(container):
    container.llm = ScriptedLLM([
        LLMResult(tool_calls=[ToolCall("a", "create_task", {"title": "One"}), ToolCall("b", "create_task", {"title": "Two"})]),
        say("Added both."),
    ])
    await container.assistant.handle("add one and two")
    assert {t.title for t in await tasks(container)} == {"One", "Two"}


async def test_plan_draft_offers_an_approve_button_then_commit_saves(container):
    container.llm = ScriptedLLM([
        call("create_task", title="Report", priority="P1", estimate_min=60),
        call("plan_day", date="tomorrow"),
        say("Here's a draft for tomorrow."),
    ])
    reply = await container.assistant.handle("add report and plan tomorrow")
    assert [b.data for b in reply.buttons] == [f"plan:{TOMORROW.isoformat()}"]
    async with container.session_factory() as s:
        assert (await s.execute(select(TimeBlock).where(TimeBlock.kind == "WORK"))).scalars().all() == []  # draft only

    container.llm = ScriptedLLM([call("plan_day", date="tomorrow", commit=True), say("Saved.")])
    reply = await container.assistant.handle("looks good")
    assert reply.buttons == []
    async with container.session_factory() as s:
        (b,) = (await s.execute(select(TimeBlock).where(TimeBlock.kind == "WORK"))).scalars().all()
        assert b.start_at == at(9, day=TOMORROW)


async def test_button_replies_are_stored_with_buttons_for_the_web_ui(container):
    container.llm = ScriptedLLM([call("create_task", title="Report", estimate_min=60), call("plan_day"), say("Draft ready.")])
    await container.assistant.handle("plan")
    async with container.session_factory() as s:
        last = (await s.execute(select(Message).where(Message.role == "assistant").order_by(Message.id.desc()))).scalars().first()
        assert last.buttons == [["✅ Approve plan", f"plan:{TODAY.isoformat()}"]]


async def test_cannot_plan_the_past(container):
    llm = ScriptedLLM([call("plan_day", date="2026-10-01"), say("no")])
    container.llm = llm
    await container.assistant.handle("plan october first")
    assert "already passed" in json.loads(llm.calls[1][1][-1].content)["error"]


async def test_update_complete_snooze_delete_by_title(container):
    container.llm = ScriptedLLM([
        call("create_task", title="Write blog post", due_at="2026-10-15T10:00:00"), say("."),
        call("update_task", task_ref="blog", priority="P1", due_at="2026-10-15T12:00:00"), say("."),
        call("snooze_task", task_ref="blog", minutes=60), say("."),
        call("complete_task", task_ref="blog", actual_min=50), say("."),
        call("delete_task", task_ref="blog"), say("."),
    ])
    for text in ("add", "change", "snooze", "done", "delete"):
        await container.assistant.handle(text)
        if text == "update":
            pass
        ts = await tasks(container)
        if text == "change":
            assert (ts[0].priority, ts[0].due_at) == ("P1", at(12, day=TOMORROW))
        if text == "snooze":
            assert ts[0].due_at == at(13, day=TOMORROW)
        if text == "done":
            assert (ts[0].status, ts[0].actual_min) == ("DONE", 50)
    assert await tasks(container) == []


async def test_update_requires_something_to_change(container):
    llm = ScriptedLLM([call("create_task", title="A"), say("."), call("update_task", task_ref="A"), say(".")])
    container.llm = llm
    await container.assistant.handle("add")
    await container.assistant.handle("update")
    assert "Nothing to change" in json.loads(llm.calls[3][1][-1].content)["error"]


async def test_event_overlap_is_flagged(container):
    llm = ScriptedLLM([
        call("create_task", title="Dentist", kind="EVENT", due_at="2026-10-14T15:00:00", estimate_min=60), say("."),
        call("create_task", title="Gym", kind="EVENT", due_at="2026-10-14T15:30:00", estimate_min=60), say("."),
    ])
    container.llm = llm
    await container.assistant.handle("a")
    await container.assistant.handle("b")
    msg = json.loads(llm.calls[3][1][-1].content)["message"]
    assert "overlaps with “Dentist”" in msg and "4:00 PM" in msg


async def test_preferences_and_blocked_windows(container):
    llm = ScriptedLLM([
        call("set_preference", key="work_end", value="17:30"), say("."),
        call("set_preference", key="max_block_min", value="60"), say("."),
        call("add_blocked_window", label="Lunch", start="13:00", end="14:00"), say("."),
        call("set_preference", key="work_end", value="25:99"), say("."),
        call("set_preference", key="favourite_colour", value="blue"), say("."),
        call("remove_blocked_window", label="lunch"), say("."),
    ])
    container.llm = llm
    for _ in range(6):
        await container.assistant.handle("x")
    async with container.session_factory() as s:
        prefs = {p.key: p.value for p in (await s.execute(select(Preference))).scalars()}
    assert prefs["work_end"] == "17:30" and prefs["max_block_min"] == 60 and prefs["blocked_windows"] == []
    errors = [json.loads(m.content) for _, msgs, _ in llm.calls for m in msgs[-1:] if m.role == "tool"]
    assert any(not e["ok"] and "HH:MM" in e["error"] for e in errors)
    assert any(not e["ok"] and "Unknown setting" in e["error"] for e in errors)


async def test_changing_timezone_changes_how_times_are_read(container):
    llm = ScriptedLLM([call("set_preference", key="timezone", value="Europe/London"), say("."),
                       call("create_task", title="Call", due_at="2026-10-14T15:00:00"), say(".")])
    container.llm = llm
    await container.assistant.handle("I'm in London")
    await container.assistant.handle("call at 3pm")
    (t,) = await tasks(container)
    assert t.due_at.hour == 14  # 15:00 BST == 14:00 UTC


async def test_end_of_day_review_tool(container):
    container.llm = ScriptedLLM([call("end_of_day_review"), say("Here's your review.")])
    reply = await container.assistant.handle("how did today go?")
    assert reply.text == "Here's your review."


async def test_snooze_button_and_stale_buttons(container):
    async with container.session_factory() as s:
        ctx = await container.make_context(s)
        t = await ctx.tasks.create(now=ctx.now, title="Call mom", kind="REMINDER", due_at=at(10))
        await s.commit()
    reply = await container.actions.run(f"snz:{t.id}:30")
    assert "Snoozed" in reply.text and [b.data.split(":")[0] for b in reply.buttons] == ["done", "snz", "tmr"]
    assert "Moved" in (await container.actions.run(f"tmr:{t.id}")).text
    assert "Done" in (await container.actions.run(f"done:{t.id}")).text
    assert "already done" in (await container.actions.run(f"done:{t.id}")).text
    assert "no longer exists" in (await container.actions.run("done:deadbeef")).text
    assert "out of date" in (await container.actions.run("bogus")).text
    assert "out of date" in (await container.actions.run("")).text
    assert "already passed" in (await container.actions.run("plan:2020-01-01")).text
    assert (await container.actions.run("plan:garbage")).text  # bad input -> a message, not a crash


async def test_tomorrow_button_keeps_the_time_of_day(container):
    async with container.session_factory() as s:
        ctx = await container.make_context(s)
        t = await ctx.tasks.create(now=ctx.now, title="Report", due_at=at(15))
        r = await ctx.tasks.create(now=ctx.now, title="Nag", kind="REMINDER")
        await s.commit()
    await container.actions.run(f"tmr:{t.id}")
    await container.actions.run(f"tmr:{r.id}")
    async with container.session_factory() as s:
        assert (await s.get(Task, t.id)).due_at == at(15, day=TOMORROW)
        assert (await s.get(Task, r.id)).due_at == at(9, day=TOMORROW)  # no time -> start of working day
        pending = (await s.execute(select(Reminder).where(Reminder.task_id == r.id, Reminder.status == "PENDING"))).scalars().all()
        assert [p.kind for p in pending] == ["REMINDER"]


async def test_settings_changed_earlier_in_a_turn_apply_to_planning_in_the_same_turn(container):
    """Regression: the model saved a lunch window and planned in one step, but the plan ignored the lunch window."""
    async with container.session_factory() as s:
        ctx = await container.make_context(s)
        await ctx.tasks.create(now=ctx.now, title="Report", priority="P1", estimate_min=240)
        await s.commit()
    container.llm = ScriptedLLM([
        LLMResult(tool_calls=[ToolCall("a", "add_blocked_window", {"label": "Lunch", "start": "11:00", "end": "14:00"}),
                              ToolCall("b", "plan_day", {"date": "tomorrow", "commit": True})]),
        say("Saved."),
    ])
    await container.assistant.handle("keep 11 to 2 free and plan tomorrow")
    async with container.session_factory() as s:
        blocks = (await s.execute(select(TimeBlock).where(TimeBlock.kind == "WORK"))).scalars().all()
    assert blocks
    for b in blocks:
        assert not (b.start_at < at(14, day=TOMORROW) and b.end_at > at(11, day=TOMORROW)), (b.start_at, b.end_at)


class TestFastReplies:
    @pytest.fixture(autouse=True)
    def fast(self, container):
        container.settings = container.settings.model_copy(update={"FAST_REPLIES": True})

    async def test_successful_actions_reply_without_a_second_llm_call(self, container):
        llm = ScriptedLLM([call("create_task", title="Buy milk", due_at="2026-10-15T17:00:00")])
        container.llm = llm
        reply = await container.assistant.handle("buy milk tomorrow 5pm")
        assert len(llm.calls) == 1 and reply.text.startswith("Created task: Buy milk")

    async def test_parallel_results_are_joined_and_buttons_kept(self, container):
        container.llm = ScriptedLLM([LLMResult(tool_calls=[
            ToolCall("a", "create_task", {"title": "Report", "estimate_min": 60}), ToolCall("b", "plan_day", {})])])
        reply = await container.assistant.handle("add report and plan my day")
        assert "Created task: Report" in reply.text and "Draft plan" in reply.text and reply.buttons

    async def test_errors_still_go_back_to_the_model(self, container):
        llm = ScriptedLLM([call("complete_task", task_ref="zebra"), say("Which task did you mean?")])
        container.llm = llm
        reply = await container.assistant.handle("done with zebra")
        assert len(llm.calls) == 2 and reply.text == "Which task did you mean?"

    async def test_questions_about_the_list_still_get_reasoned_answers(self, container):
        llm = ScriptedLLM([call("list_tasks"), say("Start with the report - it's due first.")])
        container.llm = llm
        reply = await container.assistant.handle("what should I do first?")
        assert len(llm.calls) == 2 and reply.text.startswith("Start with")
