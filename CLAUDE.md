# CLAUDE.md

Guidance for Claude Code when working in this repository. Keep this file short and operational; the reasoning behind each subsystem lives in `docs/design/` and is linked from the table at the bottom.

## What Palmer is

Palmer is a personal AI delivered entirely over SMS via Twilio. A FastAPI web dyno (`palmer/main.py`) handles inbound SMS webhooks and runs every background job in-process via APScheduler. There is no separate worker, and `WEB_CONCURRENCY=1` is required in production because all cross-request coordination is in memory.

## Commands

```bash
python3 -m venv venv && source venv/bin/activate      # Python 3.11 (.python-version)
pip install -r requirements-dev.txt

uvicorn palmer.main:app --reload                       # local dev, hot reload
ngrok http 8000                                        # point the Twilio SMS webhook at https://<ngrok>/sms

pytest                                                 # ~1,430 tests, all offline, ~5s; config in pyproject.toml
pytest tests/test_morning.py::TestSendWindow           # one class
ruff check .

curl 'http://localhost:8000/preview?phone=+15551234567'   # a morning briefing without sending
python -m scripts.send_morning                         # morning job once (respects window + per-day guard)
python -m scripts.send_reminders                       # reminder delivery once
python -m scripts.preview_opening <city>               # review an Opening metro before users see it
PALMER_NO_SCHEDULER=1 python -c "import palmer.main"   # import main without starting the job loop
```

Without `PALMER_NO_SCHEDULER=1`, importing `palmer.main` starts APScheduler and `send_due_reminders` will send **real SMS** on a one-minute interval. `.env` variables are listed with comments in `.env.example`; `DATABASE_URL` is optional locally and falls back to SQLite at `palmer.db`.

## Layout

```
palmer/     the application, one module per concern (below)
scripts/    one-off entry points: send_morning, send_reminders, preview_opening, migrate_profile_prune
tests/      15 files grouped by the module under test; conftest.py + helpers.py hold the shared fakes
docs/       design notes, one file per area
```

Modules, roughly bottom to top of the import graph:

```
llm, netutil, sources, smstext, timeutil, serpapi     leaves; import nothing from Palmer
prompts, tools_def                                    SYSTEM_PROMPT / EXTRACT_PROMPT; the TOOLS schema
weather, datafeeds, tickers, traffic, sports, shows,  one external source each
  opening, trends, shopping, amazon, flights, hotels
db                                                    dual-backend (Postgres / SQLite) — see docs/design/db.md
userprofile, guards, sms_util, artifacts, cards, page, home
agent                                                 _build_system, get_reply and its tool dispatch
morning, reminders, followup, watches, scorewatch,    the scheduled senders
  flightwatch, price_alert, tapback, onboard, wxaudit
main                                                  FastAPI app + the APScheduler job table
```

Dependencies run downward only. Import from the module that owns a thing, never through `agent` — `agent.py` exports exactly `_build_system`, `get_reply` and `save_assistant_turn`. An underscore prefix means "internal to Palmer", not "private to this module" (`smstext._sms_clean` has six importers); grep before renaming.

## Scheduler cadence

```
send_due_reminders       every 1 min
send_morning_messages    every 5 min   (each user has a local target time; per-day guard prevents double-sends)
run_watches              every 30 min
send_missing_data_asks   every 60 min  (asks users with no city so mornings can target them; DATA_ASK_DRY_RUN=1 to preview)
run_followups            every 2 hr   (cron, NOT interval — see main.py; the per-user gap in DAYS (followup.GAP_DAYS), not the tick, is the cadence)
run_score_alerts         every 2 min   (interval, deliberately — a game is a window, not a clock time; polls only leagues someone set a `live` level on, two-speed)
run_price_watches        00:00 + 16:00 UTC (cron, NOT interval — see below; SerpAPI Google Shopping + Amazon; baseline seeded at watch creation, alerts on target-hit or ANY move over $2 in either direction, then re-baselines)
```

Morning briefings go out at each user's chosen local time (default 7:00). Jobs that run daily or less are **cron** triggers, never intervals — an interval's clock restarts on every deploy. See `docs/design/shopping-and-flights.md` for the incident.

## Rules that hold everywhere

