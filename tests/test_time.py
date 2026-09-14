"""Clocks, dates, reminders and recurrence.

Merged from test_timeutil.py, test_reminder_time.py, test_reminder_recurrence.py, test_clock_correctness.py; each section keeps its
original file's notes, because those carry the incident that led to the test.
"""
import importlib
from datetime import date, datetime, timezone, timedelta
from unittest.mock import patch
from palmer import timeutil, agent, db, datafeeds, userprofile, weather
from zoneinfo import ZoneInfo
from palmer.timeutil import next_occurrence


# ============================================================================
# from test_timeutil.py
# ============================================================================
#
# Tests for user-local date/time helpers.
#
# Palmer's dyno runs UTC but almost everything user-facing needs the user's
# local calendar day. These tests lock in the semantics (real tz honored, no-tz
# falls back to UTC, unknown tz doesn't raise) and prove the late-night west-
# coast off-by-one is actually fixed.

class TestLocalNow:
    def test_returns_tz_aware_datetime_for_valid_tz(self):
        out = timeutil.local_now("America/Los_Angeles")
        assert out.tzinfo is not None
        # tz name should be present on the returned aware dt
        assert "Los_Angeles" in str(out.tzinfo)

    def test_none_tz_falls_back_to_utc(self):
        out = timeutil.local_now(None)
        assert out.tzinfo == timezone.utc

    def test_empty_tz_falls_back_to_utc(self):
        assert timeutil.local_now("").tzinfo == timezone.utc

    def test_bogus_tz_falls_back_to_utc_without_raising(self):
        assert timeutil.local_now("Not/A/Real_Zone").tzinfo == timezone.utc


