import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.models import OAuthToken
from app.db.session import create_tables, make_engine, make_session_factory
from app.integrations.google_calendar import CalendarError, GoogleCalendar, NotConnected, make_state, verify_state
from tests.conftest import Clock, at

API = "https://www.googleapis.com/calendar/v3"


class FakeGoogle:
    """Just enough of Google's OAuth + Calendar API to exercise the client."""

    def __init__(self):
        self.calendars = {}
        self.events = {}
        self.primary_events = []
        self.token_requests = 0
        self.expire_access_tokens = False
        self.calendar_name_exists = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, method = str(request.url), request.method
        path = request.url.path
        if url.startswith("https://oauth2.googleapis.com/token"):
            self.token_requests += 1
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                if form["code"] == ["bad"]:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT", "expires_in": 3600, "scope": "calendar"})
            return httpx.Response(200, json={"access_token": f"AT{self.token_requests}", "expires_in": 3600})
        if request.headers.get("authorization", "").endswith("EXPIRED"):
            return httpx.Response(401, json={"error": {"message": "expired"}})
        if path.endswith("/users/me/calendarList"):
            items = [{"id": "cal-existing", "summary": "Assistant"}] if self.calendar_name_exists else [{"id": "primary", "summary": "me"}]
            return httpx.Response(200, json={"items": items})
        if method == "POST" and path.endswith("/calendars"):
            return httpx.Response(200, json={"id": "cal-new"})
        if path.endswith("/calendars/primary/events") and method == "GET":
            return httpx.Response(200, json={"items": self.primary_events})
        if method == "POST" and path.endswith("/events"):
            eid = f"ev{len(self.events) + 1}"
            self.events[eid] = json.loads(request.content)
            return httpx.Response(200, json={"id": eid})
        if "/events/" in path:
            eid = path.rsplit("/", 1)[-1]
            if method == "PATCH":
                if eid not in self.events:
                    return httpx.Response(404, json={"error": {"message": "gone"}})
                self.events[eid] = json.loads(request.content)
                return httpx.Response(200, json={"id": eid})
            if method == "DELETE":
                if eid not in self.events:
                    return httpx.Response(410)
                del self.events[eid]
                return httpx.Response(204)
        return httpx.Response(500, json={"error": {"message": f"unhandled {method} {path}"}})


@pytest.fixture
async def gcal(tmp_path, http_mock):
    settings = Settings(_env_file=None, SECRET_KEY="s3cret", GOOGLE_CLIENT_ID="cid", GOOGLE_CLIENT_SECRET="csecret",
                        GOOGLE_REDIRECT_URI="http://localhost:8000/auth/google/callback")
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'g.db'}")
    await create_tables(engine)
    sf = make_session_factory(engine)
    fake = FakeGoogle()
    clock = Clock(at(9))
    cal = GoogleCalendar(settings, sf, client=http_mock(fake), clock=clock, timezone_name=lambda: "Asia/Karachi")
    cal.fake, cal.test_clock, cal.sf = fake, clock, sf  # type: ignore[attr-defined]
    yield cal
    await engine.dispose()


async def connect(cal):
    await cal.exchange_code("good")


# ---------------------------------------------------------------------- OAuth state
def test_state_roundtrip_expiry_and_tampering():
    state = make_state("k")
    assert verify_state("k", state)
    assert not verify_state("other-key", state)
    assert not verify_state("k", state + "x")
    assert not verify_state("k", "garbage") and not verify_state("k", None) and not verify_state("k", "")
    assert not verify_state("k", state, now=time.time() + 601)  # expired
    assert make_state("k") != make_state("k")


def test_auth_url_contents(gcal):
    url = gcal.auth_url("STATE")
    q = parse_qs(urlparse(url).query)
    assert q["state"] == ["STATE"] and q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["redirect_uri"] == ["http://localhost:8000/auth/google/callback"] and q["scope"] == [API.replace("/calendar/v3", "/auth/calendar")]


def test_auth_url_requires_configuration():
    cal = GoogleCalendar(Settings(_env_file=None), None)  # type: ignore[arg-type]
    with pytest.raises(CalendarError, match="isn't configured"):
        cal.auth_url("s")


# ---------------------------------------------------------------------- tokens
async def test_exchange_stores_tokens_encrypted(gcal):
    assert not await gcal.is_connected()
    await connect(gcal)
    assert await gcal.is_connected()
    async with gcal.sf() as s:
        row = (await s.execute(select(OAuthToken))).scalars().one()
        # unreadable at rest: a Fernet token, with none of the JSON inside visible
        # (don't search for the short fake token values: random ciphertext can contain "RT" by chance)
        assert row.data_encrypted.startswith("gAAAAA")
        assert "access_token" not in row.data_encrypted and "refresh_token" not in row.data_encrypted
        assert (await gcal._load())["refresh_token"] == "RT"
    status = await gcal.status()
    assert status["connected"] and "AT1" not in json.dumps(status) and "RT" not in json.dumps(status)


async def test_bad_code_is_rejected_cleanly(gcal):
    with pytest.raises(CalendarError, match="invalid_grant"):
        await gcal.exchange_code("bad")
    assert not await gcal.is_connected()


