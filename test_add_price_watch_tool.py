"""add_price_watch's dispatch used to insert a row with baseline_price=NULL and
defer baseline-setting to the next scheduler tick (12h later), unlike
add_amazon_watch which seeds it immediately. If that first scheduler-side
match ever failed, the baseline stayed NULL forever and run_price_watches
could never reach the alert comparison — a real drop would just get silently
recorded as the (late) baseline with no alert. Seed it at creation time,
same as Amazon, so a bad match is visible immediately instead of silent."""
from unittest.mock import patch

import agent


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, content, stop_reason):
        self.content, self.stop_reason = content, stop_reason


def _drive(tool_input, check_price_result):
    calls = []
    responses = [
        _Resp([_Block(type="tool_use", name="add_price_watch", id="t1", input=tool_input)], "tool_use"),
        _Resp([_Block(type="text", text="done")], "end_turn"),
    ]

    def _create(**kw):
        calls.append(kw)
        return responses[len(calls) - 1]

    with patch.object(agent, "_build_system", return_value="sys"), \
         patch.object(agent, "get_history", return_value=[]), \
         patch.object(agent, "get_profile", return_value={"timezone": "America/Chicago"}), \
         patch.object(agent, "save_price_watch", return_value=42) as save, \
         patch.object(agent, "set_price_watch_baseline") as set_baseline, \
         patch("shopping.check_price", return_value=check_price_result), \
         patch.object(agent.client.messages, "create", side_effect=_create):
        agent.get_reply("+1555", "track this for me")
    result = calls[1]["messages"][-1]["content"][0]["content"]
    return result, save, set_baseline


class TestBaselineSeededAtCreation:
    def test_a_successful_match_seeds_the_baseline_immediately(self):
        current = {"price": 29.99, "url": "https://example.com/p", "merchant": "Target"}
        result, save, set_baseline = _drive({"product_name": "Premier Protein Chocolate 30-pack"}, current)
        save.assert_called_once()
        set_baseline.assert_called_once_with(42, 29.99, "https://example.com/p", "Target")
        assert "29.99" in result

    def test_a_failed_match_does_not_seed_a_baseline_but_still_creates_the_watch(self):
        result, save, set_baseline = _drive({"product_name": "some obscure item"}, None)
        save.assert_called_once()
        set_baseline.assert_not_called()
        assert "couldn't pin down a confident match" in result.lower()