class TestLocalToday:
    def test_returns_date_type(self):
        assert isinstance(timeutil.local_today("America/Chicago"), date)

    def test_none_tz_uses_utc_date(self):
        # Freeze datetime.now so we can compare exactly
        with patch("palmer.timeutil.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: datetime(2026, 8, 18, 20, 0, tzinfo=tz or timezone.utc)
            assert timeutil.local_today(None) == date(2026, 8, 18)

    def test_west_coast_late_evening_stays_on_yesterday_local(self):
        """The bug this whole exercise was for: at 22:00 America/Los_Angeles
        on Aug 17 the server is already Aug 18 UTC, so anything using
        date.today() sees Aug 18. local_today('America/Los_Angeles') must
        still see Aug 17."""
        # Fake datetime.now so it returns Aug 18 05:00 UTC (=Aug 17 22:00 PDT)
        with patch("palmer.timeutil.datetime") as mock_dt:
            def _now(tz=None):
                base = datetime(2026, 8, 18, 5, 0, tzinfo=timezone.utc)
                return base.astimezone(tz) if tz else base
            mock_dt.now.side_effect = _now
            assert timeutil.local_today("America/Los_Angeles") == date(2026, 8, 17)
            assert timeutil.local_today(None) == date(2026, 8, 18)  # UTC still Aug 18


class TestResolveDayDeltaHonorsTz:
    """The end-to-end bug: at 22:00 PDT Thursday, someone asking 'weather
    Friday' should get delta=1 (tomorrow), not delta=8 (next Friday)."""

    def test_friday_at_thursday_night_local_is_tomorrow(self):
        from palmer.timeutil import resolve_day_delta
        # Freeze both timeutil.local_today and weather._date.today so the module
        # under test sees a consistent 'now'.
        with patch("palmer.timeutil.datetime") as mock_dt:
            def _now(tz=None):
                # Aug 21 2026 is a Friday. So 05:00 UTC on Aug 21 = 22:00 PDT on Aug 20 (Thursday).
                base = datetime(2026, 8, 21, 5, 0, tzinfo=timezone.utc)
                return base.astimezone(tz) if tz else base
            mock_dt.now.side_effect = _now

            # UTC-only path (no tz): 'friday' seen from Friday should give 7 (next friday)
            assert resolve_day_delta("friday", "friday", tz=None) == 7
            # With user tz PDT (still Thursday locally): 'friday' should give 1 (tomorrow)
            assert resolve_day_delta("friday", "friday", tz="America/Los_Angeles") == 1

    def test_tomorrow_always_one_day_out(self):
        from palmer.timeutil import resolve_day_delta
        assert resolve_day_delta("tomorrow", "tomorrow", tz="America/Chicago") == 1
        assert resolve_day_delta("tomorrow", "tomorrow", tz=None) == 1


# ============================================================================
# from test_reminder_time.py
# ============================================================================
#
# The clock the model is given, and what happens to the time it hands back.
#
# Palmer used to be told the dyno's clock and the dyno's date, then asked to work
# the user's timezone out from the city string in their profile. Two conversions
# per reminder, in the model's head, off an anchor that is simply the wrong day
# for every user west of UTC from 5pm local onward. At 17:42 on Sunday in Los
# Angeles the server already reads Monday, so "remind me tomorrow at 9" files for
# Tuesday. The model was not making a mistake; it was told the wrong day.
#
# These tests hold both halves of the fix: the prompt states the user's own day,
# and the server vets what comes back rather than trusting the string.

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


class TestNormalizeDueAt:
    """The write-path vetting. `due_at` is TEXT and claim_due_reminders orders it
    lexicographically, so a wrong shape is not a cosmetic problem — it fires the
    reminder at the wrong hour."""

    def _norm(self, raw, tz="America/Chicago", now=None):
        with patch.object(agent, "get_profile", return_value={"timezone": tz}), \
             patch("palmer.agent.datetime") as dt:
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
    def test_a_saved_reminder_reports_the_local_time(self, fresh_db):
        with patch.object(agent, "get_profile", return_value={"timezone": "America/Chicago"}):
            due, label, err = agent._normalize_due_at(
                PHONE, (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        assert err is None
        # The old result echoed the raw UTC string, which made the model convert
        # a second time for the half the user actually reads.
        assert "+00:00" not in label


class TestStoredRowsAreCanonical:
    def test_save_reminder_rewrites_the_offset(self, fresh_db):
        db.save_reminder(PHONE, "call mom", "2026-08-31T09:00:00-05:00")
        conn = db._conn()
        cur = conn.cursor()
        cur.execute(f"SELECT due_at FROM reminders WHERE phone = {db.PH}", (PHONE,))
        stored = cur.fetchone()["due_at"]
        conn.close()
        assert stored == "2026-08-31T14:00:00+00:00"

    def test_an_unparseable_due_at_is_not_stored(self, fresh_db):
        db.save_reminder(PHONE, "call mom", "whenever")
        conn = db._conn()
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) AS c FROM reminders WHERE phone = {db.PH}", (PHONE,))
        assert cur.fetchone()["c"] == 0
        conn.close()

    def test_normalize_repairs_a_legacy_row_and_is_idempotent(self, fresh_db):
        conn = db._conn()
        cur = conn.cursor()
        # Write past save_reminder, the way rows were written before it normalized.
        cur.execute(f"INSERT INTO reminders (phone, text, due_at) VALUES ({db.PH}, {db.PH}, {db.PH})",
                    (PHONE, "old row", "2026-08-31T09:00:00-05:00"))
        conn.commit()
        conn.close()
        assert db.normalize_due_at_rows() == 1
        assert db.normalize_due_at_rows() == 0

    def test_a_corrected_row_no_longer_fires_early(self, fresh_db):
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
        from palmer.timeutil import resolve_day_delta
        assert not hasattr(importlib.import_module("palmer.weather"), "_resolve_day_delta")
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
        from palmer.tools_def import TOOLS
        schema = next(t for t in TOOLS if t["name"] == "set_reminder")
        assert "RIGHT NOW" in schema["input_schema"]["properties"]["due_at"]["description"]


# ============================================================================
# from test_reminder_recurrence.py
# ============================================================================
#
# Recurring reminders, and the dedup guard that was wrong in both directions.
#
# Production evidence that motivated all of this — one user asked for a *daily*
# update and got four texts in the same minute, then nothing, ever:
#
#     133 | Daily Eagles camp update for the user             | 2026-08-11T20:00:00Z
#     134 | Eagles camp update - how the day went             | 2026-08-11T20:00:00Z
#     135 | Eagles camp update - how did today's practice go? | 2026-08-11T20:00:00Z
#     136 | Eagles camp update - how the day went             | 2026-08-11T20:00:00Z
#
# Two independent defects: reminders had no recurrence, and the duplicate guard
# matched exact text while ignoring due_at entirely.

CHI = ZoneInfo("America/Chicago")


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

    def test_the_four_eagles_rows_collapse_to_one(self, fresh_db):
        for text in self.EAGLES:
            db.save_reminder(PHONE, text, "2026-08-11T20:00:00Z")
        rows = _pending(PHONE)
        assert len(rows) == 1, [r["text"] for r in rows]

    def test_em_dash_vs_hyphen_is_not_a_new_reminder(self, fresh_db):
        # Rows 134 and 136 differed by exactly this and both got stored.
        db.save_reminder(PHONE, "Eagles camp update — how the day went", "2026-08-11T20:00:00Z")
        db.save_reminder(PHONE, "Eagles camp update - how the day went", "2026-08-11T20:00:00Z")
        assert len(_pending(PHONE)) == 1

    def test_same_text_at_a_different_time_is_kept(self, fresh_db):
        """The old guard ignored due_at, so this silently dropped the second one."""
        db.save_reminder(PHONE, "Call mom", "2026-08-11T20:00:00Z")
        db.save_reminder(PHONE, "Call mom", "2026-08-18T20:00:00Z")
        assert len(_pending(PHONE)) == 2

    def test_different_errands_at_the_same_time_both_survive(self, fresh_db):
        # Time alone would have merged these.
        db.save_reminder(PHONE, "Call your mom", "2026-08-11T20:00:00Z")
        db.save_reminder(PHONE, "Take your meds", "2026-08-11T20:00:00Z")
        assert len(_pending(PHONE)) == 2

    def test_offset_and_z_forms_compare_equal(self, fresh_db):
        db.save_reminder(PHONE, "Move the car", "2026-08-11T20:00:00Z")
        db.save_reminder(PHONE, "Move the car", "2026-08-11T20:00:00+00:00")
        assert len(_pending(PHONE)) == 1


class TestCancelAndRearm:
    def test_cancel_clears_recurrence_so_rearm_cannot_resurrect(self, fresh_db):
        """Claiming and cancelling both set sent = 1, so they're indistinguishable
        by `sent` alone. cancel_reminders also nulls recurrence, which is what
        makes a cancel landing between claim and re-arm stick."""
        db.save_reminder(PHONE, "Take your meds", "2026-08-11T20:00:00Z", "daily")
        rid = _pending(PHONE)[0]["id"]

        db.cancel_reminders(PHONE)
        assert db.rearm_reminder(rid, "2026-08-12T20:00:00Z") is False
        assert _pending(PHONE) == []

    def test_rearm_makes_a_recurring_reminder_pending_again(self, fresh_db):
        db.save_reminder(PHONE, "Take your meds", "2026-08-11T20:00:00Z", "daily")
        rid = _pending(PHONE)[0]["id"]
        db.claim_due_reminders()
        assert _pending(PHONE) == []
        assert db.rearm_reminder(rid, "2026-08-12T20:00:00Z") is True
        assert len(_pending(PHONE)) == 1

    def test_one_shot_reminders_are_never_rearmed(self, fresh_db):
        db.save_reminder(PHONE, "Confirm lunch", "2026-08-11T20:00:00Z")
        rid = _pending(PHONE)[0]["id"]
        db.claim_due_reminders()
        assert db.rearm_reminder(rid, "2026-08-12T20:00:00Z") is False

    def test_claim_carries_recurrence_and_due_at(self, fresh_db):
        db.save_reminder(PHONE, "Take your meds", "2026-08-11T20:00:00Z", "daily")
        claimed = db.claim_due_reminders()
        assert claimed[0]["recurrence"] == "daily"
        # save_reminder canonicalizes on write: one shape in the column, because
        # claim_due_reminders orders it lexicographically.
        assert claimed[0]["due_at"] == "2026-08-11T20:00:00+00:00"


class TestSendLoop:
    """End to end through send_due_reminders with Twilio and the drafter stubbed."""

    def _run(self, profile=None):
        from palmer import reminders
        sent = []
        with patch("palmer.sms_util.send_sms", side_effect=lambda p, b, **k: sent.append((p, b)) or True), \
             patch("palmer.reminders._personalize_reminder", side_effect=lambda p, t, pr: t), \
             patch("palmer.reminders.get_profile", return_value=profile or {"timezone": "America/Chicago"}), \
             patch("palmer.reminders.save_message"):
            reminders.send_due_reminders()
        return sent

    def test_recurring_reminder_sends_then_rearms_into_the_future(self, fresh_db):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        db.save_reminder(PHONE, "Take your meds", past, "daily")

        sent = self._run()
        assert len(sent) == 1

        pending = _pending(PHONE)
        assert len(pending) == 1, "recurring reminder should be pending again"
        assert db._parse_due(pending[0]["due_at"]) > datetime.now(timezone.utc)

    def test_one_shot_reminder_does_not_come_back(self, fresh_db):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        db.save_reminder(PHONE, "Confirm lunch with grandpa", past)
        assert len(self._run()) == 1
        assert _pending(PHONE) == []

    def test_near_identical_reminders_due_together_send_once(self, fresh_db):
        """The observed failure. These reach the send loop only if they were
        written before the dedup guard existed, so the guard is scoped to the
        tick rather than to _is_duplicate_subject — a reminder is explicitly
        requested, and suppressing one for topical overlap hours later would be
        worse than the duplicate it prevents."""
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        for text in TestDedupGuard.EAGLES:
            _force_insert(PHONE, text, past)
        assert len(_pending(PHONE)) == 4
        assert len(self._run()) == 1

    def test_a_failed_send_still_rearms(self, fresh_db):
        """The claim already consumed this occurrence, so bailing on a Twilio
        hiccup would silently end a standing reminder."""
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        db.save_reminder(PHONE, "Take your meds", past, "daily")

        from palmer import reminders
        with patch("palmer.sms_util.send_sms", return_value=False), \
             patch("palmer.reminders._personalize_reminder", side_effect=lambda p, t, pr: t), \
             patch("palmer.reminders.get_profile", return_value={"timezone": "America/Chicago"}), \
             patch("palmer.reminders.save_message"):
            reminders.send_due_reminders()

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
        from palmer import agent

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
        from palmer.timeutil import RECURRENCES
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
        from palmer.timeutil import RECURRENCES
        from palmer.tools_def import TOOLS
        schema = next(t for t in TOOLS if t["name"] == "set_reminder")
        enum = schema["input_schema"]["properties"]["recurrence"]["enum"]
        assert set(enum) == set(RECURRENCES)


# ============================================================================
# from test_clock_correctness.py
# ============================================================================
#
# The remaining places a date was computed on the wrong calendar.
#
# Each of these answered confidently and was wrong by a day, which is worse than
# failing: the output names the date, so the user reads a specific claim that
# does not match reality.

class TestTheMarketDayIsNewYorks:
    def test_the_exchange_timezone_is_pinned(self):
        """Not the server's day, and deliberately not the reader's either — a
        session closes when New York says it does, whoever is asking."""
        assert datafeeds._MARKET_TZ == "America/New_York"

    def test_after_the_utc_rollover_it_is_still_the_same_trading_day(self):
        # 00:30Z on Aug 31 is 20:30 ET on Aug 30 — the afternoon's close is
        # TODAY's, and the old date.today() labelled it "yesterday".
        instant = datetime(2026, 8, 31, 0, 30, tzinfo=timezone.utc)
        with patch("palmer.timeutil.datetime") as dt:
            dt.now.side_effect = lambda tz=None: instant.astimezone(tz) if tz else instant
            assert timeutil.local_today(datafeeds._MARKET_TZ) == date(2026, 8, 30)
            assert timeutil.local_today(None) == date(2026, 8, 31)


class TestWeatherDayResolution:
    """The convention lives in timeutil now, not weather.

    It is generic date reasoning, and leaving it in the weather module was
    how the reminder path — the one place the model computes a date itself —
    ended up with no answer for "next friday" at all, while weather had a
    considered one."""

    def _delta(self, when, on):
        with patch.object(timeutil, "local_today", return_value=on):
            return timeutil.resolve_day_delta(when, when.lower())

    FRIDAY = date(2026, 8, 28)
    WEDNESDAY = date(2026, 8, 26)

    def test_tomorrow_is_one_day(self):
        assert self._delta("tomorrow", self.FRIDAY) == 1

    def test_a_bare_weekday_is_the_next_one(self):
        assert self._delta("friday", self.WEDNESDAY) == 2

    def test_next_weekday_is_a_week_past_that(self):
        """"friday" and "next friday" used to be indistinguishable, so someone
        planning a week out got this week's forecast under next week's name."""
        assert self._delta("next friday", self.WEDNESDAY) == 9

    def test_the_two_rules_compose_rather_than_stacking(self):
        """A bare weekday naming TODAY resolves a week out (an existing, tested
        decision), so a flat +7 on top would put "next friday" a fortnight away."""
        assert self._delta("friday", self.FRIDAY) == 7
        assert self._delta("next friday", self.FRIDAY) == 7

    def test_an_explicit_date_still_wins(self):
        assert self._delta("2026-08-30", self.FRIDAY) == 2

    def test_a_misspelt_weekday_still_resolves(self):
        """Substring matching is doing real work here: "thursdayish" is still a
        Thursday, and "next" still bumps it."""
        assert self._delta("next thursdayish", self.FRIDAY) == 13

    def test_unparseable_input_returns_none(self):
        """The caller then answers for TODAY and logs it. It used to silently
        become tomorrow, so an unreadable phrase was answered for a day the user
        never named — and the output states that date as fact."""
        for phrase in ("sometime soon", "when it cools off", "later"):
            assert self._delta(phrase, self.FRIDAY) is None, phrase

    def test_the_callers_default_to_today_not_tomorrow(self):
        import inspect
        src = inspect.getsource(weather)
        assert "delta = 1" not in src, "unparseable input must not mean tomorrow"

    def test_the_target_is_anchored_on_the_readers_calendar(self):
        import inspect
        src = inspect.getsource(weather._nws_report)
        # delta is computed in the user's zone, so it must be added to the
        # user's today — not to the forecast grid's, which is a different place.
        assert "_lt(tz) if valid_zone(tz)" in src


class TestTimezoneIsValidatedAndRepaired:
    def _apply(self, profile, updates, derived=None):
        with patch.object(userprofile, "upsert_profile") as up, \
             patch.object(userprofile, "get_profile", return_value=profile), \
             patch.object(userprofile, "_derive_timezone", return_value=derived), \
             patch.object(userprofile, "_eager_build_home"):
            userprofile._apply_profile_updates("+15550001111", profile, updates)
        return up.call_args[0][1] if up.call_args else {}

    def test_junk_from_the_extractor_is_dropped(self):
        """`timezone` is in EXTRACT_PROMPT's schema, so Haiku can write anything.
        An unresolvable value degrades every local_now call to UTC, silently."""
        written = self._apply({"city": "Chicago"}, {"timezone": "Pacific Time"})
        assert "timezone" not in written

    def test_a_real_zone_is_kept(self):
        written = self._apply({"city": "Chicago"}, {"timezone": "America/Denver"})
        assert written["timezone"] == "America/Denver"

    def test_a_move_re_derives_the_zone(self):
        """It used to derive only when ABSENT, so someone who moved kept the old
        zone forever and their morning arrived at the wrong hour from then on."""
        written = self._apply({"city": "Chicago", "timezone": "America/Chicago"},
                              {"city": "Los Angeles"},
                              derived="America/Los_Angeles")
        assert written["timezone"] == "America/Los_Angeles"

    def test_no_move_leaves_it_alone(self):
        written = self._apply({"city": "Chicago", "timezone": "America/Chicago"},
                              {"vibe": "cheerful"}, derived="America/Denver")
        assert "timezone" not in written

    def test_an_explicit_timezone_update_is_not_overridden_by_the_city(self):
        written = self._apply({"city": "Chicago", "timezone": "America/Chicago"},
                              {"city": "Denver", "timezone": "America/Denver"},
                              derived="America/Boise")
        assert written["timezone"] == "America/Denver"

    def test_the_forecast_correction_path_does_not_reach_this_function(self):
        """CLAUDE.md: correcting a weather city must not move the hour the
        morning arrives. That write calls upsert_profile directly."""
        import inspect
        from palmer import agent
        src = inspect.getsource(agent.get_reply)
        assert "_city_from_weather_topic" in src
        assert "_apply_profile_updates" not in src


class TestConsolidationDoesNotRunEveryTurn:
    def test_the_gate_exists(self):
        assert userprofile.CONSOLIDATE_EVERY >= 10

    def test_it_is_skipped_until_enough_new_messages(self):
        with patch.object(userprofile, "get_message_count", return_value=45), \
             patch.object(userprofile, "get_profile",
                          return_value={"consolidated_at_count": 40}), \
             patch.object(userprofile, "get_older_messages") as older:
            userprofile._consolidate_history("+15550001111")
            older.assert_not_called()

    def test_it_runs_once_the_gap_is_wide_enough(self):
        with patch.object(userprofile, "get_message_count", return_value=65), \
             patch.object(userprofile, "get_profile",
                          return_value={"consolidated_at_count": 40}), \
             patch.object(userprofile, "get_older_messages", return_value=[]) as older:
            userprofile._consolidate_history("+15550001111")
            older.assert_called_once()

    def test_the_watermark_is_bookkeeping_not_an_extraction_field(self):
        from palmer import prompts
        assert "consolidated_at_count" in userprofile.PROFILE_FIELDS
        assert "consolidated_at_count" not in prompts.EXTRACT_PROMPT


class TestCurationIsToldTheReadersDate:
    def test_curate_takes_a_date(self):
        import inspect
        sig = inspect.signature(importlib.import_module("palmer.opening")._curate)
        assert "today" in sig.parameters

    def test_the_snapshot_passes_the_local_day(self):
        import inspect
        from palmer import opening
        src = inspect.getsource(opening.opening_snapshot)
        assert "_curate(metro or _metro(city), pool, today=today)" in src
