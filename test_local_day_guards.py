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


class TestTheAlertCapsUseTheReadersDay:
    """alerts.py was fixed for this and the two watch caps were not.

    A cap keyed on the dyno's UTC date rolls at 17:00 Pacific — inside the
    evening, not between days — so a user could take the whole day's
    allowance across one local evening and be capped by lunch the next day.
    The read (_daily_ok) and the write (update_*_alerted) agreed with each
    other and both disagreed with the reader.
    """

    def test_a_news_watch_cap_reads_the_day_it_is_given(self):
        import watches
        watch = {"daily_alert_date": "2026-08-31", "daily_alert_count": 99}
        # Their day is still the 30th while the server has rolled to the 31st.
        assert watches._daily_ok(watch, cap=1, today="2026-08-30") is True
        assert watches._daily_ok(watch, cap=1, today="2026-08-31") is False

    def test_a_price_watch_cap_reads_the_day_it_is_given(self):
        import shopping
        watch = {"daily_alert_date": "2026-08-31",
                 "daily_alert_count": shopping.PRICE_DAILY_ALERT_MAX}
        assert shopping._daily_ok(watch, today="2026-08-30") is True
        assert shopping._daily_ok(watch, today="2026-08-31") is False

    def test_the_watch_loop_derives_the_day_from_the_profile_it_already_reads(self):
        """One profile read per user already happens for the pacing cap; the
        local day rides along on it rather than costing another connection."""
        import inspect, watches
        src = inspect.getsource(watches.run_watches)
        assert "local_today" in src
        assert "today=today" in src

    def test_the_price_loop_reads_every_profile_in_one_query(self):
        """Never `for phone in ...: get_profile(phone)` — that is N+1 a tick."""
        import inspect, shopping
        src = inspect.getsource(shopping.run_price_watches)
        assert "get_all_profiles" in src
        assert "get_profile(" not in src

    def test_the_write_side_takes_the_same_day_as_the_read(self):
        import inspect, db
        for fn in (db.update_watch_alerted, db.update_price_watch_alerted):
            assert "today" in inspect.signature(fn).parameters


class TestProfileFactsAgeOnTheReadersCalendar:
    """field_dates is stamped with local_today and was aged against
    date.today(), so the two ends of one subtraction used different
    calendars. After 17:00 Pacific a fact asserted minutes ago came back to
    the model as days_old: 1, and a volatile field was dropped a day early."""

    def test_the_prompt_profile_is_aged_on_the_users_day(self):
        import inspect, agent
        src = inspect.getsource(agent._prompt_safe_profile)
        assert "local_today" in src
        assert "fresh_profile_for_prompt(profile)" not in src, \
            "the default argument is date.today() — the dyno's day"

    def test_a_fact_stamped_today_is_not_a_day_old(self):
        import userprofile
        from datetime import date as _d
        profile = {"stressed_about": "the move",
                   "field_dates": {"stressed_about": "2026-08-30"}}
        # Their day is the 30th; the server has already rolled to the 31st.
        out = userprofile.fresh_profile_for_prompt(profile, _d(2026, 8, 30))
        assert out["stressed_about"] == "the move"      # undated, i.e. age 0
