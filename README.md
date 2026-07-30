# Palmer

**The AI that texts like a friend who actually pays attention.**

Palmer is a personal AI delivered entirely over SMS. No app to download, no interface to learn — just text it. It knows who you are, remembers what you care about, and reaches out when something worth knowing happens.

---

## What Palmer does

### Knows you
Every exchange teaches Palmer something new. Your city, your job, your teams, your interests — it builds a picture of you and uses it the way a real friend would: casually, in context, without citation. The longer you text, the sharper it gets.

### Sends your morning
Each morning Palmer texts you a short, personalized briefing on what you actually care about — weather, markets, sports, news. Not a newsletter. One text, just the things that matter, from today.

### Watches for things
Tell Palmer to watch for something — a geopolitical event, a company move, an athlete's health update, anything — and it runs that in the background. When it hits, you get a text. No feed to check.

### Sets reminders
Natural-language reminders that arrive when you need them. "Remind me Friday morning to prep for the meeting." Done.

### Sends alerts
When something massive breaks in an area you care about, Palmer texts you before you'd think to check. Score threshold is high — it texts when it's actually worth knowing, not for every update.

### Answers anything
Crypto prices, stock quotes, weather forecasts, sports scores, current events — all through the same text thread, no switching apps.

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
| Crypto prices | CoinGecko |
| Stock prices | yfinance |
| GIFs | Giphy |
| Background jobs | APScheduler (reminders 1min, watches + alerts 30min) |
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
  TWILIO_PHONE_NUMBER=... TAVILY_API_KEY=... GIPHY_API_KEY=... OWM_API_KEY=... APP_URL=...
heroku config:set WEB_CONCURRENCY=1
git push heroku main
```

Set your Twilio SMS webhook to `https://<your-app>.herokuapp.com/sms` (POST).  
Set your Twilio status callback to `https://<your-app>.herokuapp.com/sms-status` (POST).

**Heroku Scheduler:** add `python send_morning.py` on an hourly interval. The function guards against double-sends and only fires in each user's 6–9am local window.

---

## Preview endpoints

```
GET /preview?phone=+15551234567         # generate morning briefing without sending
GET /preview/hourly?phone=+15551234567  # run hourly weather/sports/deals checks
```

---

## Architecture

- `WEB_CONCURRENCY=1` is required. In-memory phone locks and APScheduler only work correctly in a single process.
- Per-phone `threading.Lock` serializes inbound messages so conversation history never interleaves under concurrent requests from the same number.
- Watches and alert checks both run every 30 minutes via APScheduler. Watches enforce a per-watch cooldown (default 4 hours) to prevent repeat alerts on slow-moving stories.
- Reminder delivery uses `FOR UPDATE SKIP LOCKED` on Postgres — safe for multiple scheduler ticks, no double-sends.
- Twilio webhook signatures (HMAC-SHA1) validated on every inbound request. All DB queries parameterized and scoped to phone number.
- Tool routing is hard: `get_weather` → OWM only, `get_price` → CoinGecko/yfinance only, web search → Tavily news mode. No overlap, no hallucinated data.
