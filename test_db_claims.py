from dotenv import load_dotenv
load_dotenv()

from datetime import datetime, timedelta, timezone

import db


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test_claims.db")
    db.init_db()


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
