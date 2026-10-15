"""A "once a day" guard must mean the reader's day.

(The daily news-alert job that first got this wrong — UTC-keyed guard, local
send window, two "daily" alerts in one Pacific day — has since been retired;
its lesson lives on in every remaining daily sender keying on local_today.)

morning._recent_assistant_texts took the last 4 assistant messages of any
kind, so for anyone who actually texts Palmer the anti-repetition guard was
comparing today's morning line against ordinary chat rather than against
yesterday's morning — which is the failure it was written for.
"""

from palmer import db
from palmer import morning


PHONE = "+15550001111"


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test_local_day.db")
    db.init_db()


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
        from palmer import guards
        _fresh(tmp_path, monkeypatch)
        db.save_message(PHONE, "assistant", "103 today in Woodland Hills, stay inside",
                        kind="morning")
        db.save_message(PHONE, "assistant", "sure, on it", kind="reply")
        recent = morning._recent_assistant_texts(PHONE, n=4)
        assert guards.repeats_opening("106 today in Woodland Hills, brutal again", recent)


class TestTheAlertCapsUseTheReadersDay:
    """The retired daily-alert job was fixed for this and the two watch caps were not.

    A cap keyed on the dyno's UTC date rolls at 17:00 Pacific — inside the
    evening, not between days — so a user could take the whole day's
    allowance across one local evening and be capped by lunch the next day.
    The read (_daily_ok) and the write (update_*_alerted) agreed with each
    other and both disagreed with the reader.
    """

    def test_a_news_watch_cap_reads_the_day_it_is_given(self):
        from palmer import watches
        watch = {"daily_alert_date": "2026-08-31", "daily_alert_count": 99}
        # Their day is still the 30th while the server has rolled to the 31st.
        assert watches._daily_ok(watch, cap=1, today="2026-08-30") is True
        assert watches._daily_ok(watch, cap=1, today="2026-08-31") is False

    def test_a_price_watch_cap_reads_the_day_it_is_given(self):
        from palmer import shopping
        watch = {"daily_alert_date": "2026-08-31",
                 "daily_alert_count": shopping.PRICE_DAILY_ALERT_MAX}
        assert shopping._daily_ok(watch, today="2026-08-30") is True
        assert shopping._daily_ok(watch, today="2026-08-31") is False

    def test_the_watch_loop_derives_the_day_from_the_profile_it_already_reads(self):
        """One profile read per user already happens for the pacing cap; the
        local day rides along on it rather than costing another connection."""
        import inspect
        from palmer import watches
        src = inspect.getsource(watches.run_watches)
        assert "local_today" in src
        assert "today=today" in src

    def test_the_price_loop_reads_every_profile_in_one_query(self):
        """Never `for phone in ...: get_profile(phone)` — that is N+1 a tick."""
        import inspect
        from palmer import shopping
        src = inspect.getsource(shopping.run_price_watches)
        assert "get_all_profiles" in src
        assert "get_profile(" not in src

    def test_the_write_side_takes_the_same_day_as_the_read(self):
        import inspect
        from palmer import db
        for fn in (db.update_watch_alerted, db.update_price_watch_alerted):
            assert "today" in inspect.signature(fn).parameters


class TestProfileFactsAgeOnTheReadersCalendar:
    """field_dates is stamped with local_today and was aged against
    date.today(), so the two ends of one subtraction used different
    calendars. After 17:00 Pacific a fact asserted minutes ago came back to
    the model as days_old: 1, and a volatile field was dropped a day early."""

    def test_the_prompt_profile_is_aged_on_the_users_day(self):
        import inspect
        from palmer import agent
        src = inspect.getsource(agent._prompt_safe_profile)
        assert "local_today" in src
        assert "fresh_profile_for_prompt(profile)" not in src, \
            "the default argument is date.today() — the dyno's day"

    def test_a_fact_stamped_today_is_not_a_day_old(self):
        from palmer import userprofile
        from datetime import date as _d
        profile = {"stressed_about": "the move",
                   "field_dates": {"stressed_about": "2026-08-30"}}
        # Their day is the 30th; the server has already rolled to the 31st.
        out = userprofile.fresh_profile_for_prompt(profile, _d(2026, 8, 30))
        assert out["stressed_about"] == "the move"      # undated, i.e. age 0
