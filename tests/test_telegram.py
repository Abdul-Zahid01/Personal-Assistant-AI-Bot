import json

import httpx
import pytest
from sqlalchemy import select

from app.channels.notifier import Notifier
from app.channels.telegram import TelegramChannel, TelegramError, _chunks, handle_update
from app.config import Settings
from app.container import build_container
from app.db.models import Message, Task
from app.messaging import Button
from tests.conftest import ScriptedLLM, at, say

CHAT = "4242"


def ok(result=True):
    return httpx.Response(200, json={"ok": True, "result": result})


@pytest.fixture
def tg_env(settings, clock, http_mock):
    """A container wired to a Telegram channel whose HTTP is mocked."""
    sent: list[dict] = []

    def handler(request: httpx.Request):
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content or b"{}")
        sent.append({"method": method, **payload})
        return ok([] if method == "getUpdates" else True)

    client = http_mock(handler)
    channel = TelegramChannel("123:ABC", CHAT, client=client)
    s = settings.model_copy(update={"TELEGRAM_BOT_TOKEN": "123:ABC", "TELEGRAM_CHAT_ID": CHAT})
    return s, channel, sent, client


async def make(tg_env, clock, llm=None):
    s, channel, sent, client = tg_env
    c = build_container(s, clock=clock, telegram=channel, llm=llm or ScriptedLLM([say("hello back")]))
    await c.start()
    return c, sent


def message(text, chat=CHAT):
    return {"update_id": 1, "message": {"chat": {"id": int(chat)}, "text": text}}


def callback(data, chat=CHAT):
    return {"update_id": 2, "callback_query": {"id": "cb1", "data": data, "message": {"chat": {"id": int(chat)}}}}


async def test_message_from_owner_gets_an_assistant_reply(tg_env, clock):
    c, sent = await make(tg_env, clock)
    try:
        await handle_update(c, message("hello"))
        assert [(m["method"], m.get("text")) for m in sent] == [("sendMessage", "hello back")]
        assert sent[0]["chat_id"] == CHAT
        async with c.session_factory() as s:
            assert (await s.execute(select(Message.channel))).scalars().all() == ["telegram", "telegram"]
    finally:
        await c.close()


async def test_strangers_are_ignored_completely(tg_env, clock):
    c, sent = await make(tg_env, clock, llm=ScriptedLLM([]))  # any LLM call would blow up
    try:
        await handle_update(c, message("add task steal data", chat="999"))
        await handle_update(c, callback("done:abc", chat="999"))
        assert sent == []
        async with c.session_factory() as s:
            assert (await s.execute(select(Message))).scalars().all() == []
    finally:
        await c.close()


async def test_unconfigured_chat_id_tells_the_owner_what_to_set(tg_env, clock):
    s, channel, sent, client = tg_env
    c = build_container(s.model_copy(update={"TELEGRAM_CHAT_ID": ""}), clock=clock, telegram=channel, llm=ScriptedLLM([]))
    await c.start()
    try:
        await handle_update(c, message("hi", chat="777"))
        assert len(sent) == 1 and "TELEGRAM_CHAT_ID=777" in sent[0]["text"]
        assert not [m for m in sent if "reply_markup" in m]
    finally:
        await c.close()


async def test_button_tap_completes_task_and_answers_callback(tg_env, clock):
    c, sent = await make(tg_env, clock)
    try:
        async with c.session_factory() as s:
            ctx = await c.make_context(s)
            t = await ctx.tasks.create(now=ctx.now, title="Call mom", kind="REMINDER", due_at=at(10))
            await s.commit()
        await handle_update(c, callback(f"done:{t.id}"))
        assert [m["method"] for m in sent] == ["answerCallbackQuery", "sendMessage"]
        assert "Done: Call mom" in sent[1]["text"]
        async with c.session_factory() as s:
            assert (await s.get(Task, t.id)).status == "DONE"
    finally:
        await c.close()


