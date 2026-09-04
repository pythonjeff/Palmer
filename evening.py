"""The evening update: what changed since this morning, and nothing else.

Palmer used to fill the hours between two mornings with unprompted texts —
a live score poller, a daily "a friend would text this" news alert, a
check-in about something in the profile. Each was rationed separately and
together they were still a drumbeat, and every one of them was Palmer
deciding on its own that a moment deserved an interruption.

This replaces all of them with one scheduled text at a time the user chose
(default 6pm local), built as a DIFF against what the morning update said:

    scores    how their team's game went, or stands, since the morning
    markets   tickers that moved more than MARKET_MOVE_MIN_PCT since the open
    news      headlines on their topics that were not on the page this morning

Three properties are load-bearing:

* **It is a diff, so it needs the morning's state.** `record_day_open` is
  called by `send_morning_messages` on a delivered send and stores, on the
  page payload, exactly what the user was told: each ticker's price, the
  headline URLs, each game's state and score. The evening compares against
  that, never against the page's last view — a page the user refreshed at
  noon is not what they were told at seven.

* **Nothing changed means nothing sent.** A quiet day consumes the daily
  guard and produces no text. That is the whole difference between this and
  the senders it replaced: an empty diff is an answer, not a gap to fill.

* **It costs nothing new.** `home.ensure_fresh` runs the page's ordinary
  refresh, whose windows (prices 5 min, scores 10 min, headlines 6h) all
  lapse between a 7am and a 6pm send anyway. The evening rides the refresh
  the page would have done for a view; there is no second news pass.

Drafted through `agent._build_system` like every other user-facing message,
with the plain lines as the fallback — the same shape `price_alert` uses.
"""
from __future__ import annotations

from datetime import date as date_type, datetime, timedelta

from db import get_profile, upsert_profile, get_all_profiles, save_message, claim_daily_guard
from llm import client, SONNET_MODEL
from smstext import _sms_clean

DEFAULT_EVENING_TIME = "18:00"
# Same catch-up shape as the morning: a missed tick or a transient failure is
# retried for this long after the target, then the day is given up.
CATCHUP_WINDOW_MINUTES = 120
# A ticker has to move this much from its morning price to earn a line. Flat
# and small on purpose — the section is "what changed", and on a quiet day the
# right answer is that nothing did.
MARKET_MOVE_MIN_PCT = 1.0
# New headlines mentioned at most. The page has the rest.
MAX_NEW_HEADLINES = 3
# Shorter than the morning's cap: this is a diff, and the link rides with it.
EVENING_LINE_MAX = 320


def _open_snapshot(payload: dict, day: date_type) -> dict:
    """What the morning told them, in the shape the evening compares against."""
    return {
        "date": day.isoformat(),
        "prices": {(p.get("label") or ""): p.get("price")
                   for p in (payload.get("prices") or []) if p.get("price") is not None},
        "headline_urls": [h.get("url") for h in (payload.get("headlines") or []) if h.get("url")],
        "scores": {
            (row.get("today") or {}).get("id"): {
                "state": (row.get("today") or {}).get("state"),
                "home": ((row.get("today") or {}).get("home") or {}).get("score"),
                "away": ((row.get("today") or {}).get("away") or {}).get("score"),
            }
            for row in (payload.get("scores") or []) if (row.get("today") or {}).get("id")
        },
    }


def record_day_open(phone: str, day: str | date_type) -> None:
    """Stamp the page with the state the morning update was drafted from.

    Called by the morning job right after a delivered send. Never raises —
    a failure here costs the evening its baseline, not the morning its text."""
    from home import home_token, load, save
    try:
        token = home_token(phone)
        payload = load(token)
        if not payload:
            return
        if isinstance(day, str):
            day = date_type.fromisoformat(day)
        payload["day_open"] = _open_snapshot(payload, day)
        save(token, payload)
    except Exception as e:
        print(f"evening.record_day_open failed for {phone}: {type(e).__name__}: {e}")


