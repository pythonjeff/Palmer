import os
import re
from datetime import datetime, date as date_type, timedelta, timezone
from agent import _build_system
from llm import client, HAIKU_MODEL, SONNET_MODEL
from smstext import _sms_clean
from weather import _weather_report
from datafeeds import _search_raw, _get_price
from userprofile import _derive_timezone
from db import get_profile, upsert_profile, get_all_profiles, save_message, get_history, claim_daily_guard
from traffic import get_city_traffic, get_travel_time

DEFAULT_MORNING_TIME = "07:00"

# What a user gets if they switch the morning on without naming anything.
#
# "Yeah set that up" used to produce morning_topics = [] — the briefing was
# weather and nothing else, the page had an empty News card and no Markets, and
# the user had to volunteer subjects before Palmer was worth reading. A product
# whose baseline is weather, commute, news and prices cannot start three of
# those empty and wait to be asked.
#
# Two topics, not six: enough that the page has something on it from day one,
# few enough that a user who never tunes it is not paying for six searches a
# day about nothing in particular. Local first, because that is the half no
# other app is already giving them.
def default_topics(city: str | None) -> list[str]:
    # Phrasing matters more than it should here, because the search matches
    # query text: "Top national news" returned "Clemson Army ROTC earns top
    # national honors" — a literal word match — and "top US and world news
    # stories", "world news" and "breaking news" all return nothing at all.
    # "National and international news" is the one that works, and it is not a
    # guess: it is the phrasing already in a real user's topic list, hitting
    # Reuters and AP daily since it was added. Do not "improve" it without
    # running it against the live search first.
    topics = ["National and international news"]
    if not city:
        return topics
    # Local coverage is written about metros, not suburbs: "Kirkwood, MO" is
    # also an IndyCar driver, and "Culver City local news" reaches one niche
    # paper where "Los Angeles local news" reaches four newsrooms. Resolved once
    # here, on the seeding path, and cached — never on read.
    place = city
    try:
        from opening import _metro
        place = _metro(city) or city
    except Exception as e:
        print(f"default_topics: metro lookup failed for {city!r}: {type(e).__name__}: {e}")
    topics.insert(0, f"{place} local news")
    return topics
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
# Users answer "what should I get every morning?" with delivery preferences as
# often as with subjects — "Format: bullet points per subject" was sitting in one
# user's topic list and being sent to the news search as a query.
_DIRECTIVE_PREFIXES = ("format:", "formatting:", "style:", "tone:", "length:",
                       "note:", "preference:", "prefer:", "please ")


def _is_directive(topic: str) -> bool:
    """True if this 'topic' is really an instruction, not a subject to search."""
    return topic.strip().lower().startswith(_DIRECTIVE_PREFIXES)


def _price_asset_for_topic(topic: str) -> str | None:
    """Asset symbol if this topic is a price request, else None.

    Resolution lives in tickers.py so the text briefing and the page's Markets
    section can never disagree about what a topic means."""
    from tickers import resolve_topic_asset
    got = resolve_topic_asset(topic)
    return got[0] if got else None


# How many stories per topic reach the drafting model. Three gives it enough to
# pick the most consequential angle without burning the character budget.
_STORIES_PER_TOPIC = 3
_TOPIC_MAX_AGE_HOURS = 24


def _topic_digest(topic: str) -> str | None:
    """Recent, source-ranked stories for one topic, or None when nothing solid.

    _search_raw now drops blocked sources and returns best-source-first, so the
    ordering that used to happen here is gone. What stays is the [domain] label:
    the drafting model had no idea whether a line came from Reuters or a content
    farm, and it needs that to decide what to state flatly and what to hedge.

    Returns None rather than the string "No results found." so a thin topic is
    left out of the data entirely instead of being handed to the model to
    silently skip."""
    from sources import canonical_domain
    results = _search_raw(topic, days=1, max_age_hours=_TOPIC_MAX_AGE_HOURS, min_score=0.5)
    if not results:
        return None
    lines = []
    for r in results[:_STORIES_PER_TOPIC]:
        domain = canonical_domain(r.get("url", "")) or "unknown source"
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
    """Full text of the last N MORNING messages, oldest→newest.

    Used in the morning prompt for anti-repetition: the 250-char truncation in
    _build_system cuts off the personal engagement question at the end of prior
    mornings, so the drafting model can't otherwise see what it said yesterday.

    It used to take the last N assistant messages of any kind out of a 25-message
    window, which for anyone who actually texts Palmer is four chat replies. So
    guards.repeats_opening — written for three consecutive mornings that all
    opened the same way — was comparing today's morning against ordinary
    conversation and almost never against yesterday's morning. The `kind` column
    is what makes the right query possible; fall back to any assistant message
    for users whose history predates it."""
    from db import get_recent_messages_of_kind
    texts = get_recent_messages_of_kind(phone, "morning", limit=n)
    if texts:
        return texts
    history = get_history(phone, limit=25)
    return [m["content"] for m in history if m["role"] == "assistant"][-n:]


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


