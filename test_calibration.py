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
    return agent.SYSTEM_PROMPT.format(date="Monday, January 01, 2026", now_utc="12:00",
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
        assert "Monday, January 01, 2026" in out
        assert "12:00" in out


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
