"""Tests for the adjacent-story pick.

This is the one part of the briefing that isn't something the user asked for,
so the guards matter more than the feature: it must never invent a trend, never
repeat what the briefing already covers, and must fail to silence rather than to
filler. Haiku is mocked — its judgement quality is checked live, not here.
"""
from datetime import date
from unittest.mock import patch, MagicMock

import trends


def _reply(text: str) -> MagicMock:
    b = MagicMock(); b.text = text
    r = MagicMock(); r.content = [b]
    return r


CANDIDATES = [
    {"query": "jeff bezos zero income tax idea", "volume": 200000, "categories": ["Business"]},
    {"query": "espanyol vs real madrid", "volume": 500000, "categories": ["Sports"]},
]
INTERESTS = ["Bitcoin and stocks", "SpaceX"]


def _patch_candidates():
    return patch.object(trends, "trending_now", return_value=list(CANDIDATES))


class TestTrendingFetch:
    def test_filters_low_volume_and_sorts_by_volume(self):
        payload = {"trending_searches": [
            {"query": "tiny", "search_volume": 10, "categories": []},
            {"query": "big", "search_volume": 500000, "categories": [{"name": "News"}]},
            {"query": "mid", "search_volume": 50000, "categories": []},
        ]}
        trends._cache.clear()
        with patch.object(trends.serpapi, "search", return_value=payload):
            out = trends.trending_now("US", date(2026, 8, 23))
        assert [i["query"] for i in out] == ["big", "mid"], "low-volume noise must be dropped"

    def test_cached_per_geo_and_day(self):
        """Trending is identical for everyone — one fetch should serve the run."""
        trends._cache.clear()
        payload = {"trending_searches": [{"query": "x", "search_volume": 100000, "categories": []}]}
        with patch.object(trends.serpapi, "search", return_value=payload) as api:
            trends.trending_now("US", date(2026, 8, 23))
            trends.trending_now("US", date(2026, 8, 23))
        assert api.call_count == 1

    def test_new_day_refetches(self):
        trends._cache.clear()
        payload = {"trending_searches": [{"query": "x", "search_volume": 100000, "categories": []}]}
        with patch.object(trends.serpapi, "search", return_value=payload) as api:
            trends.trending_now("US", date(2026, 8, 23))
            trends.trending_now("US", date(2026, 8, 24))
        assert api.call_count == 2

    def test_api_failure_returns_empty(self):
        trends._cache.clear()
        with patch.object(trends.serpapi, "search", side_effect=RuntimeError("boom")):
            assert trends.trending_now("US", date(2026, 8, 23)) == []


class TestAdjacentPick:
    def test_returns_story_for_a_valid_pick(self):
        with _patch_candidates(), \
             patch.object(trends.client.messages, "create",
                          return_value=_reply('{"query": "jeff bezos zero income tax idea", "why": "markets angle"}')), \
             patch.object(trends, "_search_raw",
                          return_value=[{"title": "Bezos floats plan", "content": "details"}]):
            out = trends.adjacent_story(INTERESTS, ["Bitcoin flat"])
        assert out["query"] == "jeff bezos zero income tax idea"
        assert "Bezos floats plan" in out["story"]

    def test_none_verdict_is_respected(self):
        with _patch_candidates(), \
             patch.object(trends.client.messages, "create", return_value=_reply('{"query": "NONE"}')), \
             patch.object(trends, "_search_raw") as search:
            assert trends.adjacent_story(INTERESTS, []) is None
        search.assert_not_called()

    def test_invented_trend_is_rejected(self):
        """A hallucinated trend is exactly the filler this feature must not add."""
        with _patch_candidates(), \
             patch.object(trends.client.messages, "create",
                          return_value=_reply('{"query": "aliens land in ohio", "why": "big if true"}')), \
             patch.object(trends, "_search_raw") as search:
            assert trends.adjacent_story(INTERESTS, []) is None
        search.assert_not_called()

    def test_pick_with_no_story_behind_it_is_dropped(self):
        with _patch_candidates(), \
             patch.object(trends.client.messages, "create",
                          return_value=_reply('{"query": "jeff bezos zero income tax idea", "why": "x"}')), \
             patch.object(trends, "_search_raw", return_value=[]):
            assert trends.adjacent_story(INTERESTS, []) is None

    def test_covered_headlines_reach_the_prompt(self):
        captured = {}

        def _create(**kw):
            captured["p"] = kw["messages"][0]["content"]
            return _reply('{"query": "NONE"}')

        with _patch_candidates(), patch.object(trends.client.messages, "create", side_effect=_create):
            trends.adjacent_story(INTERESTS, ["Bitcoin flat at 77k"])
        assert "Bitcoin flat at 77k" in captured["p"], "the pick must know what's already covered"

    def test_no_interests_means_no_pick(self):
        with patch.object(trends, "trending_now") as t:
            assert trends.adjacent_story([], []) is None
        t.assert_not_called()

    def test_model_failure_is_silent(self):
        with _patch_candidates(), \
             patch.object(trends.client.messages, "create", side_effect=RuntimeError("boom")):
            assert trends.adjacent_story(INTERESTS, []) is None

    def test_unparseable_reply_is_silent(self):
        with _patch_candidates(), \
             patch.object(trends.client.messages, "create", return_value=_reply("not json")):
            assert trends.adjacent_story(INTERESTS, []) is None


class TestBriefingIntegration:
    def test_adjacent_failure_does_not_break_the_briefing(self):
        import morning
        with patch.object(morning, "_weather_report", return_value="warm"), \
             patch.object(morning, "_topic_digest", return_value="story"), \
             patch.object(morning, "get_city_traffic", return_value="clear"), \
             patch.object(morning, "get_travel_time", return_value="17 min"), \
             patch("trends.adjacent_story", side_effect=RuntimeError("boom")):
            out = morning._gather_morning_data({"city": "Kirkwood", "morning_topics": ["SpaceX news"]})
        assert any("story" in s for s in out), "topics must survive a trends failure"

    def test_adjacent_section_is_labelled_for_the_drafter(self):
        import morning
        with patch.object(morning, "_weather_report", return_value="warm"), \
             patch.object(morning, "_topic_digest", return_value="story"), \
             patch.object(morning, "get_city_traffic", return_value="clear"), \
             patch.object(morning, "get_travel_time", return_value="17 min"), \
             patch("trends.adjacent_story",
                   return_value={"query": "q", "why": "close to markets", "story": "Big thing happened"}):
            out = morning._gather_morning_data({"city": "Kirkwood", "morning_topics": ["SpaceX news"]})
        adj = [s for s in out if s.startswith("ADJACENT")]
        assert adj and "close to markets" in adj[0] and "Big thing happened" in adj[0]