def _payload_digest(payload: dict) -> str:
    """The page's contents as a few plain lines, for the drafter to pick from.

    Deliberately terse and unopinionated — this is source data for a line that
    already carries the system prompt, not a briefing in its own right."""
    lines = []
    w = payload.get("weather") or {}
    if w.get("temp_now") is not None or w.get("high") is not None:
        bits = []
        if w.get("description"):
            bits.append(str(w["description"]))
        if w.get("high") is not None and w.get("low") is not None:
            if w.get("high_confident") is False:
                # The forecasters disagree about this location today. Hand the
                # drafter the disagreement rather than one side of it, so it
                # hedges instead of asserting a number nobody can stand behind.
                bits.append(f"high somewhere between {w.get('high_low_est')} and "
                            f"{w.get('high_high_est')} (forecasts disagree by "
                            f"{w.get('high_spread')} degrees — do NOT state a single high), "
                            f"low {w['low']:.0f}")
            else:
                bits.append(f"high {w['high']:.0f}, low {w['low']:.0f}")
        if w.get("rain_pct"):
            bits.append(f"{w['rain_pct']}% rain")
        # Label the forecast with the place it was actually fetched for, not the
        # profile's city string. The two agree until profile["city"] drifts, and
        # on the day it drifts this is what stops a Los Angeles temperature from
        # going out under the name Culver City. `resolved` comes back from the
        # same geocode that produced these numbers, so the name and the number
        # cannot disagree.
        where = w.get("resolved") or payload.get("city") or "their city"
        lines.append(f"Weather in {where}: " + ", ".join(bits))
    t = payload.get("traffic") or {}
    if t.get("live_min"):
        from timeutil import friendly_hhmm
        delay = t.get("delay_min") or 0
        # Say which moment the number is FOR. A commute routed for the user's
        # leave time is a forecast for that departure, not current traffic,
        # and the drafter is told so here rather than trusted to infer it.
        if t.get("depart_at"):
            lead = (f"Commute at {friendly_hhmm(t['depart_at'])} (their usual leave "
                    f"time — predicted for that departure)")
        else:
            lead = "Commute right now"
        arrive = f", arriving about {friendly_hhmm(t['arrive_at'])}" if t.get("arrive_at") else ""
        lines.append(f"{lead}: {t['live_min']} min"
                     + (f", {delay} min slower than normal" if delay >= 2 else ", normal")
                     + arrive)
    for p in (payload.get("prices") or [])[:3]:
        lines.append(f"{p.get('label')}: {p.get('pct_24h', 0):+.1f}% in 24h")
    for h in (payload.get("headlines") or [])[:4]:
        lines.append(f"Headline ({h.get('topic') or 'news'}): {h.get('title')}")
    # Followed shows live on the page by default and reach the TEXT only if the
    # user asked for that. A weekly "new episode!!" to someone who never asked
    # is exactly the drumbeat this product keeps having to remove.
    rows = [o for o in (payload.get("opening") or [])
            if o.get("kind") != "episode" or payload.get("episode_alerts")]
    for o in rows[:3]:
        bits = ", ".join(x for x in (o.get("subtitle"), o.get("when")) if x)
        lines.append(f"Opening near them: {o.get('title')}" + (f" — {bits}" if bits else ""))
    return "\n".join(lines)


# Models reach for a placeholder when they know a link is coming — "[link]",
# "(url)", "<here>" — and the prompt alone does not reliably stop it. Left in,
# it ships to the user as literal text sitting right next to the real URL.
_LINK_PLACEHOLDER = re.compile(
    r"""\s*(?:[\[(<]\s*(?:link|url|here|page|site|dashboard)\s*[\])>]|https?://\S+)\s*""",
    re.IGNORECASE,
)


