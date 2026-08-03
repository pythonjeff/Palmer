import re
from datetime import datetime, date as date_type, timedelta
from agent import (
    client, _build_system, _sms_clean, _search, _weather_report, _get_price,
    _parse_json, _derive_timezone, _normalize_hhmm, _CRYPTO_IDS, HAIKU_MODEL, SONNET_MODEL,
)
from db import get_profile, upsert_profile, get_all_phones, save_message

DEFAULT_MORNING_TIME = "08:30"
# How long after the target time we'll still send (covers missed scheduler ticks
# or a transient generation failure) before giving up for the day.
CATCHUP_WINDOW_MINUTES = 120
MAX_TOPICS = 3


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
5. morning_time — if they mention a preferred send time (e.g. "7am", "9:30"), return in 24-hour HH:MM format

Return JSON only:
{{"morning_topics": ["..."], "city": "...", "name": "...", "interests": ["..."], "morning_time": "HH:MM"}}
Omit keys with no value. morning_topics can be []."""}],
        )
        data = _parse_json(response.content[0].text)
        if isinstance(data, dict):
            updates = {k: v for k, v in data.items() if v}
            topics = updates.pop("morning_topics", [])
            raw_time = updates.pop("morning_time", None)
            if raw_time:
                normalized = _normalize_hhmm(raw_time)
                if normalized:
                    upsert_profile(phone, {"morning_time": normalized})
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
    except Exception as e:
        print(f"extract_morning_prefs failed for {phone}: {e}")
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
    except Exception as e:
        print(f"_infer_city_from_topics failed: {e}")
    return None


_WEATHER_KEYWORDS = ("weather", "forecast", "temperature", "rain", "snow", "wind", "humidity")
_PRICE_KEYWORDS = ("price", "stock", "shares", "ticker", "crypto")


def _price_asset_for_topic(topic: str) -> str | None:
    """Return an asset identifier if this topic is a price request, else None."""
    low = topic.lower()
    for name in _CRYPTO_IDS:
        if re.search(rf"\b{re.escape(name)}\b", low):
            return name
    if any(k in low for k in _PRICE_KEYWORDS):
        ticker = re.search(r"\b([A-Z]{1,5})\b", topic)
        if ticker:
            return ticker.group(1)
    return None


def _gather_morning_data(profile: dict) -> list[str]:
    """Fetch the briefing data: local weather always (when city known), then up to
    MAX_TOPICS user topics — prices from the price API, everything else from
    dated news search only."""
    sections = []
    city = profile.get("city") or ""
    if city:
        try:
            sections.append(f"Local weather:\n{_weather_report(city, 'today')}")
        except Exception as e:
            # Skip the weather section rather than feeding a failure message to
            # the drafting model (which used to produce "weather tool failed"
            # texts). If weather was the whole briefing, generate_morning raises
            # and the 5-minute scheduler retries within the catch-up window.
            print(f"Morning weather unavailable for {city!r}: {e}")

    topics = [
        t for t in (profile.get("morning_topics") or [])
        if t and not any(w in t.lower() for w in _WEATHER_KEYWORDS)  # weather already covered
    ]
    for topic in topics[:MAX_TOPICS]:
        asset = _price_asset_for_topic(topic)
        if asset:
            sections.append(f"{topic}:\n{_get_price(asset)}")
        else:
            sections.append(f"{topic}:\n{_search(topic, days=1, require_date=True)}")
    return sections


_META_COMMENTARY_PHRASES = [
    "not sending", "won't send", "can't include", "skipping this",
    "this one falls", "this one's right", "avoid zone", "dark content zone",
    "preference", "they asked", "they set",
]


def _reject_meta_commentary(text: str):
    lower = text.lower()
    for phrase in _META_COMMENTARY_PHRASES:
        if phrase in lower:
            raise ValueError(f"generate_morning produced meta-commentary: {repr(text[:100])}")


def generate_morning(phone: str) -> str:
    profile = get_profile(phone)
    system = _build_system(phone, include_recent=True)
    today = _local_today(profile.get("timezone")).strftime("%B %d, %Y")

    sections = _gather_morning_data(profile)
    if not sections:
        raise ValueError("no morning data available — user has no city and no topics")
    data = "\n\n".join(sections)

    threads = [t for t in (profile.get("ongoing_threads") or []) if t][:3]
    life_ctx = (profile.get("life_context") or "").strip()
    context_lines = []
    if threads:
        context_lines.append(f"Open threads: {', '.join(threads)}")
    if life_ctx:
        context_lines.append(f"Life context: {life_ctx}")
    context_block = ("\n\n" + "\n".join(context_lines)) if context_lines else ""

    prefs = profile.get("morning_prefs") or {}
    avoid_list = prefs.get("avoid") or []
    avoid_rule = (
        f"- Silently skip any item that involves: {', '.join(avoid_list)}. "
        "Do NOT mention that you're skipping it — just omit it and move on.\n"
        if avoid_list else ""
    )

    prompt = f"""Today is {today}. Write this person's morning text using only the data below.

