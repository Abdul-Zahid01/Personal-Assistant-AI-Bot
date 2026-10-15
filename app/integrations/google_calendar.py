"""Google Calendar: read busy time, write planned blocks/events to a dedicated calendar.

Tokens are encrypted at rest (Fernet, key derived from SECRET_KEY) and never returned by any API.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlencode

import httpx
from cryptography.fernet import InvalidToken
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import OAuthToken
from app.domain.planner import Interval
from app.security import make_fernet

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/calendar"
STATE_MAX_AGE = 600


class CalendarError(RuntimeError):
    pass


class NotConnected(CalendarError):
    pass


def make_state(secret: str) -> str:
    """Signed, expiring OAuth `state` value (CSRF protection for the callback)."""
    payload = f"{int(time.time())}.{secrets.token_urlsafe(8)}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def verify_state(secret: str, state: str | None, now: float | None = None) -> bool:
    try:
        ts, nonce, sig = (state or "").split(".")
        expected = hmac.new(secret.encode(), f"{ts}.{nonce}".encode(), hashlib.sha256).hexdigest()[:32]
        age = (now or time.time()) - int(ts)
        return hmac.compare_digest(sig, expected) and 0 <= age <= STATE_MAX_AGE
    except (ValueError, TypeError):
        return False


class GoogleCalendar:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        timezone_name: Callable[[], str] = lambda: "UTC",
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.client = client or httpx.AsyncClient(timeout=20)
        self.clock = clock
        self.timezone_name = timezone_name
        self._fernet = make_fernet(settings.SECRET_KEY)

    @property
    def configured(self) -> bool:
        return bool(self.settings.GOOGLE_CLIENT_ID and self.settings.GOOGLE_CLIENT_SECRET)

    # ------------------------------------------------------------------ OAuth
    def auth_url(self, state: str) -> str:
        if not self.configured:
            raise CalendarError("Google OAuth isn't configured (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET).")
        return AUTH_URL + "?" + urlencode({
            "client_id": self.settings.GOOGLE_CLIENT_ID, "redirect_uri": self.settings.GOOGLE_REDIRECT_URI,
            "response_type": "code", "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
        })

    async def exchange_code(self, code: str) -> None:
        resp = await self.client.post(TOKEN_URL, data={
            "code": code, "client_id": self.settings.GOOGLE_CLIENT_ID, "client_secret": self.settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": self.settings.GOOGLE_REDIRECT_URI, "grant_type": "authorization_code",
        })
        if resp.status_code != 200:
            raise CalendarError(f"Google rejected the authorization code ({_google_error(resp)}).")
        payload = resp.json()
        existing = await self._load() or {}
        data = {
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token") or existing.get("refresh_token", ""),
            "expires_at": self._expiry(payload),
            "scope": payload.get("scope", SCOPE),
            "calendar_id": existing.get("calendar_id", ""),
        }
        await self._save(data)

    async def disconnect(self) -> None:
        async with self.session_factory() as session:
            row = await session.get(OAuthToken, "google")
            if row is not None:
                await session.delete(row)
                await session.commit()

    async def is_connected(self) -> bool:
        data = await self._load()
        return bool(data and (data.get("refresh_token") or data.get("access_token")))

    async def status(self) -> dict[str, Any]:
        data = await self._load()
        return {
            "configured": self.configured,
            "connected": bool(data and (data.get("refresh_token") or data.get("access_token"))),
            "calendar": self.settings.GOOGLE_CALENDAR_NAME,
            "expires_at": (data or {}).get("expires_at"),
        }

    # ------------------------------------------------------------------ calendar operations
    async def busy(self, start: datetime, end: datetime) -> list[Interval]:
        resp = await self._request("GET", f"{API}/calendars/primary/events", params={
            "timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
            "orderBy": "startTime", "maxResults": 250,
        })
        out: list[Interval] = []
        for item in resp.json().get("items", []):
            if item.get("status") == "cancelled" or item.get("transparency") == "transparent":
                continue
            if (item.get("extendedProperties") or {}).get("private", {}).get("assistant") == "1":
                continue  # our own blocks are not "busy"; the planner replaces them
            s, e = (item.get("start") or {}).get("dateTime"), (item.get("end") or {}).get("dateTime")
            if not s or not e:  # all-day events don't block time
                continue
            out.append(Interval(datetime.fromisoformat(s).astimezone(timezone.utc), datetime.fromisoformat(e).astimezone(timezone.utc)))
        return out

    async def upsert_event(self, *, event_id: str | None, title: str, start: datetime, end: datetime, block_id: str) -> str:
        calendar_id = await self._calendar_id()
        body = {
            "summary": title,
            "description": "Planned by your Personal AI Assistant",
            "start": {"dateTime": start.astimezone(timezone.utc).isoformat()},
            "end": {"dateTime": end.astimezone(timezone.utc).isoformat()},
            "extendedProperties": {"private": {"assistant": "1", "block_id": block_id}},
        }
        if event_id:
            resp = await self._request("PATCH", f"{API}/calendars/{calendar_id}/events/{event_id}", json=body, ok=(200, 404))
            if resp.status_code == 200:
                return event_id
        resp = await self._request("POST", f"{API}/calendars/{calendar_id}/events", json=body)
        return resp.json()["id"]

    async def delete_event(self, event_id: str) -> None:
        calendar_id = await self._calendar_id()
        await self._request("DELETE", f"{API}/calendars/{calendar_id}/events/{event_id}", ok=(200, 204, 404, 410))

    # ------------------------------------------------------------------ internals
    async def _calendar_id(self) -> str:
        name = self.settings.GOOGLE_CALENDAR_NAME
        if name.lower() == "primary":
            return "primary"
        data = await self._load() or {}
        if data.get("calendar_id"):
            return data["calendar_id"]
        resp = await self._request("GET", f"{API}/users/me/calendarList", params={"minAccessRole": "owner"})
        found = next((c["id"] for c in resp.json().get("items", []) if c.get("summary") == name), None)
        if found is None:
            created = await self._request("POST", f"{API}/calendars", json={"summary": name, "timeZone": self.timezone_name()})
            found = created.json()["id"]
        data["calendar_id"] = found
        await self._save(data)
        return found

    async def _request(self, method: str, url: str, ok: tuple[int, ...] = (200, 201, 204), **kw: Any) -> httpx.Response:
        token = await self._access_token()
        resp = await self.client.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
        if resp.status_code == 401:  # token revoked/expired early: refresh once and retry
            token = await self._access_token(force_refresh=True)
            resp = await self.client.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
        if resp.status_code not in ok:
            raise CalendarError(f"Google Calendar {method} failed: {resp.status_code} {_google_error(resp)}")
        return resp

    async def _access_token(self, force_refresh: bool = False) -> str:
        data = await self._load()
        if not data:
            raise NotConnected("Google Calendar isn't connected.")
        expires = _parse_iso(data.get("expires_at"))
        fresh = data.get("access_token") and expires and self.clock() < expires - timedelta(seconds=60)
        if fresh and not force_refresh:
            return data["access_token"]
        if not data.get("refresh_token"):
            raise NotConnected("Google connection expired; please reconnect.")
        resp = await self.client.post(TOKEN_URL, data={
            "client_id": self.settings.GOOGLE_CLIENT_ID, "client_secret": self.settings.GOOGLE_CLIENT_SECRET,
            "refresh_token": data["refresh_token"], "grant_type": "refresh_token",
        })
        if resp.status_code != 200:
            raise CalendarError(f"Google token refresh failed ({_google_error(resp)}).")
        payload = resp.json()
        data.update(access_token=payload["access_token"], expires_at=self._expiry(payload))
        await self._save(data)
        return data["access_token"]

    def _expiry(self, payload: dict[str, Any]) -> str:
        return (self.clock() + timedelta(seconds=int(payload.get("expires_in", 3600)))).isoformat()

    async def _load(self) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            row = await session.get(OAuthToken, "google")
            if row is None:
                return None
            try:
                return json.loads(self._fernet.decrypt(row.data_encrypted.encode()))
            except (InvalidToken, ValueError):
                log.error("Stored Google token can't be decrypted (SECRET_KEY changed?); reconnect Google.")
                return None

    async def _save(self, data: dict[str, Any]) -> None:
        blob = self._fernet.encrypt(json.dumps(data).encode()).decode()
        async with self.session_factory() as session:
            row = await session.get(OAuthToken, "google")
            if row is None:
                session.add(OAuthToken(provider="google", data_encrypted=blob, updated_at=self.clock()))
            else:
                row.data_encrypted, row.updated_at = blob, self.clock()
            await session.commit()


def _parse_iso(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _google_error(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("status") or resp.status_code)
        return str(err or resp.status_code)
    except ValueError:
        return str(resp.status_code)
