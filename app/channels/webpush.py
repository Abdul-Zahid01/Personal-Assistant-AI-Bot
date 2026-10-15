"""Web Push: notifications on your computer or phone even when the app's tab is closed.

The server owns one VAPID key pair (generated on first use, stored encrypted). Browsers subscribe with the public key;
we send each notification, encrypted end-to-end, to every subscribed device. Buttons on a notification carry a signed
action so the service worker can run "Done" / "+30m" without an API key.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import OAuthToken, PushSubscription
from app.messaging import Button
from app.security import make_fernet, sign, verify

log = logging.getLogger(__name__)

ACTION_PURPOSE = "push-action"
ALLOWED_ACTIONS = ("done:", "snz:", "tmr:", "plan:")
MAX_ACTIONS = 2  # what most browsers show on a notification
GONE = (404, 410)  # the browser unsubscribed or the subscription expired


@dataclass
class Subscription:
    endpoint: str
    p256dh: str
    auth: str


# (subscription_info, json payload, private key, subject) -> HTTP status code. Blocking; run in a thread.
Sender = Callable[[dict, str, ec.EllipticCurvePrivateKey, str], int]


def pywebpush_sender(subscription_info: dict, data: str, key: ec.EllipticCurvePrivateKey, subject: str) -> int:
    from py_vapid import Vapid02  # RFC 8292 "vapid t=..., k=..." (what Apple requires)
    from pywebpush import WebPushException, webpush

    try:
        resp = webpush(subscription_info, data=data, vapid_private_key=Vapid02(private_key=key),
                       vapid_claims={"sub": subject}, ttl=6 * 3600, timeout=10)
        return resp.status_code
    except WebPushException as exc:
        if exc.response is not None:
            return exc.response.status_code
        raise


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class WebPushChannel:
    def __init__(self, settings: Settings, session_factory: async_sessionmaker[AsyncSession],
                 clock: Callable[[], datetime], sender: Sender = pywebpush_sender):
        self.settings = settings
        self.session_factory = session_factory
        self.clock = clock
        self.sender = sender
        self._fernet = make_fernet(settings.SECRET_KEY)
        self._key: ec.EllipticCurvePrivateKey | None = None
        self._key_lock = asyncio.Lock()

    # ------------------------------------------------------------------ keys
    async def _private_key(self) -> ec.EllipticCurvePrivateKey:
        if self._key is not None:
            return self._key
        async with self._key_lock:
            if self._key is not None:
                return self._key
            async with self.session_factory() as session:
                row = await session.get(OAuthToken, "vapid")
                key = None
                if row is not None:
                    try:
                        key = serialization.load_pem_private_key(self._fernet.decrypt(row.data_encrypted.encode()), None)
                    except Exception:
                        log.error("Stored Web Push key can't be decrypted (SECRET_KEY changed?); generating a new one. "
                                  "Devices must enable notifications again.")
                        await session.delete(row)
                        await session.flush()
                        for sub in (await session.execute(select(PushSubscription))).scalars():
                            await session.delete(sub)  # they were tied to the old key
                        row = None
                if key is None:
                    key = ec.generate_private_key(ec.SECP256R1())
                    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption())
                    session.add(OAuthToken(provider="vapid", data_encrypted=self._fernet.encrypt(pem).decode(),
                                           updated_at=self.clock()))
                await session.commit()
            self._key = key  # type: ignore[assignment]
            return self._key  # type: ignore[return-value]

    async def public_key(self) -> str:
        """The applicationServerKey browsers need to subscribe (uncompressed P-256 point, base64url)."""
        key = await self._private_key()
        raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return _b64url(raw)

    # ------------------------------------------------------------------ subscriptions
    async def subscribe(self, endpoint: str, p256dh: str, auth: str, user_agent: str | None = None) -> int:
        if not endpoint.startswith("https://"):
            raise ValueError("A push endpoint must be an https URL.")
        async with self.session_factory() as session:
            existing = (await session.execute(select(PushSubscription).where(PushSubscription.endpoint == endpoint))).scalar_one_or_none()
            if existing is None:
                session.add(PushSubscription(endpoint=endpoint, p256dh=p256dh, auth=auth, user_agent=(user_agent or "")[:255],
                                             created_at=self.clock()))
            else:
                existing.p256dh, existing.auth, existing.failures = p256dh, auth, 0
            await session.commit()
            return len((await session.execute(select(PushSubscription.id))).all())

    async def unsubscribe(self, endpoint: str) -> None:
        async with self.session_factory() as session:
            sub = (await session.execute(select(PushSubscription).where(PushSubscription.endpoint == endpoint))).scalar_one_or_none()
            if sub is not None:
                await session.delete(sub)
                await session.commit()

    async def device_count(self) -> int:
        async with self.session_factory() as session:
            return len((await session.execute(select(PushSubscription.id))).all())

    # ------------------------------------------------------------------ actions
    def sign_action(self, data: str) -> str:
        return sign(self.settings.SECRET_KEY, ACTION_PURPOSE, data)

    def verify_action(self, data: str, signature: str | None) -> bool:
        return data.startswith(ALLOWED_ACTIONS) and verify(self.settings.SECRET_KEY, ACTION_PURPOSE, data, signature)

    # ------------------------------------------------------------------ sending
    def payload(self, text: str, buttons: list[Button] | None) -> str:
        lines = text.strip().split("\n")
        title, body = (lines[0], "\n".join(lines[1:]).strip()) if len(lines) > 1 else ("Your assistant", lines[0])
        actions = [{"action": b.data, "title": b.label, "sig": self.sign_action(b.data)}
                   for b in (buttons or []) if b.data.startswith(ALLOWED_ACTIONS)][:MAX_ACTIONS]
        return json.dumps({"title": title[:120], "body": body[:1000], "url": "/", "actions": actions})

    async def send(self, text: str, buttons: list[Button] | None = None) -> int:
        """Push to every subscribed device. Returns how many accepted it. Never raises."""
        async with self.session_factory() as session:
            subs = (await session.execute(select(PushSubscription))).scalars().all()
            if not subs:
                return 0
            key = await self._private_key()
            data = self.payload(text, buttons)
            delivered = 0
            for sub in subs:
                info = {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}
                try:
                    status = await asyncio.to_thread(self.sender, info, data, key, self.settings.VAPID_SUBJECT)
                except Exception as exc:
                    status = 0
                    log.warning("Web Push to %s… failed: %s", sub.endpoint[:40], exc)
                if 200 <= status < 300:
                    delivered += 1
                    sub.failures, sub.last_success_at = 0, self.clock()
                elif status in GONE:
                    log.info("Removing expired push subscription %s…", sub.endpoint[:40])
                    await session.delete(sub)
                else:
                    sub.failures += 1
                    if sub.failures >= 20:  # dead for a long time: stop trying
                        await session.delete(sub)
            await session.commit()
            return delivered
