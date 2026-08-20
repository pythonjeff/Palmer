"""Tests for morning send-window logic and time parsing. Run: pytest test_morning_schedule.py"""
from dotenv import load_dotenv
load_dotenv()

from datetime import datetime
from zoneinfo import ZoneInfo

from morning import _in_send_window, _parse_morning_time, _local_today, DEFAULT_MORNING_TIME
from agent import _normalize_hhmm, _parse_published


def _dt(hour, minute, tz="America/Chicago"):
    return datetime(2026, 8, 3, hour, minute, tzinfo=ZoneInfo(tz))


class TestParseMorningTime:
    def test_default(self):
        assert _parse_morning_time(DEFAULT_MORNING_TIME) == (7, 0)

    def test_custom(self):
        assert _parse_morning_time("08:30") == (8, 30)
        assert _parse_morning_time("21:15") == (21, 15)

    def test_invalid_falls_back_to_default(self):
        for bad in (None, "", "notatime", "25:00", "08:99", "8", 700):
            assert _parse_morning_time(bad) == (7, 0), bad


class TestSendWindow:
    def test_before_time_no_send(self):
        assert not _in_send_window(_dt(8, 29), "08:30")

    def test_exactly_at_time_sends(self):
        assert _in_send_window(_dt(8, 30), "08:30")

    def test_within_catchup_sends(self):
        assert _in_send_window(_dt(9, 45), "08:30")
        assert _in_send_window(_dt(10, 29), "08:30")

    def test_after_catchup_no_send(self):
        assert not _in_send_window(_dt(10, 30), "08:30")
        assert not _in_send_window(_dt(21, 0), "08:30")

    def test_none_uses_default(self):
        assert _in_send_window(_dt(7, 0), None)
        assert not _in_send_window(_dt(6, 30), None)

    def test_custom_time(self):
        assert _in_send_window(_dt(7, 0), "07:00")
        assert not _in_send_window(_dt(6, 55), "07:00")
        assert not _in_send_window(_dt(9, 5), "07:00")

    def test_other_timezone(self):
        assert _in_send_window(_dt(8, 35, tz="America/New_York"), "08:30")

    def test_invalid_pref_falls_back_to_default(self):
        assert _in_send_window(_dt(7, 0), "garbage")
        assert not _in_send_window(_dt(6, 30), "garbage")


class TestLocalToday:
    def test_valid_tz(self):
        assert _local_today("America/Chicago") is not None

    def test_missing_or_bad_tz_falls_back(self):
        """Fallback is the UTC date, not the runner's local date — comparing to
        date.today() made this fail every evening west of UTC."""
        from datetime import datetime, timezone
        utc_today = datetime.now(timezone.utc).date()
        assert _local_today(None) == utc_today
        assert _local_today("Not/AZone") == utc_today


class TestNormalizeHhmm:
    def test_valid(self):
        assert _normalize_hhmm("07:00") == "07:00"
        assert _normalize_hhmm("7:05") == "07:05"
        assert _normalize_hhmm("23:59") == "23:59"
        assert _normalize_hhmm(" 08:30 ") == "08:30"

    def test_invalid(self):
        for bad in ("24:00", "12:60", "7am", "730", "", None, "7:5"):
            assert _normalize_hhmm(bad) is None, bad


class TestParsePublished:
    def test_rfc2822(self):
        dt = _parse_published("Mon, 03 Aug 2026 10:00:00 GMT")
        assert dt is not None and dt.year == 2026

    def test_iso(self):
        dt = _parse_published("2026-08-03T10:00:00Z")
        assert dt is not None and dt.hour == 10

    def test_garbage(self):
        assert _parse_published("unknown") is None
        assert _parse_published(None) is None
        assert _parse_published("") is None
