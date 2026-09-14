"""add_price_watch's dispatch used to insert a row with baseline_price=NULL and
defer baseline-setting to the next scheduler tick (12h later), unlike
add_amazon_watch which seeds it immediately. If that first scheduler-side
match ever failed, the baseline stayed NULL forever and run_price_watches
could never reach the alert comparison — a real drop would just get silently
recorded as the (late) baseline with no alert. Seed it at creation time,
same as Amazon, so a bad match is visible immediately instead of silent."""
from unittest.mock import patch

from palmer import agent
from tests.helpers import drive_tool


def _drive_price_watch(tool_input, check_price_result):
    _, result, (save, set_baseline, _check) = drive_tool(
        "add_price_watch", tool_input, message="track this for me",
        profile={"timezone": "America/Chicago"},
        patches=[patch.object(agent, "save_price_watch", return_value=42),
                 patch.object(agent, "set_price_watch_baseline"),
                 patch("palmer.shopping.check_price", return_value=check_price_result)])
    return result, save, set_baseline


class TestBaselineSeededAtCreation:
    def test_a_successful_match_seeds_the_baseline_immediately(self):
        current = {"price": 29.99, "url": "https://example.com/p", "merchant": "Target"}
        result, save, set_baseline = _drive_price_watch({"product_name": "Premier Protein Chocolate 30-pack"}, current)
        save.assert_called_once()
        set_baseline.assert_called_once_with(42, 29.99, "https://example.com/p", "Target")
        assert "29.99" in result

    def test_a_failed_match_does_not_seed_a_baseline_but_still_creates_the_watch(self):
        result, save, set_baseline = _drive_price_watch({"product_name": "some obscure item"}, None)
        save.assert_called_once()
        set_baseline.assert_not_called()
        assert "couldn't pin down a confident match" in result.lower()


class TestTheWatchNamesWhatItMatched:
    """add_amazon_watch echoes the resolved listing; this path echoed the
    user's own words back. The match is picked by a model with no confidence
    floor, so "AirPods" can baseline on Gen 2, Gen 4 or Pro — and a wrong pick
    stayed invisible until an alert arrived about the wrong product."""

    def test_the_matched_title_is_in_the_tool_result(self):
        import inspect
        from palmer import agent
        block = inspect.getsource(agent.get_reply).split('"add_price_watch"')[1] \
                                                  .split("elif b.name")[0]
        assert 'current.get("title")' in block
        assert "It matched:" in block

    def test_the_model_is_told_to_say_it_out_loud(self):
        """A resolved thing named only in the tool result is still invisible
        to the person who can correct it."""
        import inspect
        from palmer import agent
        block = inspect.getsource(agent.get_reply).split('"add_price_watch"')[1] \
                                                  .split("elif b.name")[0]
        assert "correct you" in block
