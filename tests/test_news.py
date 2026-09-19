"""Source quality, trends and tickers.

Merged from test_sources.py, test_trends.py, test_tickers.py; each section keeps its
original file's notes, because those carry the incident that led to the test.
"""
import json
import pytest
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from unittest.mock import patch, MagicMock
from palmer import sources, datafeeds, trends, tickers
from tests.helpers import llm_reply
from palmer.tickers import resolve_topic_asset as resolve


# ============================================================================
# from test_sources.py
# ============================================================================
#
# Tests for sources.py — the one place that decides which news Palmer repeats.
#
# Every news surface (watch alerts, the morning briefing, Palmer Home, and the
# conversation search) reaches this module through datafeeds._search_raw, so a
# regression here is a regression everywhere at once. The integration class at
# the bottom pins that wiring; the rest pin the gate itself.
#
# trusted_sources.json is meant to be hand-edited without touching code, which
# makes TestSourceListIntegrity load-bearing rather than pedantic — a stray
# "https://" or a domain listed in both the allowlist and the blocklist would
# otherwise fail silently at runtime.

def _fresh(hours_ago: float = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _r(url: str, score: float = 0.7, hours_ago: float = 1) -> dict:
    return {"url": url, "title": "t", "content": "c", "score": score,
            "published_date": _fresh(hours_ago)}


class TestIsBlocked:
    def test_press_release_wire_blocked(self):
        assert sources.is_blocked("https://www.prnewswire.com/news-releases/thing-123.html")
        assert sources.is_blocked("https://globenewswire.com/x")

    def test_republishing_aggregator_blocked(self):
        assert sources.is_blocked("https://www.msn.com/en-us/news/other/story")
        assert sources.is_blocked("https://biztoc.com/x/abc")
        assert sources.is_blocked("https://news.google.com/articles/xyz")

    def test_subdomain_of_blocked_host_blocked(self):
        assert sources.is_blocked("https://ir.globenewswire.com/x")

    def test_real_newsroom_not_blocked(self):
        assert not sources.is_blocked("https://www.reuters.com/world/x")

    def test_lookalike_suffix_not_blocked(self):
        """notmsn.com must not match msn.com — suffix matching is on a dot boundary."""
        assert not sources.is_blocked("https://notmsn.com/x")

    def test_malformed_and_empty_are_not_blocked(self):
        assert not sources.is_blocked("")
        assert not sources.is_blocked("not a url")


class TestSourceTier:
    def test_tier1_newsroom(self):
        assert sources.source_tier("https://www.reuters.com/world/x") == 1

    def test_tier2_outlet(self):
        assert sources.source_tier("https://www.theverge.com/x") == 2

    def test_unknown_is_tier3(self):
        assert sources.source_tier("https://contentfarm.example/x") == 3

    def test_gov_and_edu_are_tier1(self):
        assert sources.source_tier("https://www.weather.gov/alert/1") == 1
        assert sources.source_tier("https://news.mit.edu/2026/x") == 1

    def test_subdomain_inherits_tier(self):
        assert sources.source_tier("https://feeds.bbc.co.uk/news/x") == 1

    def test_lookalike_suffix_does_not_inherit_tier(self):
        """A domain ending in the trusted string but not on a dot boundary is
        tier 3 — otherwise notreuters.com would launder itself into tier 1."""
        assert sources.source_tier("https://notreuters.com/x") == 3
        assert sources.source_tier("https://reuters.com.evil.example/x") == 3

    def test_blocked_domain_still_reports_tier3(self):
        """is_blocked is the gate; tier is only an ordering. They are separate
        so a blocked host can never be ranked as merely-untrusted by accident."""
        assert sources.source_tier("https://www.msn.com/x") == 3

    def test_malformed_url_is_tier3(self):
        assert sources.source_tier("") == 3
        assert sources.source_tier("garbage") == 3


class TestCanonicalDomain:
    def test_collapses_subdomain_to_known_domain(self):
        assert sources.canonical_domain("https://www.reuters.com/x") == "reuters.com"
        assert sources.canonical_domain("https://feeds.bbc.co.uk/n") == "bbc.co.uk"

    def test_unknown_domain_falls_back_to_last_two_labels(self):
        assert sources.canonical_domain("https://a.b.contentfarm.example/x") == "contentfarm.example"

    def test_blocked_domains_are_canonicalized_too(self):
        """Blocked hosts are normally dropped before counting, but corroboration
        must not treat two msn.com copies as two independent sources if one ever
        reaches it."""
        assert sources.canonical_domain("https://www.msn.com/en-us/x") == "msn.com"

    def test_empty_on_garbage(self):
        assert sources.canonical_domain("") == ""
        assert sources.canonical_domain("not a url") == ""


class TestRank:
    def test_drops_blocked_results(self):
        out = sources.rank([_r("https://www.msn.com/a"), _r("https://www.reuters.com/b")])
        assert [r["url"] for r in out] == ["https://www.reuters.com/b"]

    def test_tier_beats_score(self):
        """A wire report at score 0.5 outranks a content farm at 0.99 — Tavily's
        score measures query match, not whether the page is worth believing."""
        out = sources.rank([
            _r("https://contentfarm.example/a", score=0.99),
            _r("https://www.reuters.com/b", score=0.50),
        ])
        assert [r["url"] for r in out] == ["https://www.reuters.com/b",
                                           "https://contentfarm.example/a"]

    def test_score_breaks_ties_within_a_tier(self):
        out = sources.rank([
            _r("https://www.reuters.com/low", score=0.4),
            _r("https://apnews.com/high", score=0.9),
        ])
        assert [r["url"] for r in out] == ["https://apnews.com/high",
                                           "https://www.reuters.com/low"]

    def test_missing_score_does_not_raise(self):
        out = sources.rank([{"url": "https://www.reuters.com/a"}])
        assert len(out) == 1

    def test_trusted_only_drops_tier3(self):
        out = sources.rank([
            _r("https://contentfarm.example/a", score=0.99),
            _r("https://www.theverge.com/b", score=0.1),
        ], trusted_only=True)
        assert [r["url"] for r in out] == ["https://www.theverge.com/b"]

    def test_trusted_only_can_return_nothing(self):
        """Palmer Home would rather show no row than an untrusted one."""
        assert sources.rank([_r("https://contentfarm.example/a")], trusted_only=True) == []

    def test_default_keeps_tier3_as_last_resort(self):
        """Conversation and the morning briefing answer a real question, so an
        obscure-but-real source beats 'nothing found'."""
        out = sources.rank([_r("https://contentfarm.example/a")])
        assert len(out) == 1

    def test_empty_input(self):
        assert sources.rank([]) == []


class TestMeetsScore:
    """The relevance floor runs before the tier sort, so a flat floor lets an
    SEO-tuned content farm survive a cut that a real newsroom does not."""

    def test_tier3_gets_no_slack(self):
        assert not sources.meets_score("https://contentfarm.example/a", 0.45, 0.5)
        assert sources.meets_score("https://contentfarm.example/a", 0.50, 0.5)

    def test_trusted_source_clears_a_lower_bar(self):
        assert sources.meets_score("https://www.reuters.com/a", 0.45, 0.5)
        assert sources.meets_score("https://www.theverge.com/a", 0.40, 0.5)

    def test_trusted_slack_is_bounded(self):
        """Relaxed, not waived — an off-topic wire story is still off-topic."""
        assert not sources.meets_score("https://www.reuters.com/a", 0.20, 0.5)

    def test_missing_score_never_passes_a_positive_floor(self):
        assert not sources.meets_score("https://www.reuters.com/a", None, 0.5)

    def test_floor_never_goes_negative(self):
        assert sources.meets_score("https://www.reuters.com/a", 0.0, 0.1)


class TestSourceListIntegrity:
    """trusted_sources.json is edited by hand and read at import. These pin the
    shape so a typo fails the suite instead of silently demoting a newsroom."""

    @classmethod
    def setup_class(cls):
        with open(Path(sources.__file__).parent / "trusted_sources.json") as f:
            cls.data = json.load(f)

    def test_every_allowlist_entry_has_domain_and_valid_tier(self):
        for d in self.data["domains"]:
            assert d.get("domain"), f"entry missing domain: {d}"
            assert d.get("tier") in (1, 2), f"{d['domain']} has tier {d.get('tier')}"

    def test_every_blocked_entry_explains_why(self):
        """The 'why' is the guardrail against the list drifting from structural
        junk into editorial opinion about who reports well."""
        for d in self.data["blocked"]:
            assert d.get("domain"), f"blocked entry missing domain: {d}"
            assert d.get("why"), f"{d['domain']} blocked with no reason given"

    def test_domains_are_bare_hosts(self):
        for d in self.data["domains"] + self.data["blocked"]:
            host = d["domain"]
            assert host == host.lower(), f"{host} is not lowercase"
            assert "://" not in host, f"{host} includes a scheme"
            assert "/" not in host, f"{host} includes a path"
            assert not host.startswith("www."), f"{host} should not carry a www. prefix"
            assert "." in host, f"{host} is not a domain"

    def test_no_duplicate_domains(self):
        hosts = [d["domain"] for d in self.data["domains"]]
        assert len(hosts) == len(set(hosts)), \
            f"duplicates: {sorted({h for h in hosts if hosts.count(h) > 1})}"

    def test_allowlist_and_blocklist_do_not_overlap(self):
        allowed = {d["domain"] for d in self.data["domains"]}
        blocked = {d["domain"] for d in self.data["blocked"]}
        assert not (allowed & blocked), f"listed as both trusted and blocked: {allowed & blocked}"

    def test_loaded_sets_match_the_file(self):
        assert len(sources._TIER1_DOMAINS) + len(sources._TIER2_DOMAINS) == len(self.data["domains"])
        assert len(sources._BLOCKED_DOMAINS) == len(self.data["blocked"])


class TestCorroboratedIsShared:
    """test_corroboration.py imports corroborated from watches. It moved
    here; this pins that watches still exposes the same object so the gate can
    never fork into two implementations."""

    def test_watches_reexports_the_same_function(self):
        from palmer import watches
        assert watches.corroborated is sources.corroborated


class TestSearchRawAppliesTheGate:
    """The wiring that matters: every news surface goes through _search_raw, so
    the gate has to be applied there rather than by each caller."""

    def _tavily_returning(self, results):
        fake = MagicMock()
        fake.search.return_value = {"results": results}
        return fake

    def test_blocked_results_never_reach_callers(self):
        fake = self._tavily_returning([
            _r("https://www.prnewswire.com/a", score=0.99),
            _r("https://www.reuters.com/b", score=0.6),
        ])
        with patch.object(datafeeds, "_tavily", fake):
            out = datafeeds._search_raw("some topic")
        assert [r["url"] for r in out] == ["https://www.reuters.com/b"]

    def test_results_come_back_source_ranked(self):
        fake = self._tavily_returning([
            _r("https://contentfarm.example/a", score=0.99),
            _r("https://apnews.com/b", score=0.6),
        ])
        with patch.object(datafeeds, "_tavily", fake):
            out = datafeeds._search_raw("some topic")
        assert [r["url"] for r in out] == ["https://apnews.com/b",
                                           "https://contentfarm.example/a"]

    def test_trusted_only_is_forwarded(self):
        fake = self._tavily_returning([_r("https://contentfarm.example/a")])
        with patch.object(datafeeds, "_tavily", fake):
            assert datafeeds._search_raw("t", trusted_only=True) == []
            assert len(datafeeds._search_raw("t")) == 1

    def test_recency_and_score_filters_still_apply_first(self):
        fake = self._tavily_returning([
            _r("https://apnews.com/stale", hours_ago=48),
            _r("https://apnews.com/weak", score=0.1),  # under the floor even with slack
            _r("https://apnews.com/good"),
        ])
        with patch.object(datafeeds, "_tavily", fake):
            out = datafeeds._search_raw("t", max_age_hours=12, min_score=0.5)
        assert [r["url"] for r in out] == ["https://apnews.com/good"]

    def test_trusted_source_survives_a_cut_that_drops_a_content_farm(self):
        """The end-to-end shape of the bug: the farm scores higher because it is
        built to, and a flat floor would have left it as the only survivor."""
        fake = self._tavily_returning([
            _r("https://contentfarm.example/a", score=0.55),
            _r("https://www.reuters.com/b", score=0.42),
        ])
        with patch.object(datafeeds, "_tavily", fake):
            out = datafeeds._search_raw("t", min_score=0.5)
        assert [r["url"] for r in out] == ["https://www.reuters.com/b",
                                           "https://contentfarm.example/a"]

    def test_pulls_ten_candidates(self):
        """The recency window throws most of a page away. Five candidates left
        the tier sort nothing to choose between, which is how a lone content
        farm became the best available source. Tavily bills per search, not per
        result, so the wider pull is free."""
        fake = self._tavily_returning([])
        with patch.object(datafeeds, "_tavily", fake):
            datafeeds._search_raw("t")
        assert fake.search.call_args.kwargs["max_results"] == 10

    def test_search_failure_returns_empty(self):
        fake = MagicMock()
        fake.search.side_effect = RuntimeError("tavily down")
        with patch.object(datafeeds, "_tavily", fake):
            assert datafeeds._search_raw("t") == []


class TestConversationSearchLabelsSources:
    """_search feeds the drafting model directly. It had no provenance at all,
    so Palmer could not tell a wire report from a content farm."""

    def test_domain_is_labelled_and_junk_is_dropped(self):
        fake = MagicMock()
        fake.search.return_value = {"results": [
            _r("https://www.msn.com/junk"),
            _r("https://www.reuters.com/real"),
        ]}
        with patch.object(datafeeds, "_tavily", fake):
            out = datafeeds._search("what happened")
        assert "[reuters.com]" in out
        assert "msn.com" not in out

    def test_no_results_message_when_everything_is_filtered(self):
        """An empty search says so — and says it is empty, not broken.

        This used to assert the literal "No results found.", which is the shape
        of failure string that gets paraphrased into "I can't find news on that"
        and then into a competitor. The gates firing is the common case, not a
        rare one, so what the model is told here matters."""
        fake = MagicMock()
        fake.search.return_value = {"results": [_r("https://biztoc.com/a")]}
        with patch.object(datafeeds, "_tavily", fake):
            out = datafeeds._search("q")
        assert "'q'" in out                      # names what was searched for
        assert "DO have news search" in out      # never implies the capability is gone
        assert "another site" in out             # and never hands them off


# ============================================================================
# from test_trends.py
# ============================================================================
#
# Tests for the adjacent-story pick.
#
# This is the one part of the briefing that isn't something the user asked for,
# so the guards matter more than the feature: it must never invent a trend, never
# repeat what the briefing already covers, and must fail to silence rather than to
# filler. Haiku is mocked — its judgement quality is checked live, not here.

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
                          return_value=llm_reply('{"query": "jeff bezos zero income tax idea", "why": "markets angle"}')), \
             patch.object(trends, "_search_raw",
                          return_value=[{"title": "Bezos floats plan", "content": "details"}]):
            out = trends.adjacent_story(INTERESTS, ["Bitcoin flat"])
        assert out["query"] == "jeff bezos zero income tax idea"
        assert "Bezos floats plan" in out["story"]

    def test_none_verdict_is_respected(self):
        with _patch_candidates(), \
             patch.object(trends.client.messages, "create", return_value=llm_reply('{"query": "NONE"}')), \
             patch.object(trends, "_search_raw") as search:
            assert trends.adjacent_story(INTERESTS, []) is None
        search.assert_not_called()

    def test_invented_trend_is_rejected(self):
        """A hallucinated trend is exactly the filler this feature must not add."""
        with _patch_candidates(), \
             patch.object(trends.client.messages, "create",
                          return_value=llm_reply('{"query": "aliens land in ohio", "why": "big if true"}')), \
             patch.object(trends, "_search_raw") as search:
            assert trends.adjacent_story(INTERESTS, []) is None
        search.assert_not_called()

    def test_pick_with_no_story_behind_it_is_dropped(self):
        with _patch_candidates(), \
             patch.object(trends.client.messages, "create",
                          return_value=llm_reply('{"query": "jeff bezos zero income tax idea", "why": "x"}')), \
             patch.object(trends, "_search_raw", return_value=[]):
            assert trends.adjacent_story(INTERESTS, []) is None

    def test_covered_headlines_reach_the_prompt(self):
        captured = {}

        def _create(**kw):
            captured["p"] = kw["messages"][0]["content"]
            return llm_reply('{"query": "NONE"}')

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
             patch.object(trends.client.messages, "create", return_value=llm_reply("not json")):
            assert trends.adjacent_story(INTERESTS, []) is None


