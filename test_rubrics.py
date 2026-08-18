"""Tests for the genre classifier and per-genre rubrics.

Haiku is mocked; we're testing that:
  1. The classifier normalizes Haiku's reply into a VALID_GENRES value.
  2. In-process memoization actually short-circuits the second call.
  3. Every declared genre has a non-empty rubric.
  4. rubric_for() falls back safely on unknown or None input.
"""
from unittest.mock import patch, MagicMock

import rubrics


def _haiku_reply(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


class TestRubricCoverage:
    def test_every_genre_has_content(self):
        for g in rubrics.VALID_GENRES:
            body = rubrics.GENRE_RUBRICS.get(g, "")
            assert body.strip(), f"genre {g!r} has empty rubric"
            # Rubric must have both sides ("would text" and "wouldn't")
            low = body.lower()
            assert "would text about" in low, f"genre {g!r} missing 'would text about' block"
            assert "nobody would text" in low or g == "other", \
                f"genre {g!r} missing 'nobody would text' block"

    def test_rubric_for_known_genre(self):
        assert rubrics.rubric_for("sports_team") == rubrics.GENRE_RUBRICS["sports_team"]

    def test_rubric_for_none_falls_back_to_other(self):
        assert rubrics.rubric_for(None) == rubrics.GENRE_RUBRICS["other"]

    def test_rubric_for_unknown_falls_back_to_other(self):
        assert rubrics.rubric_for("kittens") == rubrics.GENRE_RUBRICS["other"]


class TestClassifyGenre:
    def setup_method(self):
        rubrics._reset_cache_for_tests()

    def test_exact_reply_maps(self):
        with patch("rubrics.client") as mock_client:
            mock_client.messages.create.return_value = _haiku_reply("sports_team")
            assert rubrics.classify_genre("Cardinals") == "sports_team"

    def test_normalizes_reply_with_punctuation(self):
        with patch("rubrics.client") as mock_client:
            mock_client.messages.create.return_value = _haiku_reply("Category: market_instrument.")
            assert rubrics.classify_genre("AAPL stock") == "market_instrument"

    def test_uppercase_reply_maps(self):
        with patch("rubrics.client") as mock_client:
            mock_client.messages.create.return_value = _haiku_reply("GEOPOLITICS")
            assert rubrics.classify_genre("Iran conflict") == "geopolitics"

    def test_unknown_reply_falls_back_to_other(self):
        with patch("rubrics.client") as mock_client:
            mock_client.messages.create.return_value = _haiku_reply("nonsense reply here")
            assert rubrics.classify_genre("something") == "other"

    def test_api_failure_falls_back_to_other(self):
        with patch("rubrics.client") as mock_client:
            mock_client.messages.create.side_effect = RuntimeError("boom")
            assert rubrics.classify_genre("anything") == "other"

    def test_empty_topic_returns_other_without_api_call(self):
        with patch("rubrics.client") as mock_client:
            assert rubrics.classify_genre("") == "other"
            assert rubrics.classify_genre("   ") == "other"
            mock_client.messages.create.assert_not_called()

    def test_memoization_short_circuits_second_call(self):
        with patch("rubrics.client") as mock_client:
            mock_client.messages.create.return_value = _haiku_reply("sports_team")
            rubrics.classify_genre("Cardinals")
            rubrics.classify_genre("Cardinals")
            rubrics.classify_genre("cardinals")   # case-insensitive
            rubrics.classify_genre("  Cardinals  ")   # trim
            assert mock_client.messages.create.call_count == 1

    def test_memoization_distinct_topics_call_separately(self):
        with patch("rubrics.client") as mock_client:
            mock_client.messages.create.side_effect = [
                _haiku_reply("sports_team"),
                _haiku_reply("market_instrument"),
            ]
            assert rubrics.classify_genre("Cardinals") == "sports_team"
            assert rubrics.classify_genre("AAPL") == "market_instrument"
            assert mock_client.messages.create.call_count == 2

    def test_failure_result_is_cached(self):
        """Once 'other' is cached for a topic, a later successful reply won't override it.
        This is deliberate — repeated Haiku hits on the same string are wasted budget."""
        with patch("rubrics.client") as mock_client:
            mock_client.messages.create.side_effect = RuntimeError("boom")
            assert rubrics.classify_genre("weird") == "other"
        with patch("rubrics.client") as mock_client:
            mock_client.messages.create.return_value = _haiku_reply("sports_team")
            assert rubrics.classify_genre("weird") == "other"
            mock_client.messages.create.assert_not_called()
