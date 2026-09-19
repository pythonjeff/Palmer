"""The database layer.

Merged from test_db_claims.py, test_phantom_history.py, test_local_day_guards.py; each section keeps its
original file's notes, because those carry the incident that led to the test.
"""
from datetime import datetime, timedelta, timezone
from palmer import db, morning
from unittest.mock import patch


# ============================================================================
# from test_db_claims.py
# ============================================================================

class TestSaveMessageTimestamp:
    """Regression test: save_message must write created_at in the same ISO8601+offset
    format used for cutoff comparisons, or same-day messages silently fail to match
    (SQLite's old CURRENT_TIMESTAMP default sorted before any same-day ISO cutoff)."""

    def test_message_saved_now_matches_a_recent_cutoff(self, fresh_db):
        phone = "+15551234567"
        db.save_message(phone, "assistant", "hello there")
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        assert db.get_recent_assistant_messages(phone, cutoff) == ["hello there"]

    def test_old_message_does_not_match_a_recent_cutoff(self, fresh_db):
        phone = "+15551234567"
        db.save_message(phone, "assistant", "old message")
        cutoff = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        assert db.get_recent_assistant_messages(phone, cutoff) == []


class TestClaimDailyGuard:
    def test_first_claim_succeeds(self, fresh_db):
        assert db.claim_daily_guard("+15551234567", "morning_sent_date", "2026-08-13") is True

    def test_second_claim_same_value_fails(self, fresh_db):
        phone = "+15551234567"
        assert db.claim_daily_guard(phone, "morning_sent_date", "2026-08-13") is True
        assert db.claim_daily_guard(phone, "morning_sent_date", "2026-08-13") is False

    def test_claim_different_value_succeeds(self, fresh_db):
        phone = "+15551234567"
        assert db.claim_daily_guard(phone, "morning_sent_date", "2026-08-13") is True
        assert db.claim_daily_guard(phone, "morning_sent_date", "2026-08-14") is True

    def test_release_allows_reclaim(self, fresh_db):
        phone = "+15551234567"
        assert db.claim_daily_guard(phone, "morning_sent_date", "2026-08-13") is True
        db.upsert_profile(phone, {"morning_sent_date": None})
        assert db.claim_daily_guard(phone, "morning_sent_date", "2026-08-13") is True


class TestClaimWatchAlert:
    def test_fresh_row_claims(self, fresh_db):
        watch_id = db.save_watch("+15551234567", "test topic", ["test query"], cooldown_hours=4)
        assert db.claim_watch_alert(watch_id, 4) is True

    def test_immediate_reclaim_fails(self, fresh_db):
        watch_id = db.save_watch("+15551234567", "test topic", ["test query"], cooldown_hours=4)
        assert db.claim_watch_alert(watch_id, 4) is True
        assert db.claim_watch_alert(watch_id, 4) is False


class TestWatchAlertUrlPersistence:
    """The 'Watching' page section links a watch to the article that
    fired it, so update_watch_alerted must round-trip the URL/domain, not just
    the dedup title, and a watch that has never fired must stay linkless."""

    def test_update_watch_alerted_persists_url_and_domain(self, fresh_db):
        phone = "+15551234567"
        watch_id = db.save_watch(phone, "test watch", ["test query"], cooldown_hours=4)
        db.update_watch_alerted(watch_id, "summary", [], url="https://apnews.com/x", domain="apnews.com")
        row = db.get_user_watches(phone)[0]
        assert row["last_alert_url"] == "https://apnews.com/x"
        assert row["last_alert_domain"] == "apnews.com"

    def test_update_watch_alerted_without_url_leaves_columns_null(self, fresh_db):
        phone = "+15551234567"
        watch_id = db.save_watch(phone, "test watch", ["test query"], cooldown_hours=4)
        db.update_watch_alerted(watch_id, "summary", [])
        row = db.get_user_watches(phone)[0]
        assert row["last_alert_url"] is None
        assert row["last_alert_domain"] is None


class TestGetUserPriceWatches:
    """last_seen_url/last_seen_merchant already existed on the row and were
    already populated by set_price_watch_baseline — they just weren't selected."""

    def test_baseline_url_and_merchant_round_trip(self, fresh_db):
        phone = "+15551234567"
        watch_id = db.save_price_watch(phone, "AirPods Pro", target_price=199.0)
        db.set_price_watch_baseline(watch_id, 220.0, "https://www.amazon.com/x", "amazon.com")
        row = db.get_user_price_watches(phone)[0]
        assert row["last_seen_url"] == "https://www.amazon.com/x"
        assert row["last_seen_merchant"] == "amazon.com"

    def test_reclaim_after_cooldown_succeeds(self, fresh_db):
        watch_id = db.save_watch("+15551234567", "test topic", ["test query"], cooldown_hours=4)
        old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        conn = db._conn()
        cur = conn.cursor()
        cur.execute(f"UPDATE watches SET last_alerted = {db.PH} WHERE id = {db.PH}", (old, watch_id))
        conn.commit()
        conn.close()
        assert db.claim_watch_alert(watch_id, 4) is True