class TestBriefingIntegration:
    def test_adjacent_failure_does_not_break_the_briefing(self):
        from palmer import morning
        with patch.object(morning, "_weather_report", return_value="warm"), \
             patch.object(morning, "_topic_digest", return_value="story"), \
             patch.object(morning, "get_city_traffic", return_value="clear"), \
             patch.object(morning, "get_travel_time", return_value="17 min"), \
             patch("palmer.trends.adjacent_story", side_effect=RuntimeError("boom")):
            out = morning._gather_morning_data({"city": "Kirkwood", "morning_topics": ["SpaceX news"]})
        assert any("story" in s for s in out), "topics must survive a trends failure"

    def test_adjacent_section_is_labelled_for_the_drafter(self):
        from palmer import morning
        with patch.object(morning, "_weather_report", return_value="warm"), \
             patch.object(morning, "_topic_digest", return_value="story"), \
             patch.object(morning, "get_city_traffic", return_value="clear"), \
             patch.object(morning, "get_travel_time", return_value="17 min"), \
             patch("palmer.trends.adjacent_story",
                   return_value={"query": "q", "why": "close to markets", "story": "Big thing happened"}):
            out = morning._gather_morning_data({"city": "Kirkwood", "morning_topics": ["SpaceX news"]})
        adj = [s for s in out if s.startswith("ADJACENT")]
        assert adj and "close to markets" in adj[0] and "Big thing happened" in adj[0]


