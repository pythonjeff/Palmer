"""What Palmer knows about a person, and what he has already said to them.

Profile extraction/consolidation plus the two cross-send dedup gates that
stop unprompted messages repeating a subject.
"""
import json

from db import (
    get_profile, upsert_profile, get_message_count, get_older_messages, HISTORY_LIMIT,
)
from llm import client, HAIKU_MODEL, _parse_json
from prompts import EXTRACT_PROMPT, CONSOLIDATE_PROMPT


# Canonical profile schema. Everything reads these keys; aliases are normalized on write.
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
    """Map any alias keys to their canonical names and null out the aliases."""
    result = {}
    for k, v in updates.items():
        canonical = _PROFILE_ALIASES.get(k, k)
        if canonical not in result:
            result[canonical] = v
        if k != canonical:
            result[k] = None  # null the alias so it doesn't persist
    return result

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

def _apply_profile_updates(phone: str, profile: dict, updates: dict) -> dict:
    """Merge canonical profile updates and derive timezone when city is set."""
    if not updates:
        return profile
    updates = _canonical_updates(updates)
    new_city = updates.get("city")
    if new_city and not profile.get("timezone") and "timezone" not in updates:
        tz = _derive_timezone(new_city)
        if tz:
            updates["timezone"] = tz
    upsert_profile(phone, updates)
    return get_profile(phone)

def _consolidate_history(phone: str):
    """Fold messages beyond the live window into long-term profile fields."""
    if get_message_count(phone) < HISTORY_LIMIT * 2:
        return
    older = get_older_messages(phone, skip_recent=HISTORY_LIMIT)
    if len(older) < 10:
        return
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

def _is_duplicate_subject(phone: str, new_text: str, window_hours: float = 6) -> bool:
    """True if new_text covers the same subject as something already sent to this
    phone in the last window_hours — catches cross-job topical redundancy (e.g. a
    watch alert and a followup both about the same story within the same afternoon)
    that per-job cooldowns can't see, since each job only checks its own history.
    Fails open (False) on any error so a broken check never blocks a real send."""
    from datetime import datetime, timezone, timedelta
    from db import get_recent_assistant_messages

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
