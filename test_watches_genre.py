"""Tests for lazy genre classification + rubric-in-prompt inside watches.py.

Haiku is mocked; we're testing that:
  1. _watch_genre returns the stored genre without classifying when set.
  2. _watch_genre classifies + persists via set_watch_genre when missing.
  3. _check_watch_hit splices the correct rubric into the Haiku prompt.
"""
from unittest.mock import patch, MagicMock

import rubrics
import watches as watches_mod


def _haiku_reply(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


class TestWatchGenre:
    def setup_method(self):
        rubrics._reset_cache_for_tests()

    def test_stored_genre_is_returned_verbatim(self):
        with patch("watches.classify_genre") as mock_classify, \
             patch("watches.set_watch_genre") as mock_set:
            watch = {"id": 1, "description": "Cardinals", "genre": "sports_team"}
            assert watches_mod._watch_genre(watch) == "sports_team"
            mock_classify.assert_not_called()
            mock_set.assert_not_called()

    def test_missing_genre_triggers_classify_and_persist(self):
        with patch("watches.classify_genre", return_value="sports_team") as mock_classify, \
             patch("watches.set_watch_genre") as mock_set:
            watch = {"id": 42, "description": "Cardinals winning streak", "genre": None}
            assert watches_mod._watch_genre(watch) == "sports_team"
            mock_classify.assert_called_once_with("Cardinals winning streak")
            mock_set.assert_called_once_with(42, "sports_team")
            # The dict gets updated in place so a second call skips both
            assert watch["genre"] == "sports_team"

    def test_persist_failure_does_not_raise(self):
        with patch("watches.classify_genre", return_value="market_instrument"), \
             patch("watches.set_watch_genre", side_effect=RuntimeError("db down")):
            watch = {"id": 7, "description": "Bitcoin", "genre": None}
            # Should still return the classified genre and not blow up
            assert watches_mod._watch_genre(watch) == "market_instrument"
            assert watch["genre"] == "market_instrument"


class TestCheckWatchHitPrompt:
    def _capture_prompt(self):
        """Return (client_patch_ctx, captured) so callers can .append to captured."""
        captured: list[str] = []

        def _create(**kwargs):
            captured.append(kwargs["messages"][0]["content"])
            return _haiku_reply("YES")

        return _create, captured

    def test_prompt_contains_genre_rubric_text(self):
        create, captured = self._capture_prompt()
        with patch("watches.client") as mock_client:
            mock_client.messages.create.side_effect = create
            hit = watches_mod._check_watch_hit(
                results="Cardinals moved into first place today.",
                description="St. Louis Cardinals",
                recent_summaries=[],
                engaged=False,
                genre="sports_team",
            )
        assert hit is True
        prompt = captured[0]
        assert "Genre: sports_team" in prompt
        # A distinctive line from the sports rubric — proves the rubric text was spliced in
        assert "streaks starting to matter" in prompt.lower()
        # And NOT rubric text from an unrelated genre
        assert "mortgage" not in prompt.lower()

    def test_prompt_uses_other_rubric_when_genre_unknown(self):
        create, captured = self._capture_prompt()
        with patch("watches.client") as mock_client:
            mock_client.messages.create.side_effect = create
            watches_mod._check_watch_hit(
                results="Something happened.",
                description="Weird watch",
                recent_summaries=[],
                engaged=False,
                genre="not_a_real_genre",
            )
        prompt = captured[0]
        # rubric_for() falls back to 'other'; assert the header line still lists genre
        assert "Genre: not_a_real_genre" in prompt
        assert "genuinely notable, time-sensitive development" in prompt.lower()

    def test_engaged_footer_lowers_bar(self):
        create, captured = self._capture_prompt()
        with patch("watches.client") as mock_client:
            mock_client.messages.create.side_effect = create
            watches_mod._check_watch_hit(
                results="Some update.",
                description="Cardinals",
                recent_summaries=[],
                engaged=True,
                genre="sports_team",
            )
        prompt = captured[0]
        assert "following this closely" in prompt.lower()

    def test_no_reply_is_no(self):
        with patch("watches.client") as mock_client:
            mock_client.messages.create.return_value = _haiku_reply("NO — routine game")
            hit = watches_mod._check_watch_hit(
                results="Cardinals lost 5-4 to the Reds.",
                description="Cardinals",
                recent_summaries=[],
                engaged=False,
                genre="sports_team",
            )
        assert hit is False