# ============================================================================
# from test_tickers.py
# ============================================================================
#
# Topic -> symbol resolution.
#
# The Markets section is derived from morning topics, so this resolver decides
# whether "add Nvidia to my site" produces a price or silently produces nothing.
# It used to be a bare uppercase-word regex, which meant it worked only when the
# model happened to write the ticker into the topic itself.
#
# Two failure directions matter and both are tested: a real company that resolves
# to NOTHING (silently empty Markets section) and a non-company that resolves to
# SOMETHING (a wrong price on someone's personal page). The second is worse.

class TestCompanyNames:
    """The actual bug: users say "Nvidia", not "NVDA"."""

    @pytest.mark.parametrize("topic,symbol", [
        ("Nvidia stock", "NVDA"),
        ("Tesla stock", "TSLA"),
        ("Apple shares", "AAPL"),
        ("coinbase stock", "COIN"),
        ("microsoft stock price", "MSFT"),
        ("what's amazon stock doing", "AMZN"),
    ])
    def test_a_company_name_resolves(self, topic, symbol):
        assert resolve(topic)[0] == symbol

    def test_case_does_not_matter(self):
        assert resolve("NVIDIA STOCK")[0] == "NVDA"
        assert resolve("nvidia stock")[0] == "NVDA"

    def test_the_longest_name_wins(self):
        """"dow jones" must not be shadowed by a shorter key."""
        assert resolve("dow jones")[0] == "^DJI"
        assert resolve("s&p 500")[0] == "^GSPC"


