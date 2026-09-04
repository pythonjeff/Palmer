"""What Palmer records must be what Palmer sent.

Four senders got this wrong in two different ways, and both produce the same
symptom the user sees: Palmer referring to a message that never arrived, or
repeating one that did.

watches.py (and the since-retired alerts job) called send_sms and ignored the
result, then saved unconditionally. send_sms returns False on a Twilio failure AND on a
leaks_deliberation block, so history accumulated messages nobody received —
and _build_system feeds history straight back to the model.

shopping.py and flightwatch.py never called save_message at all. So a user
replying "how much?" to a price alert got an answer with no referent, and each
sender's own _is_duplicate_subject check — which reads assistant messages —
could never see its own repeats.
"""
from unittest.mock import patch

import db


PHONE = "+15550001111"


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test_phantom.db")
    db.init_db()


class TestTheKindColumn:
    def test_a_kind_round_trips(self, tmp_path, monkeypatch):
        _fresh(tmp_path, monkeypatch)
        db.save_message(PHONE, "assistant", "morning line", kind="morning")
        conn = db._conn()
        cur = conn.cursor()
        cur.execute(f"SELECT kind FROM messages WHERE phone = {db.PH}", (PHONE,))
        assert cur.fetchone()["kind"] == "morning"
        conn.close()

    def test_kind_is_optional_so_existing_callers_are_unaffected(self, tmp_path, monkeypatch):
        _fresh(tmp_path, monkeypatch)
        db.save_message(PHONE, "user", "hey")
        assert db.get_history(PHONE) == [{"role": "user", "content": "hey"}]

    def test_migration_is_idempotent(self, tmp_path, monkeypatch):
        _fresh(tmp_path, monkeypatch)
        db.init_db()
        db.init_db()
        db.save_message(PHONE, "assistant", "x", kind="alert")
        assert len(db.get_history(PHONE)) == 1


class TestPriceAlertsReachHistory:
    """run_price_watches imports its db helpers inside the function, so they are
    patched at their source rather than on the shopping module."""

    def _run(self, tmp_path, monkeypatch, sent: bool):
        _fresh(tmp_path, monkeypatch)
        import shopping
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
             patch("userprofile._is_duplicate_subject", return_value=False), \
             patch("sms_util.send_sms", return_value=sent):
            shopping.run_price_watches()
        return [m for m in db.get_history(PHONE) if m["role"] == "assistant"]

    def test_a_sent_alert_is_recorded(self, tmp_path, monkeypatch):
        rows = self._run(tmp_path, monkeypatch, sent=True)
        assert any("pegasus" in m["content"].lower() for m in rows), rows

    def test_a_failed_price_alert_is_not_recorded(self, tmp_path, monkeypatch):
        assert self._run(tmp_path, monkeypatch, sent=False) == []

    def test_the_alert_is_visible_to_the_next_duplicate_check(self, tmp_path, monkeypatch):
        """The point of recording it: _is_duplicate_subject reads assistant
        messages, so before this the sender could never see its own repeats."""
        from datetime import datetime, timedelta, timezone
        self._run(tmp_path, monkeypatch, sent=True)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        assert db.get_recent_assistant_messages(PHONE, cutoff)


class TestProactiveSendersNeverShipTheFallbackString:
    def test_no_proactive_sender_calls_ensure_sms(self):
        """ensure_sms's last resort is FALLBACK_SMS. That is right for a reply
        the user is waiting on and wrong for anything unprompted — a failed
        price check texted 'something went sideways on my end, try again' to
        someone who had asked for nothing."""
        import pathlib
        proactive = ["watches.py", "morning.py", "evening.py",
                     "shopping.py", "flightwatch.py", "send_reminders.py"]
        for name in proactive:
            src = pathlib.Path(name).read_text()
            calls = [l for l in src.splitlines()
                     if "ensure_sms(" in l and not l.strip().startswith("#")]
            assert not calls, (name, calls)
