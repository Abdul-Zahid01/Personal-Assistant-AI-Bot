"""Delivers proactive messages (reminders, briefs, reviews).

Every message is recorded in the database, which is what the web chat shows. If Telegram is configured the
message is pushed there too, and a Telegram failure counts as a failed delivery so the caller retries.
Web Push notifications go out best-effort after the message is recorded.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.telegram import TelegramChannel
from app.channels.webpush import WebPushChannel
from app.db.models import Message
from app.messaging import Button

log = logging.getLogger(__name__)


class NotifierPort(Protocol):
    async def send(self, text: str, buttons: list[Button] | None = None) -> bool: ...


class Notifier:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Callable[[], datetime],
        telegram: TelegramChannel | None = None,
        push: WebPushChannel | None = None,
    ):
        self.session_factory = session_factory
        self.clock = clock
        self.telegram = telegram if telegram and telegram.configured else None
        self.push = push

    async def send(self, text: str, buttons: list[Button] | None = None) -> bool:
        if self.telegram is not None:
            try:
                await self.telegram.send(text, buttons)
            except Exception as exc:
                log.warning("Telegram delivery failed: %s", exc)
                return False
        async with self.session_factory() as session:
            session.add(Message(
                role="assistant", content=text, channel="proactive", created_at=self.clock(),
                buttons=[b.as_pair() for b in buttons] if buttons else None,
            ))
            await session.commit()
        if self.push is not None:  # best effort: the message is already safely recorded
            try:
                await self.push.send(text, buttons)
            except Exception as exc:
                log.warning("Web Push delivery failed: %s", exc)
        return True
