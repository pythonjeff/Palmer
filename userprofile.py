"""What Palmer knows about a person, and what he has already said to them.

Profile extraction/consolidation plus the two cross-send dedup gates that
stop unprompted messages repeating a subject.
"""
import json
import re

from db import (
    get_profile, upsert_profile, get_message_count, get_older_messages, HISTORY_LIMIT,
)
from llm import client, HAIKU_MODEL, _parse_json
from prompts import EXTRACT_PROMPT, CONSOLIDATE_PROMPT


# Canonical profile schema. Everything reads these keys; aliases are normalized on write.
# Every key a profile is allowed to hold. The extractor is a language model and
# will happily invent a new key every turn if nothing stops it — one profile had
# accumulated 624 keys, 604 of them one-offs like "monday_night_behavior",
# "kendrick_fan" and "alternatively". The whole profile is dumped as JSON into
# every system prompt, so that was ~21,700 tokens of noise per turn, roughly
# double SYSTEM_PROMPT and the tool schemas combined, burying the 20 keys that
# mattered. Anything new goes here deliberately, or it doesn't persist.
PROFILE_FIELDS = frozenset({
    # who they are — the extraction schema in prompts.EXTRACT_PROMPT
    "name", "city", "timezone", "job", "interests", "sports_teams", "brands",
    "relationships", "life_context", "life_summary", "vibe", "stressed_about",
    "follow_up", "ongoing_threads", "communication_style", "commute",
    # briefing / scheduling config
    "morning_topics", "morning_time", "morning_enabled", "morning_onboarded",
    "morning_prefs", "morning_sent_date", "interest_genres", "home_token",
    "shows", "followed_teams", "weather_locations",
    # bookkeeping the jobs and handlers read
    "intro_sent", "conversation_topics", "reactions", "reactions_folded_count",
    "pending_morning_suggestion", "pending_preference_notice",
    "alert_sent_date", "followup_sent_date", "city_ask_sent_date",
    "onboarding_ask_sent",
    # Which thread the last check-in was about, so the next one moves on, and
    # the message count at the last consolidation, so it does not re-run every
    # turn. Both bookkeeping, NOT extraction fields — deliberately absent from
    # EXTRACT_PROMPT's schema so Haiku never writes them.
    "followup_last_thread", "consolidated_at_count",
    "field_dates",
})

# Volatile facts, and how many days they stay true by default.
#
# The whole profile is dumped into every system prompt as CURRENT fact, and
# nothing in it ever expired. One user's profile said `city: "Culver City"` and,
# three lines below, `life_context: "Based in LA"` — both true when written,
# and together the exact contradiction behind the LA-temperature complaints.
# Another still carried `stressed_about: "active fire emergency in LA area"`
# weeks after the fire, and a flight watch for a trip that ends in September.
#
# These are not wrong, they are OLD, and the prompt had no way to say so. Each
# write stamps `field_dates`; _build_system renders the age beside the value and
# drops it once past its life. Durable facts — name, city, job, relationships,
# communication_style — are deliberately absent: those do not rot.
VOLATILE_FIELDS = {
    "stressed_about": 21,
    "follow_up": 21,
    "ongoing_threads": 30,
    "life_context": 60,
    "pending_morning_suggestion": 7,
    "pending_preference_notice": 7,
}


_PROFILE_ALIASES = {
    "location": "city",
    "favorite_teams": "sports_teams",
    "teams": "sports_teams",
    "sports": "sports_teams",
    "tracked_brands": "brands",
    "shopping_interests": "brands",
    "fashion_taste": "brands",
}

def _canonical_updates(updates: dict) -> dict:
    """Map alias keys to canonical names, null the aliases, and drop anything
    outside PROFILE_FIELDS so the schema can't drift one turn at a time."""
    result = {}
    dropped = []
    for k, v in updates.items():
        canonical = _PROFILE_ALIASES.get(k, k)
        if canonical not in PROFILE_FIELDS:
            dropped.append(k)
            continue
        if canonical not in result:
            result[canonical] = v
        if k != canonical:
            result[k] = None  # null the alias so it doesn't persist
    if dropped:
        print(f"profile: dropped {len(dropped)} non-schema field(s): {dropped[:8]}")
    return result


