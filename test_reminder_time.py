"""The clock the model is given, and what happens to the time it hands back.

Palmer used to be told the dyno's clock and the dyno's date, then asked to work
the user's timezone out from the city string in their profile. Two conversions
per reminder, in the model's head, off an anchor that is simply the wrong day
for every user west of UTC from 5pm local onward. At 17:42 on Sunday in Los
Angeles the server already reads Monday, so "remind me tomorrow at 9" files for
Tuesday. The model was not making a mistake; it was told the wrong day.

These tests hold both halves of the fix: the prompt states the user's own day,
and the server vets what comes back rather than trusting the string.
"""
from datetime import datetime, timedelta, timezone, date
from unittest.mock import patch

import pytest

import agent
import db
import timeutil


PHONE = "+15550001111"

# 2026-08-31T00:42Z is 2026-08-30 17:42 in Los Angeles — the exact window where
# the server's date and the user's date disagree.
ROLLOVER = datetime(2026, 8, 31, 0, 42, tzinfo=timezone.utc)


class TestValidZone:
    def test_a_real_zone_resolves(self):
        assert timeutil.valid_zone("America/Los_Angeles") == "America/Los_Angeles"

    def test_junk_is_rejected_rather_than_passed_through(self):
        assert timeutil.valid_zone("Pacific Time") is None
        assert timeutil.valid_zone("US/Notreal") is None

    def test_empty_is_none(self):
        assert timeutil.valid_zone(None) is None
        assert timeutil.valid_zone("") is None


class TestClockBlock:
    def test_it_states_the_users_day_not_the_servers(self):
        out = timeutil.clock_block("America/Los_Angeles", now=ROLLOVER)
        # The whole point: the server has already rolled over to Monday the
        # 31st while the reader is still on Sunday the 30th.
        assert "Sunday, August 30, 2026" in out   # their today
        assert "Monday August 31" in out          # their tomorrow
        assert "Monday, August 31" in out         # the server's day, labelled
        assert "America/Los_Angeles" in out

    def test_the_users_today_leads_and_the_server_is_an_aside(self):
        out = timeutil.clock_block("America/Los_Angeles", now=ROLLOVER)
        assert out.index("Their local time") < out.index("Server clock")
        assert "for your reference only" in out

    def test_the_local_hour_is_local(self):
        out = timeutil.clock_block("America/Los_Angeles", now=ROLLOVER)
        assert "17:42" in out

    def test_no_zone_asserts_no_local_date(self):
        out = timeutil.clock_block(None, now=ROLLOVER)
        assert "don't know this person's timezone" in out
        assert "Do not state or assume a local date" in out
        # The failure being prevented: presenting the server's day as theirs.
        assert "Their local time" not in out

    def test_an_unresolvable_zone_degrades_to_the_no_zone_form(self):
        out = timeutil.clock_block("Pacific Time", now=ROLLOVER)
        assert "don't know this person's timezone" in out

    def test_it_never_raises(self):
        for bad in (None, "", "junk", 17, object()):
            timeutil.clock_block(bad, now=ROLLOVER)


class TestTheClockReachesTheSystemPrompt:
    def _build(self, profile: dict) -> str:
        with patch.object(agent, "get_profile", return_value=profile), \
             patch.object(agent, "get_user_watches", return_value=[]), \
             patch.object(agent, "get_user_price_watches", return_value=[]):
            return agent._build_system(PHONE)

    def test_the_users_zone_is_named(self):
        out = self._build({"name": "Ada", "timezone": "America/Chicago"})
        assert "America/Chicago" in out
        assert "RIGHT NOW, WHERE THEY ARE" in out

    def test_a_profile_without_a_zone_gets_the_honest_form(self):
        out = self._build({"name": "Ada"})
        assert "don't know this person's timezone" in out

    def test_base_system_renders_without_a_profile(self):
        out = agent.base_system()
        assert "RIGHT NOW" in out
        assert "{" not in out.split("RIGHT NOW")[1][:200]


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test_reminder_time.db")
    db.init_db()


