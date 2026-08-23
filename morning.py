import os
import re
from datetime import datetime, date as date_type, timedelta, timezone
from agent import _build_system
from llm import client, HAIKU_MODEL, SONNET_MODEL
from smstext import _sms_clean
from weather import _weather_report
from datafeeds import _search_raw, _get_price, _CRYPTO_IDS
from userprofile import _derive_timezone
from db import get_profile, upsert_profile, get_all_profiles, save_message, get_history, claim_daily_guard
from traffic import get_city_traffic, get_travel_time

DEFAULT_MORNING_TIME = "07:00"
# How long after the target time we'll still send (covers missed scheduler ticks
# or a transient generation failure) before giving up for the day.
CATCHUP_WINDOW_MINUTES = 120
# Topics pulled per briefing. Each is one Tavily search. Was 3, which silently
# dropped most of what users had subscribed to — someone tracking 8 subjects got
# the first 3 and never knew.
MAX_TOPICS = 6


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


# A commute is a route, not a news topic. Users phrase it as one anyway
# ("Daily commute traffic: <origin> to <destination>"), so accept that shape and
# route it to TomTom point-to-point instead of the city-wide traffic line.
_COMMUTE_RE = re.compile(r"(?:commute|drive|route|traffic)\b[^:]*:\s*(.+?)\s+to\s+(.+)$", re.I)

# get_travel_time always returns a string, including its failures. Fall back to
# the city line rather than putting "Couldn't find that starting address" in a
# briefing.
_ROUTE_FAILURES = ("couldn't find", "routing failed", "not configured", "need both")


def _parse_commute_topic(topic: str) -> tuple[str, str] | None:
    m = _COMMUTE_RE.search((topic or "").strip())
    if not m:
        return None
    origin, dest = m.group(1).strip(" .,"), m.group(2).strip(" .,")
    if len(origin) < 5 or len(dest) < 5:
        return None
    return origin, dest


def _commute_route(profile: dict) -> tuple[str, str] | None:
    """The user's saved commute, preferring the structured field over the
    free-text topic it may still be stored as."""
    c = profile.get("commute") or {}
    if isinstance(c, dict) and c.get("origin") and c.get("destination"):
        return c["origin"], c["destination"]
    for topic in profile.get("morning_topics") or []:
        parsed = _parse_commute_topic(topic)
        if parsed:
            return parsed
    return None


def _route_line_ok(line: str | None) -> bool:
    return bool(line) and not any(f in line.lower() for f in _ROUTE_FAILURES)


_WEATHER_KEYWORDS = ("weather", "forecast", "temperature", "rain", "snow", "wind", "humidity")
_TRAFFIC_KEYWORDS = ("traffic", "commute", "highway", "roads")
_PRICE_KEYWORDS = ("price", "stock", "shares", "ticker", "crypto")


# Users answer "what should I get every morning?" with delivery preferences as
# often as with subjects — "Format: bullet points per subject" was sitting in one
# user's topic list and being sent to the news search as a query.
_DIRECTIVE_PREFIXES = ("format:", "formatting:", "style:", "tone:", "length:",
                       "note:", "preference:", "prefer:", "please ")


def _is_directive(topic: str) -> bool:
    """True if this 'topic' is really an instruction, not a subject to search."""
    return topic.strip().lower().startswith(_DIRECTIVE_PREFIXES)


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


# How many stories per topic reach the drafting model. Three gives it enough to
# pick the most consequential angle without burning the character budget.
_STORIES_PER_TOPIC = 3
_TOPIC_MAX_AGE_HOURS = 24


def _topic_digest(topic: str) -> str | None:
    """Recent, source-ranked stories for one topic, or None when nothing solid.

    _search returns whatever Tavily ranked first and drops the URL, so the
    drafting model had no idea whether a line came from Reuters or a content
    farm. This reuses the same tier-then-score ordering watches.py uses to pick
    what is worth alerting on, and returns None rather than the string "No
    results found." so a thin topic is left out of the data entirely instead of
    being handed to the model to silently skip."""
    from watches import _source_tier, _canonical_domain
    results = _search_raw(topic, days=1, max_age_hours=_TOPIC_MAX_AGE_HOURS, min_score=0.5)
    if not results:
        return None
    results.sort(key=lambda r: (_source_tier(r.get("url", "")), -(r.get("score") or 0)))
    lines = []
    for r in results[:_STORIES_PER_TOPIC]:
        domain = _canonical_domain(r.get("url", "")) or "unknown source"
        lines.append(f"[{domain}] {r.get('title','')}\n{r.get('content','')}")
    return "\n\n".join(lines)