def prune_profile(profile: dict) -> tuple[dict, list[str]]:
    """Profile reduced to PROFILE_FIELDS. Returns (kept, dropped_key_names).

    Used to clean rows that accumulated invented keys before the allow-list
    existed. Pure — the caller decides whether to write it back."""
    kept, dropped = {}, []
    for k, v in (profile or {}).items():
        if k in PROFILE_FIELDS:
            kept[k] = v
        else:
            dropped.append(k)
    return kept, dropped

def _normalize_profile(phone: str, profile: dict) -> dict:
    """Migrate alias keys in an existing profile to canonical form. Idempotent."""
    migrations = {}
    for alias, canonical in _PROFILE_ALIASES.items():
        val = profile.get(alias)
        if val is not None:
            if not profile.get(canonical):
                migrations[canonical] = val
            migrations[alias] = None
    city = migrations.get("city") or profile.get("city")
    if city and not profile.get("timezone"):
        tz = _derive_timezone(city)
        if tz:
            migrations["timezone"] = tz
    if migrations:
        upsert_profile(phone, migrations)
        return {**profile, **migrations}
    return profile

def _all_interests(profile: dict) -> list[str]:
    """Collect all interest signals — morning_topics, sports_teams, interests — deduplicated."""
    seen_lower: set[str] = set()
    result = []
    for key in ["morning_topics", "sports_teams", "interests"]:
        val = profile.get(key)
        if not val:
            continue
        items = val if isinstance(val, list) else [str(val)]
        for item in items:
            if item.lower() not in seen_lower:
                seen_lower.add(item.lower())
                result.append(item)
    return result

def _derive_timezone(city: str) -> str | None:
    """Return an IANA timezone string for a city, or None if it can't be determined."""
    try:
        from zoneinfo import ZoneInfo
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=30,
            messages=[{"role": "user", "content": f"What is the IANA timezone identifier for {city}? Reply with only the identifier, e.g. America/Chicago"}],
        )
        tz = response.content[0].text.strip()
        ZoneInfo(tz)  # raises if invalid
        return tz
    except Exception:
        return None

def _track_conversation_topic(phone: str, user_msg: str, reply: str, profile: dict):
    """Track what topic this exchange was about; flag it for morning suggestion if recurring."""
    if profile.get("pending_morning_suggestion"):
        return  # already have a pending suggestion — don't pile on
    morning_topics = profile.get("morning_topics") or []
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=15,
            messages=[{"role": "user", "content": f"""What topic did this text exchange touch on? Two words or fewer. If it's small talk, a reminder set, or nothing topical, say NONE.

User: {user_msg[:200]}
Reply: {reply[:200]}

Topic (2 words max, or NONE):"""}],
        )
        topic = response.content[0].text.strip()
        if not topic or topic.upper() == "NONE" or len(topic) > 30:
            return
        topic_low = topic.lower()
        # Skip if already tracked in morning topics
        if any(topic_low in t.lower() or t.lower() in topic_low for t in morning_topics):
            return
        # Update rolling topic history (last 20 exchanges)
        recent = list(profile.get("conversation_topics") or [])
        recent.append(topic_low)
        recent = recent[-20:]
        count = sum(1 for t in recent if topic_low in t or t in topic_low)
        updates: dict = {"conversation_topics": recent}
        if count >= 3:
            updates["pending_morning_suggestion"] = topic
        upsert_profile(phone, updates)
    except Exception:
        pass

def _eager_build_home(phone: str) -> None:
    """The first time a user's city becomes known, build their Palmer Home page
    right away rather than waiting for get_my_page or the next morning send.

    This never sends the link — get_my_page and the morning job are still the
    only paths that hand it to the user, and SYSTEM_PROMPT's rule against
    volunteering URLs still applies. It just means the page is already sitting
    there, populated, whenever one of those paths does run — instead of a user
    asking for it on day one and hitting home.ensure_fresh's cold-build path
    live inside that reply. Only fires once (see the `not old_city` gate in the
    caller): later city corrections ride the normal refresh/invalidate path,
    not another full rebuild. Never raises."""
    import os
    if not os.environ.get("APP_URL"):
        return  # nowhere to serve the page; don't spend on a link nobody can open
    try:
        from home import rebuild, load, home_token
        token = home_token(phone)
        if load(token) is not None:
            return
        rebuild(phone, refresh_news=True)
    except Exception as e:
        print(f"userprofile: eager home build failed for {phone}: {type(e).__name__}: {e}")


