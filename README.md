# Palmer

**A personal assistant that works entirely over text — and only texts you on a schedule you set.**

Palmer is a personal AI delivered entirely over SMS. No app to download, no interface to learn — just text it. It knows who you are, remembers what you care about, and reaches out when something worth knowing happens.

---

## What Palmer does

### Knows you
Every exchange teaches Palmer something it can use. Your city, your commute, your teams, what you track — it keeps that on file and uses it to do the job without asking twice: weather for where you are, drive times from your address, your team's game in your updates. It does not recite it back and it does not bring your life up on its own.

### Sends your morning
Each morning Palmer texts you the basics — weather, your commute, your team's game, what's opening nearby — and a link to your page with the markets and news you follow. Not a newsletter. One text, just the things that matter, from today.

### Evening update
At 6pm, a second short text with only what changed since the morning: how the game went, tickers that moved, new headlines on your topics. On a day when nothing changed, nothing is sent.

### Watches for things
Tell Palmer to watch for something — a geopolitical event, a company move, an athlete's health update, anything — and it runs that in the background. When it hits, you get a text. No feed to check.

### Sets reminders
Natural-language reminders that arrive when you need them. "Remind me Friday morning to prep for the meeting." Done.

### Stays quiet otherwise
Palmer never texts on its own initiative. Unprompted messages come from exactly three places: the two scheduled updates, and watches you set. No live score pings, no "saw this and thought of you", no checking in on how the interview went.

### Sees photos
Send Palmer a picture and it'll actually respond to what's in it — a menu, a whiteboard, a receipt, a dog. It's using vision, not guessing from a filename.

### Answers anything
Crypto prices, stock quotes, weather forecasts, sports scores, current events, city traffic, drive times with live traffic — all through the same text thread, no switching apps.

### Stays out of the way
Tell Palmer to pause the morning texts, drop a topic, forget a fact about you, or change when it sends — all by texting. No settings screen.

---

## Personality

Palmer is dry, quick, and direct. It leads with the answer, has opinions, doesn't pad and doesn't flatter. It sounds like a person rather than a product, but it is a tool first: the personality lives in precision and the occasional dry observation, not in making conversation. Built on Claude.

---

## Stack

| Layer | Technology |
|---|---|
| Conversation + reasoning | Claude Sonnet 4.6 |
| Fast inference (extraction, scoring) | Claude Haiku 4.5 |
| SMS/MMS | Twilio |
| Web server | FastAPI on Heroku |
| News + web search | Tavily |
| Weather | NWS (US), Open-Meteo (elsewhere) |
| Traffic + routing | TomTom (Traffic Flow, Traffic Incidents, Routing, Search geocoding) |
| Product prices + sale watches | SerpAPI Google Shopping |
| Crypto prices | CoinGecko |
| Stock prices | yfinance |
| GIFs | Giphy |
| Background jobs | APScheduler (reminders 1m · mornings 5m · evenings 5m · watches 30m · missing-data asks 60m · price watches 2×/day · flight watches 1×/day) |
| Database | Heroku Postgres |

---

## Running Palmer

**1. Clone and install**
```bash
git clone https://github.com/pythonjeff/Palmer.git
cd Palmer
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

**2. Environment**
```bash
cp .env.example .env
# Fill in the variables below
```

| Variable | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `TWILIO_ACCOUNT_SID` | twilio.com dashboard |
| `TWILIO_AUTH_TOKEN` | twilio.com dashboard |
| `TWILIO_PHONE_NUMBER` | Your Twilio number (+15551234567) |
| `TAVILY_API_KEY` | app.tavily.com |
| `GIPHY_API_KEY` | developers.giphy.com (free) |
| `TMDB_API_KEY` | themoviedb.org (free, non-commercial) — Opening section |
| `TICKETMASTER_API_KEY` | developer.ticketmaster.com (free, 5k calls/day) — Opening section |
| `TOMTOM_API_KEY` | developer.tomtom.com (free tier, ~2,500 requests/day) |
| `SERP_API_KEY` | serpapi.com (free plan is 250 searches/mo; paid from $50/mo for 5,000) |
| `APP_URL` | Your deployed app URL (for Twilio status callbacks) |
| `DATABASE_URL` | Postgres connection string (auto-set by Heroku) |

**3. Run locally**
```bash
uvicorn main:app --reload
```

**4. Expose for Twilio (local dev)**
```bash
ngrok http 8000
# Set Twilio webhook → https://<ngrok-url>/sms (POST)
```

---

## Deploy to Heroku

```bash
heroku create
heroku addons:create heroku-postgresql:essential-0
heroku config:set ANTHROPIC_API_KEY=... TWILIO_ACCOUNT_SID=... TWILIO_AUTH_TOKEN=... \
  TWILIO_PHONE_NUMBER=... TAVILY_API_KEY=... GIPHY_API_KEY=... \
  TOMTOM_API_KEY=... APP_URL=...
