# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What Palmer is

Palmer is a personal AI delivered entirely over SMS via Twilio. A FastAPI web dyno handles inbound SMS webhooks and runs all background jobs in-process via APScheduler. There is no separate worker.

## Running / commands

```bash
# Install (uses .python-version, currently 3.12)
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Local dev — hot reload
uvicorn main:app --reload

# Expose to Twilio for local dev
ngrok http 8000     # point Twilio SMS webhook at https://<ngrok>/sms

# Tests (pytest — no config file, discovers test_*.py)
pytest
pytest test_morning_schedule.py::TestSendWindow::test_before_time_no_send   # single test

# Preview a morning briefing without sending
curl 'http://localhost:8000/preview?phone=+15551234567'

# Trigger the morning job once (bypasses schedule; still respects per-user send window + per-day guard)
python send_morning.py

# Trigger reminder delivery once
python send_reminders.py

# Import main without starting the job loop (tests, shells, one-off scripts).
# Without this, importing main starts APScheduler and send_due_reminders will
# send REAL SMS on a 1-minute interval.
PALMER_NO_SCHEDULER=1 python -c "import main"
```

`.env` variables (see README for full table): `ANTHROPIC_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, `TAVILY_API_KEY`, `OWM_API_KEY`, `TOMTOM_API_KEY`, `GIPHY_API_KEY`, `APP_URL`, `DATABASE_URL` (optional locally — falls back to SQLite `palmer.db`).

## Architecture that spans multiple files

### Single-process is a hard requirement
`WEB_CONCURRENCY=1` must be set in production. `main.py` holds all cross-request coordination in memory: per-phone `threading.Lock` (`_phone_locks`) serializes inbound messages so conversation history never interleaves, `_in_flight` tracks concurrent turns from the same number so replies can be quote-prefixed, and `_seen_sids` deduplicates Twilio webhook retries. APScheduler also runs in-process. A second worker breaks all of the above.

### DB layer is dual-backend
`db.py` transparently switches between Postgres (when `DATABASE_URL` is set) and SQLite (`palmer.db` in repo root) using a `PH` placeholder (`%s` vs `?`) — every query in `db.py` uses `PH` and `_conn()`. Any new DB code must follow this pattern; do not hard-code `%s` or `?`.

Reminder delivery uses `FOR UPDATE SKIP LOCKED` on Postgres in `claim_due_reminders()` so multiple scheduler ticks are safe against double-sends. The SQLite branch does a plain select-then-update (fine for single-process local dev).

Schema is created lazily in `init_db()`, called at import time from `agent.py`. New columns on existing tables must be added to the `new_cols` migration list — Postgres uses `ADD COLUMN IF NOT EXISTS`, SQLite catches the duplicate-column exception.

### The big graph of imports
Almost everything imports from `agent.py`. `morning.py`, `alerts.py`, `followup.py`, `watches.py`, `send_reminders.py`, and `traffic.py` all reuse `client`, `_build_system`, `_sms_clean`, `_search`, `_weather_report`, `_get_price`, `HAIKU_MODEL`, `SONNET_MODEL`, etc. When editing `agent.py`, treat underscore-prefixed helpers as internal-to-the-package rather than private — search before renaming.

### Scheduler cadence (main.py)
```
send_due_reminders       every 1 min
send_morning_messages    every 5 min   (each user has a local target time; per-day guard prevents double-sends)
run_watches              every 30 min
run_alert_checks         every 60 min
send_missing_data_asks   every 60 min  (asks users with no city so mornings can target them; DATA_ASK_DRY_RUN=1 to preview)
run_followups            every 4 hr
run_price_watches        every 12 hr   (SerpAPI Google Shopping + Amazon; establishes baseline on first check, alerts on target-hit or ≥15% drop)
```

Morning briefings are sent by APScheduler at each user's chosen local time (default 7:00). No Heroku Scheduler job is required; if one runs `send_morning.py`, the per-user sent-date guard prevents double-sends.

### One voice: all user-facing text goes through `_build_system`
Anything the user reads is drafted with `agent._build_system(phone)` as the system prompt, on `SONNET_MODEL`. That is what carries SYSTEM_PROMPT, the CALIBRATION section, the user's `communication_style`, and their reaction history — so Palmer sounds like the same person everywhere.

There used to be a second tier of paths carrying their own one-line persona ("You're Palmer, a dry, sharp texting friend") that never saw any of it, which meant a user who asked for less sarcasm still got the breezy default on price alerts and reminders. Do not reintroduce that. If you add an outbound message, it goes through `_build_system`.

The deliberate exception is `traffic.py`: its output is *source data* for a draft that already carries the system prompt (morning briefings, and the `get_city_traffic` tool inside `get_reply`), so it is a plain factual summarizer on Haiku. Voicing it there would layer a second, uncalibrated Palmer under a real one.

### Reactions (tapback.py)
iMessage and Google Messages degrade reactions to plain text over SMS (`Liked "..."`), so they arrive as ordinary inbound messages. `main._handle_sms_inner` short-circuits on them before anything else runs:

1. `parse_reaction()` — free regex; is this a reaction at all
2. `interpret_reaction()` — Haiku, in context and per person: what is the reaction *doing*
3. act — silence for everything except `answer` (a 👍 on "want me to add that?" is a yes)

Silence is both the default and the failure default, so a Haiku outage degrades to silence rather than to unwanted texts. **Returning `True` from the reaction branch is load-bearing** — `_handle_sms` fires `FALLBACK_SMS` on a falsy return.

Reactions then feed `communication_style`, `morning_prefs["avoid"]`, and a pacing factor that stretches followup gaps and lowers the alert cap. Each is behind a threshold so one stray tap can't reshape Palmer, and a dropped topic is announced once via `pending_preference_notice` rather than silently vanishing.

### Shared modules — don't re-copy these
- `serpapi.py` — SerpAPI key, base URL, timeout, and request transport. Both `shopping.py` and `amazon.py` use it. Each still parses its own engine's payload; only the transport is shared.
- `price_alert.py` — the one drafter for price-watch alerts, used by both price sources. It lives in its own module because `shopping.py` already imports `amazon.py`, and putting it in either would make that coupling bidirectional.

### DB access patterns
- `get_all_profiles()` returns every `(phone, profile)` in ONE query. The scheduler jobs use it. Do not write `for phone in get_all_phones(): get_profile(phone)` — `_conn()` opens a fresh connection per call, so that is N+1 per tick.
- `upsert_profile()` does its read and write on one connection, and takes a row lock on Postgres. It used to be two connections with an unsynchronised gap, so concurrent writers could drop each other's fields.
- Pass profiles down rather than re-reading them. The reaction path once cost five `get_profile` calls for a single inbound tapback.

### Model routing
Two Claude models, chosen in `agent.py`:
- `SONNET_MODEL = "claude-sonnet-4-6"` — conversation, drafting, all user-facing replies
- `HAIKU_MODEL = "claude-haiku-4-5-20251001"` — extraction, scoring, classification, topic-inference, thread selection

New extraction/scoring logic should go on Haiku; new user-facing drafting on Sonnet. When updating model IDs, grep for both constants — they're re-exported and imported across most modules.

### Tool routing is strict and important
The system prompt in `agent.py` hard-routes user asks to specific tools. Never mix:
- `get_weather` → OpenWeatherMap only
- `get_price` → CoinGecko (crypto) / yfinance (stocks) only
- `get_travel_time` / `get_city_traffic` → TomTom only
- `add_price_watch` / `run_price_watches` → SerpAPI Google Shopping only (product prices, distinct from `get_price` for crypto/stocks)
- `web_search` → Tavily news mode only, never for weather or prices

If you add a new tool, follow the same discipline: one data source per tool, and update the `USE THE RIGHT TOOL` block in `SYSTEM_PROMPT` so Claude routes correctly.

### Watches: two source-quality gates
`watches.py` and `alerts.py` both filter search results before firing a text:
1. **Trusted-domain ranking** — `trusted_sources.json` classifies domains as tier 1 (AP/Reuters/BBC/NYT/WSJ/Bloomberg/ESPN etc., plus `.gov`/`.edu`) or tier 2 (mainstream). Tier picks which URL to send.
2. **Corroboration** — a watch will not fire unless ≥ 2 distinct canonical domains agree, OR ≥ 1 tier-1 source confirms.

Plus a strict criticality gate, 12-hour recency, per-watch cooldown (default 4h), a `DAILY_ALERT_MAX` cap, and a dedup check against recent alert summaries. When editing this pipeline, keep all gates — removing any one produced noisy or bad alerts historically.

### Landmarks vs. addresses in the traffic pipeline
TomTom's geocoder is a mapping API, not a search engine, and mis-ranks landmark names (e.g. "White House", "Fenway", "LAX"). `traffic.py` and the `get_travel_time` tool run landmark destinations through Sonnet to resolve them to street addresses *before* geocoding. Preserve this indirection when touching routing code.

### SMS send pipeline
All outbound SMS goes through `sms_util.send_sms` / `ensure_sms`. It cleans text (`_sms_clean` strips markdown and non-SMS glyphs), splits on paragraph breaks over 1500 chars, and falls back through progressively shorter candidates (original → `shorten_message` → hard truncate → `FALLBACK_SMS`) so a user is never left with silence. Never call Twilio's `messages.create` directly from feature code; go through this module.

### Twilio safety
Every `/sms` and `/sms-status` request is validated with Twilio's HMAC-SHA1 `RequestValidator`. Requests failing validation return 403. All DB queries are parameterized and scoped by phone number.

### `/sms-status` retry
Twilio delivery failures with error codes `30019` or `21617` (content-size issues) trigger an automatic shorten-and-retry via the `/sms-status` webhook. Other delivery failures are logged and dropped — do not add blanket retry-on-any-failure without thinking about loops.

## Voice / prompt rules (see `SYSTEM_PROMPT` in `agent.py`)

Palmer has a specific voice: dry, observational, plain-text SMS (no markdown, no bullets except the one numbered onboarding list). If you touch the system prompt or write new drafting prompts (Haiku personalizations, morning drafts, followups), keep to the same rules — no "Great question", no summarizing user words back, no ending every message with a question, and **never redirect the user to competing apps** (Google Maps, Waze, ChatGPT, etc.).