def _score_changes(payload: dict, opened: dict) -> list[str]:
    """Games that moved since the morning, from the team's side."""
    from sports import result_line
    out = []
    told = opened.get("scores") or {}
    for row in (payload.get("scores") or []):
        game = row.get("today")
        if not game or game.get("state") == "pre":
            continue          # the morning already said they play tonight
        then = told.get(game.get("id"))
        now = {"state": game.get("state"),
               "home": (game.get("home") or {}).get("score"),
               "away": (game.get("away") or {}).get("score")}
        if then and then == now:
            continue          # already final this morning, or nothing has happened
        team = {"abbrev": row.get("abbrev"), "name": row.get("team")}
        out.append(f"{row.get('team') or 'Their team'} {result_line(game, team)}")
    return out


def _market_changes(payload: dict, opened: dict) -> list[str]:
    """Tickers that moved more than the bar since their morning price."""
    out = []
    at_open = opened.get("prices") or {}
    for p in (payload.get("prices") or []):
        label = p.get("label") or ""
        price, base = p.get("price"), at_open.get(label)
        if price is None or not base:
            continue
        pct = (price - base) / base * 100.0
        if abs(pct) < MARKET_MOVE_MIN_PCT:
            continue
        shown = f"${price:,.0f}" if price >= 1000 else f"${price:,.2f}"
        out.append(f"{label} {pct:+.1f}% since this morning, now {shown}")
    return out


def _news_changes(payload: dict, opened: dict) -> list[str]:
    """Headlines on their topics that were not on the page this morning."""
    seen = set(opened.get("headline_urls") or [])
    out = []
    for h in (payload.get("headlines") or []):
        if not h.get("url") or h["url"] in seen:
            continue
        src = f" ({h['source']})" if h.get("source") else ""
        out.append(f"New on {h.get('topic') or 'news'}: {h.get('title')}{src}")
        if len(out) >= MAX_NEW_HEADLINES:
            break
    return out


def day_changes(payload: dict, today: date_type) -> list[str]:
    """Everything that changed since the morning update, as plain lines in a
    fixed order: scores, markets, news. [] when nothing did — including when
    there is no baseline for today, because a diff against nothing is not a
    diff. A user who joined at noon gets their first evening tomorrow, with a
    real morning behind it; the one exception is a game, which needs no
    baseline to have a result."""
    opened = payload.get("day_open") or {}
    if opened.get("date") != today.isoformat():
        opened = {"scores": {}}
        return _score_changes(payload, opened)
    return (_score_changes(payload, opened)
            + _market_changes(payload, opened)
            + _news_changes(payload, opened))


def generate_evening_line(phone: str, changes: list[str]) -> str:
    """The text, in Palmer's voice, from the change lines. Falls back to the
    lines themselves — they are already sentences."""
    from agent import _build_system
    from morning import _strip_link_placeholder, _NAMES_THE_LINK, _reject_meta_commentary
    plain = ". ".join(c.rstrip(".") for c in changes) + "."
    try:
        system = _build_system(phone, include_recent=True)
        listed = "\n".join(f"- {c}" for c in changes)
        prompt = f"""Write the short evening text that goes out with the link to their page.

Everything below changed since their morning update. This is the whole message — nothing else goes in it:
{listed}

Rules:
- Cover every item above, one short sentence each, in the order given. Nothing that is not on the list: no weather, no greeting, no commentary about their day, no check-in, no question.
- Use every number exactly as written. Do not round, do not reinterpret.
- Under {EVENING_LINE_MAX} characters total. Plain and scannable — this is a status update, not a conversation opener.
- Never say the word "link", "page", "dashboard", "site", or "click" — the link is attached automatically after your text and speaks for itself. Do not write a URL or leave a placeholder like [link] where you think one goes.
- Palmer's voice: direct, dry, no filler. Plain ASCII, no emoji, no markdown, no bullets, no sign-off."""

        def _draft(correction: str = "") -> str:
            resp = client.messages.create(
                model=SONNET_MODEL, max_tokens=120, system=system,
                messages=[{"role": "user", "content": prompt + correction}])
            line = _strip_link_placeholder(_sms_clean(resp.content[0].text.strip()))
            line = " ".join(line.split())
            if len(line) > EVENING_LINE_MAX:
                line = line[:EVENING_LINE_MAX].rsplit(" ", 1)[0].rstrip(" ,;:-")
            return line

        line = _draft()
        if _NAMES_THE_LINK.search(line):
            retry = _draft("\n\nYou just wrote: " + repr(line) + "\nThat names the link, "
                           "which is the one thing you cannot do. Write it again with no "
                           "reference to a link, page, site or dashboard.")
            if retry and not _NAMES_THE_LINK.search(retry):
                line = retry
        if len(line) < 8:
            return plain
        _reject_meta_commentary(line)
        return line
    except Exception as e:
        print(f"generate_evening_line failed for {phone}: {type(e).__name__}: {e}")
        return _sms_clean(plain)


