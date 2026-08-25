"""Recurring reminders, and the dedup guard that was wrong in both directions.

Production evidence that motivated all of this — one user asked for a *daily*
update and got four texts in the same minute, then nothing, ever:

    133 | Daily Eagles camp update for the user             | 2026-08-11T20:00:00Z
    134 | Eagles camp update - how the day went             | 2026-08-11T20:00:00Z
    135 | Eagles camp update - how did today's practice go? | 2026-08-11T20:00:00Z
    136 | Eagles camp update - how the day went             | 2026-08-11T20:00:00Z

Two independent defects: reminders had no recurrence, and the duplicate guard
matched exact text while ignoring due_at entirely.
"""
from dotenv import load_dotenv
load_dotenv()

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import db
from timeutil import next_occurrence

CHI = ZoneInfo("America/Chicago")
PHONE = "+15551234567"


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test_recurrence.db")
    db.init_db()


class TestNextOccurrence:
    def test_preserves_local_wall_clock_across_dst_end(self):
        """The reminder is "3pm", not "20:00Z". Chicago leaves CDT on 2026-11-01,
        so holding the UTC instant fixed would walk a 3pm reminder to 2pm and
        leave it there. The UTC instant MUST move for the local time to stay put."""
        due = datetime(2026, 10, 31, 15, 0, tzinfo=CHI)
        nxt = next_occurrence(due, "daily", "America/Chicago", now=due)
        assert nxt.astimezone(CHI).hour == 15
        assert due.astimezone(timezone.utc).hour == 20   # CDT, UTC-5
        assert nxt.astimezone(timezone.utc).hour == 21   # CST, UTC-6 — it moved

    def test_skips_missed_periods_instead_of_bursting(self):
        """due_at <= now has catch-up semantics, so advancing by exactly one
        period after an outage would fire once per missed day on recovery."""
        due = datetime(2026, 8, 10, 15, 0, tzinfo=CHI)
        now = datetime(2026, 8, 13, 18, 0, tzinfo=CHI)   # three days stale
        nxt = next_occurrence(due, "daily", "America/Chicago", now=now)
        assert nxt.astimezone(CHI).date() == datetime(2026, 8, 14).date()
        assert nxt > now

    def test_weekdays_rolls_friday_to_monday(self):
        friday = datetime(2026, 8, 14, 9, 0, tzinfo=CHI)
        assert friday.weekday() == 4
        nxt = next_occurrence(friday, "weekdays", "America/Chicago", now=friday)
        assert nxt.astimezone(CHI).weekday() == 0
        assert nxt.astimezone(CHI).hour == 9

    def test_weekly_keeps_the_weekday(self):
        friday = datetime(2026, 8, 14, 9, 0, tzinfo=CHI)
        nxt = next_occurrence(friday, "weekly", "America/Chicago", now=friday)
        assert nxt.astimezone(CHI).weekday() == 4
        assert (nxt.astimezone(CHI).date() - friday.date()).days == 7

    def test_unknown_recurrence_is_none(self):
        # Guards the send path: an unrecognised value must not re-arm anything.
        due = datetime(2026, 8, 14, 9, 0, tzinfo=CHI)
        for bad in ("hourly", "monthly", "", None, "DAILY-ISH"):
            assert next_occurrence(due, bad, "America/Chicago", now=due) is None

    def test_recurrence_is_case_and_space_tolerant(self):
        due = datetime(2026, 8, 14, 9, 0, tzinfo=CHI)
        assert next_occurrence(due, " Daily ", "America/Chicago", now=due) is not None

    def test_missing_timezone_falls_back_to_utc(self):
        due = datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc)
        nxt = next_occurrence(due, "daily", None, now=due)
        assert nxt.hour == 9


