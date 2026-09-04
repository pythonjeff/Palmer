"""User-local time helpers.

Palmer's dyno runs in UTC on Heroku, but nearly everything the USER perceives
("today's weather", "did we already text them today", "is it late enough
locally to send a check-in") has to be evaluated in the user's own timezone.

Modules that already had ad-hoc UTC calls (agent.py's weather day resolver,
alerts.py's 'news broke today' gate) now use these helpers instead — keeping
one source of truth for what "today" means for a given profile.
"""
from __future__ import annotations

import re
from datetime import datetime, date, timezone


def local_now(tz_name: str | None) -> datetime:
    """Aware `datetime` in the given IANA timezone. Falls back to UTC if
    tz_name is missing, invalid, or zoneinfo can't resolve it (e.g. missing
    tzdata package on some minimal Linuxes). Never raises."""
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def local_today(tz_name: str | None) -> date:
    """User-local calendar date. UTC fallback keeps behavior deterministic
    when the profile has no timezone — but any UTC fallback is a hint that
    the user hasn't gone through the city/tz onboarding yet."""
    return local_now(tz_name).date()


def valid_zone(tz_name: str | None) -> str | None:
    """`tz_name` if zoneinfo can resolve it, else None.

    The resolution `_zone` already does, surfaced as a validator so `timeutil`
    stays the single owner of what counts as a timezone. `profile["timezone"]`
    is writable by the Haiku extractor (it is named in EXTRACT_PROMPT's schema),
    and an unresolvable value there degrades every local_now/local_today call in
    the codebase to UTC — silently, and for good."""
    if not tz_name:
        return None
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(str(tz_name))
        return str(tz_name)
    except Exception:
        return None


# Today plus a full week. The extra day is not padding: it makes the weekday
# that IS today appear twice, which is the only way the convention this repo
# already uses — a bare weekday naming today means the one a week out — has a
# date to point at instead of being derived.
_DATE_RUN_DAYS = 8


def _date_run(start) -> str:
    """The next _DATE_RUN_DAYS days as `Sat 2026-09-05` entries.

    Full ISO on every entry, deliberately. The point is that the model can lift
    a date straight into due_at rather than rebuilding one from "Friday,
    September 04, 2026" — and reassembling is the arithmetic being removed. It
    also survives a year boundary, which an abbreviated form does not."""
    from datetime import timedelta
    out = []
    for i in range(_DATE_RUN_DAYS):
        d = start + timedelta(days=i)
        label = {0: " (today)", 1: " (tomorrow)"}.get(i, "")
        out.append(f"{d.strftime('%a')} {d.isoformat()}{label}")
    return ", ".join(out)


def clock_block(tz_name: str | None, now: datetime | None = None) -> str:
    """What time it is *where the user is*, for the system prompt.

    The model used to be handed the dyno's clock and the dyno's date — "Current
    time: 00:42 UTC", "Today is Sunday, August 31" — and asked to work out the
    rest from the city string in the profile. Two things went wrong with that,
    and only one of them was obvious.

    The obvious one: every reminder needed a DST-aware conversion in each
    direction, done in the model's head, off a profile field nobody had checked.

    The one that actually reached users: from 17:00 Pacific onward the UTC date
    is already tomorrow. "Today is Sunday" is simply false for a Los Angeles
    user at 8pm Saturday, so "remind me tomorrow at 9" lands on Monday. The
    model was not making a mistake — it was told the wrong day and reasoned
    correctly from it.

    So state the user's day and clock first and plainly, and keep the server
    clock as a labelled aside rather than the headline. When the zone is missing
    or unresolvable, say so and assert no local date at all: presenting UTC as
    though it were their day is what caused this."""
    now = now or datetime.now(timezone.utc)
    resolved = valid_zone(tz_name)
    server = now.astimezone(timezone.utc)
    if not resolved:
        return (
            "RIGHT NOW\n"
            f"You don't know this person's timezone. The server clock is "
            f"{server.strftime('%H:%M')} UTC on {server.strftime('%A, %B %d, %Y')}.\n"
            f"Server dates, if a weekday gets named: {_date_run(server.date())}. "
            "Their own day may be either side of these.\n"
            "Do not state or assume a local date or hour for them. If a time matters, "
            "name the zone you're assuming so they can correct you."
        )
    local = now.astimezone(_zone(resolved))
    from datetime import timedelta
    tomorrow = local + timedelta(days=1)
    return (
        "RIGHT NOW, WHERE THEY ARE\n"
        f"Their local time is {local.strftime('%H:%M')} on "
        f"{local.strftime('%A, %B %d, %Y')} ({resolved}, "
        f"UTC{local.strftime('%z')[:3]}:{local.strftime('%z')[3:]}).\n"
        f"For this person \"today\" means {local.strftime('%A %B %d')} and \"tomorrow\" "
        f"means {tomorrow.strftime('%A %B %d')}. Always mean their day, never the "
        f"server's.\n"
        f"Their dates, to read off rather than count: {_date_run(local.date())}.\n"
        f"(Server clock, for your reference only: {server.strftime('%H:%M')} UTC on "
        f"{server.strftime('%A, %B %d')}.)"
    )


