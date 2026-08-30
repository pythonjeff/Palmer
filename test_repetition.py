"""Repetition, and the two opposite remedies it needs.

Measured across every message Palmer has sent: 39 near-duplicate pairs for one
user, 11 for another. They were not one problem.

SUPPRESSION — an unprompted message repeating one already sent. One user got the
identical followup twice, verbatim, because `_is_duplicate_subject` looked back
six hours while the followup job runs every four and the subject stayed live for
days. Another got "Here you go - <link>" three times, word for word.

VARIATION — a scheduled message the user DID ask for, said the same way every
time: "Morning Drew - 103 today in Woodland Hills", "106 in Woodland Hills
today, Drew", "111 today in Woodland Hills, Drew". Suppressing those would be
wrong; they asked for a daily briefing. Only the phrasing may not repeat. And
token overlap cannot see it — those score 0.23 against each other.

LEAKAGE — a third thing found on the way: Palmer narrating its own filtering to
the reader, in the third person, about her. morning.py had a guard; the four
other senders never ran it.

The corpora here are real messages. All offline.
"""
from unittest.mock import MagicMock, patch

import guards


REPEAT_MORNINGS = [
    "Morning Drew - 103 today in Woodland Hills, stay inside if you can.",
    "106 in Woodland Hills today, Drew - hottest it's been all week.",
    "111 today in Woodland Hills, Drew - and there's actually a chance of thunderstorms.",
    "110 in Woodland Hills today, so outdoor plans need a rethink.",
]
FRESH_LINES = [
    "Courtney Barnett plays the Hollywood Palladium Friday if you want a reason to get out.",
    "Bitcoin's up 3.1% overnight - quiet week so that move's worth a look.",
    "85 and sunny in Kirkwood, commute's clean at 17 minutes.",
]


class TestSuppressingVerbatimRepeats:
    def test_the_identical_followup_is_caught(self):
        line = "yo how'd practice look today? hurts moving like they said?"
        assert guards.near_duplicate(line, [line]) == line

    def test_the_repeated_link_reply_is_caught(self):
        a = "Here you go -\n\nhttps://palmer-app.example/h/abc"
        b = "Here you go -\n\nhttps://palmer-app.example/h/abc"
        assert guards.near_duplicate(a, [b])

    def test_the_url_is_not_what_makes_them_similar(self):
        """Two different sentences carrying the same page link are not repeats —
        otherwise every message that ends in their URL reads as identical."""
        a = "Muse plays the Hollywood Bowl Sunday. https://x.example/h/abc"
        b = "Bitcoin's up 3% overnight. https://x.example/h/abc"
        assert guards.near_duplicate(a, [b]) is None

    def test_genuinely_different_messages_pass(self):
        for i, line in enumerate(FRESH_LINES):
            others = FRESH_LINES[:i] + FRESH_LINES[i + 1:]
            assert guards.near_duplicate(line, others) is None, line

    def test_the_lexical_check_runs_before_any_model_call(self):
        """The point is that a verbatim repeat costs nothing to catch, so it can
        look back three days where the semantic check cannot afford to."""
        import inspect
        import userprofile
        src = inspect.getsource(userprofile._is_duplicate_subject)
        assert src.index("near_duplicate") < src.index("client.messages.create")
        assert userprofile.VERBATIM_WINDOW_HOURS > 24

    def test_no_history_is_not_a_duplicate(self):
        assert guards.near_duplicate("anything", []) is None
        assert guards.near_duplicate("", ["anything"]) is None


class TestVaryingAScheduledMessage:
    """The content is supposed to recur. The opening is not."""

    def test_the_repeated_mornings_share_an_opening_shape(self):
        shapes = {guards.opening_shape(m) for m in REPEAT_MORNINGS[1:]}
        assert len(shapes) == 1, f"these three open identically: {shapes}"

    def test_a_repeat_is_detected_against_recent_sends(self):
        assert guards.repeats_opening(REPEAT_MORNINGS[3], REPEAT_MORNINGS[:3])

    def test_numbers_do_not_disguise_a_repeat(self):
        """103 / 106 / 111 made every day look unique to a token comparison."""
        assert guards.similarity(REPEAT_MORNINGS[1], REPEAT_MORNINGS[2]) < 0.4
        assert guards.repeats_opening(REPEAT_MORNINGS[1], [REPEAT_MORNINGS[2]])

    def test_a_genuinely_different_opening_is_left_alone(self):
        for line in FRESH_LINES:
            assert guards.repeats_opening(line, REPEAT_MORNINGS) is None, line

    def test_leading_with_something_else_clears_it(self):
        """The redraft asks for the same facts starting somewhere else."""
        recast = "Courtney Barnett's at the Hollywood Palladium Friday, and it's 110 in Woodland Hills."
        assert guards.repeats_opening(recast, REPEAT_MORNINGS) is None

    def test_the_morning_line_redrafts_on_a_repeat(self):
        import inspect
        import morning
        src = inspect.getsource(morning.generate_morning_line)
        assert "repeats_opening" in src
        # Split across source lines in the literal, so match a fragment.
        assert "number identical" in src, "a recast must not move the facts"


