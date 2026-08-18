import hashlib
import os
from datetime import datetime, timezone, date as date_type

from agent import (
    client, _build_system, _sms_clean, _all_interests, _search_raw,
    _parse_json, _is_duplicate_subject, HAIKU_MODEL, SONNET_MODEL,
)
from db import get_all_phones, get_profile, upsert_profile, save_message, claim_daily_guard
from watches import corroborated, _canonical_domain


def _daily_alert_hour(phone: str) -> int:
    """Deterministic 'random' UTC hour (13-21) for today's alert window. Different every day per user."""
    key = f"{phone}{date_type.today().isoformat()}"
    h = int(hashlib.md5(key.encode()).hexdigest(), 16)
    return 13 + (h % 9)  # 1pm-9pm UTC = roughly 8am-4pm Central


def _in_alert_window(phone: str, profile: dict) -> bool:
    tz_name = profile.get("timezone")
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            local_hour = datetime.now(ZoneInfo(tz_name)).hour
            return 13 <= local_hour <= 21  # 1pm–9pm local time
        except Exception:
            pass
    return datetime.now(timezone.utc).hour == _daily_alert_hour(phone)


def _get_alert_queries(profile: dict) -> list[str]:
    topics = _all_interests(profile)
    if not topics:
        return []

    city = profile.get("city") or ""
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=150,
            messages=[{"role": "user", "content": f"""Generate search queries to find BREAKING or MAJOR news for someone interested in: {", ".join(topics)}
City: {city or "unknown"}

Focus on significant, unexpected, or major developments — not routine updates.
Return a JSON array of 2-3 queries. Just the array."""}],
        )
        parsed = _parse_json(response.content[0].text)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass
    return []


def _check_significance(results: str, profile: dict) -> tuple[int, str]:
    """Score news significance 1-10 for this user. Returns (score, summary)."""
    topics = _all_interests(profile)
    interest_str = ", ".join(topics) if topics else "general news"
    today = date_type.today().isoformat()

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": f"""Today is {today}. Is there BREAKING news here — published TODAY — that someone interested in [{interest_str}] would want to know about RIGHT NOW, unprompted?

Search results:
{results[:2500]}

RECENCY IS REQUIRED. A score of 8+ is only valid if the news broke TODAY (within the past few hours).
If the published date is missing or not from today, cap the score at 4 regardless of how significant the story is.
Old news that happened days or weeks ago must score 1-4 even if it's major.

Score 1-10:
- 9-10: Massive breaking news from TODAY (blockbuster trade, major emergency, record broken, huge upset)
- 8: Significant and timely, published TODAY (notable signing, major market move, major local incident)
- 5-7: Interesting but not from today, or not urgent — do NOT send
- 1-4: Old news, routine, or nothing relevant — do NOT send

Reply with JSON only: {{"score": N, "summary": "one sentence of what happened and when"}}
If score < 8 set summary to ""."""}],
        )
        data = _parse_json(response.content[0].text)
        if isinstance(data, dict):
            return int(data.get("score", 0)), data.get("summary", "")
    except Exception:
        pass
    return 0, ""


def _draft_alert(phone: str, summary: str) -> str:
    """Write the alert in Palmer's voice.

    Fallback path never sends the raw Haiku summary — that's factual prose, not
    Palmer voice, and users noticed the tone shift. Instead: prefix a soft
    "heads up" so it still reads like Palmer even when the drafting call fails."""
    system = _build_system(phone, include_recent=True)
    try:
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=150,
            system=system,
            messages=[{"role": "user", "content": (
                "Send a short unprompted text about this breaking news. Palmer's voice — "
                "like you just saw it and immediately thought of them. No opener, no ceremony. "
                "Lead with what happened and why it matters to them specifically. "
                "Vary the framing: sometimes a quick observation, sometimes just the fact with a sharp aside, "
                "sometimes a question if the stakes are genuinely unclear. Don't always frame it the same way. "
                "Write the actual text only — do NOT call tools, do NOT emit tool syntax, "
                "and do NOT invent facts beyond what's in the news line below.\n\n"
                f"News: {summary}"
            )}],
        )
        return _sms_clean(response.content[0].text.strip())
    except Exception:
        return _sms_clean(f"quick one — {summary}")


def run_alert_checks():
    from sms_util import send_sms
    today = date_type.today().isoformat()

    for phone in get_all_phones():
        profile = get_profile(phone)

        if not profile.get("morning_onboarded"):
            continue
        if profile.get("alert_sent_date") == today:
            continue
        if not _in_alert_window(phone, profile):
            continue
        if not claim_daily_guard(phone, "alert_sent_date", today):
            continue

        queries = _get_alert_queries(profile)
        if not queries:
            upsert_profile(phone, {"alert_sent_date": None})
            continue

        try:
            # Collect raw Tavily hits across every query so we can source-gate
            # before spending Haiku on significance scoring. Same corroboration
            # rules as watches.py — an unprompted daily push deserves at least
            # the same quality bar as a user-created watch.
            all_raw: list[dict] = []
            seen_urls: set[str] = set()
            for q in queries:
                for r in _search_raw(q, days=1, max_age_hours=12):
                    url = r.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        all_raw.append(r)

            if not corroborated(all_raw):
                upsert_profile(phone, {"alert_sent_date": None})
                domains = {_canonical_domain(r.get("url", "")) for r in all_raw}
                domains.discard("")
                print(f"No alert for {phone}: no corroboration ({len(domains)} domain(s))")
                continue

            combined = "\n\n".join(
                f"{r.get('title','')}\nPublished: {r.get('published_date','unknown')}\n{r.get('content','')[:400]}"
                for r in all_raw
            )
            score, summary = _check_significance(combined, profile)

            if score >= 8 and summary:
                message = _draft_alert(phone, summary)
                if _is_duplicate_subject(phone, message):
                    upsert_profile(phone, {"alert_sent_date": None})
                    print(f"No alert for {phone}: subject already covered by a recent message")
                    continue
                send_sms(phone, message)
                save_message(phone, "assistant", message)
                print(f"Alert sent to {phone} (score={score}): {message}")
            else:
                upsert_profile(phone, {"alert_sent_date": None})
                print(f"No alert for {phone} (score={score})")
        except Exception as e:
            upsert_profile(phone, {"alert_sent_date": None})
            print(f"Alert check failed for {phone}: {e}")