def _strip_link_placeholder(line: str) -> str:
    """Remove any stand-in for the URL the caller is about to append, plus any
    real URL the model invented. Leaves the sentence's punctuation intact."""
    return _LINK_PLACEHOLDER.sub(" ", line).strip().strip("-–—:").strip()


# Naming the link is the failure mode this line falls into most often — "page
# has your full rundown", "everything's on your dashboard". It turns a text from
# a friend into a product notification, and the prompt alone does not stop it,
# so the rule is enforced here with one redraft.
_NAMES_THE_LINK = re.compile(
    r"\b(link|page|dashboard|site|website|click|tap here)\b", re.IGNORECASE)


# How long the morning line may be. The link has to survive alongside it in one
# message, and the detail still lives on the page — this carries the basics
# (weather, commute, opening), not the topics and headlines, which is why it
# is longer than a single teaser sentence but well short of the old full
# text briefing.
MORNING_LINE_MAX = 420


def generate_morning_line(phone: str, payload: dict) -> str:
    """The short text that rides with the morning link.

    Every user gets the same shape: today's weather, the commute if they have
    an address on file, and 1-2 things newly open or worth catching nearby
    this week — then the link. Anything else they track (a price move, a
    headline) is an optional bonus on top when it's genuinely notable, never
    a substitute for those three — the page is where the rest of what they
    asked to track lives.

    Goes through `_build_system` like every other user-facing message, so it
    carries the user's calibration rather than a second, breezier Palmer."""
    profile = get_profile(phone)
    system = _build_system(phone, include_recent=True)
    today = _local_today(profile.get("timezone")).strftime("%B %d, %Y")
    digest = _payload_digest(payload)
    recent = _recent_assistant_texts(phone, n=4)
    recent_block = ("\n\nThe last few things you sent them (do not reuse an opener "
                    "or phrasing from these):\n" + "\n---\n".join(recent)) if recent else ""

    required = []
    if payload.get("weather"):
        required.append("today's weather")
    if (payload.get("traffic") or {}).get("live_min"):
        required.append("the commute")
    if payload.get("opening"):
        required.append("1-2 things newly open or worth catching near them this week, named specifically")
    required_block = (
        "REQUIRED — the data below has these, so every one of them must appear: "
        + "; ".join(required) + "."
    ) if required else "Nothing structured is loaded for them yet — a plain greeting is correct."

    prompt = f"""Today is {today}. Write the short text that goes out with the link to their page this morning.

What is on their page right now:
{digest or "(nothing loaded yet)"}{recent_block}

This is not the full briefing — the briefing is the page, they are about to tap it and see everything laid out, including whatever else they've asked Palmer to track. But this text is not a single teaser line either: it carries the basics itself, in Palmer's voice, and the link follows it.

{required_block}

Rules:
- Cover every REQUIRED item above, in your own words, with real specifics — the actual temperature, the actual commute time, the actual name of the place or event. Never gesture at a category ("some stuff opened nearby") instead of naming it.
- Weave the required items into two or three short sentences that read like a friend talking, not a bulleted list and not separate labeled lines.
- Beyond the required items, you may add ONE more sentence about something else on the page (a price move, a headline) ONLY if it is genuinely notable — a real change, not routine. Skip it rather than padding; this text is basics plus link, not the briefing.
- Under {MORNING_LINE_MAX} characters total. Tight and scannable, not a wall of text.
- Never say the word "link", "page", "dashboard", "site", or "click" - the link speaks for itself and naming it makes this sound like a product notification.
- Write ONLY the text. The link is attached automatically after it. Do not write a URL, and do not leave a placeholder like [link] or (url) where you think one goes - anything like that ships to them as literal text.
- Do not end with a question. The page is the ask.
- Use the numbers from the data verbatim.
- If the drive-time line names a leave time, that number is for THAT departure — say so ("your 8:30 drive is 34 min today"), never as if it were traffic right now. If it says "right now", it is live and you don't know when they leave, so don't invent a time.
- If the weather data says the forecasts disagree, do NOT pick one and state it. Give the range or say "around", the way a person hedges out loud — "upper 90s to maybe 110", "somewhere around 100". Stating a precise high nobody can stand behind is how this went wrong before.
- If you mention the weather, name the city exactly as the data writes it, and never pair a number with any other place. Their profile may call where they live something broader or narrower than the forecast does - the data wins. If the two disagree, the data is the one that was actually measured.
- Palmer's voice. Plain ASCII, no emoji, no markdown, no bullets, no sign-off."""

    def _draft(correction: str = "") -> str:
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=120,
            system=system,
            messages=[{"role": "user", "content": prompt + correction}],
        )
        line = _strip_link_placeholder(_sms_clean(response.content[0].text.strip()))
        line = " ".join(line.split())
        if len(line) > MORNING_LINE_MAX:
            line = line[:MORNING_LINE_MAX].rsplit(" ", 1)[0].rstrip(" ,;:-")
        return line

    line = _draft()
    if _NAMES_THE_LINK.search(line):
        retry = _draft(
            "\n\nYou just wrote: " + repr(line) + "\nThat names the link, which is "
            "the one thing you cannot do - it reads like a push notification "
            "instead of a friend. Write it again with no reference to a link, "
            "page, site, or dashboard at all. Just the observation, or just the "
            "greeting if there is nothing worth flagging."
        )
        # Keep the retry only if it actually fixed the problem — a second
        # violation means take the better-formed of the two, not a worse one.
        if retry and not _NAMES_THE_LINK.search(retry):
            line = retry
    # The prompt already says not to reuse an opener and the model does it
    # anyway: three consecutive mornings opened "103 today in Woodland Hills",
    # "106 in Woodland Hills today", "111 today in Woodland Hills". Token
    # overlap cannot see it — those score 0.23 against each other because the
    # numbers and trailing clauses differ — so this compares the SHAPE of the
    # opening instead. Suppressing the message would be wrong: they asked for a
    # daily briefing. Only the phrasing has to move.
    from guards import repeats_opening
    if recent and repeats_opening(line, recent):
        again = _draft(
            "\n\nYou just wrote: " + repr(line) + "\nThat opens the same way as a "
            "recent morning — same order, same first beat. Say the same facts "
            "starting somewhere else: lead with the thing that changed, or the "
            "event, or the commute, rather than the temperature. Keep every "
            "number identical."
        )
        if again and not _NAMES_THE_LINK.search(again) and not repeats_opening(again, recent):
            line = again

    if len(line) < 8:
        raise ValueError(f"generate_morning_line produced nothing usable: {repr(line)}")
    _reject_meta_commentary(line)
    return line


