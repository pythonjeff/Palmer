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
    ])
    def test_a_news_topic_gets_no_ticker(self, topic):
        assert resolve(topic) is None

    def test_a_price_word_alone_is_not_enough(self):
        """"stock" in the sentence must not make any capitalised word a ticker."""
        assert resolve("stock up on groceries") is None

    @pytest.mark.parametrize("word", ["US", "AI", "ETF", "IPO", "CEO", "NFL", "THE"])
    def test_known_non_tickers_are_rejected(self, word):
        assert word in tickers.NOT_TICKERS


class TestPrivateCompanies:
    """Asked for SpaceX's ticker a model will answer SPCE, which is Virgin
    Galactic. The guard runs before the model is ever consulted."""

    @pytest.mark.parametrize("topic", ["SpaceX stock", "OpenAI stock",
                                       "Stripe stock price", "Anthropic shares"])
    def test_a_private_company_never_resolves(self, topic):
        assert resolve(topic) is None

    @pytest.mark.parametrize("topic", ["SpaceX stock", "openai stock price"])
    def test_the_model_is_not_even_asked(self, topic):
        with patch("llm.client") as client:
            assert tickers.resolve_company_ticker(topic) is None
        client.messages.create.assert_not_called()

    def test_spacex_news_is_still_a_valid_news_topic(self):
        """Private only blocks a PRICE. The topic itself is fine."""
        assert resolve("SpaceX news") is None
        assert tickers.looks_like_price_topic("SpaceX news") is False


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

    def _resolve(self, answer):
        with patch("llm.client") as client:
            client.messages.create.return_value = _Resp(answer)
            return tickers.resolve_company_ticker("Lululemon shares"), client

    def test_a_ticker_comes_back(self):
        assert self._resolve("LULU")[0] == "LULU"

    def test_it_runs_on_haiku_not_sonnet(self):
        _, client = self._resolve("LULU")
        from llm import HAIKU_MODEL
        assert client.messages.create.call_args.kwargs["model"] == HAIKU_MODEL

    @pytest.mark.parametrize("answer", ["NONE", "none", "", "I'm not sure",
                                        "The ticker is LULU", "US", "$$$"])
    def test_a_non_answer_yields_nothing(self, answer):
        """Anything that isn't cleanly a symbol must produce no ticker rather
        than a guess."""
        assert self._resolve(answer)[0] is None

    def test_stray_punctuation_is_tolerated(self):
        assert self._resolve("$LULU.")[0] == "LULU"

    def test_an_api_failure_is_not_fatal(self):
        with patch("llm.client") as client:
            client.messages.create.side_effect = RuntimeError("haiku down")
            assert tickers.resolve_company_ticker("Lululemon shares") is None

    def test_the_prompt_forbids_guessing(self):
        _, client = self._resolve("LULU")
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
