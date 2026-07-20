import os
import json
from ddgs import DDGS
from agent import client, _search
from db import get_all_phones, get_profile, get_due_reminders, mark_reminder_sent


def _ask_haiku(prompt: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def _shop(query: str) -> str:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.shopping(query, max_results=5))
        if not results:
            return "No results found."
        return "\n\n".join(
            f"{r.get('title', '')}\n${r.get('price', 'N/A')} at {r.get('source', 'unknown')}"
            for r in results
        )
    except Exception as e:
        return f"Shopping search failed: {e}"


def _check_weather(profile: dict) -> str | None:
    import requests
    city = profile.get("city") or profile.get("location")
    if not city:
        return None
    try:
        resp = requests.get(f"https://wttr.in/{city.replace(' ', '+')}?format=j1", timeout=10)
        data = resp.json()
        current = data["current_condition"][0]
        hourly = data["weather"][0]["hourly"]
        current_desc = current["weatherDesc"][0]["value"]
        current_temp = current["temp_F"]
        upcoming = "\n".join(
            f"{h['time'].zfill(4)[:2]}:00 — {h['weatherDesc'][0]['value']}, {h['tempF']}°F, {h['chanceofrain']}% rain"
            for h in hourly[:6]
        )
        summary = f"Current: {current_desc}, {current_temp}°F\n\nNext 6 hours:\n{upcoming}"
        answer = _ask_haiku(f"""Based on this real weather forecast for {city}, is there a notable change worth a heads-up? Rain starting, storm, big temp swing, etc.

{summary}

If yes, 1 casual sentence. If nothing notable, reply exactly: NO_ALERT""")
        return None if answer == "NO_ALERT" else answer
    except Exception:
        return None


def _check_sports(profile: dict) -> str | None:
    teams = profile.get("sports_teams") or profile.get("favorite_teams") or profile.get("teams") or profile.get("sports")
    if not teams:
        return None
    team_str = teams if isinstance(teams, str) else ', '.join(teams)
    results = _search(f"{team_str} game result score recap today")
    answer = _ask_haiku(f"""Did any of these teams play recently? Give a quick recap of the result or latest news.

Teams: {team_str}
Search results: {results}

If there's a game result or notable news, write 1-2 casual sentences. If nothing, reply exactly: NO_ALERT""")
    return None if answer == "NO_ALERT" else answer


def _check_deals(profile: dict) -> str | None:
    brands = profile.get("brands") or profile.get("tracked_brands") or profile.get("shopping_interests") or profile.get("fashion_taste")
    if not brands:
        return None
    brand_str = brands if isinstance(brands, str) else ' '.join(str(b) for b in brands[:3])
    results = _shop(f"{brand_str} sale")
    answer = _ask_haiku(f"""Are any of these brands on sale right now?

Tracking: {brand_str}
Shopping results: {results}

If there's a real discount or sale, share the item, price, and where to get it in 1-2 casual sentences. If nothing worth it, reply exactly: NO_ALERT""")
    return None if answer == "NO_ALERT" else answer


def _send(twilio, from_number: str, phone: str, alerts: list[str]):
    try:
        twilio.messages.create(
            body="\n\n".join(alerts),
            from_=from_number,
            to=phone,
        )
        print(f"Sent {len(alerts)} alert(s) to {phone}")
    except Exception as e:
        print(f"Send failed for {phone}: {e}")


INTRO_QUESTIONS = [
    "Yo, where are you based?",
    "Quick one — what city are you in?",
    "Hey, where are you located? Trying to get a better read on you.",
]

def _profile_has_enough(profile: dict) -> bool:
    return any(profile.get(k) for k in ["city", "location", "sports_teams", "favorite_teams", "brands", "tracked_brands"])


def run_hourly_checks():
    import random
    from twilio.rest import Client
    from db import upsert_profile
    twilio = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    from_number = os.environ["TWILIO_PHONE_NUMBER"]

    for phone in get_all_phones():
        profile = get_profile(phone)
        alerts = []

        for reminder in get_due_reminders(phone):
            alerts.append(f"Reminder: {reminder['text']}")
            mark_reminder_sent(reminder["id"])

        if not _profile_has_enough(profile):
            if not profile.get("hourly_intro_sent"):
                upsert_profile(phone, {"hourly_intro_sent": True})
                _send(twilio, from_number, phone, [random.choice(INTRO_QUESTIONS)])
            elif alerts:
                _send(twilio, from_number, phone, alerts)
            continue

        if profile.get("morning_prefs_received"):
            for checker in [_check_weather, _check_sports, _check_deals]:
                try:
                    alert = checker(profile)
                    if alert:
                        alerts.append(alert)
                except Exception as e:
                    print(f"{checker.__name__} failed for {phone}: {e}")

        if alerts:
            _send(twilio, from_number, phone, alerts)
