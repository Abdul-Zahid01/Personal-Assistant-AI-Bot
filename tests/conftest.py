from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.assistant.llm.base import ChatMessage, LLMResult, ToolCall, ToolSpec
from app.config import Settings
from app.container import Container, build_container
from app.domain.planner import Interval
from app.domain.timeutil import local_dt
from app.messaging import Button

TZ = ZoneInfo("Asia/Karachi")  # UTC+5, no DST: easy to reason about
TODAY = date(2026, 10, 14)  # a Wednesday


def at(h: int, m: int = 0, day: date = TODAY) -> datetime:
    """Local (Karachi) wall-clock time as a UTC datetime."""
    return local_dt(day, time(h, m), TZ)


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def set(self, now: datetime) -> None:
        self.now = now

    def advance(self, **kw) -> datetime:
        self.now += timedelta(**kw)
        return self.now


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, list[Button]]] = []
        self.fail = False

    async def send(self, text: str, buttons: list[Button] | None = None) -> bool:
        if self.fail:
            return False
        self.sent.append((text, buttons or []))
        return True

    @property
    def texts(self) -> list[str]:
        return [t for t, _ in self.sent]


class ScriptedLLM:
    """Plays back a fixed script of responses and records what it was asked."""

    name = "scripted"

    def __init__(self, script: list[LLMResult | Exception]):
        self.script = list(script)
        self.calls: list[tuple[str, list[ChatMessage], list[ToolSpec]]] = []

    async def complete(self, system, messages, tools) -> LLMResult:
        self.calls.append((system, list(messages), tools))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def call(name: str, **args) -> LLMResult:
    return LLMResult(tool_calls=[ToolCall(id="c1", name=name, args=args)])


def say(text: str) -> LLMResult:
    return LLMResult(text=text)


class FakeCalendar:
    def __init__(self):
        self.connected = True
        self.busy_intervals: list[Interval] = []
        self.events: dict[str, dict] = {}
        self.fail_busy = False
        self.fail_writes = False
        self._n = 0

    async def is_connected(self) -> bool:
        return self.connected

    async def busy(self, start, end):
        if self.fail_busy:
            raise RuntimeError("boom")
        return list(self.busy_intervals)

    async def upsert_event(self, *, event_id, title, start, end, block_id):
        if self.fail_writes:
            raise RuntimeError("calendar down")
        if event_id is None or event_id not in self.events:
            self._n += 1
            event_id = f"evt{self._n}"
        self.events[event_id] = {"title": title, "start": start, "end": end, "block_id": block_id}
        return event_id

    async def delete_event(self, event_id):
        if self.fail_writes:
            raise RuntimeError("calendar down")
        self.events.pop(event_id, None)


@pytest.fixture
def clock() -> Clock:
    return Clock(at(9))  # Wednesday 09:00 local


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None, DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", TIMEZONE="Asia/Karachi",
        SCHEDULER_ENABLED=False, API_KEY="", SECRET_KEY="test-secret", APP_ENV="test",
        GEMINI_API_KEY="", OPENAI_API_KEY="", TELEGRAM_BOT_TOKEN="", GOOGLE_CLIENT_ID="",
        FAST_REPLIES=False,  # most agent tests script the model's follow-up turn; fast replies have their own tests
    )


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
async def container(settings, clock, notifier):
    c = build_container(settings, clock=clock, notifier=notifier)  # real fallback chain => rule-based (no keys)
    await c.start()
    yield c
    await c.close()


@pytest.fixture
async def ctx(container: Container):
    async with container.session_factory() as session:
        yield await container.make_context(session)


@pytest.fixture
def http_mock():
    """Build an httpx.AsyncClient backed by a handler function, recording requests."""

    def make(handler):
        seen: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
        client.seen = seen  # type: ignore[attr-defined]
        return client

    return make
