# Palmer

**The AI that texts like a friend who actually pays attention.**

Palmer is a personal AI delivered entirely over SMS. No app to download, no interface to learn — just text it. It knows who you are, remembers what you care about, and reaches out when something worth knowing happens.

---

## What Palmer does

### Knows you
Every exchange teaches Palmer something new. Your city, your job, your teams, your interests — it builds a picture of you and uses it the way a real friend would: casually, in context, without citation. The longer you text, the sharper it gets.

### Sends your morning
Each morning Palmer texts you a short, personalized briefing on what you actually care about — weather, local traffic, markets, sports, news. Not a newsletter. One text, just the things that matter, from today.

### Watches for things
Tell Palmer to watch for something — a geopolitical event, a company move, an athlete's health update, anything — and it runs that in the background. When it hits, you get a text. No feed to check.

### Sets reminders
Natural-language reminders that arrive when you need them. "Remind me Friday morning to prep for the meeting." Done.

### Sends alerts
When something massive breaks in an area you care about, Palmer texts you before you'd think to check. Score threshold is high — it texts when it's actually worth knowing, not for every update.

### Follows up
If you mentioned an interview, a doctor's visit, a rough week — Palmer notices. It circles back a day or two later to ask how it went. Not scripted check-ins; it picks the thread worth pulling on.

### Sees photos
Send Palmer a picture and it'll actually respond to what's in it — a menu, a whiteboard, a receipt, a dog. It's using vision, not guessing from a filename.

### Answers anything
Crypto prices, stock quotes, weather forecasts, sports scores, current events, city traffic, drive times with live traffic — all through the same text thread, no switching apps.

### Stays out of the way
Tell Palmer to pause the morning texts, drop a topic, forget a fact about you, or change when it sends — all by texting. No settings screen.

---

## Personality

Palmer is dry, quick, and observant. It's not an assistant and it's not a brand — it's a voice. It reads subtext, matches your energy, calls out patterns, and knows when to drop a GIF. Built on Claude, it converses like a person, not a product.

---

## Stack

| Layer | Technology |
|---|---|
| Conversation + reasoning | Claude Sonnet 4.6 |
| Fast inference (extraction, scoring) | Claude Haiku 4.5 |
| SMS/MMS | Twilio |
| Web server | FastAPI on Heroku |
| News + web search | Tavily |
| Weather | OpenWeatherMap |
| Traffic + routing | TomTom (Traffic Flow, Traffic Incidents, Routing, Search geocoding) |
| Crypto prices | CoinGecko |
| Stock prices | yfinance |
| GIFs | Giphy |
| Background jobs | APScheduler (reminders 1m · mornings 5m · watches 30m · alerts 60m · missing-data asks 60m · follow-ups 4h) |
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
| `OWM_API_KEY` | openweathermap.org (free tier) |
| `TOMTOM_API_KEY` | developer.tomtom.com (free tier, ~2,500 requests/day) |
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
  TWILIO_PHONE_NUMBER=... TAVILY_API_KEY=... GIPHY_API_KEY=... OWM_API_KEY=... \
  TOMTOM_API_KEY=... APP_URL=...
heroku config:set WEB_CONCURRENCY=1
git push heroku main
```

Set your Twilio SMS webhook to `https://<your-app>.herokuapp.com/sms` (POST).  
Set your Twilio status callback to `https://<your-app>.herokuapp.com/sms-status` (POST).

**Morning briefings** are sent by APScheduler inside the web dyno (checked every 5 minutes) at each user's chosen local time — 8:30am by default, changeable by texting Palmer. No Heroku Scheduler job is required; if one exists running `python send_morning.py`, it's a harmless redundant backup (the per-user sent-date guard prevents double-sends).

---

## Preview endpoints

```
GET /preview?phone=+15551234567         # generate morning briefing without sending
```

---

## Architecture

- `WEB_CONCURRENCY=1` is required. In-memory phone locks and APScheduler only work correctly in a single process.
- Per-phone `threading.Lock` serializes inbound messages so conversation history never interleaves under concurrent requests from the same number.
- Watches run every 30 minutes via APScheduler but only alert on major breaking developments — dated results from the last 12 hours, a strict criticality gate, per-watch cooldown (default 4 hours), and dedup against the last alerted event. Two source-quality gates keep bad links out: a curated trusted-domains file (`trusted_sources.json`, tier 1 = AP/Reuters/BBC/NYT/WSJ/Bloomberg/ESPN etc., tier 2 = mainstream, plus `.gov`/`.edu` at tier 1) ranks which URL to send, and a corroboration check requires ≥ 2 distinct domains OR ≥ 1 tier-1 source before firing.
- Traffic uses TomTom. Morning briefings auto-include a short city snapshot (`get_city_traffic`): geocode → parallel Traffic Flow + Traffic Incidents in a city bounding box → Haiku drafts one natural line, or skips silently on failure. On demand, users can ask for city conditions or point-to-point drive times (`get_travel_time`, live traffic vs. free-flow). Landmark destinations (White House, Fenway, LAX) get resolved to street addresses by Sonnet before geocoding — TomTom's geocoder is a mapping API, not a search engine, and mis-ranks landmark names.
- Proactive outbound is scheduled: **mornings** at each user's local time (5-min tick, catch-up window, per-day guard), **breaking-news alerts** every 60 min (score ≥ 8, 1–9pm local send window), **follow-ups** every 4h (Haiku picks one ongoing thread, Sonnet drafts, 1–7pm window, 3-day gap guard), and **missing-data asks** every 60 min for users onboarded without a city so mornings can target them (7-day cooldown, US-daytime UTC window; `DATA_ASK_DRY_RUN=1` to preview).
- Reminder delivery uses `FOR UPDATE SKIP LOCKED` on Postgres — safe for multiple scheduler ticks, no double-sends.
- Twilio webhook signatures (HMAC-SHA1) validated on every inbound request. All DB queries parameterized and scoped to phone number.
- Tool routing is hard: `get_weather` → OWM only, `get_price` → CoinGecko/yfinance only, traffic tools → TomTom only, web search → Tavily news mode. No overlap, no hallucinated data.