def _rotated_topics(topics: list[str], today) -> list[str]:
    """The topics to pull today, rotating the window so none is starved.

    Truncating at MAX_TOPICS drops by list position, so with 7 topics and a cap
    of 6 the 7th never ran — one user's "Daily fun fact from history" had never
    once been delivered, silently, for a month. Rotating by the user's local
    date means every topic comes up regularly and the set still changes day to
    day. Rotation is deterministic, so a retry within the same day pulls the
    same topics rather than a different briefing."""
    if len(topics) <= MAX_TOPICS:
        return topics
    offset = today.toordinal() % len(topics)
    return (topics[offset:] + topics[:offset])[:MAX_TOPICS]


def _gather_morning_data(profile: dict) -> list[str]:
    """Fetch the briefing data: local weather always (when city known), then up to
    MAX_TOPICS user topics — prices from the price API, everything else from
    dated news search only."""
    sections = []
    covered: list[str] = []   # headlines already used, so the adjacent pick can't repeat one
    city = profile.get("city") or ""
    if city:
        try:
            sections.append(f"Local weather:\n{_weather_report(city, 'today', tz=profile.get('timezone'))}")
        except Exception as e:
            # Skip the weather section rather than feeding a failure message to
            # the drafting model (which used to produce "weather tool failed"
            # texts). If weather was the whole briefing, generate_morning raises
            # and the 5-minute scheduler retries within the catch-up window.
            print(f"Morning weather unavailable for {city!r}: {e}")
        # Commute route beats city-wide conditions when we know it — "22 minutes
        # to Clayton, 3 over normal" is actionable, "roads are clear" is not.
        try:
            route = _commute_route(profile)
            traffic_line = None
            if route:
                line = get_travel_time(*route)
                if _route_line_ok(line):
                    traffic_line = line
                else:
                    print(f"Morning commute routing failed ({route[0]!r} -> {route[1]!r}): {line}")
            if traffic_line is None:
                traffic_line = get_city_traffic(city)
            if traffic_line:
                sections.append(f"Commute / traffic:\n{traffic_line}")
        except Exception as e:
            print(f"Morning traffic unavailable for {city!r}: {e}")

    _auto_covered = _WEATHER_KEYWORDS + _TRAFFIC_KEYWORDS
    topics = [
        t for t in (profile.get("morning_topics") or [])
        if t and not any(w in t.lower() for w in _auto_covered)  # weather + traffic already covered
        and not _is_directive(t)
    ]
    for topic in _rotated_topics(topics, _local_today(profile.get("timezone"))):
        asset = _price_asset_for_topic(topic)
        if asset:
            sections.append(f"{topic}:\n{_get_price(asset)}")
            continue
        digest = _topic_digest(topic)
        if digest:
            sections.append(f"{topic}:\n{digest}")
            covered.append(digest.splitlines()[0])
        else:
            print(f"Morning topic {topic!r}: nothing recent enough, skipping")

    # One story they didn't ask for but would want — see trends.py. Strictly
    # optional: returns None most days, and the drafting prompt treats it as
    # droppable, so a miss costs nothing.
    try:
        from trends import adjacent_story
        pick = adjacent_story(topics or [], covered)
        if pick:
            sections.append(
                f"ADJACENT (not one of their topics — {pick['why']}):\n{pick['story']}"
            )
    except Exception as e:
        print(f"Morning adjacent story unavailable: {e}")
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
- Each story is tagged with its source domain in brackets. Use that to judge weight — a wire service or major outlet outranks an aggregator — and to attribute when it matters ("Reuters says..."). Never print the bracket tag itself.
- ONLY include weather and traffic (when provided), the topics they explicitly asked to track, and the ADJACENT item if one is present in the data. Never add outside news, world commentary, unrelated events, or your own opinions on things they didn't ask about. The ADJACENT block is the single exception and only when it is actually there — never invent one. If a topic's results are empty, off-topic, or clearly stale, silently skip that topic — no filler, no paraphrasing old news, no inventing.
- Weather: name the city the forecast is for, exactly as it appears in the data (e.g. "St. Louis" or "Kirkwood, MO"). Don't drop it, don't swap in a nickname, don't just say "today" without saying where. Use the numbers from the data verbatim — don't round the high 10 degrees, don't invent rain chances, don't reinterpret a "clear" description as "sunny and hot". If the data says a forecast couldn't be pulled, say briefly you can't get today's weather and move on — never tell them to google it or check another app.
- Traffic: keep it to the one line provided in the data. Don't expand, embellish, or invent street/exit names beyond what's given. If traffic is normal, one short sentence is enough.
- ORDER, always the same so they know where to look:
  1. Weather, then the commute line. These are the two things they act on before leaving the house, so they go first and stay together. Never bury them mid-message.
  2. The topics, most consequential first — a real development outranks a routine update. Skip any that came back thin.
  3. The ADJACENT item, if the data has one — one sentence, placed after the topics. Lead into it so it doesn't read as a non sequitur ("unrelated, but -" or "one other thing -").
     This one is optional and you are the last check on it. Drop it entirely if it would push you over length, or if it turns out to be a non-event: an incident that resolved with nobody hurt, a scheduled game, a recall, a celebrity item. If you can't say what CHANGED, leave it out. A briefing that ends a line early is better than one padded with a story nobody needed.
  4. The personal touch, last, on its own.
  Keep the layout fixed. Vary the WORDING of the opening line instead — the first sentence should not start the same way as yesterday's, even though it is still about the weather.
