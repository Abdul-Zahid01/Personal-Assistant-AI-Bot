# Personal AI Assistant

A small "Jarvis" for managing your work. Tell it what you need to do in plain language; it keeps track,
prioritises, schedules your day around your calendar, nudges you at the right moments, and proposes tomorrow's
plan every evening.

> "finish the report by Friday 5pm, it's urgent" · "remind me to call mom at 7" · "plan my day" · "done" · "snooze 30"

## What it does

| You | The assistant |
|---|---|
| Add tasks, reminders and events by chatting (web or Telegram) | Understands them with an LLM (Gemini/OpenAI) and stores them |
| Say "plan my day" | Ranks tasks (priority, deadline, age), fits them into free time around events/calendar/lunch, shows a draft, saves on approval |
| Do nothing | **08:00 morning brief**, "time to start X" at each block, "how did it go?" at the end, deadline heads-ups, repeating nudges for open reminders |
| Finish work | Tap ✅ Done in Telegram/web, or say "done". Repeating tasks respawn automatically |
| Reach 21:00 | **Evening review**: what got done, what carries over, a draft plan for tomorrow with an Approve button |
| Use it for weeks | Learns how long you really take per category and scales future estimates |

### The app

* **Day map** — your day as a timeline. Blocks fill up live as time passes, a red line marks "now", and the
  **Now card** shows the current block or the task you started (`▶`) with % done and time left (amber when you run
  over your estimate). Tap any block for Start / Done / +30m / Tomorrow / Skip today.
* **Routines** — gym, meals, calling mom… set once (time, duration, days of the week) and they appear on every
  day's map, notify you at their start time, and the planner schedules work around them. One-tap presets included.
  Also by chat: "gym every weekday at 6pm for an hour".
* **In-app alerts** — reminders pop up as cards with a chime (🔊 to mute) and action buttons, plus system
  notifications via the 🔔 button.
* Works on a phone screen (bottom tabs) and on desktop (three panels); light/dark "glass" look.
  Deep links: `/#map`, `/#chat`, `/#tasks`, `/#routines`.

Works without an LLM key too: a built-in rule-based parser handles simple commands, and if the real model is down the
assistant falls back to it automatically.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # then edit it (at least TIMEZONE; add GEMINI_API_KEY for the smart mode)
uvicorn app.main:app_factory --factory --port 8000
```

Open <http://localhost:8000>. API docs are at <http://localhost:8000/docs>. That's all: SQLite needs no setup.

### Notifications (built in)

Click the **🔔** button in the web app and allow notifications. Reminders, the morning brief and the evening review
then pop up on your computer **even when the tab is closed**, with ✅ Done / ⏰ +30m buttons right on the notification.
On a computer this works on `http://localhost` out of the box (Chrome, Edge, Firefox).

**On your phone** browsers only allow notifications from an **HTTPS** site, so the app must be reachable over HTTPS first
(deploy it, or for a quick test use a tunnel such as `cloudflared tunnel --url http://localhost:8000`). Then open that URL
on the phone, **install it** (Android: menu → *Install app*; iPhone: Share → *Add to Home Screen*, then open it from the
home screen — iOS 16.4+), tap 🔔 and allow. Set `VAPID_SUBJECT=mailto:<your email>` (Apple rejects placeholder addresses)
and an `API_KEY` before exposing the app to the internet.

### Telegram (optional alternative)

1. In Telegram talk to **@BotFather** → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Start the app and send any message to your new bot. It replies with your chat id.
3. Put it in `TELEGRAM_CHAT_ID` and restart. The bot now only ever talks to that chat.

Polling mode (the default) needs no public URL, so it works from a laptop. For a server with HTTPS set
`TELEGRAM_MODE=webhook`, `TELEGRAM_WEBHOOK_SECRET=<random>`, then `POST /api/v1/telegram/set-webhook {"url": "https://you/api/v1/telegram/webhook"}`.

### Google Calendar (optional)

Create OAuth credentials (type *Web application*, redirect URI `http://localhost:8000/auth/google/callback`) in Google Cloud Console,
enable the Calendar API, fill `GOOGLE_CLIENT_ID/SECRET`, restart, then `POST /api/v1/google/connect` and open the returned URL.
The assistant then (a) avoids your existing events when planning and (b) writes its blocks and events to a separate **"Assistant"**
calendar (created automatically) so your main calendar stays clean. Tokens are stored encrypted and never returned by any endpoint.

### Docker

```bash
docker compose up -d --build          # app + SQLite in a volume
docker compose --profile postgres up  # with Postgres (set COMPOSE_DATABASE_URL, see docker-compose.yml)
```

## Talking to it

Anything goes with an LLM key. With no key, these work:

`remind me to call mom tomorrow at 7pm` · `add task write report by friday 3pm, 2 hours, urgent` ·
`meeting with Sara tomorrow at 2pm for 45 minutes` · `remind me to take vitamins every day at 8am` ·
`plan my day` / `plan tomorrow` · `what's on today?` · `show tasks` · `done` / `done with report` ·
`snooze 30 min` · `delete buy milk` · `end of day review`

Settings (also changeable by chatting: "I'm in London", "keep 1–2pm free for lunch", "start work at 10"):
`timezone work_start work_end focus_start focus_end max_block_min buffer_min morning_brief evening_review
nag_interval_min auto_plan proactive blocked_windows` — see `GET /api/v1/preferences`.

## Project layout

```
app/
  main.py            app factory, lifespan (starts scheduler + Telegram poller)
  container.py       wires everything; the only place that knows all the parts
  domain/            PURE logic, no I/O — prioritizer, planner, recurrence, estimates, time parsing
  services/          tasks, planning, review, preferences, calendar sync, text formatting (use the DB)
  assistant/         agent loop, tools the LLM can call, prompts, button actions
    llm/             Gemini, OpenAI, offline rules, failover chain
  channels/          Telegram client/poller/handler, notifier (web + Telegram)
  integrations/      Google Calendar
  jobs/scheduler.py  the heartbeat: reminders, morning brief, evening review, calendar sync
  api/               HTTP routes
  static/index.html  web chat + today timeline + task list
alembic/             database migrations
tests/               see below
docs/architecture.md how the pieces connect
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest                      # ~240 tests, ~1 min, no network, no Docker
```

The planner, prioritiser and recurrence logic are pure functions tested without a database. Everything else runs against a
real (temporary) SQLite database with fake LLM / Telegram / Google / clock, including full "day in the life" scenarios.

## Operating notes

* **Single process.** The scheduler and Telegram poller run inside the app. Don't run several workers against the same
  database or reminders could be sent twice. (`uvicorn` without `--workers` is correct.)
* **Production:** set `APP_ENV=production`; the app then refuses to start without `API_KEY` and a non-default `SECRET_KEY`.
  Put it behind HTTPS. Changing `SECRET_KEY` makes stored Google tokens unreadable (just reconnect).
* **Migrations:** on startup the app creates a new database or upgrades an existing one to the latest schema
  automatically, keeping your data (`AUTO_CREATE_TABLES=true`). To manage it yourself, set it to `false` and run
  `alembic upgrade head`. After changing a model: `alembic revision --autogenerate -m "what changed"`.
* **If the app was off:** reminders that were due while it was down are sent late (plain reminders) or skipped as stale (start-now
  nudges), never dumped all at once.
* The Postgres path is covered by DDL-compile tests but has not been exercised against a live Postgres server in this repo's tests.
