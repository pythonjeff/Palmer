"""A "once a day" guard must mean the reader's day.

alerts.py keyed its guard on the UTC date while _in_alert_window gates on the
LOCAL hour 13-21. For Pacific that window is 20:00Z-04:00Z, so the UTC day
rolled over at 17:00 local — INSIDE the window. A user could take two "daily"
alerts in one local day and none the next.

And morning._recent_assistant_texts took the last 4 assistant messages of any
kind, so for anyone who actually texts Palmer the anti-repetition guard was
comparing today's morning line against ordinary chat rather than against
yesterday's morning — which is the failure it was written for.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import db
import morning


PHONE = "+15550001111"


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test_local_day.db")
    db.init_db()


class TestTheAlertGuardUsesTheReadersDay:
    def _attempted_claim(self, monkeypatch):
        """Run one tick at 00:42Z on Aug 31 for a Pacific reader who already had
        an alert on their Aug 30. Returns the value any claim was attempted with,
        or None if the guard correctly skipped.

        BOTH clocks are pinned: timeutil's, which the fixed code reads, and
        alerts.date_type, which the old code read. Without the second the test
        passes against the bug, because the ambient system date happened to
        agree with the reader's."""
        import alerts
        from datetime import date as real_date
        seen = {}
        profile = {"morning_onboarded": True, "timezone": "America/Los_Angeles",
                   "alert_sent_date": "2026-08-30"}

        def _claim(phone, field, value):
            seen["value"] = value
            return False

        class _FixedDate(real_date):
            @classmethod
            def today(cls):
                return real_date(2026, 8, 31)   # the SERVER's day

        with patch.object(alerts, "get_all_profiles", return_value=[(PHONE, profile)]), \
             patch.object(alerts, "_in_alert_window", return_value=True), \
             patch.object(alerts, "date_type", _FixedDate), \
             patch.object(alerts, "claim_daily_guard", side_effect=_claim), \
             patch("timeutil.datetime") as dt:
            # The INSTANT is 00:42Z on the 31st. In Los Angeles that is 17:42 on
            # the 30th — still the reader's day, and still their alert window.
            dt.now.side_effect = lambda tz=None: (
                datetime(2026, 8, 31, 0, 42, tzinfo=timezone.utc).astimezone(tz)
                if tz else datetime(2026, 8, 31, 0, 42, tzinfo=timezone.utc))
            alerts.run_alert_checks()
        return seen.get("value")

    def test_a_second_alert_is_not_attempted_in_the_same_local_day(self, monkeypatch):
        assert self._attempted_claim(monkeypatch) is None

    def test_the_source_no_longer_keys_the_guard_on_utc(self):
        import inspect
        import alerts
        src = inspect.getsource(alerts.run_alert_checks)
        assert "local_today(profile" in src
        assert "date_type.today()" not in src


class TestTheMorningComparesAgainstMornings:
    def test_chat_replies_do_not_crowd_out_prior_mornings(self, tmp_path, monkeypatch):
        _fresh(tmp_path, monkeypatch)
        db.save_message(PHONE, "assistant", "yesterday's morning line", kind="morning")
        for i in range(10):
            db.save_message(PHONE, "assistant", f"chat reply {i}", kind="reply")
        got = morning._recent_assistant_texts(PHONE, n=4)
        assert got == ["yesterday's morning line"], got

    def test_it_falls_back_for_history_predating_the_kind_column(self, tmp_path, monkeypatch):
        _fresh(tmp_path, monkeypatch)
        db.save_message(PHONE, "assistant", "an old untagged message")
        assert morning._recent_assistant_texts(PHONE, n=4) == ["an old untagged message"]

    def test_ordering_is_oldest_first(self, tmp_path, monkeypatch):
        _fresh(tmp_path, monkeypatch)
        for i in range(3):
            db.save_message(PHONE, "assistant", f"morning {i}", kind="morning")
        assert morning._recent_assistant_texts(PHONE, n=3) == \
            ["morning 0", "morning 1", "morning 2"]

    def test_the_repetition_guard_now_sees_yesterday(self, tmp_path, monkeypatch):
        """guards.repeats_opening exists for three consecutive mornings that all
        opened the same way; it could not see them past a chatty user."""
        import guards
        _fresh(tmp_path, monkeypatch)
        db.save_message(PHONE, "assistant", "103 today in Woodland Hills, stay inside",
                        kind="morning")
        db.save_message(PHONE, "assistant", "sure, on it", kind="reply")
        recent = morning._recent_assistant_texts(PHONE, n=4)
        assert guards.repeats_opening("106 today in Woodland Hills, brutal again", recent)
