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
- Searches scoped to past 48 hours with publish dates surfaced — stale results are skipped, not paraphrased
- User-configurable topics ("add Bitcoin to my morning", "remove sports") via `update_morning_briefing` tool
- New users onboarded immediately on first text — no waiting until next morning

**Proactive Alerts**
- Once-daily scan of each user's interests for breaking news
- Searches scoped to past 24 hours; Haiku scores significance 1–10 with today's date as a hard gate
- Only fires at score ≥ 8 — massive breaking news, not routine updates
- Randomized send time per user per day (deterministic hash, 1pm–9pm UTC window)

**Reminders**
- Set one-time reminders at any future time ("remind me at 3pm to call Dave")
- Cancel reminders by topic or all at once
- Deduplication prevents double-saves; atomic Postgres claiming prevents double-sends
- Timezone-aware confirmations based on user's city

**Live Data Tools**
- **Weather** — OpenWeatherMap API: accurate current conditions and 5-day forecast. Handles "next Saturday", "this weekend", specific dates. Never uses web crawl for weather.
- **Crypto prices** — CoinGecko: real-time price, 24h change, 7d change for Bitcoin, ETH, Doge, Solana, and more. No API key required.
- **Stock prices** — yfinance: any ticker (AAPL, TSLA, SPY, QQQ, etc). Date-aware — correctly labels weekend/holiday data as "Friday close" not "today".
- **News search** — Tavily in news mode (7-day window) for sports scores, current events, general facts

**Information Curation**
- Palmer routes each query to the right tool — weather never goes through web search, prices never go through web search
- Connects information to what it knows about the user: weather to their plans, prices to context, news to why it matters to them specifically
- Surfaces adjacent things they'd care about, not just literal answers

**Reliability**
- Three-layer send pipeline: split at paragraph breaks → Haiku shorten-and-retry → plain fallback string
- All exception paths protected — Palmer always sends something, or logs why it couldn't
- `max_tokens` and tool loop edge cases handled; empty replies caught before hitting Twilio
- Morning briefing token limit sized to handle 4+ topics without truncation

**Security**
- Twilio webhook signature validation (HMAC-SHA1) on every inbound request
- All DB queries parameterized; every query scoped to phone number

## Stack

- **FastAPI** — webhook server
- **Twilio** — SMS/MMS in and out
- **Anthropic Claude Sonnet 4.6** — conversation and tool use
- **Anthropic Claude Haiku 4.5** — profile extraction, morning topic parsing, alert scoring, message shortening
- **Tavily** — news search (topic=news mode)
- **OpenWeatherMap** — weather current + forecast
- **CoinGecko** — crypto prices (free, no key)
- **yfinance** — stock prices
- **Giphy** — GIF search
- **APScheduler** — 1-minute reminder sweep + 30-minute alert sweep, in-process
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
heroku config:set ANTHROPIC_API_KEY=... TWILIO_ACCOUNT_SID=... TWILIO_AUTH_TOKEN=... \
  TWILIO_PHONE_NUMBER=... TAVILY_API_KEY=... GIPHY_API_KEY=... OWM_API_KEY=...
heroku config:set WEB_CONCURRENCY=1
git push heroku main
```

Set the Twilio webhook URL to `https://<your-app>.herokuapp.com/sms`.

Heroku Scheduler should run `python send_morning.py` once daily at your preferred morning time.

## Environment Variables

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | From console.anthropic.com |
| `TWILIO_ACCOUNT_SID` | From twilio.com dashboard |
| `TWILIO_AUTH_TOKEN` | From twilio.com dashboard |
| `TWILIO_PHONE_NUMBER` | Your Twilio number (e.g. +15551234567) |
| `TAVILY_API_KEY` | From app.tavily.com |
| `GIPHY_API_KEY` | From developers.giphy.com (free) |
| `OWM_API_KEY` | From openweathermap.org (free tier) |
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
- Anthropic client has a 45s timeout; all external API calls (Tavily, OWM, yfinance) wrapped in `ThreadPoolExecutor` with 15s timeout
- Tool routing: `get_weather` → OWM, `get_price` → CoinGecko/yfinance, `web_search` → Tavily news mode. Each tool owns its domain — no overlap.
- Alert recency enforced at two layers: Tavily `days=1` filters the search, then Haiku scores with today's date and caps old news at 4/10
