from __future__ import annotations

import hmac
import html
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, HttpUrl

from app.api.deps import get_container, require_api_key
from app.channels.telegram import TelegramError, handle_update
from app.container import Container
from app.integrations.google_calendar import CalendarError, GoogleCalendar, make_state, verify_state

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])
callbacks = APIRouter()  # reached by Google / Telegram, not by the web UI: protected by state / secret instead of the API key


def _google(container: Container) -> GoogleCalendar:
    if not isinstance(container.calendar, GoogleCalendar):
        raise HTTPException(400, "Google Calendar isn't configured (set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET).")
    return container.calendar


# ---------------------------------------------------------------------- Google
@router.post("/google/connect")
async def google_connect(container: Container = Depends(get_container)) -> dict[str, str]:
    """Returns the Google consent URL; open it in a browser."""
    try:
        return {"url": _google(container).auth_url(make_state(container.settings.SECRET_KEY))}
    except CalendarError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/google/status")
async def google_status(container: Container = Depends(get_container)) -> dict[str, Any]:
    if not isinstance(container.calendar, GoogleCalendar):
        return {"configured": False, "connected": False}
    return await container.calendar.status()


@router.post("/google/disconnect")
async def google_disconnect(container: Container = Depends(get_container)) -> dict[str, str]:
    await _google(container).disconnect()
    return {"status": "disconnected"}


@router.post("/google/sync")
async def google_sync(container: Container = Depends(get_container)) -> dict[str, int]:
    _google(container)
    return {"writes": await container.sync_calendar()}


@callbacks.get("/auth/google/callback", response_class=HTMLResponse)
async def google_callback(code: str | None = None, state: str | None = None, error: str | None = None,
                          container: Container = Depends(get_container)) -> HTMLResponse:
    """Google redirects the browser here. Tokens are stored encrypted and never shown."""
    def page(message: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>Google Calendar</title>"
                            f"<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
                            f"<h2>{html.escape(message)}</h2><p>You can close this tab.</p>", status_code=status)

    if error:
        return page(f"Google said no: {error}", 400)
    if not verify_state(container.settings.SECRET_KEY, state):
        return page("This link expired or is invalid. Start the connection again.", 400)
    if not code:
        return page("Missing authorization code.", 400)
    try:
        await _google(container).exchange_code(code)
    except (CalendarError, HTTPException) as exc:
        log.warning("Google OAuth callback failed: %s", exc)
        return page("Couldn't connect Google Calendar. Check the server logs and try again.", 400)
    container.schedule_calendar_sync()
    return page("Google Calendar connected ✅")


# ---------------------------------------------------------------------- Telegram
class WebhookIn(BaseModel):
    url: HttpUrl


@router.post("/telegram/set-webhook")
async def telegram_set_webhook(body: WebhookIn, container: Container = Depends(get_container)) -> dict[str, str]:
    if container.telegram is None:
        raise HTTPException(400, "Telegram isn't configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
    if not container.settings.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(400, "Set TELEGRAM_WEBHOOK_SECRET first.")
    try:
        await container.telegram.set_webhook(str(body.url), container.settings.TELEGRAM_WEBHOOK_SECRET)
    except TelegramError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"status": "webhook set"}


@callbacks.post("/api/v1/telegram/webhook")
async def telegram_webhook(request: Request, background: BackgroundTasks, container: Container = Depends(get_container),
                           x_telegram_bot_api_secret_token: str | None = Header(default=None)) -> dict[str, str]:
    secret = container.settings.TELEGRAM_WEBHOOK_SECRET
    if not secret or not x_telegram_bot_api_secret_token or not hmac.compare_digest(x_telegram_bot_api_secret_token, secret):
        raise HTTPException(401, "Bad secret.")
    update = await request.json()
    background.add_task(_safe_handle, container, update)  # answer Telegram fast; the LLM may take a while
    return {"status": "ok"}


async def _safe_handle(container: Container, update: dict[str, Any]) -> None:
    try:
        await handle_update(container, update)
    except Exception:
        log.exception("Telegram webhook update failed")


# ---------------------------------------------------------------------- Web Push
class PushKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscribeIn(BaseModel):
    endpoint: str
    keys: PushKeys


class PushUnsubscribeIn(BaseModel):
    endpoint: str


class PushActionIn(BaseModel):
    data: str
    sig: str


def _push(container: Container):
    if container.push is None:
        raise HTTPException(400, "Web Push is disabled (WEB_PUSH_ENABLED=false).")
    return container.push


@router.get("/push/public-key")
async def push_public_key(container: Container = Depends(get_container)) -> dict[str, str]:
    return {"public_key": await _push(container).public_key()}


@router.post("/push/subscribe")
async def push_subscribe(body: PushSubscribeIn, request: Request, container: Container = Depends(get_container)) -> dict[str, Any]:
    try:
        devices = await _push(container).subscribe(body.endpoint, body.keys.p256dh, body.keys.auth,
                                                   request.headers.get("user-agent"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "subscribed", "devices": devices}


@router.post("/push/unsubscribe")
async def push_unsubscribe(body: PushUnsubscribeIn, container: Container = Depends(get_container)) -> dict[str, str]:
    await _push(container).unsubscribe(body.endpoint)
    return {"status": "unsubscribed"}


@router.post("/push/test")
async def push_test(container: Container = Depends(get_container)) -> dict[str, int]:
    sent = await _push(container).send("🔔 Notifications are on\nThis is how your reminders will look.")
    return {"sent": sent}


@callbacks.post("/api/v1/push/action")
async def push_action(body: PushActionIn, container: Container = Depends(get_container)) -> dict[str, str]:
    """Called by the service worker when you tap a button on a notification. The signature is the permission."""
    push = _push(container)
    if not push.verify_action(body.data, body.sig):
        raise HTTPException(403, "Invalid action signature.")
    reply = await container.actions.run(body.data, channel="push")
    return {"reply": reply.text}