def _compose_evening(phone: str) -> tuple[str | None, bool]:
    """(message, carries_link), or (None, False) when there is nothing to say.

    Unlike the morning there is no text-briefing fallback: an evening with no
    page is an evening with no baseline, and a diff against nothing is
    silence, not a second briefing."""
    from home import ensure_fresh, load, home_token
    from timeutil import local_today
    profile = get_profile(phone)
    url = ensure_fresh(phone)
    if not url.startswith("http"):
        print(f"_compose_evening: APP_URL not configured for {phone}, skipping")
        return None, False
    payload = load(home_token(phone)) or {}
    changes = day_changes(payload, local_today(profile.get("timezone")))
    if not changes:
        return None, False
    line = generate_evening_line(phone, changes)
    return f"{line} {url}", True


def _parse_evening_time(value) -> tuple[int, int]:
    """'HH:MM' -> (h, m); anything unreadable is the 6pm default. Its own
    parser rather than morning's, whose invalid-input fallback is 7am."""
    try:
        h, m = str(value).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except Exception:
        pass
    return (18, 0)


def _in_send_window(now_local: datetime, evening_time: str | None,
                    catchup_minutes: int = CATCHUP_WINDOW_MINUTES) -> bool:
    h, m = _parse_evening_time(evening_time or DEFAULT_EVENING_TIME)
    target = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
    return target <= now_local < target + timedelta(minutes=catchup_minutes)


def _wants_evening(profile: dict) -> bool:
    """Same gate as the morning plus its own switch: pausing mornings pauses
    this too (it is a diff against them), and evening_enabled=False turns
    off only this half."""
    if not profile.get("morning_onboarded"):
        return False
    if profile.get("morning_enabled") is False:
        return False
    if profile.get("evening_enabled") is False:
        return False
    return True


def send_evening_messages():
    """Called every 5 minutes by APScheduler. The evening_sent_date guard
    (keyed to the user's local date) makes extra invocations harmless.

    A quiet day CONSUMES the guard: nothing changed is the answer for today,
    and recomputing it every five minutes until midnight would be the same
    answer at a cost. Only a Twilio failure releases the claim."""
    from sms_util import send_sms
    from timeutil import local_now

    for phone, profile in get_all_profiles():
        try:
            if not _wants_evening(profile):
                continue
            tz = profile.get("timezone")
            if not tz:
                continue
            try:
                now_local = local_now(tz)
            except Exception:
                continue
            today_local = now_local.date().isoformat()
            if profile.get("evening_sent_date") == today_local:
                continue
            if not _in_send_window(now_local, profile.get("evening_time")):
                continue
            if not claim_daily_guard(phone, "evening_sent_date", today_local):
                continue
            try:
                message, carries_link = _compose_evening(phone)
                if message is None:
                    print(f"Evening for {phone}: nothing changed since this morning, not sending")
                    continue
                if send_sms(phone, message, add_status_callback=not carries_link):
                    save_message(phone, "assistant", message, kind="evening")
                    print(f"Evening sent to {phone}: {message[:100]}")
                else:
                    upsert_profile(phone, {"evening_sent_date": None})
                    print(f"Evening send rejected by Twilio for {phone} — will retry next tick")
            except Exception as e:
                upsert_profile(phone, {"evening_sent_date": None})
                print(f"Evening update failed for {phone}: {e}")
        except Exception as e:
            print(f"Evening check failed for {phone}: {e}")