class TestClaimPriceWatchAlert:
    def test_fresh_row_claims(self, fresh_db):
        watch_id = db.save_price_watch("+15551234567", "test product", cooldown_hours=12)
        assert db.claim_price_watch_alert(watch_id, 12) is True

    def test_immediate_reclaim_fails(self, fresh_db):
        watch_id = db.save_price_watch("+15551234567", "test product", cooldown_hours=12)
        assert db.claim_price_watch_alert(watch_id, 12) is True
        assert db.claim_price_watch_alert(watch_id, 12) is False

    def test_release_allows_reclaim(self, fresh_db):
        watch_id = db.save_price_watch("+15551234567", "test product", cooldown_hours=12)
        assert db.claim_price_watch_alert(watch_id, 12) is True
        db.release_price_watch_claim(watch_id)
        assert db.claim_price_watch_alert(watch_id, 12) is True


class TestPriceWatchRebaseline:
    """update_price_watch_alerted must move baseline_price to the alerted price.

    baseline_price is otherwise written exactly once, by set_price_watch_baseline
    at watch creation, and never again — so a price that settles below the drop
    bar keeps re-qualifying on every tick. The per-day cap bounds that to
    PRICE_DAILY_ALERT_MAX texts, then resets the next day, forever.
    _is_duplicate_subject can't catch it either: 6h window, 12h cadence."""

    def _watch(self):
        wid = db.save_price_watch("+15551234567", "Core Power Elite Chocolate 12pk")
        db.set_price_watch_baseline(wid, 50.98, "https://amazon.com/dp/X", "Amazon")
        return wid

    def test_alert_moves_the_baseline(self, fresh_db):
        wid = self._watch()
        db.update_price_watch_alerted(
            wid, 47.98, "https://amazon.com/dp/X", "Amazon", "shake's down to 47.98")
        row = [w for w in db.get_active_price_watches() if w["id"] == wid][0]
        assert float(row["baseline_price"]) == 47.98
        assert float(row["last_seen_price"]) == 47.98

    def test_same_price_no_longer_qualifies_after_alert(self, fresh_db):
        from palmer.shopping import _should_alert
        wid = self._watch()
        row = [w for w in db.get_active_price_watches() if w["id"] == wid][0]
        assert _should_alert(row, 47.98) == "drop"

        db.update_price_watch_alerted(
            wid, 47.98, "https://amazon.com/dp/X", "Amazon", "shake's down to 47.98")
        row = [w for w in db.get_active_price_watches() if w["id"] == wid][0]
        assert _should_alert(row, 47.98) == ""      # already told, don't repeat
        assert _should_alert(row, 45.00) == "drop"  # a further move still fires


# ============================================================================
# from test_phantom_history.py
# ============================================================================
#
# What Palmer records must be what Palmer sent.
#
# Four senders got this wrong in two different ways, and both produce the same
# symptom the user sees: Palmer referring to a message that never arrived, or
# repeating one that did.
#
# watches.py (and the since-retired alerts job) called send_sms and ignored the
# result, then saved unconditionally. send_sms returns False on a Twilio failure AND on a
# leaks_deliberation block, so history accumulated messages nobody received —
# and _build_system feeds history straight back to the model.
#
# shopping.py and flightwatch.py never called save_message at all. So a user
# replying "how much?" to a price alert got an answer with no referent, and each
# sender's own _is_duplicate_subject check — which reads assistant messages —
# could never see its own repeats.

PHONE = "+15550001111"


class TestTheKindColumn:
    def test_a_kind_round_trips(self, fresh_db):
        db.save_message(PHONE, "assistant", "morning line", kind="morning")
        conn = db._conn()
        cur = conn.cursor()
        cur.execute(f"SELECT kind FROM messages WHERE phone = {db.PH}", (PHONE,))
        assert cur.fetchone()["kind"] == "morning"
        conn.close()

    def test_kind_is_optional_so_existing_callers_are_unaffected(self, fresh_db):
        db.save_message(PHONE, "user", "hey")
        assert db.get_history(PHONE) == [{"role": "user", "content": "hey"}]

    def test_migration_is_idempotent(self, fresh_db):
        db.save_message(PHONE, "assistant", "x", kind="alert")
        assert len(db.get_history(PHONE)) == 1