heroku config:set WEB_CONCURRENCY=1
git push heroku main
```

Set your Twilio SMS webhook to `https://<your-app>.herokuapp.com/sms` (POST).  
Set your Twilio status callback to `https://<your-app>.herokuapp.com/sms-status` (POST).

**Morning briefings** are sent by APScheduler inside the web dyno (checked every 5 minutes) at each user's chosen local time — 7am by default, changeable by texting Palmer. No Heroku Scheduler job is required; if one exists running `python send_morning.py`, it's a harmless redundant backup (the per-user sent-date guard prevents double-sends).

---

## Preview endpoints

```
GET /preview?phone=+15551234567         # generate morning briefing without sending
```

---

## Architecture

- `WEB_CONCURRENCY=1` is required. In-memory phone locks and APScheduler only work correctly in a single process.
- Per-phone `threading.Lock` serializes inbound messages so conversation history never interleaves under concurrent requests from the same number.
- **Source quality is one gate, applied at the search call** (`sources.py`). Every news fact and link Palmer sends — watch alerts, the morning briefing, Palmer Home, and conversational web search — goes through `datafeeds._search_raw` / `_search`, which filter before returning, so no surface can drift to its own standard. Four gates: a **blocklist** dropping press-release wires (`prnewswire`, `globenewswire`) and republishing aggregators (`msn.com`, `biztoc`, `news.google.com`) outright; a **relevance floor** that gives trusted sources 0.15 of slack, because Tavily's score measures query-text match and that is exactly what an SEO content farm is built to win; **tier ordering** from `trusted_sources.json` (tier 1 = wires and premier newsrooms plus `.gov`/`.edu`, tier 2 = mainstream and reputable specialists, tier 3 = everything else) sorting `(tier, -score)` so a wire report beats a higher-scoring blog; and **corroboration** requiring ≥ 2 distinct domains OR ≥ 1 tier-1 source before any unprompted alert fires. Palmer Home additionally drops tier 3 entirely — an untrusted row is worse than no row on a short curated list. Edit `trusted_sources.json` to add or remove sources; no code change needed.
- Watches run every 30 minutes via APScheduler but only alert on major breaking developments — dated results from the last 12 hours, a strict criticality gate, a HEAD reachability check so a dead top link falls through to the next result, per-watch cooldown (default 4 hours), and dedup against the last alerted event.
- Traffic uses TomTom. Morning briefings auto-include a short city snapshot (`get_city_traffic`): geocode → parallel Traffic Flow + Traffic Incidents in a city bounding box → Haiku drafts one natural line, or skips silently on failure. On demand, users can ask for city conditions or point-to-point drive times (`get_travel_time`, live traffic vs. free-flow). Landmark destinations (White House, Fenway, LAX) get resolved to street addresses by Sonnet before geocoding — TomTom's geocoder is a mapping API, not a search engine, and mis-ranks landmark names.
- Price watches use SerpAPI Google Shopping (`shopping.py`): user texts "watch these sneakers under $80" → `add_price_watch` tool saves to `price_watches` → `run_price_watches` job runs every 6h. Each tick: SerpAPI Google Shopping query on the product name, Haiku picks the cheapest genuine match from the top candidates (guards against firing on unrelated accessories/refurbs), first successful check establishes a baseline silently, subsequent checks alert when the target is hit or the price drops ≥ 15% from baseline. Cooldown defaults to 12h. Fails silently on any SerpAPI error — never surfaces "shopping tool failed" to the user.
- Proactive outbound is scheduled: **mornings** at each user's local time (5-min tick, catch-up window, per-day guard), **evenings** the same way (default 6pm local; a diff against the morning — scores, market moves over 1%, new headlines — and no text on a quiet day), and **missing-data asks** every 60 min for users onboarded without a city so mornings can target them (7-day cooldown, US-daytime UTC window; `DATA_ASK_DRY_RUN=1` to preview). There is no other unprompted sender: the live score poller, the daily "breaking news" alert and the profile check-in were all retired in favour of the evening update.
- Reminder delivery uses `FOR UPDATE SKIP LOCKED` on Postgres — safe for multiple scheduler ticks, no double-sends.
- Twilio webhook signatures (HMAC-SHA1) validated on every inbound request. All DB queries parameterized and scoped to phone number.
- Tool routing is hard: `get_weather` → NWS/Open-Meteo only, `get_price` → CoinGecko/yfinance only, traffic tools → TomTom only, product price watches → SerpAPI only, web search → Tavily news mode. No overlap, no hallucinated data.