class TestDedupGuard:
    """Same time AND similar text. Either condition alone gets it wrong."""

    EAGLES = [
        "Daily Eagles camp update for the user",
        "Eagles camp update — how the day went",
        "Eagles camp update - how did today's practice go?",
        "Eagles camp update - how the day went",
    ]

    def test_the_four_eagles_rows_collapse_to_one(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        for text in self.EAGLES:
            db.save_reminder(PHONE, text, "2026-08-11T20:00:00Z")
        rows = _pending(PHONE)
        assert len(rows) == 1, [r["text"] for r in rows]

    def test_em_dash_vs_hyphen_is_not_a_new_reminder(self, tmp_path, monkeypatch):
        # Rows 134 and 136 differed by exactly this and both got stored.
        _fresh_db(tmp_path, monkeypatch)
        db.save_reminder(PHONE, "Eagles camp update — how the day went", "2026-08-11T20:00:00Z")
        db.save_reminder(PHONE, "Eagles camp update - how the day went", "2026-08-11T20:00:00Z")
        assert len(_pending(PHONE)) == 1

    def test_same_text_at_a_different_time_is_kept(self, tmp_path, monkeypatch):
        """The old guard ignored due_at, so this silently dropped the second one."""
        _fresh_db(tmp_path, monkeypatch)
        db.save_reminder(PHONE, "Call mom", "2026-08-11T20:00:00Z")
        db.save_reminder(PHONE, "Call mom", "2026-08-18T20:00:00Z")
        assert len(_pending(PHONE)) == 2

    def test_different_errands_at_the_same_time_both_survive(self, tmp_path, monkeypatch):
        # Time alone would have merged these.
        _fresh_db(tmp_path, monkeypatch)
        db.save_reminder(PHONE, "Call your mom", "2026-08-11T20:00:00Z")
        db.save_reminder(PHONE, "Take your meds", "2026-08-11T20:00:00Z")
        assert len(_pending(PHONE)) == 2

    def test_offset_and_z_forms_compare_equal(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        db.save_reminder(PHONE, "Move the car", "2026-08-11T20:00:00Z")
        db.save_reminder(PHONE, "Move the car", "2026-08-11T20:00:00+00:00")
        assert len(_pending(PHONE)) == 1


class TestCancelAndRearm:
    def test_cancel_clears_recurrence_so_rearm_cannot_resurrect(self, tmp_path, monkeypatch):
        """Claiming and cancelling both set sent = 1, so they're indistinguishable
        by `sent` alone. cancel_reminders also nulls recurrence, which is what
        makes a cancel landing between claim and re-arm stick."""
        _fresh_db(tmp_path, monkeypatch)
        db.save_reminder(PHONE, "Take your meds", "2026-08-11T20:00:00Z", "daily")
        rid = _pending(PHONE)[0]["id"]

        db.cancel_reminders(PHONE)
        assert db.rearm_reminder(rid, "2026-08-12T20:00:00Z") is False
        assert _pending(PHONE) == []

    def test_rearm_makes_a_recurring_reminder_pending_again(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        db.save_reminder(PHONE, "Take your meds", "2026-08-11T20:00:00Z", "daily")
        rid = _pending(PHONE)[0]["id"]
        db.claim_due_reminders()
        assert _pending(PHONE) == []
        assert db.rearm_reminder(rid, "2026-08-12T20:00:00Z") is True
        assert len(_pending(PHONE)) == 1

    def test_one_shot_reminders_are_never_rearmed(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        db.save_reminder(PHONE, "Confirm lunch", "2026-08-11T20:00:00Z")
        rid = _pending(PHONE)[0]["id"]
        db.claim_due_reminders()
        assert db.rearm_reminder(rid, "2026-08-12T20:00:00Z") is False

    def test_claim_carries_recurrence_and_due_at(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        db.save_reminder(PHONE, "Take your meds", "2026-08-11T20:00:00Z", "daily")
        claimed = db.claim_due_reminders()
        assert claimed[0]["recurrence"] == "daily"
        assert claimed[0]["due_at"] == "2026-08-11T20:00:00Z"


class TestSendLoop:
    """End to end through send_due_reminders with Twilio and the drafter stubbed."""

    def _run(self, profile=None):
        import send_reminders
        sent = []
        with patch("sms_util.send_sms", side_effect=lambda p, b, **k: sent.append((p, b)) or True), \
             patch("send_reminders._personalize_reminder", side_effect=lambda p, t, pr: t), \
             patch("send_reminders.get_profile", return_value=profile or {"timezone": "America/Chicago"}), \
             patch("send_reminders.save_message"):
            send_reminders.send_due_reminders()
        return sent

    def test_recurring_reminder_sends_then_rearms_into_the_future(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        db.save_reminder(PHONE, "Take your meds", past, "daily")

        sent = self._run()
        assert len(sent) == 1

        pending = _pending(PHONE)
        assert len(pending) == 1, "recurring reminder should be pending again"
        assert db._parse_due(pending[0]["due_at"]) > datetime.now(timezone.utc)

    def test_one_shot_reminder_does_not_come_back(self, tmp_path, monkeypatch):
        _fresh_db(tmp_path, monkeypatch)
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        db.save_reminder(PHONE, "Confirm lunch with grandpa", past)
        assert len(self._run()) == 1
        assert _pending(PHONE) == []

    def test_near_identical_reminders_due_together_send_once(self, tmp_path, monkeypatch):
        """The observed failure. These reach the send loop only if they were
        written before the dedup guard existed, so the guard is scoped to the
        tick rather than to _is_duplicate_subject — a reminder is explicitly
        requested, and suppressing one for topical overlap hours later would be
        worse than the duplicate it prevents."""
        _fresh_db(tmp_path, monkeypatch)
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        for text in TestDedupGuard.EAGLES:
            _force_insert(PHONE, text, past)
        assert len(_pending(PHONE)) == 4
        assert len(self._run()) == 1

    def test_a_failed_send_still_rearms(self, tmp_path, monkeypatch):
        """The claim already consumed this occurrence, so bailing on a Twilio
        hiccup would silently end a standing reminder."""
        _fresh_db(tmp_path, monkeypatch)
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        db.save_reminder(PHONE, "Take your meds", past, "daily")

        import send_reminders
        with patch("sms_util.send_sms", return_value=False), \
             patch("send_reminders._personalize_reminder", side_effect=lambda p, t, pr: t), \
             patch("send_reminders.get_profile", return_value={"timezone": "America/Chicago"}), \
             patch("send_reminders.save_message"):
            send_reminders.send_due_reminders()

        assert len(_pending(PHONE)) == 1


# --- helpers -----------------------------------------------------------------

def _pending(phone):
    conn = db._conn()
    cur = conn.cursor()
    cur.execute(f"SELECT id, text, due_at, recurrence FROM reminders WHERE phone = {db.PH} AND sent = 0", (phone,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def _force_insert(phone, text, due_at):
    """Bypass save_reminder's guard, to recreate rows written before it existed."""
    conn = db._conn()
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO reminders (phone, text, due_at) VALUES ({db.PH}, {db.PH}, {db.PH})",
        (phone, text, due_at),
    )
    conn.commit()
    conn.close()
