"""Tests for register calibration — Palmer adapting HOW he sounds to who's texting.

The personality lives in prose inside SYSTEM_PROMPT, so these are structural
assertions in the style of test_rubrics.py: the sections exist, they say the
load-bearing thing, and the profile signal actually reaches the prompt.

The ordering test is not cosmetic. SOUND CHECK is nine examples in one cultural
register, and few-shot beats instructions — SAME PALMER, DIFFERENT PEOPLE has to
come after it to be the last word on voice.
"""
from unittest.mock import patch, MagicMock

import agent
import prompts
import send_reminders


def _render() -> str:
    """SYSTEM_PROMPT as _build_system renders it."""
    return agent.SYSTEM_PROMPT.format(clock_block="RIGHT NOW\n(clock)",
                                      profile_block="(none)")


class TestCalibrationSection:
    def test_section_exists(self):
        assert "CALIBRATION\n" in agent.SYSTEM_PROMPT

    def test_names_all_four_axes(self):
        body = agent.SYSTEM_PROMPT
        for axis in ("Irony tolerance", "Precision", "Formality and idiom", "Directness"):
            assert axis in body, f"calibration axis {axis!r} missing"

    def test_spine_is_non_negotiable(self):
        """Calibrating must never be licence to become a neutral assistant."""
        body = agent.SYSTEM_PROMPT.lower()
        assert "the spine doesn't" in body
        assert "neutral assistant" in body

    def test_does_not_announce_the_adjustment(self):
        assert "never announce that you're adjusting" in agent.SYSTEM_PROMPT.lower()


class TestRangeExamples:
    def test_block_exists(self):
        assert "SAME PALMER, DIFFERENT PEOPLE" in agent.SYSTEM_PROMPT

    def test_comes_after_sound_check(self):
        body = agent.SYSTEM_PROMPT
        assert body.index("SOUND CHECK") < body.index("SAME PALMER, DIFFERENT PEOPLE"), \
            "range examples must follow SOUND CHECK or the single-register block wins"

    def test_sound_check_is_labelled_as_one_register(self):
        """The original nine examples must not read as the only way to sound."""
        head = agent.SYSTEM_PROMPT.split("SOUND CHECK\n", 1)[1].split("them:", 1)[0]
        assert head.strip(), "SOUND CHECK has no lead-in labelling it as one register"
        assert "one register" in head.lower()

    def test_covers_distinct_registers(self):
        block = agent.SYSTEM_PROMPT.split("SAME PALMER, DIFFERENT PEOPLE", 1)[1] \
                                   .split("NEW USERS", 1)[0]
        assert "Poisson" in block, "no precise/technical register example"
        assert "second language" in block, "no formal/ESL register example"
        assert "rough day" in block, "no bad-day register example"


class TestPromptStillRenders:
    def test_format_does_not_raise(self):
        """SYSTEM_PROMPT is .format()ed — a stray brace in new prose breaks every reply."""
        _render()

    def test_placeholders_survive(self):
        out = _render()
        assert "RIGHT NOW\n(clock)" in out
        assert "(none)" in out


class TestBuildSystemWiring:
    def _build(self, profile: dict) -> str:
        with patch.object(agent, "get_profile", return_value=profile), \
             patch.object(agent, "get_user_watches", return_value=[]), \
             patch.object(agent, "get_user_price_watches", return_value=[]):
            return agent._build_system("+15550001111")

    def test_communication_style_reaches_the_prompt_as_a_directive(self):
        out = self._build({"name": "Ada",
                           "communication_style": "precise, wants the answer first"})
        assert "CALIBRATION READ" in out
        assert "precise, wants the answer first" in out
        assert "mirror it" in out

    def test_explicit_request_outranks_inference(self):
        out = self._build({"communication_style": "asked directly for less sarcasm"})
        assert "outranks" in out

    def test_absent_style_adds_no_calibration_read(self):
        assert "CALIBRATION READ" not in self._build({"name": "Ada"})

    def test_blank_style_adds_no_calibration_read(self):
        assert "CALIBRATION READ" not in self._build({"communication_style": "   "})

    def test_empty_profile_still_builds(self):
        assert "CALIBRATION READ" not in self._build({})


