import hashlib
import json
import os
from datetime import datetime, timezone, date as date_type

from agent import client, _search, _build_system
from db import get_all_phones, get_profile, upsert_profile


def _daily_alert_hour(phone: str) -> int:
    """Deterministic 'random' UTC hour (13-21) for today's alert window. Different every day per user."""
    key = f"{phone}{date_type.today().isoformat()}"
    h = int(hashlib.md5(key.encode()).hexdigest(), 16)
    return 13 + (h % 9)  # 1pm-9pm UTC = roughly 8am-4pm Central


def _in_alert_window(phone: str) -> bool:
    return datetime.now(timezone.utc).hour == _daily_alert_hour(phone)


def _get_alert_queries(profile: dict) -> list[str]:
    topics = list(profile.get("morning_topics") or [])
    for key in ["sports_teams", "favorite_teams", "interests"]:
        val = profile.get(key)
        if val:
            topics.append(str(val))
    if not topics:
        return []

    city = profile.get("city") or profile.get("location") or ""
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": f"""Generate search queries to find BREAKING or MAJOR news for someone interested in: {", ".join(topics)}
City: {city or "unknown"}

Focus on significant, unexpected, or major developments — not routine updates.
Return a JSON array of 2-3 queries. Just the array."""}],
        )
        text = response.content[0].text.strip()
        start, end = text.find("["), text.rfind("]") + 1
        if start != -1 and end > start:
            return json.loads(text[start:end])
    except Exception:
        pass
    return []


def _check_significance(results: str, profile: dict) -> tuple[int, str]:
    """Score news significance 1-10 for this user. Returns (score, summary)."""
    topics = profile.get("morning_topics") or []
    interest_str = ", ".join(topics) if topics else "general news"

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": f"""Is there breaking or major news here that someone interested in [{interest_str}] would want to know about RIGHT NOW, unprompted?

Search results:
{results[:2500]}

Score 1-10:
- 9-10: Massive breaking news (blockbuster trade, major emergency, record broken, huge upset)
- 8: Significant and timely (notable signing, major market move, major local incident)
- 5-7: Interesting but not urgent — do NOT send
- 1-4: Routine or nothing relevant — do NOT send

Reply with JSON only: {{"score": N, "summary": "one sentence of what happened"}}
If score < 8 set summary to ""."""}],
        )
        text = response.content[0].text.strip()
        start, end = text.find("{"), text.rfind("}") + 1
        if start != -1 and end > start:
            data = json.loads(text[start:end])
            return int(data.get("score", 0)), data.get("summary", "")
    except Exception:
        pass
    return 0, ""


def _draft_alert(phone: str, summary: str) -> str:
    """Write the alert in Palmer's voice."""
    system = _build_system(phone)
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=150,
            system=system,
            messages=[{"role": "user", "content": f"Send a short unprompted text about this news. Palmer's voice — casual, like you just saw it and thought of them. No opener, no ceremony, just the news and why it matters.\n\nNews: {summary}"}],
        )
        return response.content[0].text.strip()
    except Exception:
        return summary


def run_alert_checks():
    from twilio.rest import Client
    twilio = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    from_number = os.environ["TWILIO_PHONE_NUMBER"]
    today = date_type.today().isoformat()

    for phone in get_all_phones():
        profile = get_profile(phone)

        if not profile.get("morning_onboarded"):
            continue
        if profile.get("alert_sent_date") == today:
            continue
        if not _in_alert_window(phone):
            continue

        queries = _get_alert_queries(profile)
        if not queries:
            continue

        try:
            results = "\n\n".join(f"{q}:\n{_search(q)}" for q in queries)
            score, summary = _check_significance(results, profile)

            if score >= 8 and summary:
                message = _draft_alert(phone, summary)
                twilio.messages.create(body=message, from_=from_number, to=phone)
                upsert_profile(phone, {"alert_sent_date": today})
                print(f"Alert sent to {phone} (score={score}): {message}")
            else:
                print(f"No alert for {phone} (score={score})")
        except Exception as e:
            print(f"Alert check failed for {phone}: {e}")
