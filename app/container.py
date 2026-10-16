"""Wires the app together. Everything external (LLM, Telegram, Google, clock) is injected, so tests swap in fakes."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.assistant.actions import ActionHandler
from app.assistant.agent import Assistant
from app.assistant.llm.base import LLMProvider
from app.assistant.llm.factory import build_llm
from app.assistant.tools import ToolContext
from app.channels.notifier import Notifier, NotifierPort
from app.channels.telegram import TelegramChannel
from app.channels.webpush import WebPushChannel
from app.config import Settings
from app.db.migrate import prepare_database
from app.db.session import make_engine, make_session_factory
from app.domain.timeutil import get_tz, utcnow
from app.events import EventBus
from app.integrations.google_calendar import GoogleCalendar
from app.services.calendar_sync import reconcile
from app.services.planning import CalendarPort, PlanningService
from app.services.prefs import load_prefs
from app.services.routines import RoutineService
from app.services.tasks import TaskService

log = logging.getLogger(__name__)


class Container:
    def __init__(
        self,
        settings: Settings,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        llm: LLMProvider,
        notifier: NotifierPort,
        telegram: TelegramChannel | None = None,
        calendar: CalendarPort | None = None,
        clock: Callable[[], datetime] = utcnow,
        push: WebPushChannel | None = None,
    ):
        self.settings = settings
        self.engine = engine
        self.session_factory = session_factory
        self.llm = llm
        self.notifier = notifier
        self.telegram = telegram
        self.calendar = calendar
        self.clock = clock
        self.push = push
        self.bus = EventBus()
        self.tz: ZoneInfo = get_tz(settings.TIMEZONE)
        self.chat_lock = asyncio.Lock()
        self.assistant = Assistant(self)
        self.actions = ActionHandler(self)
        self._sync_task: asyncio.Task | None = None
        self._sync_again = False

    def now(self) -> datetime:
        return self.clock()

    async def make_context(self, session: AsyncSession) -> ToolContext:
        prefs = await load_prefs(session, self.settings)
        self.tz = get_tz(prefs["timezone"])
        return ToolContext(
            session=session, container=self, prefs=prefs, now=self.now(), tz=self.tz,
            tasks=TaskService(session, prefs), planning=PlanningService(session, prefs, self.calendar),
            routines=RoutineService(session, prefs),
        )

    # ------------------------------------------------------------------ calendar sync
    async def sync_calendar(self) -> int:
        """Push pending block changes to Google. Never raises: failures are retried on the next run."""
        if self.calendar is None:
            return 0
        try:
            async with self.session_factory() as session:
                return await reconcile(session, self.calendar, self.now())
        except Exception as exc:
            log.warning("Calendar sync failed: %s", exc)
            return 0

    def after_change(self) -> None:
        """Call after any data change: tells open tabs to refresh and syncs the calendar (fire-and-forget)."""
        self.bus.publish("changed")
        if self.calendar is None:
            return
        if self._sync_task is not None and not self._sync_task.done():
            self._sync_again = True
            return
        self._sync_task = asyncio.create_task(self._sync_loop())

    async def _sync_loop(self) -> None:
        while True:
            self._sync_again = False
            await self.sync_calendar()
            if not self._sync_again:
                return

    async def wait_for_sync(self) -> None:
        if self._sync_task is not None:
            await self._sync_task

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.settings.AUTO_CREATE_TABLES:  # create a new database, or migrate an existing one to the latest schema
            result = await prepare_database(self.engine, self.settings.DATABASE_URL)
            log.info("Database: %s", result)

    async def close(self) -> None:
        self.bus.close()
        if self._sync_task is not None and not self._sync_task.done():
            self._sync_task.cancel()
        if self.telegram is not None:
            await self.telegram.close()
        await self.engine.dispose()


def build_container(settings: Settings, **overrides) -> Container:
    engine = overrides.pop("engine", None) or make_engine(settings.DATABASE_URL)
    session_factory = make_session_factory(engine)
    clock = overrides.pop("clock", utcnow)
    holder: dict[str, Container] = {}

    telegram = overrides.pop("telegram", None)
    if telegram is None and settings.TELEGRAM_BOT_TOKEN:  # without a chat id it only answers with onboarding help
        telegram = TelegramChannel(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID)

    calendar = overrides.pop("calendar", None)
    if calendar is None and settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET:
        calendar = GoogleCalendar(settings, session_factory, clock=clock, timezone_name=lambda: holder["c"].tz.key)

    push = overrides.pop("push", None)
    if push is None and settings.WEB_PUSH_ENABLED:
        push = WebPushChannel(settings, session_factory, clock)

    llm = overrides.pop("llm", None) or build_llm(settings, lambda: (holder["c"].now(), holder["c"].tz))
    notifier = overrides.pop("notifier", None) or Notifier(session_factory, clock, telegram, push)
    if overrides:
        raise TypeError(f"Unexpected overrides: {', '.join(overrides)}")

    container = Container(settings, engine, session_factory, llm, notifier, telegram, calendar, clock, push)
    if isinstance(notifier, Notifier):
        notifier.bus = container.bus
    holder["c"] = container
    return container
