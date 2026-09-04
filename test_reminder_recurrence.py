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
        # save_reminder canonicalizes on write: one shape in the column, because
        # claim_due_reminders orders it lexicographically.
        assert claimed[0]["due_at"] == "2026-08-11T20:00:00+00:00"


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


class TestTheWritePathRefusesARecurrenceItCannotKeep:
    """next_occurrence returning None guards the SEND path. Nothing guarded the
    write path, and that is where the damage was done.

    A tool-use `enum` is guidance to the model, not a constraint the API
    enforces. So "monthly" reached db.save_reminder intact, the dispatch
    confirmed "It repeats (monthly) at that same local time until they cancel",
    and then the send path quietly declined to re-arm it. The user was told it
    repeats, got exactly one text, and had no way to know it had stopped —
    which is the outcome SYSTEM_PROMPT and the set_reminder description both
    name as the thing never to let happen.
    """

    def _set_reminder(self, recurrence):
        """Run one set_reminder tool call and return (result string, saved rows)."""
        from unittest.mock import MagicMock
        import agent

        saved = []
        block = MagicMock()
        block.type = "tool_use"
        block.name = "set_reminder"
        block.id = "tu_1"
        # Comfortably future but inside _normalize_due_at's 400-day bound, so
        # this exercises the recurrence check and not the date check.
        due = (datetime.now(timezone.utc) + timedelta(days=30)).replace(
            microsecond=0).isoformat()
        block.input = {"text": "take the bins out",
                       "due_at": due,
                       "recurrence": recurrence}

        calls = []

        def _create(**kw):
            calls.append(kw)
            if len(calls) == 1:
                return MagicMock(stop_reason="tool_use", content=[block])
            t = MagicMock(type="text", text="done")
            return MagicMock(stop_reason="end_turn", content=[t])

        with patch.object(agent.client.messages, "create", side_effect=_create), \
             patch.object(agent, "_build_system", return_value="sys"), \
             patch.object(agent, "get_profile", return_value={"timezone": "America/Chicago"}), \
             patch.object(agent, "get_history", return_value=[]), \
             patch.object(agent, "save_reminder",
                          side_effect=lambda *a, **k: saved.append(a)):
            agent.get_reply("+15550001111", "remind me", history=[])

        results = [c for m in calls[-1]["messages"] for c in (m.get("content") or [])
                   if isinstance(c, dict) and c.get("type") == "tool_result"]
        return results[0]["content"], saved

    def test_an_unsupported_recurrence_saves_nothing(self):
        result, saved = self._set_reminder("monthly")
        assert saved == [], "a repeat that can never re-arm must not be stored"
        assert "Didn't save" in result

    def test_the_model_is_told_what_it_can_use_instead(self):
        """A refusal with no alternative is how the model ends up apologising."""
        from timeutil import RECURRENCES
        result, _ = self._set_reminder("every other tuesday")
        for supported in RECURRENCES:
            assert supported in result
        assert "one-time reminder" in result

    def test_a_supported_recurrence_still_saves(self):
        _, saved = self._set_reminder("daily")
        assert len(saved) == 1
        assert saved[0][3] == "daily"

    def test_it_normalizes_the_way_the_send_path_does(self):
        """next_occurrence strips and lowercases; what is stored must match."""
        _, saved = self._set_reminder("  Weekdays ")
        assert saved[0][3] == "weekdays"

    def test_every_enum_value_in_the_schema_is_one_the_send_path_keeps(self):
        """The schema and timeutil cannot be allowed to drift apart."""
        from timeutil import RECURRENCES
        from tools_def import TOOLS
        schema = next(t for t in TOOLS if t["name"] == "set_reminder")
        enum = schema["input_schema"]["properties"]["recurrence"]["enum"]
        assert set(enum) == set(RECURRENCES)
