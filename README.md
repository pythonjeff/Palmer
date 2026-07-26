# Palmer

A hyperpersonal AI that texts like a sharp, funny friend. Built on Claude, delivered over SMS via Twilio.

Palmer learns who you are over time — your city, job, interests, ongoing threads — and uses that context the way a real friend would: casually, without citation. The longer you text, the better it knows you.

## Features

**Conversation**
- Distinct personality: dry, quick, observant, quietly loyal — not an assistant, not a brand
- SMS-aware conversation mechanics: reads message types (opener, continuation, closer, pivot), matches volume, respects closed threads
- Reads subtext and calls out patterns like a friend would
- Sends GIFs as reaction/punchline via Giphy (meme-aware search)
- Receives and understands photos sent via MMS (Claude vision)

**Memory**
- Learns each user automatically after every exchange — name, city, job, interests, ongoing threads
- Fully multi-user: each phone number gets its own isolated profile and history
- Per-phone conversation serialization — history never interleaves under concurrent messages

**Morning Briefing**
- Daily personalized text based on each user's subscribed topics
- User-configurable topics ("add Bitcoin to my morning", "remove sports") via `update_morning_briefing` tool
- New users onboarded immediately on first text — no waiting until next morning

**Reminders**
- Set one-time reminders at any future time ("remind me at 3pm to call Dave")
- Cancel reminders by topic or all at once
- Deduplication prevents double-saves; atomic Postgres claiming prevents double-sends
- Timezone-aware confirmations based on user's city

**Web Search**
- Real-time search via Tavily (news, weather, sports scores, prices)
- 15-second timeout with graceful fallback

**Security**
- Twilio webhook signature validation (HMAC-SHA1) on every inbound request
- All DB queries parameterized; every query scoped to phone number

## Stack

- **FastAPI** — webhook server
- **Twilio** — SMS/MMS in and out
- **Anthropic Claude Sonnet 4.6** — conversation and tool use
- **Anthropic Claude Haiku 4.5** — profile extraction, morning topic parsing
- **Tavily** — web search
- **Giphy** — GIF search
- **APScheduler** — 1-minute reminder sweep, in-process
- **Heroku Postgres** — message history, user profiles, reminders

## Setup

**1. Clone and install**
```bash
git clone https://github.com/pythonjeff/Palmer.git
cd Palmer
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**2. Configure environment**
```bash
cp .env.example .env
# Fill in all variables — see table below
```

**3. Run locally**
```bash
uvicorn main:app --reload
```

**4. Expose with ngrok (for local Twilio testing)**
```bash
ngrok http 8000
```

Set your Twilio webhook to `https://<ngrok-url>/sms` (HTTP POST).

## Deployment (Heroku)

```bash
heroku create
heroku addons:create heroku-postgresql:essential-0
heroku config:set ANTHROPIC_API_KEY=... TWILIO_ACCOUNT_SID=... TWILIO_AUTH_TOKEN=... TWILIO_PHONE_NUMBER=... TAVILY_API_KEY=... GIPHY_API_KEY=...
heroku config:set WEB_CONCURRENCY=1
git push heroku main
```

Set the Twilio webhook URL to `https://<your-app>.herokuapp.com/sms`.

Heroku Scheduler should run `python run_morning.py` once daily at your preferred morning time.

## Environment Variables

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | From console.anthropic.com |
| `TWILIO_ACCOUNT_SID` | From twilio.com dashboard |
| `TWILIO_AUTH_TOKEN` | From twilio.com dashboard |
| `TWILIO_PHONE_NUMBER` | Your Twilio number (e.g. +15551234567) |
| `TAVILY_API_KEY` | From app.tavily.com |
| `GIPHY_API_KEY` | From developers.giphy.com (free) |
| `DATABASE_URL` | Postgres connection string (auto-set by Heroku addon) |

## Preview Endpoints

```
GET /preview?phone=+15551234567        # preview morning briefing for a number
GET /preview/hourly?phone=+15551234567 # preview hourly weather/sports/deals checks
```

## Architecture Notes

- `WEB_CONCURRENCY=1` is required — in-memory phone locks and the APScheduler only work correctly in a single process
- Per-phone `threading.Lock` in `_phone_locks` serializes `_handle_sms` so history never interleaves between concurrent inbound messages from the same number
- Reminder claiming uses `FOR UPDATE SKIP LOCKED` on Postgres to prevent double-sends across scheduler ticks
- Anthropic client has a 45s timeout; Tavily search is wrapped in a `ThreadPoolExecutor` with a 15s timeout to prevent silent hangs from holding the phone lock