- **One voice.** Anything a user reads is drafted with `agent._build_system(phone)` on `SONNET_MODEL`. No second persona, no bare strings sent as messages. Haiku is for extraction, scoring and classification only. → `docs/design/agent-loop.md`
- **One data source per tool**, and every tool is named in `SYSTEM_PROMPT`'s routing block: weather → NWS then Open-Meteo; `get_price` → CoinGecko/yfinance; traffic → TomTom; product prices → SerpAPI; `web_search` → Tavily news mode. `tests/test_agent.py::TestEveryToolIsRouted` fails on an unrouted tool. → `docs/design/agent-loop.md`
- **Every DB query uses the `PH` placeholder and `_conn()`.** Never hard-code `%s` or `?`. New columns go in the `new_cols` migration list. → `docs/design/db.md`
- **Every outbound message goes through `sms_util.send_sms`.** Proactive senders accept `False`; `ensure_sms` is for replies only. A URL is sent last and alone, byte for byte, or not at all. → `docs/design/sms.md`
- **A daily guard means the reader's day.** Use `timeutil.local_today(tz)`, never `date.today()`; the dyno is UTC. → `docs/design/time.md`
- **The profile is a bounded schema** (`userprofile.PROFILE_FIELDS`); a new field is added there and in `EXTRACT_PROMPT`, and never collides with `_PROFILE_ALIASES`. Fields written by tool dispatch stay out of `EXTRACT_PROMPT`. → `docs/design/profile.md`
- **Resolution runs on the write path, never on read.** Tickers, shows, weather locations, commute addresses: resolve once when the user sets it; page views cost nothing. Ask when guessing wrong costs the user the turn. → `docs/design/agent-loop.md`
- **A prompt rule the model keeps breaking becomes code** in `guards.py`, checked in `agent._finalize` (redraft once) or `send_sms` (block). → `docs/design/guards.md`
- **Failure strings are prompts.** Say what failed and what to do next; never imply the capability is missing, never interpolate raw exception text. → `docs/design/agent-loop.md`
- **Rate limits are the product**, not an afterthought: the check-in is paced in days, price watches fire on a $2 move but twice a day with cooldowns, live scores are opt-in per team with a level. → `docs/design/checkin.md`, `shopping-and-flights.md`, `scores-and-shows.md`

## Testing

Patch the module a function **lives in**, not the one that re-exports it: `patch("palmer.userprofile.client")`, not `patch("palmer.agent.client")`. `tests/conftest.py` sets placeholder env values, refuses every socket connection and every unpatched Anthropic call, so an escaped fetch fails by name instead of quietly reaching the network. Shared fakes (`llm_reply`, `Block`/`Resp`, `drive_tool`, `tool_by_name`) are in `tests/helpers.py`; `fresh_db` gives a temp SQLite schema. Twenty-four assertions read `inspect.getsource(agent.get_reply)`, which is why its dispatch chain is a nested function rather than split across modules.

## Voice

Palmer is dry, quick and observant, in plain-text SMS: no markdown, no bullets except the one numbered onboarding list, no "Great question", no summarising the user back to themselves, no ending every message on a question, and **never a redirect to a competing app**. Any new drafting prompt keeps to `SYSTEM_PROMPT`'s rules.

## Design notes — read before touching

| Before changing… | Read |
|---|---|
| `agent.py`, `prompts.py`, `tools_def.py` | `docs/design/agent-loop.md`, `guards.md` |
| `main.py` job table | `docs/design/scheduler.md`, `time.md` |
| `db.py` | `docs/design/db.md` |
| `morning.py` | `docs/design/morning.md`, `time.md` |
| `onboard.py`, NEW USERS prompt blocks | `docs/design/onboarding.md` |
| `reminders.py`, `timeutil.next_occurrence` | `docs/design/reminders.md` |
| `followup.py` | `docs/design/checkin.md` |
| `sms_util.py`, `smstext.py`, `tapback.py` | `docs/design/sms.md` |
| `home.py`, `page.py`, `cards.py`, `artifacts.py`, `tickers.py` | `docs/design/page.md` |
| `opening.py` | `docs/design/opening.md` |
| `sports.py`, `scorewatch.py`, `shows.py` | `docs/design/scores-and-shows.md` |
| `userprofile.py`, `EXTRACT_PROMPT` | `docs/design/profile.md` |
| `sources.py`, `datafeeds.py`, `trusted_sources.json` | `docs/design/sources.md` |
| `weather.py`, `wxaudit.py` | `docs/design/weather.md` |
| `traffic.py` | `docs/design/commute.md` |
| `shopping.py`, `amazon.py`, `price_alert.py`, `flights.py`, `flightwatch.py` | `docs/design/shopping-and-flights.md` |
