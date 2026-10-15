import base64
import json
import os

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select

from app.channels.webpush import WebPushChannel, pywebpush_sender
from app.container import build_container
from app.db.models import Message, OAuthToken, PushSubscription, Task
from app.main import create_app
from app.messaging import Button
from tests.conftest import at

API = "/api/v1"
ENDPOINT = "https://push.example.com/send/abc"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class FakeSender:
    def __init__(self, status=201):
        self.status = status
        self.sent: list[tuple[dict, dict]] = []

    def __call__(self, info, data, key, subject):
        self.sent.append((info, json.loads(data)))
        if isinstance(self.status, Exception):
            raise self.status
        return self.status


@pytest.fixture
def sender():
    return FakeSender()


@pytest.fixture
async def env(settings, clock, sender):
    """A container whose notifier pushes through a fake sender; returns (container, push)."""
    c = build_container(settings, clock=clock)
    c.push.sender = sender
    c.notifier.push = c.push
    await c.start()
    yield c, c.push
    await c.close()


# ---------------------------------------------------------------------- keys
async def test_key_is_generated_once_and_stored_encrypted(env):
    c, push = env
    k1 = await push.public_key()
    raw = b64url_decode(k1)
    assert len(raw) == 65 and raw[0] == 4  # uncompressed P-256 point, what browsers expect
    async with c.session_factory() as s:
        row = await s.get(OAuthToken, "vapid")
        assert "PRIVATE KEY" not in row.data_encrypted  # encrypted at rest
    fresh = WebPushChannel(c.settings, c.session_factory, c.clock)  # e.g. after a restart
    assert await fresh.public_key() == k1


async def test_changed_secret_regenerates_key_and_drops_stale_subscriptions(env):
    c, push = env
    await push.public_key()
    await push.subscribe(ENDPOINT, "p", "a")
    other = WebPushChannel(c.settings.model_copy(update={"SECRET_KEY": "rotated"}), c.session_factory, c.clock)
    assert await other.public_key() != await push.public_key()
    assert await other.device_count() == 0


# ---------------------------------------------------------------------- subscriptions
async def test_subscribe_is_idempotent_and_unsubscribe_removes(env):
    _, push = env
    assert await push.subscribe(ENDPOINT, "p1", "a1") == 1
    assert await push.subscribe(ENDPOINT, "p2", "a2") == 1  # same device re-subscribing updates keys
    assert await push.subscribe(ENDPOINT + "x", "p", "a") == 2
    await push.unsubscribe(ENDPOINT)
    await push.unsubscribe("https://unknown")  # no error
    assert await push.device_count() == 1


async def test_only_https_endpoints(env):
    with pytest.raises(ValueError):
        await env[1].subscribe("http://evil.local/x", "p", "a")


# ---------------------------------------------------------------------- sending
async def test_payload_has_title_body_and_signed_actions(env, sender):
    _, push = env
    await push.subscribe(ENDPOINT, "p", "a")
    buttons = [Button("✅ Done", "done:t1"), Button("⏰ +30m", "snz:t1:30"), Button("➡️ Tomorrow", "tmr:t1"), Button("x", "evil:1")]
    assert await push.send("⏰ Reminder: Call mom", buttons) == 1
    info, payload = sender.sent[0]
    assert info == {"endpoint": ENDPOINT, "keys": {"p256dh": "p", "auth": "a"}}
    assert payload["title"] == "Your assistant" and payload["body"] == "⏰ Reminder: Call mom"
    assert [a["action"] for a in payload["actions"]] == ["done:t1", "snz:t1:30"]  # max 2, only allowed actions
    assert all(push.verify_action(a["action"], a["sig"]) for a in payload["actions"])


async def test_multiline_message_uses_first_line_as_title(env, sender):
    _, push = env
    await push.subscribe(ENDPOINT, "p", "a")
    await push.send("☀️ Good morning!\n9:00 Report\n2:00 Meeting")
    assert sender.sent[0][1]["title"] == "☀️ Good morning!" and sender.sent[0][1]["body"] == "9:00 Report\n2:00 Meeting"


async def test_no_devices_means_no_work(env, sender):
    assert await env[1].send("hi") == 0 and sender.sent == []


async def test_expired_subscriptions_are_removed(env, sender):
    _, push = env
    await push.subscribe(ENDPOINT, "p", "a")
    sender.status = 410
    assert await push.send("hi") == 0
    assert await push.device_count() == 0


async def test_errors_are_contained_and_counted(env, sender):
    c, push = env
    await push.subscribe(ENDPOINT, "p", "a")
    sender.status = RuntimeError("network down")
    assert await push.send("hi") == 0
    sender.status = 500
    assert await push.send("hi") == 0
    async with c.session_factory() as s:
        assert (await s.execute(select(PushSubscription))).scalar_one().failures == 2


async def test_signatures_cannot_be_forged_or_reused_for_other_actions(env):
    _, push = env
    sig = push.sign_action("done:t1")
    assert push.verify_action("done:t1", sig)
    assert not push.verify_action("done:t2", sig)
    assert not push.verify_action("done:t1", "0" * 32)
    assert not push.verify_action("done:t1", None)
    assert not push.verify_action("evil:t1", push.sign_action("evil:t1"))  # unknown action types refused