async def test_start_command_and_non_text(tg_env, clock):
    c, sent = await make(tg_env, clock, llm=ScriptedLLM([]))
    try:
        await handle_update(c, message("/start"))
        await handle_update(c, {"update_id": 3, "message": {"chat": {"id": int(CHAT)}, "photo": []}})
        await handle_update(c, {"update_id": 4, "edited_message": {}})
        assert "Hi!" in sent[0]["text"] and "only read text" in sent[1]["text"] and len(sent) == 2
    finally:
        await c.close()


async def test_notifier_pushes_to_telegram_with_inline_buttons_and_records(tg_env, clock):
    s, channel, sent, _ = tg_env
    c = build_container(s, clock=clock, telegram=channel)
    await c.start()
    try:
        buttons = [Button("✅ Done", "done:1"), Button("⏰ +30m", "snz:1:30"), Button("➡️ Tomorrow", "tmr:1"), Button("Extra", "x")]
        assert await c.notifier.send("Time to start", buttons) is True
        rows = sent[0]["reply_markup"]["inline_keyboard"]
        assert [[b["callback_data"] for b in r] for r in rows] == [["done:1", "snz:1:30", "tmr:1"], ["x"]]
        async with c.session_factory() as session:
            (m,) = (await session.execute(select(Message))).scalars().all()
            assert m.channel == "proactive" and m.buttons[0] == ["✅ Done", "done:1"]
    finally:
        await c.close()


async def test_notifier_reports_failure_and_does_not_record_when_telegram_is_down(settings, clock, http_mock):
    channel = TelegramChannel("123:ABC", CHAT, client=http_mock(lambda r: httpx.Response(500, json={"ok": False})))
    c = build_container(settings, clock=clock, telegram=channel)
    await c.start()
    try:
        assert await c.notifier.send("hello") is False
        async with c.session_factory() as session:
            assert (await session.execute(select(Message))).scalars().all() == []  # retry later won't duplicate it
    finally:
        await c.close()


async def test_notifier_without_telegram_still_records_for_the_web_ui(settings, clock):
    c = build_container(settings, clock=clock)
    await c.start()
    try:
        assert isinstance(c.notifier, Notifier) and await c.notifier.send("hello") is True
        async with c.session_factory() as session:
            assert (await session.execute(select(Message.content))).scalars().all() == ["hello"]
    finally:
        await c.close()


async def test_errors_never_leak_the_bot_token(http_mock):
    def handler(r):
        raise httpx.ConnectError("boom", request=r)

    channel = TelegramChannel("123:SECRET", CHAT, client=http_mock(handler))
    with pytest.raises(TelegramError) as e:
        await channel.send("x")
    assert "SECRET" not in str(e.value)

    channel = TelegramChannel("123:SECRET", CHAT, client=http_mock(lambda r: httpx.Response(401, json={"ok": False, "description": "Unauthorized"})))
    with pytest.raises(TelegramError, match="Unauthorized") as e:
        await channel.send("x")
    assert "SECRET" not in str(e.value)


def test_long_messages_are_split_on_line_boundaries():
    text = "\n".join(f"line {i:04d} " + "x" * 90 for i in range(200))
    parts = _chunks(text)
    assert len(parts) > 1 and all(len(p) <= 4000 for p in parts) and "\n".join(parts) == text
    assert len(_chunks("y" * 9000)) == 3
    assert _chunks("short") == ["short"]


async def test_poll_request_shape(tg_env):
    _, channel, sent, _ = tg_env
    assert await channel.get_updates(77, timeout=5) == []
    assert sent[0] == {"method": "getUpdates", "timeout": 5, "offset": 77, "allowed_updates": ["message", "callback_query"]}


def test_settings_default_to_no_telegram():
    assert Settings(_env_file=None).TELEGRAM_BOT_TOKEN == ""


async def test_token_without_chat_id_still_starts_in_onboarding_mode(settings, clock):
    s = settings.model_copy(update={"TELEGRAM_BOT_TOKEN": "123:ABC", "TELEGRAM_CHAT_ID": ""})
    c = build_container(s, clock=clock)
    await c.start()
    try:
        assert c.telegram is not None and not c.telegram.configured  # polls and replies with help...
        assert c.notifier.telegram is None  # ...but never pushes reminders to an unknown chat
    finally:
        await c.close()
