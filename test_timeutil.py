"""Tests for user-local date/time helpers.

Palmer's dyno runs UTC but almost everything user-facing needs the user's
local calendar day. These tests lock in the semantics (real tz honored, no-tz
falls back to UTC, unknown tz doesn't raise) and prove the late-night west-
coast off-by-one is actually fixed.
"""
from datetime import date, datetime, timezone
from unittest.mock import patch

import timeutil


class TestLocalNow:
    def test_returns_tz_aware_datetime_for_valid_tz(self):
        out = timeutil.local_now("America/Los_Angeles")
        assert out.tzinfo is not None
        # tz name should be present on the returned aware dt
        assert "Los_Angeles" in str(out.tzinfo)

    def test_none_tz_falls_back_to_utc(self):
        out = timeutil.local_now(None)
        assert out.tzinfo == timezone.utc

    def test_empty_tz_falls_back_to_utc(self):
        assert timeutil.local_now("").tzinfo == timezone.utc

    def test_bogus_tz_falls_back_to_utc_without_raising(self):
        assert timeutil.local_now("Not/A/Real_Zone").tzinfo == timezone.utc


class TestLocalToday:
    def test_returns_date_type(self):
        assert isinstance(timeutil.local_today("America/Chicago"), date)

    def test_none_tz_uses_utc_date(self):
        # Freeze datetime.now so we can compare exactly
        with patch("timeutil.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: datetime(2026, 8, 18, 20, 0, tzinfo=tz or timezone.utc)
            assert timeutil.local_today(None) == date(2026, 8, 18)

    def test_west_coast_late_evening_stays_on_yesterday_local(self):
        """The bug this whole exercise was for: at 22:00 America/Los_Angeles
        on Aug 17 the server is already Aug 18 UTC, so anything using
        date.today() sees Aug 18. local_today('America/Los_Angeles') must
        still see Aug 17."""
        # Fake datetime.now so it returns Aug 18 05:00 UTC (=Aug 17 22:00 PDT)
        with patch("timeutil.datetime") as mock_dt:
            def _now(tz=None):
                base = datetime(2026, 8, 18, 5, 0, tzinfo=timezone.utc)
                return base.astimezone(tz) if tz else base
            mock_dt.now.side_effect = _now
            assert timeutil.local_today("America/Los_Angeles") == date(2026, 8, 17)
            assert timeutil.local_today(None) == date(2026, 8, 18)  # UTC still Aug 18


class TestResolveDayDeltaHonorsTz:
    """The end-to-end bug: at 22:00 PDT Thursday, someone asking 'weather
    Friday' should get delta=1 (tomorrow), not delta=8 (next Friday)."""

    def test_friday_at_thursday_night_local_is_tomorrow(self):
        from agent import _resolve_day_delta
        # Freeze both timeutil.local_today and agent._date.today so the module
        # under test sees a consistent 'now'.
        with patch("timeutil.datetime") as mock_dt:
            def _now(tz=None):
                # Aug 21 2026 is a Friday. So 05:00 UTC on Aug 21 = 22:00 PDT on Aug 20 (Thursday).
                base = datetime(2026, 8, 21, 5, 0, tzinfo=timezone.utc)
                return base.astimezone(tz) if tz else base
            mock_dt.now.side_effect = _now

            # UTC-only path (no tz): 'friday' seen from Friday should give 7 (next friday)
            assert _resolve_day_delta("friday", "friday", tz=None) == 7
            # With user tz PDT (still Thursday locally): 'friday' should give 1 (tomorrow)
            assert _resolve_day_delta("friday", "friday", tz="America/Los_Angeles") == 1

    def test_tomorrow_always_one_day_out(self):
        from agent import _resolve_day_delta
        assert _resolve_day_delta("tomorrow", "tomorrow", tz="America/Chicago") == 1
        assert _resolve_day_delta("tomorrow", "tomorrow", tz=None) == 1
