"""Telegram Bot API client + inbound update handling (polling or webhook)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import httpx

from app.db.models import Preference
from app.messaging import Button

if TYPE_CHECKING:  # pragma: no cover
    from app.container import Container

log = logging.getLogger(__name__)
MAX_LEN = 4000  # Telegram's hard limit is 4096


class TelegramError(RuntimeError):
    pass


def _chunks(text: str) -> list[str]:
    if len(text) <= MAX_LEN:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > MAX_LEN:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:MAX_LEN])
            line = line[MAX_LEN:]
        if len(cur) + len(line) + 1 > MAX_LEN:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


class TelegramChannel:
    def __init__(self, token: str, chat_id: str, client: httpx.AsyncClient | None = None):
        self.token = token
        self.chat_id = str(chat_id)
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0))
        self._base = f"https://api.telegram.org/bot{token}"

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    async def _call(self, method: str, **payload: Any) -> Any:
        try:
            resp = await self.client.post(f"{self._base}/{method}", json=payload)
        except httpx.HTTPError as exc:
            raise TelegramError(f"{method}: {exc.__class__.__name__}") from exc  # never include the URL (it has the token)
        data: dict[str, Any] = {}
        try:
            data = resp.json()
        except ValueError:
            pass
        if resp.status_code != 200 or not data.get("ok"):
            raise TelegramError(f"{method}: {resp.status_code} {data.get('description', '')}".strip())
        return data.get("result")

    async def send(self, text: str, buttons: list[Button] | None = None) -> None:
        parts = _chunks(text or "…")
        for i, part in enumerate(parts):
            payload: dict[str, Any] = {"chat_id": self.chat_id, "text": part}
            if buttons and i == len(parts) - 1:
                rows = [[{"text": b.label, "callback_data": b.data} for b in buttons[j:j + 3]] for j in range(0, len(buttons), 3)]
                payload["reply_markup"] = {"inline_keyboard": rows}
            await self._call("sendMessage", **payload)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            await self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError as exc:  # the spinner on the button is cosmetic
            log.debug("answerCallbackQuery failed: %s", exc)

    async def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return await self._call("getUpdates", **payload) or []

    async def set_webhook(self, url: str, secret: str) -> None:
        await self._call("setWebhook", url=url, secret_token=secret, allowed_updates=["message", "callback_query"])

    async def delete_webhook(self) -> None:
        await self._call("deleteWebhook")

    async def close(self) -> None:
        await self.client.aclose()


def is_allowed_chat(container: "Container", chat_id: Any) -> bool:
    allowed = container.settings.TELEGRAM_CHAT_ID
    return bool(allowed) and str(chat_id) == str(allowed)


async def handle_update(container: "Container", update: dict[str, Any]) -> None:
    """Process one Telegram update. Only the configured chat is ever served."""
    tg = container.telegram
    if tg is None:
        return

    callback = update.get("callback_query")
    if callback:
        chat_id = (callback.get("message") or {}).get("chat", {}).get("id")
        if not is_allowed_chat(container, chat_id):
            log.warning("Ignoring callback from unauthorised chat %s", chat_id)
            return
        await tg.answer_callback(callback["id"])
        reply = await container.actions.run(callback.get("data", ""), channel="telegram")
        await tg.send(reply.text, reply.buttons)
        return

    message = update.get("message")
    if not message:
        return
    chat_id = message.get("chat", {}).get("id")
    if not is_allowed_chat(container, chat_id):
        log.warning("Ignoring message from unauthorised chat %s", chat_id)
        if not container.settings.TELEGRAM_CHAT_ID:  # onboarding help: tell the owner which id to configure
            await _quiet_send(tg, chat_id, f"Hi! To link this chat, set TELEGRAM_CHAT_ID={chat_id} in your .env and restart.")
        return

    text = (message.get("text") or "").strip()
    if not text:
        await tg.send("I can only read text messages for now.")
        return
    if text in ("/start", "/help"):
        await tg.send("Hi! Tell me what you need to do (\"finish report by Friday 5pm, it's urgent\"), "
                      "ask \"plan my day\", or say \"done\" when you finish something. I'll nudge you along the way.")
        return
    reply = await container.assistant.handle(text, channel="telegram")
    await tg.send(reply.text, reply.buttons)


async def _quiet_send(tg: TelegramChannel, chat_id: Any, text: str) -> None:
    try:
        await tg._call("sendMessage", chat_id=chat_id, text=text)
    except TelegramError as exc:
        log.warning("Could not send onboarding message: %s", exc)


class TelegramPoller:
    """Long-polls getUpdates. Works behind NAT on a laptop; no public URL needed."""

    def __init__(self, container: "Container"):
        self.container = container

    async def run(self) -> None:
        from app.services.prefs import put_internal

        tg = self.container.telegram
        assert tg is not None
        async with self.container.session_factory() as session:
            row = await session.get(Preference, "telegram_offset")
            offset = row.value if row else None

        try:  # polling and a registered webhook are mutually exclusive on Telegram's side
            await tg.delete_webhook()
        except TelegramError as exc:
            log.warning("deleteWebhook failed: %s", exc)

        failures = 0
        while True:
            try:
                updates = await tg.get_updates(offset)
                failures = 0
                for update in updates:
                    offset = update["update_id"] + 1
                    try:
                        await handle_update(self.container, update)
                    except Exception:
                        log.exception("Failed to handle Telegram update %s", update.get("update_id"))
                if updates:
                    async with self.container.session_factory() as session:
                        await put_internal(session, "telegram_offset", offset)
                        await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                delay = min(60, 2 ** min(failures, 6))
                log.warning("Telegram polling error (%s); retrying in %ss", exc, delay)
                await asyncio.sleep(delay)