class TestDeliberationNeverShips:
    LEAKED = [
        "Both of these fall into the crime/dark content category they explicitly "
        "asked to avoid. Skipping.",
        "This one's in the crime/dark content bucket they asked to avoid. Skipping it.",
    ]
    FINE = [
        "Muse plays the Hollywood Bowl Sunday if you need a reason to get out.",
        "They beat the Pirates 4-1 last night - Mathews got his first career win.",
        "You asked me to watch that fare - it's down to $668.",
        "90 in Culver City today, low 70, barely a rain chance.",
        # These four were BLOCKED by the either-signal version. Because
        # send_sms returns False and main.py answers a falsy send with
        # FALLBACK_SMS, the user got "something went sideways on my end, try
        # again" in place of a perfectly good reply. Agreeing to stop doing
        # something is not a leak, and neither is news about someone else.
        "got it, not sending those anymore",
        "noted - won't send you the crime stuff again",
        "they said the deal closes Friday",
        "they asked for a recount and the board agreed",
    ]

    def test_the_real_leaks_are_caught(self):
        for text in self.LEAKED:
            assert guards.leaks_deliberation(text), text

    def test_ordinary_messages_survive(self):
        """"They beat the Pirates" is a sentence about a baseball team, and
        "you asked me to watch that fare" is Palmer talking TO someone."""
        for text in self.FINE:
            assert not guards.leaks_deliberation(text), text

    def test_agreeing_to_stop_is_not_a_leak(self):
        """The distinction the guard has to make: "not sending those anymore" is
        a commitment TO the reader; "not sending, doesn't meet the threshold" is
        Palmer explaining its plumbing to them."""
        assert not guards.leaks_deliberation("sure, not sending those anymore")
        assert guards.leaks_deliberation("Not sending - it doesn't meet the threshold.")

    def test_news_about_a_third_party_survives(self):
        """"said" is deliberately not an intent verb here: "they said the deal
        closes Friday" is the sort of sentence Palmer exists to send."""
        for text in ("they said the deal closes Friday",
                     "they wanted a bigger deal and walked",
                     "she told me they prefer the early show"):
            assert not guards.leaks_deliberation(text), text

    def test_naming_the_reader_as_the_user_is_damning_alone(self):
        """Nobody texting a friend calls them "the user"."""
        assert guards.leaks_deliberation("the user prefers shorter updates")

    def test_internal_machinery_is_damning_alone(self):
        for text in ("scored below the bar so no alert needed",
                     "this one was filtered out",
                     "suppressing that one"):
            assert guards.leaks_deliberation(text), text

    def test_paraphrase_does_not_escape_it(self):
        """The guard it replaces matched fixed phrases, so the model wrote
        around them — "they EXPLICITLY asked" missed a rule looking for
        "they asked"."""
        for text in ("the user specified no sports so leaving that out",
                     "This one doesn't meet the threshold, not sending.",
                     "They said they prefer lighter stuff, so I'll skip it."):
            assert guards.leaks_deliberation(text), text

    def test_it_is_blocked_at_the_one_place_everything_passes_through(self):
        """morning.py had a guard for this and four other senders never ran it."""
        import inspect
        import sms_util
        assert "leaks_deliberation" in inspect.getsource(sms_util.send_sms)

    def test_blocking_returns_false_rather_than_sending_something_else(self):
        import sms_util
        with patch.object(sms_util, "_twilio") as tw:
            sent = sms_util.send_sms("+1555", self.LEAKED[0])
        assert sent is False
        tw.messages.create.assert_not_called(), "nothing may go out, not even a fallback"


class TestADeliberationLeakInAReplyIsRedraftedNotDropped:
    """send_sms blocks a leak outright, and for an unprompted message that is
    exactly right — every real violation was a drafter announcing it had decided
    NOT to send something, so doing that silently is what it was trying to do.

    On a reply it is the wrong trade. The user is waiting on an answer, and a
    block there means main.py's falsy-send path hands them FALLBACK_SMS. So
    _finalize redrafts first, and the send_sms block stays behind it."""

    def _finalize(self, first, *retries):
        import agent
        calls = []

        def _create(**kw):
            calls.append(kw)
            text = retries[min(len(calls) - 1, len(retries) - 1)] if retries else first
            return MagicMock(content=[MagicMock(text=text)])

        with patch.object(agent.client.messages, "create", side_effect=_create):
            out, _ = agent._finalize(first, "sys", [{"role": "user", "content": "hi"}], None)
        return out, calls

    def test_a_leak_is_redrafted_once(self):
        out, calls = self._finalize(
            "This one doesn't meet the threshold, not sending.",
            "nothing worth flagging today")
        assert "threshold" not in out
        assert len(calls) == 1

    def test_a_clean_reply_still_costs_nothing(self):
        out, calls = self._finalize("sure, not sending those anymore")
        assert calls == []
        assert out == "sure, not sending those anymore"

    def test_a_failed_redraft_keeps_the_original_rather_than_going_silent(self):
        import agent
        leak = "This one doesn't meet the threshold, not sending."
        with patch.object(agent.client.messages, "create", side_effect=RuntimeError("down")):
            out, _ = agent._finalize(leak, "sys", [], None)
        assert out == leak, "send_sms is the backstop; _finalize must not blank it"

    def test_the_correction_tells_it_what_to_do_instead(self):
        assert "TO them" in guards.DELIBERATION_CORRECTION

    def test_the_send_sms_block_is_still_there(self):
        """The chokepoint stays — proactive senders never reach _finalize."""
        import inspect
        import sms_util
        assert "leaks_deliberation" in inspect.getsource(sms_util.send_sms)