class TestExplicitSymbols:
    def test_a_parenthesised_ticker_is_used(self):
        """This is the shape the drafting model writes on its own."""
        assert resolve("Nvidia stock price (NVDA)")[0] == "NVDA"

    def test_a_dollar_prefixed_ticker_is_used(self):
        assert resolve("$TSLA")[0] == "TSLA"

    def test_a_bare_ticker_with_a_price_word_is_used(self):
        assert resolve("TSLA stock")[0] == "TSLA"

    def test_an_explicit_symbol_beats_the_name_map(self):
        """If the user spelled out a symbol, trust it over a name match."""
        assert resolve("Alphabet stock (GOOG)")[0] == "GOOG"


class TestFalsePositives:
    """A wrong ticker is worse than no ticker — it puts a real price for the
    wrong thing on someone's page, and nothing downstream can catch it."""

    def test_us_stock_market_is_not_the_ticker_US(self):
        """The live bug: "US stock market" resolved to "US", which yfinance
        rejects as delisted on every single page refresh."""
        got = resolve("US stock market")
        assert got is not None and got[0] != "US"

    def test_the_generic_market_ask_resolves_to_the_sp500(self):
        assert resolve("US stock market")[0] == "^GSPC"
        assert resolve("stock market updates")[0] == "^GSPC"

    @pytest.mark.parametrize("topic", [
        "AI news", "US politics", "Fintech news", "movie news",
        "St. Louis Cardinals", "Philadelphia Eagles news",
        "National and international news", "Kirkwood, MO weather",
        "Trump social media posts (overnight)", "Daily fun fact from history",
        "SpaceX news",
    ])
    def test_a_news_topic_gets_no_ticker(self, topic):
        assert resolve(topic) is None

    def test_a_price_word_alone_is_not_enough(self):
        """"stock" in the sentence must not make any capitalised word a ticker."""
        assert resolve("stock up on groceries") is None

    @pytest.mark.parametrize("word", ["US", "AI", "ETF", "IPO", "CEO", "NFL", "THE"])
    def test_known_non_tickers_are_rejected(self, word):
        assert word in tickers.NOT_TICKERS


