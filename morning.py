import os
import re
from datetime import datetime, date as date_type, timedelta, timezone
from agent import (
    client, _build_system, _sms_clean, _search, _weather_report, _get_price,
    _derive_timezone, _CRYPTO_IDS, HAIKU_MODEL, SONNET_MODEL,
)
from db import get_profile, upsert_profile, get_all_phones, save_message, get_history
from traffic import get_city_traffic

DEFAULT_MORNING_TIME = "08:30"
# How long after the target time we'll still send (covers missed scheduler ticks
# or a transient generation failure) before giving up for the day.
CATCHUP_WINDOW_MINUTES = 120
MAX_TOPICS = 3


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
_TRAFFIC_KEYWORDS = ("traffic", "commute", "highway", "roads")
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
        try:
            traffic_line = get_city_traffic(city)
            if traffic_line:
                sections.append(f"Local traffic:\n{traffic_line}")
        except Exception as e:
            print(f"Morning traffic unavailable for {city!r}: {e}")

    _auto_covered = _WEATHER_KEYWORDS + _TRAFFIC_KEYWORDS
    topics = [
        t for t in (profile.get("morning_topics") or [])
        if t and not any(w in t.lower() for w in _auto_covered)  # weather + traffic already covered
    ]
    for topic in topics[:MAX_TOPICS]:
        asset = _price_asset_for_topic(topic)
        if asset:
            sections.append(f"{topic}:\n{_get_price(asset)}")
        else:
            sections.append(f"{topic}:\n{_search(topic, days=1, require_date=True)}")
    return sections


def _recent_assistant_texts(phone: str, n: int = 4) -> list[str]:
    """Full-text of the last N assistant messages, oldest→newest. Used in the
    morning prompt for anti-repetition: the 250-char truncation in
    _build_system cuts off the personal engagement question at the end of prior
    mornings, so the drafting model can't otherwise see what it asked yesterday."""
    history = get_history(phone, limit=25)
    texts = [m["content"] for m in history if m["role"] == "assistant"]
    return texts[-n:]


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

    recent_msgs = _recent_assistant_texts(phone, n=4)
    if recent_msgs:
        joined = "\n---\n".join(recent_msgs)
        recent_block = (
            "\n\nRecent messages you sent them (do NOT reuse the opener, "
            "phrasing, or engagement question from any of these — pick a "
            "different angle today):\n" + joined
        )
    else:
        recent_block = ""

    prefs = profile.get("morning_prefs") or {}
    avoid_list = prefs.get("avoid") or []
    # always_include overrides avoid — defaults to weather/safety, customizable per user
    always_include = prefs.get("always_include") or ["weather", "traffic", "severe weather alerts", "safety alerts"]
    if avoid_list:
        always_note = f" Exception: always include {', '.join(always_include)}." if always_include else ""
        avoid_rule = (
            f"- Silently skip any item that involves: {', '.join(avoid_list)}.{always_note} "
            "Do NOT mention that you're skipping it — just omit it and move on.\n"
        )
    else:
        avoid_rule = ""

    prompt = f"""Today is {today}. Write this person's morning text using only the data below.

{data}{context_block}{recent_block}

Rules:
- ONLY include weather and traffic (when provided) and the topics they explicitly asked to track. Never add outside news, world commentary, unrelated events, or your own opinions on things they didn't ask about. If a topic's results are empty, off-topic, or clearly stale, silently skip that topic — no filler, no paraphrasing old news, no inventing.
- Traffic: keep it to the one line provided in the data. Don't expand, embellish, or invent street/exit names beyond what's given. If traffic is normal, one short sentence is enough.
- Vary the entry point. Sometimes weather leads, sometimes traffic, sometimes the most interesting topic, sometimes a quick personal note. Different angle than what you sent yesterday.
- One or two sentences per topic. Whole message under 700 characters.
{avoid_rule}- End with ONE personal touch — a single sentence in Palmer's voice that shows you actually thought about THEM today. Rotate the type so it's genuinely different from your recent mornings above:
  * a real check-in tied to something specific in their profile (an ongoing thread, someone they've mentioned, a decision they're weighing, their job, their kids/partner/pet)
  * a curious or thought-provoking question they'd enjoy chewing on with coffee — tied to their world when possible
  * a brief fun fact connected to their city, team, work, or one of their interests
Just one. Never all three. Never generic ("how's your week going" is banned). It has to land like a best friend who thought of them, not a bot doing a bit.
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
                print(f"Morning skipped for {phone}: no timezone (profile needs city)")
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


# Missing-data outreach. When a user finishes onboarding without giving us
# a city, mornings can't fire (no local time to target). Palmer proactively
# texts to ask, so the gap fills itself instead of the user just never hearing
# from us at 8:30am.

DATA_ASK_COOLDOWN_DAYS = 7
# Safe UTC window when it's daytime across US zones. 16:00-22:00 UTC = 11am-5pm ET,
# 8am-2pm PT, 6am-noon HT. Not perfect for HT/AK but acceptable — we don't know
# the user's local time (that's the whole reason we're asking).
DATA_ASK_UTC_START_HOUR = 16
DATA_ASK_UTC_END_HOUR = 22


def _needs_city_ask(profile: dict) -> bool:
    """User completed onboarding but we have no way to determine their location.
    Without city or timezone, mornings can't fire at the right local time."""
    if not profile.get("morning_onboarded"):
        return False
    if profile.get("morning_enabled") is False:
        return False
    if profile.get("city") or profile.get("timezone"):
        return False
    last_ask = profile.get("city_ask_sent_date")
    if last_ask:
        try:
            last = datetime.fromisoformat(last_ask).date()
            if (date_type.today() - last).days < DATA_ASK_COOLDOWN_DAYS:
                return False
        except Exception:
            pass
    return True