class TestNormalizeDueAt:
    """The write-path vetting. `due_at` is TEXT and claim_due_reminders orders it
    lexicographically, so a wrong shape is not a cosmetic problem — it fires the
    reminder at the wrong hour."""

    def _norm(self, raw, tz="America/Chicago", now=None):
        with patch.object(agent, "get_profile", return_value={"timezone": tz}), \
             patch("agent.datetime") as dt:
            dt.now.return_value = now or datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
            dt.side_effect = datetime
            return agent._normalize_due_at(PHONE, raw)

    def test_a_z_suffix_becomes_the_canonical_offset_form(self):
        due, label, err = self._norm("2026-08-31T20:00:00Z")
        assert err is None
        assert due == "2026-08-31T20:00:00+00:00"

    def test_a_non_utc_offset_is_corrected_rather_than_read_as_utc(self):
        # This is the five-hours-early bug. 09:00-05:00 is 14:00Z, and the old
        # string compare read the literal "09:00" as though it were UTC.
        due, label, err = self._norm("2026-08-31T09:00:00-05:00")
        assert err is None
        assert due == "2026-08-31T14:00:00+00:00"

    def test_a_naive_string_is_read_as_the_users_local_clock(self):
        # A model that drops the offset was thinking in the user's day. Reading
        # it as UTC would move the reminder by the whole offset.
        due, label, err = self._norm("2026-08-31T09:00")
        assert err is None
        assert due == "2026-08-31T14:00:00+00:00"   # 9am Chicago in August = CDT

    def test_a_naive_string_falls_back_to_utc_with_no_zone_on_file(self):
        due, label, err = self._norm("2026-08-31T09:00", tz=None)
        assert err is None
        assert due == "2026-08-31T09:00:00+00:00"
        assert "UTC" in label

    def test_a_past_time_is_refused_with_something_actionable(self):
        due, label, err = self._norm("2026-08-29T09:00:00Z")
        assert due is None
        assert "already past" in err
        assert "RIGHT NOW" in err        # tells the model where to look

    def test_an_unreadable_time_is_refused(self):
        due, label, err = self._norm("next thursday-ish")
        assert due is None
        assert err

    def test_absurdly_far_out_is_refused(self):
        due, label, err = self._norm("2099-01-01T09:00:00Z")
        assert due is None
        assert err

    def test_the_label_is_local_and_carries_no_utc(self):
        due, label, err = self._norm("2026-08-31T20:00:00Z")
        assert "3:00 PM" in label        # 20:00Z is 3pm CDT
        assert "UTC" not in label


