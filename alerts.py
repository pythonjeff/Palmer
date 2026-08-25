import hashlib
from datetime import datetime, timezone, date as date_type

from agent import _build_system
from llm import client, HAIKU_MODEL, SONNET_MODEL, _parse_json
from smstext import _sms_clean
from datafeeds import _search_raw
from userprofile import _all_interests, _is_duplicate_subject, _user_already_covered
from db import get_all_profiles, upsert_profile, save_message, claim_daily_guard
from sources import corroborated, canonical_domain
from rubrics import classify_genre, rubric_for


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


def _resolve_interest_genres(phone: str, profile: dict, topics: list[str]) -> list[tuple[str, str]]:
    """Return [(interest, genre)] for each topic. Uses profile.interest_genres as
    a persistent cache so tomorrow's run doesn't re-classify. Persists any newly
    classified entries in a single upsert per run."""
    stored = dict(profile.get("interest_genres") or {})
    resolved: list[tuple[str, str]] = []
    newly = False
    for t in topics:
        key = (t or "").strip().lower()
        if not key:
            continue
        genre = stored.get(key)
        if not genre:
            genre = classify_genre(t)
            stored[key] = genre
            newly = True
        resolved.append((t, genre))
    if newly:
        try:
            upsert_profile(phone, {"interest_genres": stored})
        except Exception as e:
            print(f"interest_genres persist failed for {phone}: {e}")
    return resolved


def _check_significance(results: str, profile: dict,
                        interests: list[tuple[str, str]] | None = None) -> tuple[int, str]:
    """Score news significance 1-10 for this user, using per-interest genre
    rubrics so each topic is judged by 'what a friend actually texts about'
    for its own kind.

    `interests` is [(interest, genre)] pairs. When omitted (older callers /
    tests that patch this in isolation), fall back to a flat interest list
    with the 'other' rubric as a strict backstop."""
    if interests is None:
        topics = _all_interests(profile)
        interests = [(t, "other") for t in topics] if topics else []
    interest_str = ", ".join(f"{t} ({g})" for t, g in interests) if interests else "general news"

    # Show only rubrics for genres actually present in this user's interest set.
    present_genres: list[str] = []
    seen: set[str] = set()
    for _, g in interests:
        if g not in seen:
            seen.add(g)
            present_genres.append(g)
    rubric_block = "\n\n".join(
        f"== Rubric for {g} ==\n{rubric_for(g)}" for g in present_genres
    ) if present_genres else rubric_for("other")

    # "Today" has to be the USER's local calendar day — otherwise a west-coast
    # user's late-day-local news reads as "not from today" to Haiku (server UTC
    # already rolled over) and gets capped under the 8-score threshold.
    from timeutil import local_today
    today = local_today(profile.get("timezone")).isoformat()

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=250,
            messages=[{"role": "user", "content": f"""Today is {today}. This person has these interests (with their type):
{interest_str}

For each type, here's what a real friend would text about vs. would not:

{rubric_block}

Search results:
{results[:2500]}

Score 1-10 how much this news clears the friend-would-text bar for any of the user's interests today:
- 9-10: A friend would 100% text this — clears the rubric strongly, published today, high-signal
- 8: A friend probably would text this — clears the rubric, published today
- 5-7: Interesting but doesn't quite clear the rubric OR not from today — do NOT send
- 1-4: Routine, opinion, recap, or nothing relevant — do NOT send

RECENCY: score 8+ requires the news broke TODAY (within the past few hours). If published date is missing or not today, cap at 4.

Reply with JSON only: {{"score": N, "summary": "one sentence of what happened and when", "interest": "which of the user's interests this attaches to"}}
If score < 8 set summary and interest to ""."""}],
        )
        data = _parse_json(response.content[0].text)
        # Haiku sometimes returns a list — one scored entry per interest — even
        # though the prompt asks for a single object. Collapse to the highest-
        # scoring entry so the caller gets the strongest hit.
        if isinstance(data, list):
            best = max(
                (d for d in data if isinstance(d, dict)),
                key=lambda d: int(d.get("score") or 0),
                default=None,
            )
            data = best
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

    for phone, profile in get_all_profiles():

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
                domains = {canonical_domain(r.get("url", "")) for r in all_raw}
                domains.discard("")
                print(f"No alert for {phone}: no corroboration ({len(domains)} domain(s))")
                continue

            combined = "\n\n".join(
                f"{r.get('title','')}\nPublished: {r.get('published_date','unknown')}\n{r.get('content','')[:400]}"
                for r in all_raw
            )
            interests = _resolve_interest_genres(phone, profile, _all_interests(profile))
            score, summary = _check_significance(combined, profile, interests)

            if score >= 8 and summary:
                message = _draft_alert(phone, summary)
                if _is_duplicate_subject(phone, message):
                    upsert_profile(phone, {"alert_sent_date": None})
                    print(f"No alert for {phone}: subject already covered by a recent message")
                    continue
                # User-mention dedup — they already brought this up themselves.
                if _user_already_covered(phone, message):
                    upsert_profile(phone, {"alert_sent_date": None})
                    print(f"No alert for {phone}: user already mentioned this story themselves")
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
