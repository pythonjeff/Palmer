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
    city = profile.get("city") or profile.get("location")
    if not city:
        return None
    results = _search(f"hourly weather forecast {city} next few hours")
    answer = _ask_haiku(f"""Is there a notable weather change in {city} worth texting someone about in the next few hours? Rain starting, storm, big temp drop, etc.

{results}

If yes, 1 casual sentence. If nothing notable, reply exactly: NO_ALERT""")
    return None if answer == "NO_ALERT" else answer


def _check_sports(profile: dict) -> str | None:
    teams = profile.get("sports_teams") or profile.get("favorite_teams") or profile.get("teams")
    if not teams:
        return None
    query = f"{teams if isinstance(teams, str) else ', '.join(teams)} score news today"
    results = _search(query)
    answer = _ask_haiku(f"""Any sports news worth texting a fan about right now? Only flag if it's fresh — game just ended, major trade, breaking news. Not old scores.

{results}

If yes, 1-2 casual sentences. If nothing new, reply exactly: NO_ALERT""")
    return None if answer == "NO_ALERT" else answer


def _check_deals(profile: dict) -> str | None:
    brands = profile.get("brands") or profile.get("tracked_brands") or profile.get("shopping_interests")
    if not brands:
        return None
    query = f"{brands if isinstance(brands, str) else ' '.join(str(b) for b in brands[:3])} sale deal"
    results = _shop(query)
    answer = _ask_haiku(f"""Any genuinely good deals on these worth texting about?

Tracking: {brands}
Results: {results}

If there's a specific notable deal, 1-2 casual sentences with the key detail and where to find it. If nothing worth it, reply exactly: NO_ALERT""")
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