async def test_access_token_is_cached_then_refreshed_before_expiry(gcal):
    await connect(gcal)
    assert await gcal._access_token() == "AT1" and gcal.fake.token_requests == 1
    gcal.test_clock.advance(minutes=30)
    assert await gcal._access_token() == "AT1" and gcal.fake.token_requests == 1  # still fresh
    gcal.test_clock.advance(minutes=30)  # inside the 60s safety margin of expiry
    assert await gcal._access_token() == "AT2" and gcal.fake.token_requests == 2


async def test_reconnect_without_new_refresh_token_keeps_the_old_one(gcal):
    await connect(gcal)
    gcal.fake.token_requests = 0
    original = gcal.client._transport.handler  # keep the real handler

    def no_refresh_token(request):
        resp = original(request)
        if request.url.host == "oauth2.googleapis.com" and b"authorization_code" in request.content:
            return httpx.Response(200, json={"access_token": "AT9", "expires_in": 3600})
        return resp

    gcal.client._transport = httpx.MockTransport(no_refresh_token)
    await gcal.exchange_code("again")
    assert (await gcal._load())["refresh_token"] == "RT"


async def test_requests_fail_cleanly_when_not_connected(gcal):
    with pytest.raises(NotConnected):
        await gcal.busy(at(9), at(18))


async def test_disconnect(gcal):
    await connect(gcal)
    await gcal.disconnect()
    assert not await gcal.is_connected()
    await gcal.disconnect()  # idempotent


async def test_unreadable_token_after_secret_change_means_disconnected(gcal, tmp_path):
    await connect(gcal)
    other = GoogleCalendar(Settings(_env_file=None, SECRET_KEY="different", GOOGLE_CLIENT_ID="c", GOOGLE_CLIENT_SECRET="s"), gcal.sf)
    assert not await other.is_connected()


# ---------------------------------------------------------------------- calendar operations
async def test_busy_reads_primary_ignoring_own_blocks_all_day_and_free_events(gcal):
    await connect(gcal)
    gcal.fake.primary_events = [
        {"status": "confirmed", "start": {"dateTime": "2026-10-14T10:00:00+05:00"}, "end": {"dateTime": "2026-10-14T11:00:00+05:00"}},
        {"status": "confirmed", "start": {"date": "2026-10-14"}, "end": {"date": "2026-10-15"}},  # all-day
        {"status": "confirmed", "transparency": "transparent", "start": {"dateTime": "2026-10-14T12:00:00+05:00"}, "end": {"dateTime": "2026-10-14T13:00:00+05:00"}},
        {"status": "cancelled", "start": {"dateTime": "2026-10-14T14:00:00+05:00"}, "end": {"dateTime": "2026-10-14T15:00:00+05:00"}},
        {"status": "confirmed", "extendedProperties": {"private": {"assistant": "1"}},
         "start": {"dateTime": "2026-10-14T16:00:00+05:00"}, "end": {"dateTime": "2026-10-14T17:00:00+05:00"}},
    ]
    busy = await gcal.busy(at(0), at(23))
    assert [(b.start, b.end) for b in busy] == [(at(10), at(11))]


async def test_assistant_calendar_is_created_once_and_events_roundtrip(gcal):
    await connect(gcal)
    eid = await gcal.upsert_event(event_id=None, title="Report", start=at(9), end=at(10), block_id="b1")
    assert gcal.fake.events[eid]["summary"] == "Report"
    assert gcal.fake.events[eid]["extendedProperties"]["private"] == {"assistant": "1", "block_id": "b1"}
    assert (await gcal._load())["calendar_id"] == "cal-new"

    assert await gcal.upsert_event(event_id=eid, title="Report v2", start=at(9), end=at(11), block_id="b1") == eid
    assert gcal.fake.events[eid]["summary"] == "Report v2"
    await gcal.delete_event(eid)
    assert gcal.fake.events == {}
    await gcal.delete_event(eid)  # already gone (410) is fine


async def test_existing_assistant_calendar_is_reused(gcal):
    gcal.fake.calendar_name_exists = True
    await connect(gcal)
    await gcal.upsert_event(event_id=None, title="x", start=at(9), end=at(10), block_id="b")
    assert (await gcal._load())["calendar_id"] == "cal-existing"


async def test_event_deleted_in_google_is_recreated_on_update(gcal):
    await connect(gcal)
    new_id = await gcal.upsert_event(event_id="vanished", title="Report", start=at(9), end=at(10), block_id="b1")
    assert new_id in gcal.fake.events and new_id != "vanished"


async def test_primary_calendar_mode(gcal):
    gcal.settings = gcal.settings.model_copy(update={"GOOGLE_CALENDAR_NAME": "primary"})
    await connect(gcal)
    await gcal.upsert_event(event_id=None, title="x", start=at(9), end=at(10), block_id="b")
    assert (await gcal._load()).get("calendar_id") in ("", None)


async def test_a_401_triggers_one_refresh_and_retry(gcal):
    await connect(gcal)
    data = await gcal._load()
    data["access_token"] = "EXPIRED"
    await gcal._save(data)
    assert await gcal.busy(at(0), at(23)) == []  # first call 401s, token refreshed, second succeeds
    assert gcal.fake.token_requests == 2


async def test_api_errors_surface_as_calendar_errors(gcal):
    await connect(gcal)
    gcal.fake.expire_access_tokens = True
    with pytest.raises(CalendarError, match="unhandled"):
        await gcal._request("GET", f"{API}/nonsense")
