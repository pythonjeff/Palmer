"""A check-in must be about something the profile actually says.

followup.py fetches no data at all — it is pure model output conditioned on a
profile string — so every guard has to be structural.

Two defects made it the largest source of "random thoughts". _pick_thread
returned whatever Haiku emitted and handed it straight to the drafter, so an
invented thread was written up as though it were real; and the draft prompt
asked for "a statement that just shows you remembered", which is an instruction
to invent specificity. Separately, every bail path nulled followup_sent_date
instead of restoring it, which silently voided the 3-to-14-day pacing gap.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import db
import followup


PHONE = "+15550001111"
THREADS = ["interviewing at Stripe next week", "sister's wedding in Portland"]

# The one instant every clock-dependent test here is frozen to. _local_now and
# _local_today must be patched TOGETHER off this value: _should_send_followup
# reads the first, but the claim_daily_guard write in run_followups reads the
# second, so patching only _local_now left the assertion comparing a frozen day
# against the real clock. It passed on 2026-08-30 and failed every day after.
FROZEN = datetime(2026, 8, 30, 15, 0, tzinfo=ZoneInfo("America/Chicago"))

# The last real send, as the bail tests expect to find it restored. Derived
# from FROZEN rather than written out, so it stays comfortably outside the
# 14-day maximum pacing gap whatever FROZEN is set to — hardcoding it meant a
# date after FROZEN made the gap negative and suppressed the send.
PRIOR_SENT = (FROZEN - timedelta(days=29)).date().isoformat()


def _freeze(monkeypatch):
    monkeypatch.setattr(followup, "_local_now", lambda tz: FROZEN)
    monkeypatch.setattr(followup, "_local_today", lambda tz: FROZEN.date())


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test_followup.db")
    db.init_db()


def _haiku(text):
    return patch.object(followup.client.messages, "create",
                        return_value=MagicMock(content=[MagicMock(text=text)]))


class TestPickThreadFailsClosed:
    def test_an_exact_echo_returns_the_stored_string(self):
        with _haiku("interviewing at Stripe next week"):
            got = followup._pick_thread({"ongoing_threads": THREADS}, [])
        assert got == "interviewing at Stripe next week"

    def test_a_confabulated_thread_is_refused(self):
        """The actual failure: Haiku names something plausible that is not on
        the list, and it used to be drafted as though it were real."""
        with _haiku("how the apartment hunt is going"):
            assert followup._pick_thread({"ongoing_threads": THREADS}, []) is None

    def test_a_paraphrase_is_refused_too(self):
        with _haiku("the Stripe interview"):
            assert followup._pick_thread({"ongoing_threads": THREADS}, []) is None

    def test_none_is_none(self):
        with _haiku("NONE"):
            assert followup._pick_thread({"ongoing_threads": THREADS}, []) is None

    def test_no_threads_costs_no_model_call(self):
        with patch.object(followup.client.messages, "create") as create:
            assert followup._pick_thread({"ongoing_threads": []}, []) is None
            create.assert_not_called()

    def test_a_model_failure_is_silence(self):
        with patch.object(followup.client.messages, "create",
                          side_effect=RuntimeError("down")):
            assert followup._pick_thread({"ongoing_threads": THREADS}, []) is None

    def test_the_last_thread_is_not_picked_twice_running(self):
        profile = {"ongoing_threads": THREADS,
                   "followup_last_thread": "interviewing at Stripe next week"}
        with _haiku("interviewing at Stripe next week"):
            # It is excluded from the candidate list, so the echo cannot match.
            assert followup._pick_thread(profile, []) is None

    def test_a_single_thread_is_still_available_after_being_used(self):
        """Excluding the only thread there is would end followups for good."""
        profile = {"ongoing_threads": [THREADS[0]],
                   "followup_last_thread": THREADS[0]}
        with _haiku(THREADS[0]):
            assert followup._pick_thread(profile, []) == THREADS[0]


class TestLifeContextAloneNeverTriggersACheckIn:
    def test_prose_about_someone_is_not_a_thread(self):
        assert not followup._should_send_followup({
            "morning_onboarded": True, "timezone": "America/Chicago",
            "life_context": "Works in finance, two kids, moved to Chicago in March",
        })

    def test_a_real_thread_does(self, monkeypatch):
        _freeze(monkeypatch)
        assert followup._should_send_followup({
            "morning_onboarded": True, "timezone": "America/Chicago",
            "ongoing_threads": THREADS,
        })


class TestThePacingGapSurvivesABail:
    """claim_daily_guard overwrites followup_sent_date with today, so nulling it
    on a bail erased the record of the last real send — and _should_send_followup
    measures the 3-to-14 day gap against exactly that field."""

    def _run(self, tmp_path, monkeypatch, *, thread, drafted="hey, how'd it go?",
             dup=False, sent=True):
        _fresh(tmp_path, monkeypatch)
        db.upsert_profile(PHONE, {
            "morning_onboarded": True, "timezone": "America/Chicago",
            "ongoing_threads": THREADS, "followup_sent_date": PRIOR_SENT,
        })
        _freeze(monkeypatch)
        with patch.object(followup, "get_all_profiles",
                          return_value=[(PHONE, db.get_profile(PHONE))]), \
             patch.object(followup, "_pick_thread", return_value=thread), \
             patch.object(followup, "_draft_followup", return_value=drafted), \
             patch.object(followup, "_is_duplicate_subject", return_value=dup), \
             patch("sms_util.send_sms", return_value=sent):
            followup.run_followups()
        return db.get_profile(PHONE)

    def test_no_thread_restores_the_prior_date(self, tmp_path, monkeypatch):
        p = self._run(tmp_path, monkeypatch, thread=None)
        assert p["followup_sent_date"] == PRIOR_SENT

    def test_an_empty_draft_restores_it(self, tmp_path, monkeypatch):
        p = self._run(tmp_path, monkeypatch, thread=THREADS[0], drafted="")
        assert p["followup_sent_date"] == PRIOR_SENT

    def test_a_duplicate_restores_it(self, tmp_path, monkeypatch):
        p = self._run(tmp_path, monkeypatch, thread=THREADS[0], dup=True)
        assert p["followup_sent_date"] == PRIOR_SENT

    def test_a_failed_send_restores_it(self, tmp_path, monkeypatch):
        p = self._run(tmp_path, monkeypatch, thread=THREADS[0], sent=False)
        assert p["followup_sent_date"] == PRIOR_SENT

    def test_a_real_send_advances_it_and_records_the_thread(self, tmp_path, monkeypatch):
        p = self._run(tmp_path, monkeypatch, thread=THREADS[0])
        assert p["followup_sent_date"] == FROZEN.date().isoformat()
        assert p["followup_last_thread"] == THREADS[0]


class TestTheDraftPromptDoesNotAskForInvention:
    def test_it_forbids_inventing_details(self):
        import inspect
        src = inspect.getsource(followup._draft_followup)
        assert "Do not invent" in src
        # The instruction that produced the problem.
        assert "just shows you remembered" not in src

    def test_the_bookkeeping_field_is_allow_listed_but_not_extractable(self):
        import userprofile
        import prompts
        assert "followup_last_thread" in userprofile.PROFILE_FIELDS
        assert "followup_last_thread" not in prompts.EXTRACT_PROMPT