def _stamp_volatile(profile: dict, updates: dict) -> None:
    """Record the date each volatile field was last asserted.

    Merged into `updates` so it rides the same write — a separate upsert would
    open a window where the value is new and its date is not."""
    from timeutil import local_today
    touched = [k for k in updates if k in VOLATILE_FIELDS and updates[k] is not None]
    if not touched:
        return
    dates = dict((profile or {}).get("field_dates") or {})
    today = local_today((profile or {}).get("timezone")).isoformat()
    for k in touched:
        dates[k] = today
    updates["field_dates"] = dates


def fresh_profile_for_prompt(profile: dict, today=None) -> dict:
    """The profile as the model should see it: stale volatile facts dropped,
    surviving ones labelled with when they were last true.

    Read-side only. Nothing is deleted from storage — a fact that has gone quiet
    is not a fact that was wrong, and the consolidator may reassert it tomorrow.
    """
    from datetime import date
    if not profile:
        return profile
    dates = profile.get("field_dates") or {}
    today = today or date.today()
    out = {}
    for k, v in profile.items():
        if k == "field_dates" or v is None:
            continue
        life = VOLATILE_FIELDS.get(k)
        if life is None:
            out[k] = v
            continue
        stamped = dates.get(k)
        if not stamped:
            out[k] = v          # written before stamping existed; leave it be
            continue
        try:
            age = (today - date.fromisoformat(stamped)).days
        except ValueError:
            out[k] = v
            continue
        if age > life:
            continue            # past its life; the model stops seeing it
        out[k] = {"value": v, "as_of": stamped, "days_old": age} if age >= 3 else v
    return out


def _apply_profile_updates(phone: str, profile: dict, updates: dict) -> dict:
    """Merge canonical profile updates, and keep `timezone` honest.

    `timezone` is the field every local_now/local_today call in the codebase
    depends on, and an unresolvable value degrades all of them to UTC silently
    and for good. Two things could put one there, and both are handled here."""
    if not updates:
        return profile
    updates = _canonical_updates(updates)
    from timeutil import valid_zone

    # 1. The extractor. `timezone` is named in EXTRACT_PROMPT's schema, so Haiku
    #    can write any string it likes — "Pacific Time", "PST", a guess. Only
    #    _derive_timezone validated its own output; nothing validated this.
    if "timezone" in updates and updates["timezone"] is not None:
        if not valid_zone(updates["timezone"]):
            print(f"profile: dropping unresolvable timezone {updates['timezone']!r} for {phone!r}")
            updates.pop("timezone")

    new_city = updates.get("city")
    old_city = profile.get("city")
    if new_city and old_city and new_city != old_city:
        print(f"profile: city changing for {phone!r}: {old_city!r} -> {new_city!r}")

    # 2. A move. The timezone was derived only when ABSENT, so someone who moved
    #    from Chicago to Los Angeles kept America/Chicago forever, with no tool,
    #    no repair job and no way to correct it — their morning arrived two hours
    #    early from then on. Re-derive when the city actually changes.
    #
    #    This is safe against the rule that correcting a forecast must not move
    #    the hour the morning arrives: the weather-topic city write in
    #    update_morning_briefing's dispatch calls upsert_profile DIRECTLY and
    #    never reaches this function. See test_weather_city.py.
    needs_tz = new_city and "timezone" not in updates and (
        not profile.get("timezone") or (old_city and new_city != old_city))
    if needs_tz:
        tz = _derive_timezone(new_city)
        if tz:
            if profile.get("timezone") and tz != profile.get("timezone"):
                print(f"profile: timezone re-derived for {phone!r}: "
                      f"{profile['timezone']!r} -> {tz!r} (city moved)")
            updates["timezone"] = tz
    _stamp_volatile(profile, updates)
    upsert_profile(phone, updates)
    if new_city and not old_city:
        _eager_build_home(phone)
    return get_profile(phone)

