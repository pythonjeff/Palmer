from dotenv import load_dotenv
load_dotenv()

from datetime import datetime, timedelta, timezone

import db


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test_claims.db")
    db.init_db()


class TestSaveMessageTimestamp:
    """Regression test: save_message must write created_at in the same ISO8601+offset
    format used for cutoff comparisons, or same-day messages silently fail to match
    (SQLite's old CURRENT_TIMESTAMP default sorted before any same-day ISO cutoff)."""

    def test_message_saved_now_matches_a_recent_cutoff(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        phone = "+15551234567"
        db.save_message(phone, "assistant", "hello there")
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        assert db.get_recent_assistant_messages(phone, cutoff) == ["hello there"]

    def test_old_message_does_not_match_a_recent_cutoff(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        phone = "+15551234567"
        db.save_message(phone, "assistant", "old message")
        cutoff = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        assert db.get_recent_assistant_messages(phone, cutoff) == []


class TestClaimDailyGuard:
    def test_first_claim_succeeds(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        assert db.claim_daily_guard("+15551234567", "morning_sent_date", "2026-08-13") is True

    def test_second_claim_same_value_fails(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        phone = "+15551234567"
        assert db.claim_daily_guard(phone, "morning_sent_date", "2026-08-13") is True
        assert db.claim_daily_guard(phone, "morning_sent_date", "2026-08-13") is False

    def test_claim_different_value_succeeds(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        phone = "+15551234567"
        assert db.claim_daily_guard(phone, "morning_sent_date", "2026-08-13") is True
        assert db.claim_daily_guard(phone, "morning_sent_date", "2026-08-14") is True

    def test_release_allows_reclaim(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        phone = "+15551234567"
        assert db.claim_daily_guard(phone, "morning_sent_date", "2026-08-13") is True
        db.upsert_profile(phone, {"morning_sent_date": None})
        assert db.claim_daily_guard(phone, "morning_sent_date", "2026-08-13") is True


class TestClaimWatchAlert:
    def test_fresh_row_claims(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        watch_id = db.save_watch("+15551234567", "test topic", ["test query"], cooldown_hours=4)
        assert db.claim_watch_alert(watch_id, 4) is True

    def test_immediate_reclaim_fails(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        watch_id = db.save_watch("+15551234567", "test topic", ["test query"], cooldown_hours=4)
        assert db.claim_watch_alert(watch_id, 4) is True
        assert db.claim_watch_alert(watch_id, 4) is False


class TestWatchAlertUrlPersistence:
    """The 'Palmer is watching' page section links a watch to the article that
    fired it, so update_watch_alerted must round-trip the URL/domain, not just
    the dedup title, and a watch that has never fired must stay linkless."""

    def test_update_watch_alerted_persists_url_and_domain(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        phone = "+15551234567"
        watch_id = db.save_watch(phone, "test watch", ["test query"], cooldown_hours=4)
        db.update_watch_alerted(watch_id, "summary", [], url="https://apnews.com/x", domain="apnews.com")
        row = db.get_user_watches(phone)[0]
        assert row["last_alert_url"] == "https://apnews.com/x"
        assert row["last_alert_domain"] == "apnews.com"

    def test_update_watch_alerted_without_url_leaves_columns_null(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        phone = "+15551234567"
        watch_id = db.save_watch(phone, "test watch", ["test query"], cooldown_hours=4)
        db.update_watch_alerted(watch_id, "summary", [])
        row = db.get_user_watches(phone)[0]
        assert row["last_alert_url"] is None
        assert row["last_alert_domain"] is None


class TestGetUserPriceWatches:
    """last_seen_url/last_seen_merchant already existed on the row and were
    already populated by set_price_watch_baseline — they just weren't selected."""

    def test_baseline_url_and_merchant_round_trip(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        phone = "+15551234567"
        watch_id = db.save_price_watch(phone, "AirPods Pro", target_price=199.0)
        db.set_price_watch_baseline(watch_id, 220.0, "https://www.amazon.com/x", "amazon.com")
        row = db.get_user_price_watches(phone)[0]
        assert row["last_seen_url"] == "https://www.amazon.com/x"
        assert row["last_seen_merchant"] == "amazon.com"

    def test_reclaim_after_cooldown_succeeds(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        watch_id = db.save_watch("+15551234567", "test topic", ["test query"], cooldown_hours=4)
        old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        conn = db._conn()
        cur = conn.cursor()
        cur.execute(f"UPDATE watches SET last_alerted = {db.PH} WHERE id = {db.PH}", (old, watch_id))
        conn.commit()
        conn.close()
        assert db.claim_watch_alert(watch_id, 4) is True


class TestClaimPriceWatchAlert:
    def test_fresh_row_claims(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        watch_id = db.save_price_watch("+15551234567", "test product", cooldown_hours=12)
        assert db.claim_price_watch_alert(watch_id, 12) is True

    def test_immediate_reclaim_fails(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        watch_id = db.save_price_watch("+15551234567", "test product", cooldown_hours=12)
        assert db.claim_price_watch_alert(watch_id, 12) is True
        assert db.claim_price_watch_alert(watch_id, 12) is False

    def test_release_allows_reclaim(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        watch_id = db.save_price_watch("+15551234567", "test product", cooldown_hours=12)
        assert db.claim_price_watch_alert(watch_id, 12) is True
        db.release_price_watch_claim(watch_id)
        assert db.claim_price_watch_alert(watch_id, 12) is True
