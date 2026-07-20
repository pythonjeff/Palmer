# Palmer

A hyperpersonal AI that texts like a sharp, funny friend. Built on Claude, delivered over SMS via Twilio.

Palmer learns who you are over time — your city, job, interests, ongoing threads — and uses that context the way a real friend would: casually, without citation. The longer you text, the better it knows you.

## Features

- Converses over SMS with a distinct personality (dry, quick, loyal)
- Searches the web for real-time info (weather, scores, news)
- Learns and remembers each user automatically after every exchange
- Fully multi-user — each phone number gets its own profile and history

## Stack

- **FastAPI** — webhook server
- **Twilio** — SMS in/out
- **Anthropic Claude** — conversation (Sonnet) + profile extraction (Haiku)
- **DuckDuckGo** — web search, no API key needed
- **SQLite** — message history and user profiles

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
# Fill in ANTHROPIC_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
```

**3. Run locally**
```bash
uvicorn main:app --reload
```

**4. Expose with ngrok (for local testing)**
```bash
ngrok http 8000
```

Set your Twilio webhook to `https://<your-ngrok-url>/sms` (HTTP POST).

## Deployment

See [Heroku deployment](#) or any platform that supports Python. Set the three env vars from `.env.example` in your platform's config.

## Environment Variables

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | From console.anthropic.com |
| `TWILIO_ACCOUNT_SID` | From twilio.com dashboard |
| `TWILIO_AUTH_TOKEN` | From twilio.com dashboard |