# How many new messages must accumulate before consolidating again. Without a
# gate this ran on EVERY turn past 40 messages, re-summarising a near-identical
# 80-message window each time — one Haiku call per turn, forever, for a profile
# that had barely moved.
CONSOLIDATE_EVERY = 20


def _consolidate_history(phone: str):
    """Fold messages beyond the live window into long-term profile fields."""
    count = get_message_count(phone)
    if count < HISTORY_LIMIT * 2:
        return
    # Only re-consolidate once the conversation has actually moved on. The
    # window slides by one message per turn, so without this the same content
    # was summarised over and over at full price.
    profile = get_profile(phone)
    last_at = profile.get("consolidated_at_count") or 0
    if count - last_at < CONSOLIDATE_EVERY:
        return
    older = get_older_messages(phone, skip_recent=HISTORY_LIMIT)
    if len(older) < 10:
        return
    upsert_profile(phone, {"consolidated_at_count": count})
    profile = get_profile(phone)
    profile = _normalize_profile(phone, profile)
    transcript = "\n".join(
        f"{m['role']}: {m['content'][:300]}" for m in older[-80:]
    )
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": CONSOLIDATE_PROMPT.format(
                profile=json.dumps(profile, indent=2) if profile else "none yet",
                messages=transcript,
            )}],
        )
        updates = _parse_json(response.content[0].text)
        if updates:
            _apply_profile_updates(phone, profile, updates)
    except Exception:
        pass

def _update_profile(phone: str, user_msg: str, reply: str):
    profile = get_profile(phone)
    profile = _normalize_profile(phone, profile)  # migrate aliases and derive timezone for existing users
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": EXTRACT_PROMPT.format(
                user_msg=user_msg,
                reply=reply,
                profile=json.dumps(profile, indent=2) if profile else "none yet",
            )}],
        )
        updates = _parse_json(response.content[0].text)
        if updates:
            profile = _apply_profile_updates(phone, profile, updates)
    except Exception:
        pass
    # _build_system shows the ONBOARDING ASK block under this exact condition —
    # intro already sent, name/city still missing, not asked yet — so this run
    # sees the same profile state that reply was drafted against (the per-phone
    # lock in main.py serializes turns, so nothing else writes in between). Mark
    # it consumed once regardless of whether they answered, so Palmer asks on
    # their second message and then drops it rather than nagging every turn.
    if (profile.get("intro_sent") and not profile.get("onboarding_ask_sent")
            and (not profile.get("name") or not profile.get("city"))):
        upsert_profile(phone, {"onboarding_ask_sent": True})
    _track_conversation_topic(phone, user_msg, reply, profile)

def _user_already_covered(phone: str, candidate: str, window_hours: float = 12) -> bool:
    """True if the USER already brought up the same story in their recent
    messages — 'did you see the Iran thing?' at 10am should stop a related
    watch fire at 2pm. Separate from _is_duplicate_subject, which only sees
    what Palmer sent.

    Fail-open: any error returns False so a broken Haiku call never silently
    suppresses a legitimate alert. Called after the topical significance
    gates already passed, so cost is bounded to the small set of would-be sends."""
    from datetime import datetime, timezone, timedelta
    from db import get_recent_user_messages

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    recent = get_recent_user_messages(phone, cutoff)
    if not recent:
        return False
    try:
        recent_block = "\n".join(f'- "{m}"' for m in recent[-6:])
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": f"""A texting assistant is about to send this UNPROMPTED alert:
"{candidate}"

But the user has already sent these messages in the last {window_hours} hours:
{recent_block}

Would sending this alert now be REDUNDANT — i.e. the user has already shown they know about this specific development?