# ---------------------------------------------------------------------- notifier integration
async def test_reminders_are_pushed_and_still_recorded_when_push_fails(env, sender):
    c, push = env
    await push.subscribe(ENDPOINT, "p", "a")
    assert await c.notifier.send("⏰ Reminder: Stretch", [Button("✅ Done", "done:x")]) is True
    assert sender.sent and sender.sent[0][1]["body"] == "⏰ Reminder: Stretch"

    sender.status = RuntimeError("push service down")
    assert await c.notifier.send("second") is True  # push is best-effort; delivery to the app still counts
    async with c.session_factory() as s:
        assert (await s.execute(select(Message.content))).scalars().all() == ["⏰ Reminder: Stretch", "second"]


# ---------------------------------------------------------------------- HTTP API
@pytest.fixture
async def client(env, settings):
    c, _ = env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, c)), base_url="http://t") as http:
        yield http


async def test_subscribe_flow_over_http(client, sender):
    key = (await client.get(f"{API}/push/public-key")).json()["public_key"]
    assert len(b64url_decode(key)) == 65
    r = await client.post(f"{API}/push/subscribe", json={"endpoint": ENDPOINT, "keys": {"p256dh": "p", "auth": "a"}})
    assert r.json() == {"status": "subscribed", "devices": 1}
    assert (await client.post(f"{API}/push/test")).json() == {"sent": 1}
    assert "Notifications are on" in sender.sent[0][1]["title"]
    assert (await client.post(f"{API}/push/unsubscribe", json={"endpoint": ENDPOINT})).status_code == 200
    assert (await client.post(f"{API}/push/test")).json() == {"sent": 0}
    assert (await client.post(f"{API}/push/subscribe", json={"endpoint": "http://x", "keys": {"p256dh": "p", "auth": "a"}})).status_code == 400


async def test_notification_buttons_work_with_a_valid_signature_only(client, env):
    c, push = env
    async with c.session_factory() as s:
        ctx = await c.make_context(s)
        t = await ctx.tasks.create(now=ctx.now, title="Call mom", kind="REMINDER", due_at=at(10))
        await s.commit()
    data = f"done:{t.id}"
    assert (await client.post(f"{API}/push/action", json={"data": data, "sig": "forged"})).status_code == 403
    r = await client.post(f"{API}/push/action", json={"data": data, "sig": push.sign_action(data)})
    assert r.status_code == 200 and r.json()["reply"].startswith("✅ Done: Call mom")
    async with c.session_factory() as s:
        assert (await s.get(Task, t.id)).status == "DONE"


async def test_push_endpoints_need_the_api_key_but_actions_do_not(settings, clock):
    s = settings.model_copy(update={"API_KEY": "k"})
    c = build_container(s, clock=clock)
    await c.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(s, c)), base_url="http://t") as http:
            assert (await http.get(f"{API}/push/public-key")).status_code == 401
            assert (await http.post(f"{API}/push/test")).status_code == 401
            # the action endpoint is reachable without the key (the signature is the permission)
            assert (await http.post(f"{API}/push/action", json={"data": "done:x", "sig": "bad"})).status_code == 403
    finally:
        await c.close()


async def test_pwa_files_are_served(client):
    sw = await client.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"] and "notificationclick" in sw.text
    manifest = await client.get("/manifest.webmanifest")
    assert manifest.status_code == 200 and manifest.json()["display"] == "standalone"
    for icon in manifest.json()["icons"]:
        r = await client.get(icon["src"])
        assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert (await client.get("/static/apple-touch-icon.png")).status_code == 200


async def test_push_can_be_disabled(settings, clock):
    c = build_container(settings.model_copy(update={"WEB_PUSH_ENABLED": False}), clock=clock)
    await c.start()
    try:
        assert c.push is None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, c)), base_url="http://t") as http:
            assert (await http.get(f"{API}/push/public-key")).status_code == 400
    finally:
        await c.close()


# ---------------------------------------------------------------------- real encryption, end to end
def test_real_web_push_encryption_roundtrip(monkeypatch):
    """Act as the browser: create subscriber keys, let pywebpush encrypt + sign, then decrypt what was sent."""
    import http_ece
    import pywebpush

    browser_key = ec.generate_private_key(ec.SECP256R1())
    browser_pub = browser_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth_secret = os.urandom(16)
    server_key = ec.generate_private_key(ec.SECP256R1())
    captured = {}

    class Resp:
        status_code = 201
        text = ""

    def fake_post(url, data=None, headers=None, timeout=None, **kw):
        captured.update(url=url, data=data, headers=headers)
        return Resp()

    monkeypatch.setattr(pywebpush.requests, "post", fake_post)
    info = {"endpoint": "https://fcm.googleapis.com/fcm/send/xyz", "keys": {"p256dh": b64url(browser_pub), "auth": b64url(auth_secret)}}
    payload = json.dumps({"title": "⏰ Reminder", "body": "Call mom"})

    assert pywebpush_sender(info, payload, server_key, "mailto:me@example.com") == 201

    h = {k.lower(): v for k, v in captured["headers"].items()}
    assert h["content-encoding"] == "aes128gcm" and h["authorization"].startswith("vapid t=")
    assert payload.encode() not in captured["data"]  # really encrypted
    plain = http_ece.decrypt(captured["data"], private_key=browser_key, auth_secret=auth_secret, version="aes128gcm")
    assert json.loads(plain) == {"title": "⏰ Reminder", "body": "Call mom"}

    # the VAPID token is signed by our server key and addressed to the push service's origin
    server_pub = server_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    assert f"k={b64url(server_pub)}" in h["authorization"]
    claims = json.loads(b64url_decode(h["authorization"].split("t=")[1].split(",")[0].split(".")[1]))
    assert claims["aud"] == "https://fcm.googleapis.com" and claims["sub"] == "mailto:me@example.com"