def resolve_day_delta(when: str, when_lower: str, tz: str | None = None) -> int | None:
    """Convert 'tomorrow' / weekday name / 'YYYY-MM-DD' into a day offset from
    the user's local today. Falls back to server UTC if tz is missing.
    Returns None if the input doesn't look like a future-date reference."""
    today = local_today(tz)
    wd = today.weekday()
    # Raw distance to the next occurrence of each weekday, where 0 means today.
    base = {
        "monday": (0 - wd) % 7,
        "tuesday": (1 - wd) % 7,
        "wednesday": (2 - wd) % 7,
        "thursday": (3 - wd) % 7,
        "friday": (4 - wd) % 7,
        "saturday": (5 - wd) % 7,
        "sunday": (6 - wd) % 7,
        "weekend": (5 - wd) % 7,
    }
    if "tomorrow" in when_lower:
        return 1
    # "next friday" is the Friday AFTER this coming one. Without this the two
    # were indistinguishable, so someone planning a week out silently got this
    # week's forecast under next week's name.
    #
    # The two rules have to compose, which is why this is not a flat +7 on top
    # of the table: a bare weekday naming TODAY resolves to a week out (asking
    # "how's Friday" on a Friday means the next one — see
    # test_timeutil.TestResolveDayDeltaHonorsTz), so adding 7 to that would put
    # "next friday" a fortnight away.
    explicit_next = bool(re.search(
        r"\bnext\s+(?:week|weekend|mon|tue|wed|thu|fri|sat|sun)", when_lower))
    for k, v in base.items():
        if k in when_lower:
            return v + 7 if explicit_next else (v or 7)
    try:
        target = datetime.strptime(when.strip(), "%Y-%m-%d").date()
        return (target - today).days
    except Exception:
        return None


# Reminder recurrence. Deliberately a small closed set — these are the shapes
# people actually ask for by text, and each one has an unambiguous "next".
RECURRENCES = ("daily", "weekdays", "weekly")


def _zone(tz_name: str | None):
    """ZoneInfo for tz_name, or UTC if it's missing or unresolvable."""
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(tz_name)
        except Exception:
            pass
    return timezone.utc


def _advance(d: date, recurrence: str) -> date:
    from datetime import timedelta
    if recurrence == "weekly":
        return d + timedelta(days=7)
    d = d + timedelta(days=1)
    if recurrence == "weekdays":
        while d.weekday() >= 5:  # Sat=5, Sun=6
            d = d + timedelta(days=1)
    return d


def next_occurrence(due_at: datetime, recurrence: str | None, tz_name: str | None,
                    now: datetime | None = None) -> datetime | None:
    """The next future occurrence of a recurring reminder, as an aware UTC datetime.
    Returns None if `recurrence` isn't one of RECURRENCES.

    Two properties carry the weight here:

    LOCAL WALL CLOCK, NOT A UTC DELTA. A 3pm daily reminder for a Chicago user is
    20:00Z under CDT and 21:00Z under CST. Adding 24h in UTC would hold the UTC
    instant fixed and walk the reminder to 2pm local the day after the DST
    change, then leave it there. So each candidate is rebuilt from the local
    wall-clock time against the new local date, and the offset is whatever that
    date implies.

    SKIP MISSED PERIODS. The next occurrence is the first one strictly after
    `now`, not simply the previous one plus a period. Without that, a reminder
    left stale by an outage would fire once per missed day on recovery — the
    catch-up semantics of `due_at <= now` turned into a burst.
    """
    recurrence = (recurrence or "").strip().lower()
    if recurrence not in RECURRENCES:
        return None
    now = now or datetime.now(timezone.utc)
    tz = _zone(tz_name)
    local = due_at.astimezone(tz)
    h, m, s = local.hour, local.minute, local.second
    d = local.date()
    # Bounded so a bad clock or an unexpected recurrence can never spin here.
    # 400 daily steps is over a year; weekly overshoots that further.
    for _ in range(400):
        d = _advance(d, recurrence)
        # Rebuilding with tzinfo=tz (rather than shifting a UTC instant) is what
        # preserves the wall clock. On a spring-forward date the named local time
        # may not exist; zoneinfo resolves it by fold rather than raising, which
        # is the right failure — an hour off once a year beats a lost reminder.
        candidate = datetime(d.year, d.month, d.day, h, m, s, tzinfo=tz)
        as_utc = candidate.astimezone(timezone.utc)
        if as_utc > now:
            return as_utc
    return None