class TestNewsTopicsAreNotPriceTopics:
    """A company name is an ordinary word in a news topic. Without a price-word
    gate, "SpaceX news" resolves to SPCX and a subject someone follows silently
    grows a stock ticker in their Markets section."""

    @pytest.mark.parametrize("topic", [
        "SpaceX news", "Disney movie news", "Nike news",
        "Apple event coverage", "Tesla recall coverage", "Amazon layoffs",
    ])
    def test_a_company_in_a_news_topic_gets_no_ticker(self, topic):
        assert resolve(topic) is None

    @pytest.mark.parametrize("topic,symbol", [
        ("SpaceX stock", "SPCX"), ("Disney stock", "DIS"), ("Nike stock", "NKE"),
    ])
    def test_the_same_company_with_a_price_word_does_resolve(self, topic, symbol):
        assert resolve(topic)[0] == symbol

    def test_indices_need_no_price_word(self):
        """"nasdaq" is unambiguously a market reference on its own."""
        assert resolve("nasdaq")[0] == "^IXIC"
        assert resolve("the dow")[0] == "^DJI"

    def test_crypto_needs_no_price_word(self):
        """Pre-existing behaviour: a live user tracks "Bitcoin and major stock
        news" and has always gotten a price for it."""
        assert resolve("Bitcoin and major stock news")[0] == "bitcoin"