class TestOnboardingAsk:
    """Message 1 never demands name/city (NEW USERS rule, above). From message
    2 on — intro already sent — if Palmer still doesn't know one or both, the
    dynamically-appended ONBOARDING ASK block tells him to work it in, once."""

    def _build(self, profile: dict, is_new_user: bool = False) -> str:
        with patch.object(agent, "get_profile", return_value=profile), \
             patch.object(agent, "get_user_watches", return_value=[]), \
             patch.object(agent, "get_user_price_watches", return_value=[]):
            return agent._build_system("+15550001111", is_new_user=is_new_user)

    def test_asks_for_both_when_both_are_missing(self):
        out = self._build({"intro_sent": True})
        assert "ONBOARDING ASK" in out
        assert "name and what city" in out

    def test_asks_only_for_the_one_still_missing(self):
        out = self._build({"intro_sent": True, "name": "Ada"})
        assert "ONBOARDING ASK" in out
        assert "their city" in out
        assert "name and what city" not in out

    def test_silent_on_the_very_first_message(self):
        """Message 1 is is_new_user=True — the NEW USERS rules own that reply,
        not this block, even if the profile happens to be empty."""
        assert "ONBOARDING ASK" not in self._build({}, is_new_user=True)

    def test_silent_once_name_and_city_are_both_known(self):
        out = self._build({"intro_sent": True, "name": "Ada", "city": "Chicago"})
        assert "ONBOARDING ASK" not in out

    def test_silent_once_already_asked(self):
        """Consumed once by userprofile._update_profile — see test_profile_schema.py."""
        out = self._build({"intro_sent": True, "onboarding_ask_sent": True})
        assert "ONBOARDING ASK" not in out

    def test_never_volunteers_the_link(self):
        out = self._build({"intro_sent": True})
        block = out.split("ONBOARDING ASK", 1)[1].lower()
        assert "send any link" in block or "don't mention their page" in block


class TestExtractionCapturesRegister:
    def test_prompt_asks_for_explicit_requests(self):
        body = prompts.EXTRACT_PROMPT.lower()
        assert "communication_style" in body
        assert "verbatim" in body, "explicit register asks must be recorded as stated"
        assert "joke back" in body


class TestReminderPathHonoursStyle:
    """Reminders used to carry their own mini-persona and never saw SYSTEM_PROMPT.
    They now go through _build_system like every other user-facing message."""

    def test_reminder_uses_the_shared_system_prompt(self):
        captured = {}

        def _fake_create(**kwargs):
            captured.update(kwargs)
            block = MagicMock()
            block.text = "hey - dentist at 3"
            resp = MagicMock()
            resp.content = [block]
            return resp

        with patch.object(send_reminders, "_build_system",
                          return_value="SYSTEM WITH CALIBRATION READ: blunt") as bs, \
             patch.object(send_reminders.client.messages, "create", side_effect=_fake_create):
            out = send_reminders._personalize_reminder(
                "+15550001111", "dentist at 3",
                {"communication_style": "blunt, asked for just the facts"},
            )

        assert out
        bs.assert_called_once()
        assert captured["system"] == "SYSTEM WITH CALIBRATION READ: blunt"
        assert captured["model"] == send_reminders.SONNET_MODEL, \
            "user-facing drafting belongs on Sonnet"

    def test_reminder_survives_a_drafting_failure(self):
        with patch.object(send_reminders, "_build_system", return_value="SYS"), \
             patch.object(send_reminders.client.messages, "create",
                          side_effect=RuntimeError("boom")):
            out = send_reminders._personalize_reminder("+15550001111", "dentist at 3", {})
        assert "dentist at 3" in out, "a failed draft must still deliver the reminder"


class TestThePromptDescribesTheProductThatExists:
    """SYSTEM_PROMPT is what Palmer paraphrases to a user when he confirms
    something. Every claim in it is a promise, and three had gone stale.

    The price rule is the sharpest case: the prompt still said "~15% drops"
    long after that bar was deleted for the second time and replaced with a
    flat $2 in either direction. So Palmer described a drop-only percentage
    watch and then sent a $2 rise alert. Nothing cross-checked the prose
    against the constant, which is why it could drift at all.
    """

    def test_the_price_bar_is_the_one_the_code_uses(self):
        import shopping
        block = agent.SYSTEM_PROMPT.split("PRICE WATCHES")[1].split("USE THE RIGHT TOOL")[0]
        assert f"${shopping.MOVE_MIN_ABS:.0f}" in block

    def test_the_prompt_never_promises_a_percentage_bar(self):
        """A percentage always encodes an assumption about the kind of product,
        and the watch list holds every kind. Two versions failed that way."""
        block = agent.SYSTEM_PROMPT.split("PRICE WATCHES")[1].split("USE THE RIGHT TOOL")[0]
        assert "%" not in block

    def test_the_price_watch_promise_includes_rises(self):
        block = agent.SYSTEM_PROMPT.split("PRICE WATCHES")[1].split("USE THE RIGHT TOOL")[0]
        assert "rise" in block.lower()

    def test_the_morning_is_described_as_basics_plus_a_link(self):
        """It stopped carrying tracked topics two versions ago; the prompt
        still promised "sports scores, news, Bitcoin price"."""
        block = agent.SYSTEM_PROMPT.split("MORNING BRIEFING")[1].split("PRICE WATCHES")[0]
        assert "page" in block
        assert "commute" in block.lower()

    def test_nothing_claims_flight_watching_is_missing(self):
        """add_flight_watch shipped, and one NEVER bullet still held up "I
        can't watch them for changes yet" as the model sentence to imitate —
        against the routing block's own "Never say you can't track flights"."""
        assert "can't watch them for changes" not in agent.SYSTEM_PROMPT


