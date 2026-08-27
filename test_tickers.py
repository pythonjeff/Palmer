"""Topic -> symbol resolution.

The Markets section is derived from morning topics, so this resolver decides
whether "add Nvidia to my site" produces a price or silently produces nothing.
It used to be a bare uppercase-word regex, which meant it worked only when the
model happened to write the ticker into the topic itself.

Two failure directions matter and both are tested: a real company that resolves
to NOTHING (silently empty Markets section) and a non-company that resolves to
SOMETHING (a wrong price on someone's personal page). The second is worse.
"""
from unittest.mock import patch

import pytest

import tickers
from tickers import resolve_topic_asset as resolve


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
        with patch("netutil._http_get_json", return_value={"quotes": quotes}) as get:
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
        with patch("netutil._http_get_json", return_value=None):
            assert tickers.search_symbol("Lululemon shares") is None

    def test_a_stopword_symbol_is_still_rejected(self):
        assert self._search([{"symbol": "AI", "quoteType": "EQUITY", "exchange": "NYQ"}])[0] is None

    def test_no_model_is_consulted(self):
        """The whole point of the rewrite."""
        with patch("llm.client") as client, \
             patch("netutil._http_get_json", return_value={"quotes": []}):
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
        with patch("netutil._http_get_json") as get:
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
        with patch("llm.client") as client:
            for t in ["Nvidia stock", "AI news", "US stock market", "$TSLA",
                      "SpaceX stock", "bitcoin", "Lululemon shares"]:
                resolve(t)
        client.messages.create.assert_not_called()