class TestSearchResolution:
    """The save-path resolver. No model is asked for a symbol — two earlier
    versions did, and both encoded a stale snapshot of who was public."""

    def _search(self, quotes, topic="Lululemon shares"):
        with patch("palmer.netutil._http_get_json", return_value={"quotes": quotes}) as get:
            return tickers.search_symbol(topic), get

    def test_a_us_equity_is_returned(self):
        assert self._search([{"symbol": "LULU", "quoteType": "EQUITY", "exchange": "NMS"}])[0] == "LULU"

    def test_a_tokenized_crypto_is_rejected(self):
        """Unfiltered, "openai" comes back as a crypto token that merely shares
        the name — a real price for something that is not the company."""
        assert self._search([{"symbol": "OPENAI-USD", "quoteType": "CRYPTOCURRENCY",
                              "exchange": "CCC"}])[0] is None

    def test_a_thematic_etf_is_rejected(self):
        assert self._search([{"symbol": "OAIW", "quoteType": "ETF", "exchange": "PCX"}])[0] is None

    def test_a_mutual_fund_is_rejected(self):
        assert self._search([{"symbol": "STRIZZX", "quoteType": "MUTUALFUND",
                              "exchange": "NAS"}])[0] is None

    def test_a_foreign_listing_is_skipped_for_the_us_one(self):
        """"lululemon" also matches Milan and Sao Paulo lines of the same
        company, which price in the wrong currency."""
        assert self._search([{"symbol": "1LUL.MI", "quoteType": "EQUITY", "exchange": "MIL"},
                             {"symbol": "LULU", "quoteType": "EQUITY", "exchange": "NMS"}])[0] == "LULU"

    def test_the_first_qualifying_result_wins(self):
        assert self._search([{"symbol": "SPACEX-USD", "quoteType": "CRYPTOCURRENCY", "exchange": "CCC"},
                             {"symbol": "SPCX", "quoteType": "EQUITY", "exchange": "NMS"},
                             {"symbol": "SPCF", "quoteType": "EQUITY", "exchange": "NMS"}])[0] == "SPCX"

    def test_no_qualifying_result_yields_nothing(self):
        assert self._search([])[0] is None

    def test_a_network_failure_yields_nothing(self):
        with patch("palmer.netutil._http_get_json", return_value=None):
            assert tickers.search_symbol("Lululemon shares") is None

    def test_a_stopword_symbol_is_still_rejected(self):
        assert self._search([{"symbol": "AI", "quoteType": "EQUITY", "exchange": "NYQ"}])[0] is None

    def test_no_model_is_consulted(self):
        """The whole point of the rewrite."""
        with patch("palmer.llm.client") as client, \
             patch("palmer.netutil._http_get_json", return_value={"quotes": []}):
            tickers.resolve_company_ticker("Lululemon shares")
        client.messages.create.assert_not_called()