{data}{context_block}

Rules:
- Lead with the weather if it's included.
- Only report what's in the data above. If a topic's results are empty or look stale, skip that topic entirely — never fill in from memory or paraphrase old news.
- One or two sentences per item. Keep the whole thing under 700 characters.
{avoid_rule}- If there's an open thread above that has a natural check-in moment (something time-sensitive, emotional, or where progress is expected), weave in one brief mention — like a friend who remembered. Skip it if nothing fits naturally.
- Palmer's voice — no bullet points, no headers, no "good morning". Just the message.
- Plain ASCII text only. No emoji, no special characters, no dashes longer than a hyphen."""

    response = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=300,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    result = _sms_clean(response.content[0].text.strip())
    if len(result) < 20:
        raise ValueError(f"generate_morning produced suspiciously short output: {repr(result)}")
    _reject_meta_commentary(result)
    return result


def _split_message(text: str, max_chars: int = 900) -> list[str]:
    """Split at paragraph breaks; fall back to hard chunks if no breaks exist."""
    if len(text) <= max_chars:
        return [text]
    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(parts) > 1:
        return parts
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def _local_now(tz_name: str) -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(tz_name))


def _local_today(tz_name: str | None) -> date_type:
    """The user's local calendar date; falls back to server date if tz is missing/bad."""
    if tz_name:
        try:
            return _local_now(tz_name).date()
        except Exception:
            pass
    return date_type.today()


def _parse_morning_time(value) -> tuple[int, int]:
    """Parse an 'HH:MM' preference; fall back to the 8:30 default on anything invalid."""
    try:
        h, m = str(value).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except Exception:
        pass
    return (8, 30)


def _format_morning_time(value) -> str:
    """Format a morning_time profile value as human-readable 12-hour time, e.g. '8:30am'."""
    h, m = _parse_morning_time(value)
    period = "am" if h < 12 else "pm"
    display_h = h % 12 or 12
    return f"{display_h}:{m:02d}{period}"


def _in_send_window(now_local: datetime, morning_time: str | None,
                    catchup_minutes: int = CATCHUP_WINDOW_MINUTES) -> bool:
    """True if now is at/after the user's morning time but within the catch-up window."""
    h, m = _parse_morning_time(morning_time or DEFAULT_MORNING_TIME)
    target = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
    return target <= now_local < target + timedelta(minutes=catchup_minutes)


# Called every 5 minutes by APScheduler in main.py. The morning_sent_date guard
# (keyed to the user's local date) makes extra invocations harmless, so the old
# hourly Heroku Scheduler job can stay on as a redundant backup or be removed.
def send_morning_messages():
    from sms_util import send_sms

    for phone in get_all_phones():
        try:
            profile = get_profile(phone)
            if not profile.get("morning_onboarded"):
                continue
            if profile.get("morning_enabled") is False:
                continue

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
            if not tz:
                continue

            try:
                now_local = _local_now(tz)
            except Exception:
                print(f"Invalid timezone {tz!r} for {phone} — skipping")
                continue

            today_local = now_local.date().isoformat()
            if profile.get("morning_sent_date") == today_local:
                continue  # already sent today (user's local day)
            if not _in_send_window(now_local, profile.get("morning_time")):
                continue

            message = generate_morning(phone)
            parts = _split_message(message)
            sent = False
            for part in parts:
                if send_sms(phone, part) and not sent:
                    sent = True
                    # Mark immediately after the first accepted part so a crash
                    # mid-send can't double-send tomorrow's window.
                    upsert_profile(phone, {"morning_sent_date": today_local})
            if sent:
                save_message(phone, "assistant", message)
                print(f"Morning sent to {phone} ({len(parts)} part(s)): {message[:100]}")
            else:
                print(f"Morning send rejected by Twilio for {phone} — will retry next tick")
        except Exception as e:
            # No morning_sent_date written, so the next 5-minute tick retries
            # (until the catch-up window closes).
            print(f"Morning update failed for {phone}: {e}")
