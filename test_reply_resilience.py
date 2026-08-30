"""A turn that needs one tool call too many must still answer.

get_reply capped the tool loop at six and RAISED past it. main.py catches that,
leaves `reply` as None, and answers a falsy reply with FALLBACK_SMS — so a turn
like "add Apple, Nvidia and Tesla, then what's my commute" died outright, threw
away every tool result it had already gathered, and told the user something went
sideways. Same shape as the deliberation guard: machinery meant to protect the
user producing the thing the user complained about.
"""
from unittest.mock import MagicMock, patch

import agent


def _blocks(*, text=None, tool=None):
    out = []
    if tool:
        b = MagicMock()
        b.type = "tool_use"
        b.name = tool
        b.id = "tu_1"
        b.input = {"query": "x"}
        del b.text          # so `hasattr(b, "text")` is False
        out.append(b)
    if text is not None:
        t = MagicMock()
        t.type = "text"
        t.text = text
        out.append(t)
    return out


class TestTrimToSentence:
    def test_a_truncated_draft_is_cut_back_to_a_boundary(self):
        out = agent._trim_to_sentence("Sure. 90 today, commute is 22 min. Anything el")
        assert out == "Sure. 90 today, commute is 22 min."

    def test_a_complete_draft_is_untouched(self):
        assert agent._trim_to_sentence("90 today.") == "90 today."

    def test_no_boundary_at_all_keeps_the_fragment(self):
        """A short fragment still beats nothing — there is no second draft."""
        assert agent._trim_to_sentence("no boundary here") == "no boundary here"

    def test_it_never_returns_something_uselessly_short(self):
        # Trimming "Ok. <long truncated clause>" back to "Ok." would be worse
        # than shipping the fragment.
        out = agent._trim_to_sentence("Ok. and then the thing about the fare wa")
        assert out.startswith("Ok. and then")

    def test_empty_is_safe(self):
        assert agent._trim_to_sentence("") == ""


class TestMaxTokensDoesNotShipHalfASentence:
    def test_the_reply_is_trimmed(self):
        resp = MagicMock(stop_reason="max_tokens",
                         content=_blocks(text="Sure. 90 today. Anything el"))
        with patch.object(agent.client.messages, "create", return_value=resp), \
             patch.object(agent, "_build_system", return_value="sys"), \
             patch.object(agent, "get_profile", return_value={}), \
             patch.object(agent, "get_history", return_value=[]):
            out, _ = agent.get_reply("+15550001111", "hi", history=[])
        assert out == "Sure. 90 today."


class TestTheLoopAnswersInsteadOfDying:
    def _run(self, final_text):
        """Every response asks for another tool, so the cap is always reached."""
        calls = []

        def _create(**kw):
            calls.append(kw)
            if "tools" not in kw:          # the final, tool-less ask
                return MagicMock(stop_reason="end_turn",
                                 content=_blocks(text=final_text))
            return MagicMock(stop_reason="tool_use", content=_blocks(tool="web_search"))

        with patch.object(agent.client.messages, "create", side_effect=_create), \
             patch.object(agent, "_build_system", return_value="sys"), \
             patch.object(agent, "get_profile", return_value={}), \
             patch.object(agent, "get_history", return_value=[]), \
             patch.object(agent, "_search", return_value="some results"):
            out, _ = agent.get_reply("+15550001111", "do lots", history=[])
        return out, calls

    def test_it_returns_an_answer_rather_than_raising(self):
        out, _ = self._run("here's what I found, in short")
        assert out == "here's what I found, in short"

    def test_the_final_ask_has_no_tools(self):
        """Tools are taken away so the model must answer from what it gathered."""
        _, calls = self._run("answer")
        assert "tools" not in calls[-1]

    def test_it_stops_at_the_cap(self):
        _, calls = self._run("answer")
        # One call per iteration, plus the final tool-less one.
        assert len(calls) == agent.TOOL_ITERATION_CAP + 1

    def test_the_gathered_work_is_carried_into_the_final_ask(self):
        _, calls = self._run("answer")
        assert len(calls[-1]["messages"]) > 1, "tool results must survive"

    def test_the_cap_is_high_enough_for_ordinary_asks(self):
        """Three tickers plus a commute is five calls before Palmer speaks."""
        assert agent.TOOL_ITERATION_CAP >= 8