YES if the user has clearly signalled they've heard about this story — even shorthand references count:
- "did you see the X thing" / "have you heard about X" / "wild what's happening with X" — when X names the alert's subject
- Discussing the same specific event (same team's same game, same asset's same move, same conflict's same escalation)
- Sharing a fact from the alert (score, price, casualty count, etc.)

NO if the user only mentioned the general topic without touching this specific development:
- "I like Middle East food" / "Cards look good this season" — general topic, not the specific event
- Background chatter that predates the news

Reply YES or NO."""}],
        )
        return response.content[0].text.strip().upper().startswith("YES")
    except Exception:
        return False

def topic_already_covered(new_topic: str, existing: list[str]) -> str | None:
    """The existing topic a new one duplicates, or None.

    Same idea as _is_duplicate_subject below, aimed at the topic list instead of
    outbound messages. The only dedup on that list was bidirectional substring
    containment, which cannot see that "Kirkwood, MO news" and "St. Louis area
    news" are the same beat — one user carried both, so both burned a slot in
    the MAX_TOPICS rotation and both could surface the identical article on the
    page.

    Runs on the ADD path only, where topics are written rarely — never on the
    read path, which runs on every page view. Fails open (None) so a broken
    check never blocks someone adding a topic."""
    if not new_topic or not existing:
        return None
    try:
        listing = "\n".join(f"- {t}" for t in existing)
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=40,
            messages=[{"role": "user", "content": f"""Someone's daily news briefing already covers these subjects:
{listing}

They want to add: "{new_topic}"

Would that new subject return essentially the same stories as one already listed?

Reply with the existing subject it duplicates, copied EXACTLY as written above and nothing else. Reply NO if it would bring something genuinely different.

Only say yes when one of them would bring almost nothing the other does not. A suburb's news IS the metro's news, so "Kirkwood, MO news" duplicates "St. Louis area news". These are NOT duplicates: "St. Louis Cardinals" vs "St. Louis area news" (a team is not its city), or "NFL headlines" vs "Philadelphia Eagles news" (the league covers 31 other teams). A wider beat that still brings its own stories is not a duplicate."""}],
        )
        answer = resp.content[0].text.strip().strip('"').rstrip(".")
        if not answer or answer.upper().startswith("NO"):
            return None
        # Match the echo back to the list rather than trusting it. Asking for a
        # NUMBER instead was tried and the model mis-indexed — it would answer
        # "2" while naming the third subject in the prose after it. An echo is
        # checkable; an index is not.
        for t in existing:
            if t.strip().lower() == answer.strip().lower():
                return t
        return None
    except Exception as e:
        print(f"topic_already_covered failed for {new_topic!r}: {type(e).__name__}: {e}")
        return None


# How far back the free lexical check looks. Much wider than the semantic
# window: a Haiku call per prior message would not be affordable over three
# days, and word overlap is.
VERBATIM_WINDOW_HOURS = 72


def _is_duplicate_subject(phone: str, new_text: str, window_hours: float = 6) -> bool:
    """True if new_text covers the same subject as something already sent to this
    phone in the last window_hours — catches cross-job topical redundancy (e.g. a
    watch alert and a followup both about the same story within the same afternoon)
    that per-job cooldowns can't see, since each job only checks its own history.
    Fails open (False) on any error so a broken check never blocks a real send."""
    from datetime import datetime, timezone, timedelta
    from db import get_recent_assistant_messages

    # A verbatim repeat needs no model and no six-hour window. One user got the
    # identical followup twice — "yo how'd practice look today? hurts moving
    # like they said?" — because the followup job runs every four hours, its
    # subject stayed live for days, and this check only ever looked back six.
    # The lexical pass is free, so it looks back much further.
    from guards import near_duplicate
    wide_cutoff = (datetime.now(timezone.utc)
                   - timedelta(hours=max(window_hours, VERBATIM_WINDOW_HOURS))).isoformat()
    wide = get_recent_assistant_messages(phone, wide_cutoff)
    prior = near_duplicate(new_text, wide)
    if prior:
        print(f"duplicate suppressed (verbatim): {new_text[:70]!r}")
        return True

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    recent = get_recent_assistant_messages(phone, cutoff)
    if not recent:
        return False
    try:
        recent_block = "\n".join(f'- "{m}"' for m in recent[-5:])
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": f"""A texting assistant is about to send this message:
"{new_text}"

It already sent these messages to the same person in the last {window_hours} hours:
{recent_block}

Is the new message about the SAME underlying subject, story, or event as any of those — even if worded differently? Reply YES or NO."""}],
        )
        return response.content[0].text.strip().upper().startswith("YES")
    except Exception:
        return False
