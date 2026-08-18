"""Tests for the story-arc gate in watches.py.

The semantic dedup that answers 'the user already knows this — is the new
candidate ADVANCING the story, or just a rehash?' — separate from the
title-based recent_summaries gate.
"""
from unittest.mock import patch, MagicMock

import watches as watches_mod


def _haiku_reply(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


def _capture_prompt(reply_text: str):
    captured: list[str] = []

    def _create(**kwargs):
        captured.append(kwargs["messages"][0]["content"])
        return _haiku_reply(reply_text)

    return _create, captured


class TestCheckWatchHitStoryBlock:
    def test_no_story_state_omits_story_block(self):
        create, captured = _capture_prompt("YES")
        with patch("watches.client") as mock_client:
            mock_client.messages.create.side_effect = create
            watches_mod._check_watch_hit(
                results="something happened",
                description="Cardinals",
                recent_summaries=[],
                engaged=False,
                genre="sports_team",
                story_state=None,
            )
        prompt = captured[0]
        assert "Current story state" not in prompt

    def test_story_state_included_in_prompt(self):
        create, captured = _capture_prompt("NO")
        with patch("watches.client") as mock_client:
            mock_client.messages.create.side_effect = create
            watches_mod._check_watch_hit(
                results="Cardinals win again to extend streak to 7",
                description="Cardinals",
                recent_summaries=[],
                engaged=False,
                genre="sports_team",
                story_state="Cardinals are on a six-game winning streak, moved into first place.",
            )
        prompt = captured[0]
        assert "Current story state" in prompt
        assert "six-game winning streak" in prompt
        # Prompt should also frame the ADVANCE-vs-rehash question
        assert "advance" in prompt.lower()

    def test_yes_reply_still_fires(self):
        with patch("watches.client") as mock_client:
            mock_client.messages.create.return_value = _haiku_reply("YES")
            assert watches_mod._check_watch_hit(
                results="candidate", description="d", recent_summaries=[],
                engaged=False, genre="sports_team",
                story_state="prior state",
            ) is True


class TestUpdateStoryState:
    def test_persists_haiku_summary(self):
        with patch("watches.client") as mock_client, \
             patch("watches.update_watch_story") as mock_update:
            mock_client.messages.create.return_value = _haiku_reply(
                "Cardinals extended their winning streak to seven with a 4-2 win over the Cubs."
            )
            watches_mod._update_story_state(
                watch_id=42,
                previous_state="Cardinals on a six-game streak, in first place.",
                new_alert_title="Cardinals beat Cubs 4-2 for seventh straight win",
                new_alert_content="The Cardinals defeated the Cubs 4-2 Tuesday...",
            )
        mock_update.assert_called_once()
        args = mock_update.call_args.args
        assert args[0] == 42
        assert "seven" in args[1].lower() or "streak" in args[1].lower()

    def test_no_previous_state_still_seeds(self):
        with patch("watches.client") as mock_client, \
             patch("watches.update_watch_story") as mock_update:
            mock_client.messages.create.return_value = _haiku_reply(
                "Cardinals moved into first place with a win over the Cubs."
            )
            watches_mod._update_story_state(
                watch_id=1, previous_state=None,
                new_alert_title="Cardinals in first place",
                new_alert_content="",
            )
        mock_update.assert_called_once()
        # First-alert seeding: prompt contains 'first alert' marker so Haiku knows
        prompt = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "first alert" in prompt.lower()

    def test_haiku_failure_swallowed(self):
        with patch("watches.client") as mock_client, \
             patch("watches.update_watch_story") as mock_update:
            mock_client.messages.create.side_effect = RuntimeError("boom")
            # Must not raise — the alert already went out; losing the state
            # update only costs us dedup benefit on the next tick.
            watches_mod._update_story_state(
                watch_id=1, previous_state=None,
                new_alert_title="t", new_alert_content="c",
            )
        mock_update.assert_not_called()

    def test_empty_haiku_reply_no_persist(self):
        with patch("watches.client") as mock_client, \
             patch("watches.update_watch_story") as mock_update:
            mock_client.messages.create.return_value = _haiku_reply("   ")
            watches_mod._update_story_state(
                watch_id=1, previous_state=None,
                new_alert_title="t", new_alert_content="c",
            )
        mock_update.assert_not_called()

    def test_summary_truncated_to_400_chars(self):
        long_text = "x" * 900
        with patch("watches.client") as mock_client, \
             patch("watches.update_watch_story") as mock_update:
            mock_client.messages.create.return_value = _haiku_reply(long_text)
            watches_mod._update_story_state(
                watch_id=1, previous_state=None,
                new_alert_title="t", new_alert_content="c",
            )
        args = mock_update.call_args.args
        assert len(args[1]) == 400
