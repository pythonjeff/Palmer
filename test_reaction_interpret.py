"""Tests for reaction interpretation and the reply/silence decision.

Haiku is mocked throughout — these test the plumbing, the validation, and above
all the failure behavior. The rule that matters: anything that goes wrong must
land on silence, never on an unwanted text.

Live model behavior (does Haiku actually call a thumbs-up on a question an
"answer"?) is verified separately against the real API, not here.
"""
from unittest.mock import patch, MagicMock

import pytest

import tapback

with patch("apscheduler.schedulers.background.BackgroundScheduler.start"):
    import main


def _haiku(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


LIKED = {"kind": "liked", "sentiment": "positive", "quoted": "want me to add that?", "emoji": ""}
THUMBS = {"kind": "emoji", "sentiment": "positive", "quoted": "", "emoji": "\U0001f44d"}


class TestInterpretVerdicts:
    @pytest.mark.parametrize("function,needs_reply", [
        ("answer", True),
        ("closer", False),
        ("applause", False),
        ("objection", False),
        ("emotional", False),
    ])
    def test_only_answer_needs_reply(self, function, needs_reply):
        payload = f'{{"function": "{function}", "sentiment": "positive", "about": "mornings"}}'
        with patch.object(tapback.client.messages, "create", return_value=_haiku(payload)):
            v = tapback.interpret_reaction(THUMBS, "want me to add that to your morning?", {})
        assert v["function"] == function
        assert v["needs_reply"] is needs_reply

    def test_sentiment_is_taken_from_the_model_not_the_emoji_map(self):
        """A skull is 'neutral' to the static map; in context it can be praise."""
        skull = {"kind": "emoji", "sentiment": "neutral", "quoted": "", "emoji": "\U0001f480"}
        payload = '{"function": "applause", "sentiment": "positive", "about": "the joke"}'
        with patch.object(tapback.client.messages, "create", return_value=_haiku(payload)):
            v = tapback.interpret_reaction(skull, "They always pick the day before the weekend.", {})
        assert v["sentiment"] == "positive"

    def test_about_is_captured_and_truncated(self):
        payload = '{"function": "objection", "sentiment": "negative", "about": "' + "x" * 200 + '"}'
        with patch.object(tapback.client.messages, "create", return_value=_haiku(payload)):
            v = tapback.interpret_reaction(LIKED, "bitcoin is up 12%", {})
        assert len(v["about"]) <= 40


class TestInterpretFailsSafe:
    """Every failure path must produce silence."""

    def test_api_exception(self):
        with patch.object(tapback.client.messages, "create", side_effect=RuntimeError("boom")):
            v = tapback.interpret_reaction(THUMBS, "want me to add that?", {})
        assert v["needs_reply"] is False and v["function"] == "closer"

    def test_unparseable_response(self):
        with patch.object(tapback.client.messages, "create", return_value=_haiku("not json at all")):
            v = tapback.interpret_reaction(THUMBS, "want me to add that?", {})
        assert v["needs_reply"] is False

    def test_invalid_function_value(self):
        with patch.object(tapback.client.messages, "create",
                          return_value=_haiku('{"function": "vibes", "sentiment": "positive"}')):
            v = tapback.interpret_reaction(THUMBS, "want me to add that?", {})
        assert v["function"] == "closer" and v["needs_reply"] is False

    def test_invalid_sentiment_falls_back_to_parsed(self):
        with patch.object(tapback.client.messages, "create",
                          return_value=_haiku('{"function": "closer", "sentiment": "spicy"}')):
            v = tapback.interpret_reaction(THUMBS, "hi", {})
        assert v["sentiment"] in ("positive", "negative", "neutral")

    def test_no_last_assistant_skips_the_model_entirely(self):
        """Nothing to answer means no question was asked — don't pay for a call."""
        with patch.object(tapback.client.messages, "create") as create:
            v = tapback.interpret_reaction(THUMBS, "", {})
        create.assert_not_called()
        assert v["needs_reply"] is False

    def test_empty_reaction(self):
        assert tapback.interpret_reaction({}, "hi", {})["needs_reply"] is False


class TestConsolidation:
    def _profile(self, n):
        return {"reactions": [
            {"kind": "liked", "sentiment": "positive", "quoted": f"m{i}", "about": ""}
            for i in range(n)
        ]}

    def test_does_not_fire_below_threshold(self):
        with patch.object(tapback, "get_profile",
                          return_value=self._profile(tapback.CONSOLIDATE_EVERY - 1)), \
             patch.object(tapback.client.messages, "create") as create, \
             patch.object(tapback, "upsert_profile") as upsert:
            tapback.maybe_consolidate("+15550001111")
        create.assert_not_called()
        upsert.assert_not_called()

    def test_fires_at_threshold_and_updates_style(self):
        stored = {}
        with patch.object(tapback, "get_profile", return_value=self._profile(tapback.CONSOLIDATE_EVERY)), \
             patch.object(tapback.client.messages, "create",
                          return_value=_haiku('{"communication_style": "likes the dry stuff"}')), \
             patch.object(tapback, "upsert_profile", side_effect=lambda p, u: stored.update(u)):
            tapback.maybe_consolidate("+15550001111")
        assert stored["communication_style"] == "likes the dry stuff"
        assert stored["reactions_folded_count"] == tapback.CONSOLIDATE_EVERY

    def test_does_not_refire_until_another_batch(self):
        prof = self._profile(tapback.CONSOLIDATE_EVERY)
        prof["reactions_folded_count"] = tapback.CONSOLIDATE_EVERY
        with patch.object(tapback, "get_profile", return_value=prof), \
             patch.object(tapback.client.messages, "create") as create:
            tapback.maybe_consolidate("+15550001111")
        create.assert_not_called()

    def test_failure_is_swallowed(self):
        with patch.object(tapback, "get_profile", side_effect=RuntimeError("db down")):
            tapback.maybe_consolidate("+15550001111")


class TestHandlerRouting:
    """End-to-end through _handle_sms with the verdict controlled."""

    def _run(self, body, verdict, last_assistant="want me to add that to your morning?"):
        history = [{"role": "assistant", "content": last_assistant}] if last_assistant else []
        with patch.object(main, "ensure_sms", return_value=True) as ensure, \
             patch.object(main, "get_reply", return_value=("done, added", None)) as get_reply, \
             patch.object(main, "interpret_reaction", return_value=verdict), \
             patch.object(main, "record_reaction") as record, \
             patch.object(main, "learn_from_reactions") as consolidate, \
             patch.object(main, "save_message"), \
             patch.object(main, "save_assistant_turn"), \
             patch.object(main, "get_history", return_value=history), \
             patch.object(main, "get_profile", return_value={"intro_sent": True}), \
             patch.object(main, "upsert_profile"):
            main._handle_sms("+15550001111", body, None)
            return ensure, get_reply, record, consolidate

    def test_closer_stays_silent(self):
        ensure, get_reply, record, consolidate = self._run(
            'Liked "the audacity of it"',
            {"function": "closer", "sentiment": "positive", "about": "", "needs_reply": False},
        )
        ensure.assert_not_called()
        get_reply.assert_not_called()
        record.assert_called_once()
        consolidate.assert_called_once()

    def test_answer_gets_exactly_one_reply(self):
        ensure, get_reply, record, _ = self._run(
            "\U0001f44d",
            {"function": "answer", "sentiment": "positive", "about": "mornings", "needs_reply": True},
        )
        get_reply.assert_called_once()
        ensure.assert_called_once()
        record.assert_called_once()

    def test_answer_is_relabelled_for_the_model(self):
        """Palmer must see 'this is their answer', not a bare emoji."""
        with patch.object(main, "ensure_sms", return_value=True), \
             patch.object(main, "get_reply", return_value=("done", None)) as get_reply, \
             patch.object(main, "interpret_reaction",
                          return_value={"function": "answer", "sentiment": "positive",
                                        "about": "", "needs_reply": True}), \
             patch.object(main, "record_reaction"), \
             patch.object(main, "learn_from_reactions"), \
             patch.object(main, "save_message"), \
             patch.object(main, "save_assistant_turn"), \
             patch.object(main, "get_history",
                          return_value=[{"role": "assistant", "content": "want me to add that?"}]), \
             patch.object(main, "get_profile", return_value={"intro_sent": True}), \
             patch.object(main, "upsert_profile"):
            main._handle_sms("+15550001111", "\U0001f44d", None)
        sent = get_reply.call_args.kwargs["message"]
        assert "this is their answer" in sent
        assert "want me to add that?" in sent

    def test_objection_stays_silent(self):
        ensure, get_reply, _, _ = self._run(
            'Disliked "bitcoin is up 12%"',
            {"function": "objection", "sentiment": "negative", "about": "bitcoin", "needs_reply": False},
        )
        ensure.assert_not_called()
        get_reply.assert_not_called()


def _neg(topic, n):
    return [{"kind": "disliked", "sentiment": "negative", "function": "objection",
             "about": topic, "quoted": ""} for _ in range(n)]


class TestPacingFactor:
    def test_normal_with_no_reactions(self):
        assert tapback.pacing_factor({}) == 1.0
        assert tapback.pacing_factor({"reactions": []}) == 1.0

    def test_positives_do_not_slow_palmer_down(self):
        prof = {"reactions": [{"kind": "liked", "sentiment": "positive", "function": "applause"}] * 6}
        assert tapback.pacing_factor(prof) == 1.0

    def test_negatives_increase_the_factor(self):
        assert tapback.pacing_factor({"reactions": _neg("crypto", 2)}) > 1.0

    def test_capped(self):
        assert tapback.pacing_factor({"reactions": _neg("crypto", 50)}) == tapback.MAX_PACING_FACTOR

    def test_decays_as_positives_push_negatives_out(self):
        """The rolling log is the decay mechanism — no reset job needed."""
        heavy = tapback.pacing_factor({"reactions": _neg("crypto", 4)})
        recovered = tapback.pacing_factor({"reactions": _neg("crypto", 1)})
        assert recovered < heavy

    def test_followup_gap_stretches(self):
        import followup
        from datetime import datetime, timedelta
        base = {"morning_onboarded": True, "timezone": "America/Chicago",
                "ongoing_threads": ["job offer"]}
        two_days_ago = (datetime.now() - timedelta(days=2)).date().isoformat()

        with patch.object(followup, "_local_now",
                          return_value=datetime.now().replace(hour=15)):
            calm = dict(base, followup_sent_date=two_days_ago, reactions=[])
            noisy = dict(base, followup_sent_date=two_days_ago, reactions=_neg("checkins", 4))
            # 2 days < 3-day base gap either way, but the noisy user's gap is longer still
            assert followup._should_send_followup(calm) is False
            assert followup._should_send_followup(noisy) is False

        four_days_ago = (datetime.now() - timedelta(days=4)).date().isoformat()
        with patch.object(followup, "_local_now",
                          return_value=datetime.now().replace(hour=15)):
            calm = dict(base, followup_sent_date=four_days_ago, reactions=[])
            noisy = dict(base, followup_sent_date=four_days_ago, reactions=_neg("checkins", 4))
            assert followup._should_send_followup(calm) is True, "normal user is due at 4 days"
            assert followup._should_send_followup(noisy) is False, "backed-off user is not"


class TestPreferenceLearning:
    def _run(self, log, existing_prefs=None, topics=("crypto", "sports", "weather")):
        stored = {}
        prof = {"reactions": log, "morning_topics": list(topics)}
        if existing_prefs is not None:
            prof["morning_prefs"] = existing_prefs
        with patch.object(tapback, "get_profile", return_value=prof), \
             patch.object(tapback, "upsert_profile", side_effect=lambda p, u: stored.update(u)):
            tapback.maybe_learn_preferences("+15550001111")
        return stored

    def test_one_dislike_changes_nothing(self):
        assert self._run(_neg("crypto", 1)) == {}

    def test_two_dislikes_change_nothing(self):
        assert self._run(_neg("crypto", 2)) == {}

    def test_threshold_adds_to_avoid(self):
        stored = self._run(_neg("crypto", tapback.NEGATIVE_STREAK_FOR_AVOID))
        assert "crypto" in stored["morning_prefs"]["avoid"]

    def test_sets_a_notice_so_it_is_not_silent(self):
        stored = self._run(_neg("crypto", tapback.NEGATIVE_STREAK_FOR_AVOID))
        assert stored["pending_preference_notice"] == "crypto"

    def test_preserves_existing_avoid_entries(self):
        stored = self._run(_neg("crypto", tapback.NEGATIVE_STREAK_FOR_AVOID),
                           existing_prefs={"avoid": ["sports"]})
        assert set(stored["morning_prefs"]["avoid"]) == {"sports", "crypto"}

    def test_does_not_duplicate(self):
        assert self._run(_neg("crypto", tapback.NEGATIVE_STREAK_FOR_AVOID),
                         existing_prefs={"avoid": ["crypto"]}) == {}

    def test_spread_across_topics_does_not_trigger(self):
        log = _neg("crypto", 1) + _neg("sports", 1) + _neg("weather", 1)
        assert self._run(log) == {}

    def test_blank_topic_ignored(self):
        assert self._run(_neg("", 5)) == {}

    def test_topic_not_in_the_briefing_is_ignored(self):
        """Only things Palmer actually sends can be dropped from what he sends."""
        assert self._run(_neg("someone's dog", tapback.NEGATIVE_STREAK_FOR_AVOID)) == {}

    def test_no_briefing_topics_means_nothing_to_drop(self):
        assert self._run(_neg("crypto", tapback.NEGATIVE_STREAK_FOR_AVOID), topics=()) == {}

    def test_matching_is_case_insensitive(self):
        stored = self._run(_neg("CRYPTO", tapback.NEGATIVE_STREAK_FOR_AVOID))
        assert "crypto" in [a.lower() for a in stored["morning_prefs"]["avoid"]]

    def test_stores_the_original_topic_casing(self):
        """morning.py matches against morning_topics, so casing must round-trip."""
        stored = self._run(_neg("bitcoin", tapback.NEGATIVE_STREAK_FOR_AVOID),
                           topics=("Bitcoin",))
        assert "Bitcoin" in stored["morning_prefs"]["avoid"]

    def test_failure_is_swallowed(self):
        with patch.object(tapback, "get_profile", side_effect=RuntimeError("db down")):
            tapback.maybe_learn_preferences("+15550001111")


class TestNoticeSurfacedAndCleared:
    def test_notice_appears_in_prompt(self):
        import agent
        with patch.object(agent, "get_profile",
                          return_value={"pending_preference_notice": "crypto"}), \
             patch.object(agent, "get_user_watches", return_value=[]), \
             patch.object(agent, "get_user_price_watches", return_value=[]):
            out = agent._build_system("+15550001111")
        assert "crypto" in out
        assert "thumbs-downed" in out

    def test_notice_absent_when_unset(self):
        import agent
        with patch.object(agent, "get_profile", return_value={"name": "Mike"}), \
             patch.object(agent, "get_user_watches", return_value=[]), \
             patch.object(agent, "get_user_price_watches", return_value=[]):
            out = agent._build_system("+15550001111")
        assert "thumbs-downed" not in out

    def test_notice_is_cleared_after_being_shown(self):
        """Otherwise Palmer mentions the dropped topic every single turn."""
        import agent
        stored = {}
        with patch.object(agent, "_update_profile"), \
             patch.object(agent, "_consolidate_history"), \
             patch.object(agent, "upsert_profile", side_effect=lambda p, u: stored.update(u)), \
             patch.object(agent, "get_profile", return_value={}):
            agent._profile_and_consolidate("+15550001111", "ok", "sure", None, "crypto")
        assert stored["pending_preference_notice"] is None