class TestSearchQuery:
    """Search matches names, not sentences."""

    def test_price_words_are_stripped(self):
        """"spacex stock" returns nothing from search; "spacex" returns SPCX."""
        assert tickers._search_query("spacex stock") == "spacex"
        assert tickers._search_query("Lululemon shares") == "Lululemon"
        assert tickers._search_query("Duolingo stock price") == "Duolingo"

    def test_topic_filler_is_stripped(self):
        assert tickers._search_query("the latest Nvidia news updates") == "Nvidia"

    def test_a_multiword_company_survives(self):
        assert tickers._search_query("Rocket Lab stock") == "Rocket Lab"

    def test_an_empty_query_short_circuits(self):
        with patch("palmer.netutil._http_get_json") as get:
            assert tickers.search_symbol("stock price news") is None
        get.assert_not_called()

    def test_indices_never_reach_search(self):
        """Search returns futures for them - "s&p 500" is ES=F, not ^GSPC - so
        they must resolve from INDEX_TICKERS before search is consulted."""
        for name in ("s&p 500", "nasdaq", "the dow", "US stock market"):
            got = resolve(name)
            assert got and got[0].startswith("^"), f"{name} must map to an index"


class TestCrypto:
    def test_crypto_still_resolves(self):
        assert resolve("Bitcoin price")[0] == "bitcoin"
        assert resolve("bitcoin")[0] == "bitcoin"

    def test_crypto_wins_over_a_stray_ticker_match(self):
        assert resolve("Bitcoin and major stock news")[0] == "bitcoin"


class TestLabels:
    """Yahoo's index symbols are correct and unreadable."""

    def test_an_index_gets_a_human_label(self):
        assert resolve("s&p 500") == ("^GSPC", "S&P 500")
        assert resolve("nasdaq") == ("^IXIC", "Nasdaq")

    def test_a_plain_ticker_labels_as_itself(self):
        assert resolve("Nvidia stock") == ("NVDA", "NVDA")

    def test_crypto_labels_readably(self):
        assert resolve("bitcoin")[1] == "Bitcoin"

    def test_crypto_alias_labels_readably_too(self):
        """"Btc"/"Avax" (a naive title-case of the matched alias) used to reach
        the page. The coingecko id behind the alias decides the real name."""
        assert resolve("add BTC to my markets")[1] == "Bitcoin"
        assert resolve("avax price")[1] == "Avalanche"
        assert resolve("what's XRP at")[1] == "XRP"


class TestPriceTopicGate:
    """Gates the paid fallback so ordinary news topics never trigger one."""

    @pytest.mark.parametrize("topic", ["Nvidia stock", "bitcoin price",
                                       "AAPL shares", "the market"])
    def test_price_topics_pass(self, topic):
        assert tickers.looks_like_price_topic(topic)

    @pytest.mark.parametrize("topic", ["AI news", "St. Louis Cardinals",
                                       "Kirkwood weather", "movie news"])
    def test_news_topics_do_not(self, topic):
        assert not tickers.looks_like_price_topic(topic)


class TestReadPathIsFree:
    def test_resolution_never_calls_a_model(self):
        """This runs on every page view. A model call here would be a bill."""
        with patch("palmer.llm.client") as client:
            for t in ["Nvidia stock", "AI news", "US stock market", "$TSLA",
                      "SpaceX stock", "bitcoin", "Lululemon shares"]:
                resolve(t)
        client.messages.create.assert_not_called()