class TestEveryToolIsRouted:
    def test_the_prompt_names_every_tool_it_ships_with(self):
        """A tool the routing block never mentions is one the model resolves
        from its own description alone — and add_watch's description tells it
        to fire on "a team, a story, a market", which is exactly what the
        block assigns to update_morning_briefing and follow_team."""
        from tools_def import TOOLS
        missing = [t["name"] for t in TOOLS if t["name"] not in agent.SYSTEM_PROMPT]
        assert missing == [], f"tools with no routing guidance: {missing}"

    def test_the_three_way_track_collision_is_resolved(self):
        block = agent.SYSTEM_PROMPT.split("USE THE RIGHT TOOL")[1]
        line = next(l for l in block.split("\n") if l.startswith("- add_watch vs"))
        for other in ("update_morning_briefing", "follow_team"):
            assert other in line

    def test_cancelling_is_routed_too(self):
        """"Stop tracking the Eagles" matches four tools, and guessing deletes
        something they wanted."""
        block = agent.SYSTEM_PROMPT.split("USE THE RIGHT TOOL")[1]
        for verb in ("cancel_watch", "cancel_reminders", "unfollow_team",
                     "unfollow_show", "cancel_price_watch", "cancel_flight_watch"):
            assert verb in block


class TestFailureHasAPolicyOfItsOwn:
    """It used to be a subordinate clause inside the anti-competitor NEVER
    bullet — the prompt said at length what Palmer must not say on a failure
    and almost nothing about what he should."""

    def test_there_is_a_section_for_it(self):
        assert "WHEN A TOOL COMES BACK EMPTY OR BROKEN" in agent.SYSTEM_PROMPT

    def test_it_separates_empty_from_broken_from_incapable(self):
        block = agent.SYSTEM_PROMPT.split("WHEN A TOOL COMES BACK EMPTY OR BROKEN")[1] \
                                   .split("CURATION")[0]
        assert "Empty is not broken" in block
        assert "invent" in block
        assert "somewhere else" in block

    def test_a_failed_lookup_is_not_a_fact_about_the_world(self):
        """The failed SpaceX lookup confirmed the model's own stale prior and
        Palmer told the user the company was private while SPCX was trading."""
        block = agent.SYSTEM_PROMPT.split("WHEN A TOOL COMES BACK EMPTY OR BROKEN")[1] \
                                   .split("CURATION")[0]
        assert "not a company that isn't listed" in block


class TestClarificationOutranksTheRhythmRules:
    """The rule permitting a clarifying question was stated once. The pressure
    against ending on a question was stated four times, three of them
    absolute — including one that is shape-based and unconditional, and so
    fires exactly when a clarification needs a second turn."""

    def test_the_precedence_is_stated(self):
        block = agent.SYSTEM_PROMPT.split("WHEN YOU DON'T KNOW WHAT THEY MEAN")[1] \
                                   .split("READ THE SUBTEXT")[0]
        assert "outranks" in block

    def test_asking_twice_is_allowed_when_it_is_still_unclear(self):
        block = agent.SYSTEM_PROMPT.split("WHEN YOU DON'T KNOW WHAT THEY MEAN")[1] \
                                   .split("READ THE SUBTEXT")[0]
        assert "ask again" in block

    def test_a_resolved_thing_is_named_back_not_confirmed_silently(self):
        block = agent.SYSTEM_PROMPT.split("WHEN YOU DON'T KNOW WHAT THEY MEAN")[1] \
                                   .split("READ THE SUBTEXT")[0]
        assert "resolved" in block
        assert "correct it" in block
