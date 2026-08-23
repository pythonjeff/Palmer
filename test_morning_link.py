"""The morning update is a link plus one line.

The briefing itself lives on the user's page; the text above it exists to say
why today is worth a tap. Two properties are load-bearing and tested here:

1. The URL is LAST in the message and nothing follows it. Message apps only
   render the rich preview when the link sits at a boundary, and that preview
   is the reason this shape works at all.
2. Every failure in the page path falls back to the full text briefing. A user
   never gets a link to an empty page, and never gets silence.
"""
from unittest.mock import patch

import pytest

import morning


PAYLOAD = {
    "city": "Kirkwood, MO",
    "weather": {"temp_now": 71, "high": 88, "low": 64, "description": "clear", "rain_pct": 10},
    "traffic": {"live_min": 34, "delay_min": 9},
    "prices": [{"label": "BTC", "pct_24h": -4.2}],
    "headlines": [{"title": "Starship static fire slips a week", "topic": "SpaceX news"}],
}

URL = "https://palmer.example.com/h/AbC123xyz"


class TestDigest:
    def test_carries_every_section_as_plain_lines(self):
        d = morning._payload_digest(PAYLOAD)
        assert "Kirkwood, MO" in d and "clear" in d
        assert "34 min" in d and "9 min slower" in d
        assert "BTC: -4.2%" in d
        assert "Starship static fire" in d

    def test_normal_commute_is_not_dressed_up_as_a_delay(self):
        d = morning._payload_digest({"traffic": {"live_min": 20, "delay_min": 0}})
        assert "normal" in d and "slower" not in d

    def test_empty_payload_is_falsy_so_callers_can_gate_on_it(self):
        assert morning._payload_digest({}) == ""
        assert not morning._payload_digest({"weather": {}, "prices": [], "headlines": []})


class TestComposeShape:
    def _compose(self, line="Cool 71 and clear, but your drive is 9 min heavier than usual."):
        with patch("home.ensure_fresh", return_value=URL), \
             patch("home.load", return_value=PAYLOAD), \
             patch("home.home_token", return_value="tok"), \
             patch.object(morning, "generate_morning_line", return_value=line), \
             patch.object(morning, "generate_morning") as full:
            msg, carries = morning._compose_morning("+1555")
        return msg, carries, full

    def test_url_is_last_with_nothing_after_it(self):
        msg, carries, full = self._compose()
        assert carries is True
        assert msg.endswith(URL), "a trailing character kills the link preview"
        full.assert_not_called()

    def test_message_carries_exactly_one_url(self):
        msg, _, _ = self._compose()
        assert msg.count("http") == 1, "a second URL suppresses the preview"

    def test_the_line_survives_ahead_of_the_link(self):
        msg, _, _ = self._compose(line="Bitcoin is off 4.2% overnight.")
        assert msg.startswith("Bitcoin is off 4.2% overnight. ")

    def test_it_is_one_message_not_two(self):
        """The briefing used to be its own text with the link chasing it."""
        msg, _, _ = self._compose()
        assert len(msg) < 900, "must fit one send, not trigger the chunker"


class TestFallbacks:
    def _compose_with(self, **patches):
        defaults = {"ensure_fresh": URL, "load": PAYLOAD, "home_token": "tok"}
        defaults.update(patches)
        with patch("home.ensure_fresh", return_value=defaults["ensure_fresh"]), \
             patch("home.load", return_value=defaults["load"]), \
             patch("home.home_token", return_value=defaults["home_token"]), \
             patch.object(morning, "generate_morning", return_value="the long briefing"), \
             patch.object(morning, "generate_morning_line",
                          side_effect=defaults.get("line_error") or (lambda *a, **k: "a line")):
            return morning._compose_morning("+1555")

    def test_missing_app_url_falls_back_to_the_text_briefing(self):
        msg, carries = self._compose_with(ensure_fresh="/h/tok")
        assert msg == "the long briefing" and carries is False

    def test_empty_page_falls_back_rather_than_linking_to_nothing(self):
        msg, carries = self._compose_with(load={})
        assert msg == "the long briefing" and carries is False

    def test_missing_page_falls_back(self):
        msg, carries = self._compose_with(load=None)
        assert msg == "the long briefing" and carries is False

    def test_a_failed_line_draft_falls_back(self):
        msg, carries = self._compose_with(line_error=RuntimeError("sonnet down"))
        assert msg == "the long briefing" and carries is False

    def test_the_fallback_never_carries_a_link(self):
        """carries_link gates the /sms-status shorten-and-retry, which would
        happily cut a URL in half. A text briefing must keep that retry."""
        for kw in ({"ensure_fresh": "/h/tok"}, {"load": {}},
                   {"line_error": RuntimeError("x")}):
            assert self._compose_with(**kw)[1] is False


