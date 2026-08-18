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