def _in_data_ask_window() -> bool:
    hour = datetime.now(timezone.utc).hour
    return DATA_ASK_UTC_START_HOUR <= hour < DATA_ASK_UTC_END_HOUR


def _draft_city_ask(phone: str) -> str:
    """One or two-sentence ask in Palmer's voice, explaining why he needs the city."""
    from agent import _build_system
    system = _build_system(phone, include_recent=True)
    try:
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=100,
            system=system,
            messages=[{"role": "user", "content": (
                "Text them out of the blue for one reason: you don't have their city "
                "on file, so you can't get their morning briefing sent at the right "
                "local time. Ask for their city naturally in Palmer's voice — one or "
                "two sentences. Mention WHY you need it (morning timing) so they know "
                "what this is about. No opener, no apology, no ceremony. Warm, not needy."
            )}],
        )
        return _sms_clean(response.content[0].text.strip())
    except Exception:
        return _sms_clean(
            "quick one — I don't have your city on file, so I can't get your "
            "morning sent at the right time. where are you?"
        )


def send_missing_data_asks():
    """Ask users with critical missing profile data. Runs hourly; uses a broad
    UTC window since we don't know the user's local time.

    Set DATA_ASK_DRY_RUN=1 to log matches + drafted messages without sending
    or writing cooldown state — useful for verifying who'd be contacted before
    turning it loose."""
    from sms_util import send_sms

    dry_run = os.environ.get("DATA_ASK_DRY_RUN") == "1"

    if not _in_data_ask_window():
        if dry_run:
            print(f"[dry-run] outside window (UTC hour {datetime.now(timezone.utc).hour})")
        return

    matched = 0
    for phone in get_all_phones():
        try:
            profile = get_profile(phone)
            if not _needs_city_ask(profile):
                continue
            matched += 1
            message = _draft_city_ask(phone)
            if dry_run:
                print(f"[dry-run] would text {phone}: {message}")
                continue
            if send_sms(phone, message):
                save_message(phone, "assistant", message)
                upsert_profile(phone, {"city_ask_sent_date": date_type.today().isoformat()})
                print(f"City ask sent to {phone}: {message[:80]}")
            else:
                print(f"City ask send rejected by Twilio for {phone}")
        except Exception as e:
            print(f"City ask failed for {phone}: {e}")

    if dry_run:
        print(f"[dry-run] {matched} user(s) matched")