class TestPriceAlertsReachHistory:
    """run_price_watches imports its db helpers inside the function, so they are
    patched at their source rather than on the shopping module."""

    def _run(self, fresh_db, sent: bool):
        from palmer import shopping
        db.save_price_watch(PHONE, "Nike Pegasus 40", None)
        w = dict(db.get_user_price_watches(PHONE)[0])
        w["baseline_price"] = 120.0
        w["cooldown_hours"] = 12
        w["phone"] = PHONE   # get_user_price_watches scopes by phone and omits it
        with patch.object(db, "get_active_price_watches", return_value=[w]), \
             patch.object(db, "claim_price_watch_alert", return_value=True), \
             patch.object(db, "update_price_watch_alerted"), \
             patch.object(db, "release_price_watch_claim"), \
             patch.object(shopping, "check_price", return_value={
                 "price": 90.0, "url": "https://m.example/x", "merchant": "Nike"}), \
             patch.object(shopping, "_draft_alert", return_value="pegasus dropped to $90"), \
             patch("palmer.userprofile._is_duplicate_subject", return_value=False), \
             patch("palmer.sms_util.send_sms", return_value=sent):
            shopping.run_price_watches()
        return [m for m in db.get_history(PHONE) if m["role"] == "assistant"]

    def test_a_sent_alert_is_recorded(self, fresh_db):
        rows = self._run(fresh_db, sent=True)
        assert any("pegasus" in m["content"].lower() for m in rows), rows

    def test_a_failed_price_alert_is_not_recorded(self, fresh_db):
        assert self._run(fresh_db, sent=False) == []

    def test_the_alert_is_visible_to_the_next_duplicate_check(self, fresh_db):
        """The point of recording it: _is_duplicate_subject reads assistant
        messages, so before this the sender could never see its own repeats."""
        from datetime import datetime, timedelta, timezone
        self._run(fresh_db, sent=True)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        assert db.get_recent_assistant_messages(PHONE, cutoff)


class TestProactiveSendersNeverShipTheFallbackString:
    def test_no_proactive_sender_calls_ensure_sms(self):
        """ensure_sms's last resort is FALLBACK_SMS. That is right for a reply
        the user is waiting on and wrong for anything unprompted — a failed
        price check texted 'something went sideways on my end, try again' to
        someone who had asked for nothing."""
        import pathlib
        import palmer
        pkg = pathlib.Path(palmer.__file__).parent
        proactive = ["watches.py", "followup.py", "morning.py", "scorewatch.py",
                     "shopping.py", "flightwatch.py", "reminders.py"]
        for name in proactive:
            src = (pkg / name).read_text()
            calls = [ln for ln in src.splitlines()
                     if "ensure_sms(" in ln and not ln.strip().startswith("#")]
            assert not calls, (name, calls)


# ============================================================================
# from test_local_day_guards.py
# ============================================================================
#
# A "once a day" guard must mean the reader's day.
#
# (The daily news-alert job that first got this wrong — UTC-keyed guard, local
# send window, two "daily" alerts in one Pacific day — has since been retired;
# its lesson lives on in every remaining daily sender keying on local_today.)
#
# morning._recent_assistant_texts took the last 4 assistant messages of any
# kind, so for anyone who actually texts Palmer the anti-repetition guard was
# comparing today's morning line against ordinary chat rather than against
# yesterday's morning — which is the failure it was written for.

class TestTheMorningComparesAgainstMornings:
    def test_chat_replies_do_not_crowd_out_prior_mornings(self, fresh_db):
        db.save_message(PHONE, "assistant", "yesterday's morning line", kind="morning")
        for i in range(10):
            db.save_message(PHONE, "assistant", f"chat reply {i}", kind="reply")
        got = morning._recent_assistant_texts(PHONE, n=4)
        assert got == ["yesterday's morning line"], got

    def test_it_falls_back_for_history_predating_the_kind_column(self, fresh_db):
        db.save_message(PHONE, "assistant", "an old untagged message")
        assert morning._recent_assistant_texts(PHONE, n=4) == ["an old untagged message"]

    def test_ordering_is_oldest_first(self, fresh_db):
        for i in range(3):
            db.save_message(PHONE, "assistant", f"morning {i}", kind="morning")
        assert morning._recent_assistant_texts(PHONE, n=3) == \
            ["morning 0", "morning 1", "morning 2"]

    def test_the_repetition_guard_now_sees_yesterday(self, fresh_db):
        """guards.repeats_opening exists for three consecutive mornings that all
        opened the same way; it could not see them past a chatty user."""
        from palmer import guards
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