def _compose_morning(phone: str) -> tuple[str, bool]:
    """The morning message: weather, commute if they have one, 1-2 opening
    highlights, then the link to their page.

    The full briefing used to be the message and the link followed as a second
    text. It is one message now because the page IS the briefing — sending both
    meant saying everything twice and burning two segments to do it. The text
    still carries the basics itself rather than being a bare teaser, so a user
    who never taps the link still gets the three things everyone gets every
    day; anything beyond that (their tracked topics, prices, headlines) lives
    on the page only.

    The URL goes last and alone at the end of the message. Link previews only
    render when a message carries exactly one URL at a boundary, and that
    preview is most of the value: it is what turns a bare link into something
    worth tapping.

    Falls back to the full text briefing if the page can't be built or the line
    can't be drafted — a user is never left holding a link to nothing. Returns
    (message, carries_link); the caller needs the flag because the /sms-status
    shorten-and-retry would happily truncate a URL into garbage."""
    from home import ensure_fresh, load, home_token
    url = ensure_fresh(phone)
    if not url.startswith("http"):
        print(f"_compose_morning: APP_URL not configured for {phone}, sending text briefing")
        return generate_morning(phone), False
    payload = load(home_token(phone)) or {}
    if not _payload_digest(payload):
        print(f"_compose_morning: page is empty for {phone}, sending text briefing")
        return generate_morning(phone), False
    try:
        line = generate_morning_line(phone, payload)
    except Exception as e:
        print(f"generate_morning_line failed for {phone}: {type(e).__name__}: {e}")
        return generate_morning(phone), False
    return f"{line} {url}", True


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
                message, carries_link = _compose_morning(phone)
                # A link message opts out of the status callback: the
                # shorten-and-retry path there would cut the URL in half.
                if send_sms(phone, message, add_status_callback=not carries_link):
                    save_message(phone, "assistant", message, kind="morning")
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
                save_message(phone, "assistant", message, kind="city_ask")
                print(f"City ask sent to {phone}: {message[:80]}")
            else:
                print(f"City ask send rejected by Twilio for {phone}")
        except Exception as e:
            print(f"City ask failed for {phone}: {e}")

    if dry_run:
        print(f"[dry-run] {matched} user(s) matched")
