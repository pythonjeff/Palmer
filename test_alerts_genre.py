"""Tests for pre-classified interests and genre-aware significance in alerts.py."""
from unittest.mock import patch, MagicMock

import rubrics
import alerts


def _haiku_reply(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


class TestResolveInterestGenres:
    def setup_method(self):
        rubrics._reset_cache_for_tests()

    def test_uses_cached_genres_from_profile(self):
        profile = {"interest_genres": {"cardinals": "sports_team", "aapl": "market_instrument"}}
        with patch("alerts.classify_genre") as mock_classify, \
             patch("alerts.upsert_profile") as mock_upsert:
            out = alerts._resolve_interest_genres("+15550000000", profile,
                                                  ["Cardinals", "AAPL"])
        assert out == [("Cardinals", "sports_team"), ("AAPL", "market_instrument")]
        mock_classify.assert_not_called()
        mock_upsert.assert_not_called()

    def test_classifies_and_persists_new_interests(self):
        profile = {"interest_genres": {"cardinals": "sports_team"}}
        with patch("alerts.classify_genre", return_value="market_instrument") as mock_classify, \
             patch("alerts.upsert_profile") as mock_upsert:
            out = alerts._resolve_interest_genres("+15550000000", profile,
                                                  ["Cardinals", "Bitcoin"])
        assert out == [("Cardinals", "sports_team"), ("Bitcoin", "market_instrument")]
        mock_classify.assert_called_once_with("Bitcoin")
        # Single upsert with the merged dict
        mock_upsert.assert_called_once()
        args = mock_upsert.call_args
        assert args.args[0] == "+15550000000"
        updates = args.args[1]
        assert updates["interest_genres"]["cardinals"] == "sports_team"
        assert updates["interest_genres"]["bitcoin"] == "market_instrument"

    def test_skips_blank_topics(self):
        profile = {}
        with patch("alerts.classify_genre", return_value="sports_team"), \
             patch("alerts.upsert_profile"):
            out = alerts._resolve_interest_genres("+15550000000", profile,
                                                  ["", "  ", "Eagles"])
        assert out == [("Eagles", "sports_team")]


class TestCheckSignificancePrompt:
    def _capture_prompt(self):
        captured: list[str] = []

        def _create(**kwargs):
            captured.append(kwargs["messages"][0]["content"])
            return _haiku_reply('{"score": 5, "summary": "", "interest": ""}')

        return _create, captured

    def test_prompt_lists_each_interest_with_genre(self):
        create, captured = self._capture_prompt()
        interests = [("Cardinals", "sports_team"), ("AAPL", "market_instrument")]
        with patch("alerts.client") as mock_client:
            mock_client.messages.create.side_effect = create
            alerts._check_significance("some search results", {}, interests)
        prompt = captured[0]
        assert "Cardinals (sports_team)" in prompt
        assert "AAPL (market_instrument)" in prompt
        # Both applicable rubrics should be present
        assert "Rubric for sports_team" in prompt
        assert "Rubric for market_instrument" in prompt

    def test_unrelated_rubrics_are_not_included(self):
        create, captured = self._capture_prompt()
        interests = [("Cardinals", "sports_team")]
        with patch("alerts.client") as mock_client:
            mock_client.messages.create.side_effect = create
            alerts._check_significance("some search results", {}, interests)
        prompt = captured[0]
        assert "Rubric for sports_team" in prompt
        # No mortgage/geopolitics rubric leak
        assert "Rubric for market_instrument" not in prompt
        assert "Rubric for geopolitics" not in prompt

    def test_missing_interests_uses_other_rubric(self):
        """Older callers that don't pass the interests= kwarg still get scored,
        with the strict 'other' rubric as the backstop."""
        create, captured = self._capture_prompt()
        with patch("alerts.client") as mock_client:
            mock_client.messages.create.side_effect = create
            alerts._check_significance("some search results", {"morning_topics": []})
        prompt = captured[0]
        # 'general news' is the interest_str fallback in _check_significance
        assert "general news" in prompt.lower()

    def test_parses_json_score(self):
        with patch("alerts.client") as mock_client:
            mock_client.messages.create.return_value = _haiku_reply(
                '{"score": 9, "summary": "Cardinals moved into first place today", "interest": "Cardinals"}'
            )
            score, summary = alerts._check_significance(
                "results", {}, [("Cardinals", "sports_team")]
            )
        assert score == 9
        assert "first place" in summary

    def test_parses_json_array_and_picks_highest(self):
        """Haiku sometimes returns one entry per interest as a JSON array even
        when we asked for a single object. Verified in live probe. We collapse
        to the highest-scoring entry so a strong hit on one interest still fires."""
        with patch("alerts.client") as mock_client:
            mock_client.messages.create.return_value = _haiku_reply('''[
                {"score": 3, "summary": "", "interest": ""},
                {"score": 9, "summary": "BTC crashed 12% today", "interest": "Bitcoin"}
            ]''')
            score, summary = alerts._check_significance(
                "results", {}, [("Cardinals", "sports_team"), ("Bitcoin", "market_instrument")]
            )
        assert score == 9
        assert "BTC" in summary