class TestLinkPlaceholder:
    """The drafter knows a link is coming and reaches for a stand-in. Left in,
    "[link]" ships as literal text sitting next to the real URL."""

    @pytest.mark.parametrize("raw", [
        "Arenado to the Dodgers. [link]",
        "Arenado to the Dodgers. (url)",
        "Arenado to the Dodgers. <here>",
        "Arenado to the Dodgers. [Link]",
        "Arenado to the Dodgers. (page)",
        "Arenado to the Dodgers. [dashboard]",
    ])
    def test_placeholders_are_stripped(self, raw):
        assert morning._strip_link_placeholder(raw) == "Arenado to the Dodgers."

    def test_an_invented_url_is_stripped(self):
        """A URL Palmer made up is worse than an awkward sentence — and it
        would also break the preview by making this the second link."""
        out = morning._strip_link_placeholder("Big day https://made.up/thing")
        assert "http" not in out

    def test_a_dangling_separator_goes_with_it(self):
        assert morning._strip_link_placeholder("Rain all day -") == "Rain all day"

    def test_a_clean_line_is_untouched(self):
        line = "Cool 71 and clear, and your drive is 9 min heavier than usual."
        assert morning._strip_link_placeholder(line) == line

    def test_ordinary_brackets_survive(self):
        line = "Cards lost (again)."
        assert morning._strip_link_placeholder(line) == line


class _Block:
    def __init__(self, t): self.text = t


class _Resp:
    def __init__(self, t): self.content = [_Block(t)]


def _draft_returning(*texts):
    """Run the drafter against a scripted sequence of model outputs."""
    calls = []

    def _create(**kw):
        calls.append(kw)
        return _Resp(texts[min(len(calls) - 1, len(texts) - 1)])

    with patch.object(morning, "get_profile", return_value={"timezone": "America/Chicago"}), \
         patch.object(morning, "_build_system", return_value="sys"), \
         patch.object(morning, "_recent_assistant_texts", return_value=[]), \
         patch.object(morning.client.messages, "create", side_effect=_create):
        return morning.generate_morning_line("+1555", PAYLOAD), calls


class TestNamingTheLink:
    """"page has your full rundown" turns a text from a friend into a push
    notification. The prompt asks for this; the code enforces it."""

    @pytest.mark.parametrize("bad", [
        "Morning Jeff, page has your full rundown.",
        "Morning Jeff, everything's on your dashboard.",
        "Morning Jeff, click through for the details.",
        "Morning Jeff, the link has today's stuff.",
        "Morning Jeff, tap here for more.",
    ])
    def test_a_line_that_names_the_link_is_redrafted(self, bad):
        out, calls = _draft_returning(bad, "Morning Jeff, cool and clear.")
        assert out == "Morning Jeff, cool and clear."
        assert len(calls) == 2, "must redraft exactly once"

    def test_a_clean_line_is_not_redrafted(self):
        out, calls = _draft_returning("Morning Jeff, cool and clear.")
        assert out == "Morning Jeff, cool and clear."
        assert len(calls) == 1, "a clean line must not cost a second call"

    def test_the_redraft_is_told_what_it_did_wrong(self):
        _, calls = _draft_returning("Check the page.", "Cool and clear today.")
        assert "Check the page." in calls[1]["messages"][0]["content"]

    def test_it_gives_up_after_one_redraft(self):
        """Two calls, not a loop — a stubborn model must not burn the budget."""
        out, calls = _draft_returning("Check the page.", "Still on your page.")
        assert len(calls) == 2
        assert out, "a second violation still ships rather than failing the morning"

    def test_ordinary_words_are_not_false_positives(self):
        line = "Morning Jeff, the Cards page-turner of a ninth inning aside, cool and clear."
        out, calls = _draft_returning("Morning Jeff, cool and clear and 81.")
        assert len(calls) == 1
        assert not morning._NAMES_THE_LINK.search("Cool and clear, pages of rain later")


class TestLineDiscipline:
    def _draft(self, text):
        return _draft_returning(text)[0]

    def test_long_output_is_trimmed_on_a_word_boundary(self):
        out = self._draft("word " * 200)
        assert len(out) <= morning.MORNING_LINE_MAX
        assert not out.endswith("wor"), "must not cut mid-word"

    def test_newlines_are_flattened_to_one_line(self):
        out = self._draft("Cool and clear today.\n\nBitcoin is down.")
        assert "\n" not in out

    def test_empty_output_raises_so_the_caller_can_fall_back(self):
        with pytest.raises(ValueError):
            self._draft("   ")

    def test_meta_commentary_raises(self):
        with pytest.raises(ValueError):
            self._draft("Not sending the weather since they asked me to skip it.")

    def test_a_normal_line_passes_through_untouched(self):
        assert self._draft("Cool 71 and clear.") == "Cool 71 and clear."

    def test_the_drafter_strips_a_placeholder_before_returning(self):
        assert self._draft("Cool 71 and clear. [link]") == "Cool 71 and clear."

    def test_the_prompt_also_warns_against_placeholders(self):
        assert "placeholder" in TestPrompt()._prompt()["messages"][0]["content"].lower()


class TestPrompt:
    def _prompt(self):
        return _draft_returning("Cool 71 and clear.")[1][0]

    def test_drafts_in_palmers_voice_not_a_second_persona(self):
        """Every user-facing message goes through _build_system. A one-line
        greeting is still a user-facing message."""
        assert self._prompt()["system"] == "sys"

    def test_drafts_on_sonnet_like_every_other_user_facing_message(self):
        assert self._prompt()["model"] == morning.SONNET_MODEL

    def test_the_page_data_reaches_the_drafter(self):
        body = self._prompt()["messages"][0]["content"]
        assert "Starship static fire" in body and "34 min" in body

    def test_it_is_told_not_to_name_the_link(self):
        body = self._prompt()["messages"][0]["content"].lower()
        for word in ("link", "page", "dashboard", "click"):
            assert word in body, f"the prompt must explicitly ban {word!r}"