class TestTheDispatchEchoesLocalTime:
    def test_a_saved_reminder_reports_the_local_time(self, tmp_path, monkeypatch):
        _fresh(tmp_path, monkeypatch)
        with patch.object(agent, "get_profile", return_value={"timezone": "America/Chicago"}):
            due, label, err = agent._normalize_due_at(
                PHONE, (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        assert err is None
        # The old result echoed the raw UTC string, which made the model convert
        # a second time for the half the user actually reads.
        assert "+00:00" not in label


class TestStoredRowsAreCanonical:
    def test_save_reminder_rewrites_the_offset(self, tmp_path, monkeypatch):
        _fresh(tmp_path, monkeypatch)
        db.save_reminder(PHONE, "call mom", "2026-08-31T09:00:00-05:00")
        rows = db.get_pending_reminders(PHONE) if hasattr(db, "get_pending_reminders") else None
        conn = db._conn()
        cur = conn.cursor()
        cur.execute(f"SELECT due_at FROM reminders WHERE phone = {db.PH}", (PHONE,))
        stored = cur.fetchone()["due_at"]
        conn.close()
        assert stored == "2026-08-31T14:00:00+00:00"

    def test_an_unparseable_due_at_is_not_stored(self, tmp_path, monkeypatch):
        _fresh(tmp_path, monkeypatch)
        db.save_reminder(PHONE, "call mom", "whenever")
        conn = db._conn()
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) AS c FROM reminders WHERE phone = {db.PH}", (PHONE,))
        assert cur.fetchone()["c"] == 0
        conn.close()

    def test_normalize_repairs_a_legacy_row_and_is_idempotent(self, tmp_path, monkeypatch):
        _fresh(tmp_path, monkeypatch)
        conn = db._conn()
        cur = conn.cursor()
        # Write past save_reminder, the way rows were written before it normalized.
        cur.execute(f"INSERT INTO reminders (phone, text, due_at) VALUES ({db.PH}, {db.PH}, {db.PH})",
                    (PHONE, "old row", "2026-08-31T09:00:00-05:00"))
        conn.commit()
        conn.close()
        assert db.normalize_due_at_rows() == 1
        assert db.normalize_due_at_rows() == 0

    def test_a_corrected_row_no_longer_fires_early(self, tmp_path, monkeypatch):
        _fresh(tmp_path, monkeypatch)
        # 09:00-05:00 is 14:00Z. Lexicographically "09:00" sorts below a 12:00Z
        # "now", so before normalization this row was claimed five hours early.
        conn = db._conn()
        cur = conn.cursor()
        cur.execute(f"INSERT INTO reminders (phone, text, due_at) VALUES ({db.PH}, {db.PH}, {db.PH})",
                    (PHONE, "too early", "2999-01-01T09:00:00-05:00"))
        conn.commit()
        conn.close()
        db.normalize_due_at_rows()
        assert db.claim_due_reminders() == []


class TestTheWeekIsATableToReadNotArithmetic:
    """clock_block named "today" and "tomorrow" and emitted no ISO date at all,
    so anything past tomorrow — "next Friday", "the 15th", "a week Tuesday" —
    was the model rebuilding a date from the prose "Friday, September 04,
    2026" and counting forward in its head. That is the one computation on the
    reminder path nothing checks: _normalize_due_at catches an unreadable
    string, a past time and a date over a year out, but a plausible wrong
    Friday sails through and reads correctly in the confirmation.
    """

    def _block(self):
        return timeutil.clock_block("America/Los_Angeles", now=ROLLOVER)

    def test_todays_iso_date_is_there_to_be_copied(self):
        assert "Sun 2026-08-30 (today)" in self._block()

    def test_the_whole_week_is_dated(self):
        out = self._block()
        for day in ("Mon 2026-08-31", "Fri 2026-09-04", "Sat 2026-09-05"):
            assert day in out

    def test_the_run_is_anchored_on_their_day_not_the_servers(self):
        """The server has already rolled to Monday the 31st."""
        out = self._block()
        assert out.index("Sun 2026-08-30") < out.index("Mon 2026-08-31")
        assert "Mon 2026-08-30" not in out

    def test_todays_weekday_appears_twice(self):
        """A bare weekday naming today means the one a week out — that rule
        needs a second Sunday in the list to point at."""
        out = self._block()
        assert out.count("2026-08-30") == 1 and "Sun 2026-09-06" in out

    def test_the_no_zone_form_labels_them_as_the_servers(self):
        out = timeutil.clock_block(None, now=ROLLOVER)
        assert "Server dates" in out
        assert "Their local time" not in out
        # The refusal is still the last word, which is the whole point of the branch.
        assert "Do not state or assume a local date" in out

    def test_the_block_stays_small(self):
        """It ships on every single turn, so its size is a property worth
        holding, not a comment."""
        assert len(self._block()) < 650


class TestOneAnswerForWhatNextFridayMeans:
    def test_the_convention_lives_in_timeutil(self):
        """It was in weather.py, so the only path where the MODEL computes a
        date — reminders — had no answer for "next friday" while the weather
        path had a considered one. The same user could get both in one thread."""
        from timeutil import resolve_day_delta
        assert not hasattr(__import__("weather"), "_resolve_day_delta")
        with patch.object(timeutil, "local_today", return_value=date(2026, 8, 26)):
            assert resolve_day_delta("friday", "friday") == 2        # this Friday
            assert resolve_day_delta("next friday", "next friday") == 9

    def test_the_prompt_states_it_where_the_model_computes_the_date(self):
        block = agent.SYSTEM_PROMPT.split("REMINDERS")[1].split("MORNING BRIEFING")[0]
        assert "AFTER this coming one" in block
        assert "read the date off" in block

    def test_the_prompt_says_what_to_do_with_an_unsupported_repeat(self):
        """The write path refuses it; the model needs to know what to offer."""
        block = agent.SYSTEM_PROMPT.split("REMINDERS")[1].split("MORNING BRIEFING")[0]
        assert "every other Tuesday" in block
        assert "won't repeat" in block

    def test_the_due_at_field_points_at_the_block(self):
        from tools_def import TOOLS
        schema = next(t for t in TOOLS if t["name"] == "set_reminder")
        assert "RIGHT NOW" in schema["input_schema"]["properties"]["due_at"]["description"]
