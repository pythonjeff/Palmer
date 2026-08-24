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


class TestVerification:
    """The model does not get the last word on a symbol. It must also name the
    company, and the exchange has to agree that is what the symbol is."""

    def test_the_wrong_company_is_rejected(self):
        """The SPCE trap: right company name, wrong symbol."""
        with patch("llm.client") as client, \
             patch("yfinance.Ticker") as yft:
            client.messages.create.return_value = _Resp("SPCE | Space Exploration Technologies")
            yft.return_value.info = {"longName": "Virgin Galactic Holdings, Inc."}
            yft.return_value.fast_info.last_price = 3.08
            assert tickers.resolve_company_ticker("SpaceX stock") is None

    def test_the_right_company_is_accepted(self):
        with patch("llm.client") as client, \
             patch("yfinance.Ticker") as yft:
            client.messages.create.return_value = _Resp("SPCX | Space Exploration Technologies Corp.")
            yft.return_value.info = {"longName": "Space Exploration Technologies Corp."}
            yft.return_value.fast_info.last_price = 136.97
            assert tickers.resolve_company_ticker("SpaceX stock") == "SPCX"

    def test_a_symbol_with_no_listing_is_rejected(self):
        with patch("llm.client") as client, \
             patch("yfinance.Ticker") as yft:
            client.messages.create.return_value = _Resp("SQ | Block, Inc.")
            yft.return_value.info = {}
            yft.return_value.fast_info.last_price = None
            assert tickers.resolve_company_ticker("Block stock") is None

    def test_a_lookup_failure_fails_closed(self):
        """A network blip costs one unresolved topic. Failing open would cost a
        wrong price on someone's page, silently."""
        with patch("llm.client") as client, \
             patch("yfinance.Ticker", side_effect=RuntimeError("network")):
            client.messages.create.return_value = _Resp("LULU | Lululemon Athletica Inc.")
            assert tickers.resolve_company_ticker("Lululemon shares") is None

    def test_an_answer_without_a_name_is_rejected(self):
        """A bare symbol cannot be checked, so it is not trusted."""
        with patch("llm.client") as client:
            client.messages.create.return_value = _Resp("LULU")
            assert tickers.resolve_company_ticker("Lululemon shares") is None


class TestNameAgreement:
    @pytest.mark.parametrize("a,b", [
        ("Space Exploration Technologies Corp.", "Space Exploration Technologies Corp."),
        ("Apple Inc", "Apple Inc."),
        ("Meta Platforms", "Meta Platforms, Inc."),
        ("Lululemon Athletica Inc.", "lululemon athletica inc."),
        ("Block", "Block, Inc."),
    ])
    def test_the_same_company_agrees_across_suffix_noise(self, a, b):
        assert tickers._names_agree(a, b)

    @pytest.mark.parametrize("a,b", [
        ("Space Exploration Technologies", "Virgin Galactic Holdings, Inc."),
        ("Alphabet Inc.", "Amazon.com, Inc."),
        ("Block, Inc.", "BlackRock, Inc."),
        ("", "Apple Inc."),
        ("Apple Inc.", ""),
    ])
    def test_different_companies_disagree(self, a, b):
        assert not tickers._names_agree(a, b)

    def test_boilerplate_alone_is_never_a_match(self):
        """Two companies sharing only "Inc." and "Holdings" are not the same."""
        assert not tickers._names_agree("Holdings Inc.", "Group Corp.")


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


class _Block:
    def __init__(self, t): self.text = t


class _Resp:
    def __init__(self, t): self.content = [_Block(t)]


class TestModelFallback:
    """For names the map doesn't carry. Save-time only — never on the read
    path, which runs on every page view."""

    def _resolve(self, answer, listed="Lululemon Athletica Inc.", price=121.07):
        with patch("llm.client") as client, patch("yfinance.Ticker") as yft:
            client.messages.create.return_value = _Resp(answer)
            yft.return_value.info = {"longName": listed}
            yft.return_value.fast_info.last_price = price
            return tickers.resolve_company_ticker("Lululemon shares"), client

    def test_a_ticker_comes_back(self):
        assert self._resolve("LULU | Lululemon Athletica Inc.")[0] == "LULU"

    def test_it_runs_on_haiku_not_sonnet(self):
        _, client = self._resolve("LULU | Lululemon Athletica Inc.")
        from llm import HAIKU_MODEL
        assert client.messages.create.call_args.kwargs["model"] == HAIKU_MODEL

    @pytest.mark.parametrize("answer", ["NONE", "none", "", "I'm not sure",
                                        "US | United States", "$$$", "LULU"])
    def test_a_non_answer_yields_nothing(self, answer):
        """Anything that isn't cleanly a symbol must produce no ticker rather
        than a guess."""
        assert self._resolve(answer)[0] is None

    def test_stray_punctuation_is_tolerated(self):
        assert self._resolve("$LULU. | Lululemon Athletica Inc.")[0] == "LULU"

    def test_an_api_failure_is_not_fatal(self):
        with patch("llm.client") as client:
            client.messages.create.side_effect = RuntimeError("haiku down")
            assert tickers.resolve_company_ticker("Lululemon shares") is None

    def test_the_prompt_forbids_guessing(self):
        _, client = self._resolve("LULU | Lululemon Athletica Inc.")
        body = client.messages.create.call_args.kwargs["messages"][0]["content"].lower()
        assert "never guess" in body


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
