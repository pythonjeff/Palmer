import json
import os
from datetime import datetime, date as date_type
from agent import client, _build_system, _sms_clean, _search, _get_weather, _parse_json, _derive_timezone, HAIKU_MODEL, SONNET_MODEL
from db import get_profile, upsert_profile, get_all_phones, save_message


def extract_morning_prefs(phone: str, pref_text: str) -> list[str]:
    """Extract morning topics and profile fields from onboarding reply. Returns extracted topics."""
    try:
        profile = get_profile(phone)
        city = profile.get("city") or ""
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": f"""Someone just replied to "What city are you in, and what should I be tracking for you?"

Their reply: "{pref_text}"
Their city (if already known): "{city}"

Extract everything worth saving:
1. morning_topics — specific searchable topics for daily briefing (include city where relevant). Examples: "Chicago weather", "Bitcoin price", "Cardinals game score"
2. city — if they mention where they live
3. name — if they introduce themselves
4. interests, sports_teams — anything else they mention caring about

Return JSON only:
{{"morning_topics": ["..."], "city": "...", "name": "...", "interests": ["..."]}}
Omit keys with no value. morning_topics can be []."""}],
        )
        data = _parse_json(response.content[0].text)
        if isinstance(data, dict):
            updates = {k: v for k, v in data.items() if v}
            topics = updates.pop("morning_topics", [])
            if updates:
                if updates.get("city") and not profile.get("timezone"):
                    tz = _derive_timezone(updates["city"])
                    if tz:
                        updates["timezone"] = tz
                upsert_profile(phone, updates)
            if topics:
                existing = profile.get("morning_topics") or []
                merged = list(existing)
                for t in topics:
                    if not any(t.lower() in e.lower() or e.lower() in t.lower() for e in merged):
                        merged.append(t)
                upsert_profile(phone, {"morning_topics": merged})
                return topics
    except Exception:
        pass
    return []


def _infer_city_from_topics(topics: list[str]) -> str | None:
    """Extract a city from morning topics when the profile has no city set."""
    if not topics:
        return None
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=20,
            messages=[{"role": "user", "content": f"What city is implied by these morning briefing topics? Reply with just the city name (e.g. 'Kirkwood, MO'), or NONE.\n{topics}"}],
        )
        result = response.content[0].text.strip()
        if result.upper() != "NONE" and len(result) < 60:
            return result
    except Exception:
        pass
    return None


def _get_search_queries(profile: dict) -> list[str]:
    today = date_type.today().strftime("%B %d, %Y")
    topics = profile.get("morning_topics")
    if topics:
        topic_list = ", ".join(topics)
        prompt = f"""Today is {today}. Convert these morning briefing topics into search queries that will find fresh, current results from today or last night.

Topics: {topic_list}
City (if relevant): {profile.get("city") or "unknown"}

Make queries time-specific — include "today", "this morning", "last night", or "{today}" where it helps get current results.
Return a JSON array of search queries, one per topic. Example: ["Bitcoin price {today}", "St. Louis weather today", "Cardinals score last night"]. Just the JSON array."""
    else:
        prompt = f"""Today is {today}. Based on this user profile, what should I search for their morning briefing?

Profile: {json.dumps(profile, indent=2)}

Make queries time-specific — use "today", "this morning", or "{today}" to get current results.
Return a JSON array of 1-3 search queries. Example: ["St. Louis weather today", "Cardinals score last night"]. If unclear, return []. Just the JSON array."""

    response = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    parsed = _parse_json(response.content[0].text)
    return parsed if isinstance(parsed, list) else []


_WEATHER_KEYWORDS = ("weather", "forecast", "temperature", "rain", "snow", "wind", "humidity")


def generate_morning(phone: str) -> str:
    profile = get_profile(phone)
    system = _build_system(phone, include_recent=True)
    today = date_type.today().strftime("%B %d, %Y")
    queries = _get_search_queries(profile)
    city = profile.get("city") or ""

    result_parts = []
    for q in queries:
        if city and any(w in q.lower() for w in _WEATHER_KEYWORDS):
            # Route weather queries through the live weather API, not web search
            result_parts.append(f"{q}:\n{_get_weather(city, 'today')}")
        else:
            result_parts.append(f"{q}:\n{_search(q, days=2)}")

    results = "\n\n".join(result_parts) if result_parts else ""
    prompt = f"""Today is {today}. Write a morning text for this person based on what you found below.

Search results:
{results}

Rules:
- Only include information that's clearly from today or last night. Check the Published dates.
- If results for a topic look stale, outdated, or generic — skip that topic entirely. Do not make something up or paraphrase old news.
- One or two sentences per topic max. The whole thing should fit in a single text.
- Palmer's voice — no bullet points, no headers, no "good morning". Just the message.
- Plain ASCII text only. No emoji, no special characters, no dashes longer than a hyphen."""

    response = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=500,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    result = _sms_clean(response.content[0].text.strip())
    if len(result) < 20:
        raise ValueError(f"generate_morning produced suspiciously short output: {repr(result)}")
    return result


def _split_message(text: str, max_chars: int = 900) -> list[str]:
    """Split at paragraph breaks; fall back to hard chunks if no breaks exist."""
    if len(text) <= max_chars:
        return [text]
    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(parts) > 1:
        return parts
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def _is_morning_local(tz_name: str) -> bool:
    """Return True if it's currently 6–10am in the given IANA timezone."""
    try:
        from zoneinfo import ZoneInfo
        return 6 <= datetime.now(ZoneInfo(tz_name)).hour < 10
    except Exception:
        return False


# NOTE: Heroku Scheduler must run send_morning.py every hour (not once daily)
# for per-timezone sends to work. The morning_sent_date guard prevents double-sends.
def send_morning_messages():
    from sms_util import send_sms
    today = date_type.today().isoformat()

    for phone in get_all_phones():
        profile = get_profile(phone)
        if not profile.get("morning_onboarded"):
            continue
        if profile.get("morning_sent_date") == today:
            continue  # already sent today
        tz = profile.get("timezone")
        if not tz:
            city = profile.get("city")
            if not city:
                # Try to recover city from morning topics (e.g. "Kirkwood, MO weather")
                city = _infer_city_from_topics(profile.get("morning_topics") or [])
                if city:
                    upsert_profile(phone, {"city": city})
            if city:
                tz = _derive_timezone(city)
                if tz:
                    upsert_profile(phone, {"timezone": tz})
        if not tz or not _is_morning_local(tz):
            continue  # skip if timezone still unknown or not currently 6-10am local
        try:
            message = generate_morning(phone)
            parts = _split_message(message)
            for part in parts:
                send_sms(phone, part)
            save_message(phone, "assistant", message)
            upsert_profile(phone, {"morning_sent_date": today})
            print(f"Sent to {phone} ({len(parts)} part(s)): {message}")
        except Exception as e:
            print(f"Failed for {phone}: {e}")