- One or two sentences per topic. Whole message under 1000 characters.
- Give each distinct subject (weather, traffic, each topic, the personal touch) its own line — a blank line between them — so it reads as separate, scannable items rather than one run-on paragraph. Still plain text, no bullet characters or numbering.
- Never label a line with its subject. Write "Cards lost 5-4 in Cincinnati" — never "Cardinals - lost 5-4", never "Weather:", never a header of any kind. Each line should read like a sentence a friend typed, not a field in a report.
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


def _local_now(tz_name: str) -> datetime:
    from timeutil import local_now
    return local_now(tz_name)


def _local_today(tz_name: str | None) -> date_type:
    """The user's local calendar date; falls back to server date if tz is missing/bad.
    Thin wrapper over timeutil.local_today so existing imports (followup.py)
    keep working while the canonical helper lives in one place."""
    from timeutil import local_today
    return local_today(tz_name)


def _parse_morning_time(value) -> tuple[int, int]:
    """Parse an 'HH:MM' preference; fall back to the 7:00 default on anything invalid."""
    try:
        h, m = str(value).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except Exception:
        pass
    return (7, 0)


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

    for phone, profile in get_all_profiles():
        try:
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
            if not claim_daily_guard(phone, "morning_sent_date", today_local):
                continue  # another process/tick already claimed today's send

            try:
                message = generate_morning(phone)
                if send_sms(phone, message):
                    save_message(phone, "assistant", message)
                    print(f"Morning sent to {phone}: {message[:100]}")
                else:
                    upsert_profile(phone, {"morning_sent_date": None})  # release claim, retry next tick
                    print(f"Morning send rejected by Twilio for {phone} — will retry next tick")
            except Exception as e:
                upsert_profile(phone, {"morning_sent_date": None})  # release claim, retry next tick
                print(f"Morning update failed for {phone}: {e}")
        except Exception as e:
            print(f"Morning check failed for {phone}: {e}")


# Missing-data outreach. When a user finishes onboarding without giving us
# a city, mornings can't fire (no local time to target). Palmer proactively
# texts to ask, so the gap fills itself instead of the user just never hearing
# from us at 7am.

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
    for phone, profile in get_all_profiles():
        try:
            if not _needs_city_ask(profile):
                continue
            matched += 1
            if dry_run:
                message = _draft_city_ask(phone)
                print(f"[dry-run] would text {phone}: {message}")
                continue
            today_str = date_type.today().isoformat()
            if not claim_daily_guard(phone, "city_ask_sent_date", today_str):
                continue
            message = _draft_city_ask(phone)
            if send_sms(phone, message):
                save_message(phone, "assistant", message)
                print(f"City ask sent to {phone}: {message[:80]}")
            else:
                print(f"City ask send rejected by Twilio for {phone}")
        except Exception as e:
            print(f"City ask failed for {phone}: {e}")

    if dry_run:
        print(f"[dry-run] {matched} user(s) matched")
