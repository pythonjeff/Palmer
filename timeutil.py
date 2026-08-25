"""User-local time helpers.

Palmer's dyno runs in UTC on Heroku, but nearly everything the USER perceives
("today's weather", "did we already text them today", "is it late enough
locally to send a check-in") has to be evaluated in the user's own timezone.

Modules that already had ad-hoc UTC calls (agent.py's weather day resolver,
alerts.py's 'news broke today' gate) now use these helpers instead — keeping
one source of truth for what "today" means for a given profile.
"""
from __future__ import annotations

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
