"""Tests for Amazon price-watch logic. Pure logic + mocked SerpAPI/Haiku —
no real network or LLM calls. Run: pytest test_amazon_watches.py"""
from dotenv import load_dotenv
load_dotenv()

from unittest.mock import patch, MagicMock

import amazon
import shopping


class TestExtractPrice:
    def test_extracted_price_number(self):
        assert amazon._extract_price({"extracted_price": 42.99}) == 42.99

    def test_extracted_price_int(self):
        assert amazon._extract_price({"extracted_price": 30}) == 30.0

    def test_price_string_with_dollar_sign(self):
        assert amazon._extract_price({"price": "$54.00"}) == 54.0

    def test_price_string_with_comma(self):
        assert amazon._extract_price({"price": "$1,299.99"}) == 1299.99

    def test_price_string_malformed_returns_none(self):
        assert amazon._extract_price({"price": "call for price"}) is None

    def test_missing_price_returns_none(self):
        assert amazon._extract_price({}) is None

    def test_non_dict_returns_none(self):
        assert amazon._extract_price(None) is None
        assert amazon._extract_price("not a dict") is None

    def test_extracted_price_wins_over_string(self):
        # Both present — the numeric field is authoritative
        assert amazon._extract_price({"extracted_price": 40.0, "price": "$99.99"}) == 40.0


class TestSerpapiSearch:
    def _payload(self, items):
        return {"organic_results": items}

    def test_missing_key_returns_empty(self):
        with patch("amazon.SERP_API_KEY", ""):
            assert amazon._serpapi_search("protein") == []

    def test_empty_query_returns_empty(self):
        with patch("amazon.SERP_API_KEY", "fake"):
            assert amazon._serpapi_search("") == []

    def test_http_failure_returns_empty(self):
        with patch("amazon.SERP_API_KEY", "fake"), \
             patch("amazon._http_get_json", return_value=None):
            assert amazon._serpapi_search("protein") == []

    def test_skips_items_without_asin(self):
        payload = self._payload([
            {"title": "no asin here", "extracted_price": 20.0},
            {"asin": "B01", "title": "has asin", "extracted_price": 25.0},
        ])
        with patch("amazon.SERP_API_KEY", "fake"), \
             patch("amazon._http_get_json", return_value=payload):
            out = amazon._serpapi_search("thing")
        assert len(out) == 1
        assert out[0]["asin"] == "B01"

    def test_skips_items_without_price(self):
        payload = self._payload([
            {"asin": "B01", "title": "priced", "extracted_price": 20.0},
            {"asin": "B02", "title": "no price"},
        ])
        with patch("amazon.SERP_API_KEY", "fake"), \
             patch("amazon._http_get_json", return_value=payload):
            out = amazon._serpapi_search("thing")
        assert [r["asin"] for r in out] == ["B01"]

    def test_falls_back_to_dp_url_when_missing_link(self):
        payload = self._payload([
            {"asin": "B0ABC123", "title": "t", "extracted_price": 10.0},
        ])
        with patch("amazon.SERP_API_KEY", "fake"), \
             patch("amazon._http_get_json", return_value=payload):
            out = amazon._serpapi_search("thing")
        assert out[0]["url"] == "https://www.amazon.com/dp/B0ABC123"


class TestPickBestMatch:
    def _haiku_reply(self, text: str) -> MagicMock:
        block = MagicMock()
        block.text = text
        resp = MagicMock()
        resp.content = [block]
        return resp

    def _candidates(self):
        return [
            {"asin": "B01", "title": "Optimum Nutrition Gold Standard 5lb", "price": 54.0, "url": ""},
            {"asin": "B02", "title": "Optimum Nutrition Gold Standard 2lb", "price": 32.0, "url": ""},
        ]

    def test_empty_candidates_returns_none(self):
        assert amazon._pick_best_match("whey", []) is None

    def test_picks_indexed_candidate(self):
        with patch("amazon.client") as mock_client:
            mock_client.messages.create.return_value = self._haiku_reply("0")
            out = amazon._pick_best_match("Optimum Nutrition 5lb", self._candidates())
        assert out["asin"] == "B01"

    def test_none_reply_returns_none(self):
        with patch("amazon.client") as mock_client:
            mock_client.messages.create.return_value = self._haiku_reply("NONE")
            assert amazon._pick_best_match("random", self._candidates()) is None

    def test_out_of_range_index_returns_none(self):
        with patch("amazon.client") as mock_client:
            mock_client.messages.create.return_value = self._haiku_reply("99")
            assert amazon._pick_best_match("q", self._candidates()) is None

    def test_haiku_exception_returns_none(self):
        with patch("amazon.client") as mock_client:
            mock_client.messages.create.side_effect = RuntimeError("boom")
            assert amazon._pick_best_match("q", self._candidates()) is None


class TestExtractAsin:
    def test_empty_returns_none(self):
        assert amazon._extract_asin("") is None
        assert amazon._extract_asin(None) is None

    def test_plain_text_returns_none(self):
        assert amazon._extract_asin("protein shakes I like") is None

    def test_bare_amazon_url_extracts(self):
        assert amazon._extract_asin("https://www.amazon.com/dp/B0ABCDEFGH") == "B0ABCDEFGH"

    def test_amazon_url_with_slug_extracts(self):
        url = "https://www.amazon.com/Optimum-Nutrition-Standard-Whey-Chocolate/dp/B000QSNYGI/ref=xyz"
        assert amazon._extract_asin(url) == "B000QSNYGI"

    def test_gp_product_url_extracts(self):
        assert amazon._extract_asin("https://www.amazon.com/gp/product/B01AABBCCD?psc=1") == "B01AABBCCD"

    def test_url_embedded_in_text_extracts(self):
        assert amazon._extract_asin("check this out https://www.amazon.com/dp/B0ZZ111111 pretty good") == "B0ZZ111111"

    def test_lowercase_asin_does_not_match(self):
        # Amazon ASINs are always uppercase; a lowercase 10-char string is not an ASIN.
        assert amazon._extract_asin("https://www.amazon.com/dp/abcdefghij") is None

    def test_a_co_short_url_follows_redirect(self):
        # Simulate a.co redirecting to the canonical amazon.com/dp/… URL
        with patch("amazon._resolve_short_url",
                   return_value="https://www.amazon.com/dp/B0XYZ12345?ref=short"):
            assert amazon._extract_asin("https://a.co/d/08q64W9B") == "B0XYZ12345"

    def test_amzn_to_short_url_follows_redirect(self):
        with patch("amazon._resolve_short_url",
                   return_value="https://www.amazon.com/gp/product/B0AMZTOAAA"):
            assert amazon._extract_asin("here: https://amzn.to/3abcXYZ") == "B0AMZTOAAA"

    def test_short_url_redirect_failure_returns_none(self):
        with patch("amazon._resolve_short_url", return_value=None):
            assert amazon._extract_asin("https://a.co/d/broken") is None

    def test_short_url_redirects_to_non_product_page(self):
        # a.co could resolve to an amazon.com homepage / cart / list URL —
        # if there's no /dp/<ASIN> in the final URL, return None.
        with patch("amazon._resolve_short_url",
                   return_value="https://www.amazon.com/hz/wishlist/ls/XYZ"):
            assert amazon._extract_asin("https://a.co/d/anythg") is None


class TestResolveAsin:
    def _search_result(self):
        return [{"asin": "B0ABC", "title": "thing", "price": 42.0, "url": "https://amazon.com/dp/B0ABC"}]

    def test_no_match_returns_none(self):
        with patch("amazon._extract_asin", return_value=None), \
             patch("amazon._serpapi_search", return_value=[]), \
             patch("amazon._pick_best_match", return_value=None):
            assert amazon.resolve_asin("nonsense") is None

    def test_pick_returns_watchable_dict(self):
        with patch("amazon._extract_asin", return_value=None), \
             patch("amazon._serpapi_search", return_value=self._search_result()), \
             patch("amazon._pick_best_match", return_value=self._search_result()[0]):
            out = amazon.resolve_asin("thing")
        assert out["asin"] == "B0ABC"
        assert out["title"] == "thing"
        assert out["price"] == 42.0
        assert out["merchant"] == "Amazon"
        assert out["url"].endswith("B0ABC")

    def test_url_fast_path_skips_search(self):
        """When the user pastes an Amazon URL, resolve_asin extracts the ASIN
        and hits amazon_product directly — no SerpAPI search, no Haiku pick."""
        product = {"title": "Optimum Nutrition Gold Standard", "price": 54.0}
        with patch("amazon._extract_asin", return_value="B000QSNYGI"), \
             patch("amazon._amazon_product", return_value=product) as amp, \
             patch("amazon._serpapi_search") as search, \
             patch("amazon._pick_best_match") as pick:
            out = amazon.resolve_asin("https://www.amazon.com/dp/B000QSNYGI")
        assert out == {
            "asin": "B000QSNYGI",
            "title": "Optimum Nutrition Gold Standard",
            "price": 54.0,
            "url": "https://www.amazon.com/dp/B000QSNYGI",
            "merchant": "Amazon",
        }
        amp.assert_called_once_with("B000QSNYGI")
        search.assert_not_called()  # URL path skips search
        pick.assert_not_called()

    def test_url_fast_path_product_lookup_fails(self):
        """If the URL parses but amazon_product returns None, don't silently
        fall back to searching the URL string — return None so the caller can
        tell the user something went wrong with this specific listing."""
        with patch("amazon._extract_asin", return_value="B0DEADBEEF"), \
             patch("amazon._amazon_product", return_value=None), \
             patch("amazon._serpapi_search") as search:
            assert amazon.resolve_asin("https://www.amazon.com/dp/B0DEADBEEF") is None
        search.assert_not_called()


class TestCheckPrice:
    """check_price hits amazon_product with the stored ASIN, not the original query."""

    def _watch(self, **kw):
        base = {"id": 1, "asin": "B0PROT", "product_name": "Protein"}
        base.update(kw)
        return base

    def test_missing_asin_returns_none(self):
        with patch("amazon.SERP_API_KEY", "fake"):
            assert amazon.check_price(self._watch(asin=None)) is None

    def test_missing_key_returns_none(self):
        with patch("amazon.SERP_API_KEY", ""):
            assert amazon.check_price(self._watch()) is None

    def test_http_failure_returns_none(self):
        with patch("amazon.SERP_API_KEY", "fake"), \
             patch("amazon._http_get_json", return_value=None):
            assert amazon.check_price(self._watch()) is None

    def test_reads_product_results_price(self):
        payload = {"product_results": {"title": "Full title", "extracted_price": 42.0}}
        with patch("amazon.SERP_API_KEY", "fake"), \
             patch("amazon._http_get_json", return_value=payload):
            out = amazon.check_price(self._watch())
        assert out["price"] == 42.0
        assert out["title"] == "Full title"
        assert out["merchant"] == "Amazon"
        assert out["url"] == "https://www.amazon.com/dp/B0PROT"

    def test_falls_back_to_buybox_when_product_results_priceless(self):
        payload = {"product_results": {"title": "T"}, "buybox_winner": {"extracted_price": 39.99}}
        with patch("amazon.SERP_API_KEY", "fake"), \
             patch("amazon._http_get_json", return_value=payload):
            out = amazon.check_price(self._watch())
        assert out["price"] == 39.99

    def test_no_price_anywhere_returns_none(self):
        payload = {"product_results": {"title": "T"}}
        with patch("amazon.SERP_API_KEY", "fake"), \
             patch("amazon._http_get_json", return_value=payload):
            assert amazon.check_price(self._watch()) is None

    def test_uses_asin_not_query(self):
        """Regression: check_price MUST send the ASIN, not the product name.
        Rebuilding on the phrase would drift over time as Amazon's ranking changes."""
        captured = {}
        def _capture(url, timeout):
            captured["url"] = url
            return {"product_results": {"extracted_price": 10.0}}
        with patch("amazon.SERP_API_KEY", "fake"), \
             patch("amazon._http_get_json", side_effect=_capture):
            amazon.check_price(self._watch(asin="B0XYZ", product_name="totally different"))
        assert "asin=B0XYZ" in captured["url"]
        assert "totally+different" not in captured["url"]


class TestDraftAlert:
    def _watch(self, **kw):
        base = {"target_price": None, "baseline_price": 60.0}
        base.update(kw)
        return base

    def _current(self, **kw):
        base = {"price": 42.0, "url": "https://www.amazon.com/dp/B0PROT"}
        base.update(kw)
        return base

    def test_appends_url_even_on_haiku_failure(self):
        with patch("amazon.client") as mock_client:
            mock_client.messages.create.side_effect = RuntimeError("boom")
            body = amazon.draft_alert("Protein", self._current(), self._watch(), "drop")
        assert body.endswith("https://www.amazon.com/dp/B0PROT")

    def test_appends_url_on_success(self):
        block = MagicMock()
        block.text = "your protein just dropped to $42, was $60"
        resp = MagicMock()
        resp.content = [block]
        with patch("amazon.client") as mock_client:
            mock_client.messages.create.return_value = resp
            body = amazon.draft_alert("Protein", self._current(), self._watch(), "drop")
        assert body.endswith("https://www.amazon.com/dp/B0PROT")
        assert "$42" in body


class TestRunPriceWatchesDispatch:
    """The critical integration point: shopping-source rows go to shopping.check_price;
    amazon-source rows go to amazon.check_price. A mistake here corrupts baselines silently."""

    def _amazon_watch(self):
        return {
            "id": 1, "phone": "+15551234567", "product_name": "Protein",
            "source": "amazon", "asin": "B0ABC",
            "target_price": None, "baseline_price": None,
            "last_seen_price": None, "last_seen_url": None, "last_seen_merchant": None,
            "cooldown_hours": 12, "last_alerted": None, "last_alert_summary": None,
            "currency": "USD",
        }

    def _shopping_watch(self):
        return {
            "id": 2, "phone": "+15551234567", "product_name": "Sneakers",
            "source": "shopping", "asin": None,
            "target_price": None, "baseline_price": None,
            "last_seen_price": None, "last_seen_url": None, "last_seen_merchant": None,
            "cooldown_hours": 12, "last_alerted": None, "last_alert_summary": None,
            "currency": "USD",
        }

    def test_amazon_row_routes_to_amazon_check_price(self):
        amz = self._amazon_watch()
        shp = self._shopping_watch()
        current = {"price": 40.0, "title": "t", "merchant": "Amazon",
                   "url": "https://www.amazon.com/dp/B0ABC"}
        with patch("db.get_active_price_watches", return_value=[amz, shp]), \
             patch("amazon.check_price", return_value=current) as amz_check, \
             patch("shopping.check_price", return_value=None) as shp_check, \
             patch("db.set_price_watch_baseline") as set_baseline, \
             patch("sms_util.ensure_sms", return_value=True):
            shopping.run_price_watches()
        # Amazon path called with the full watch dict (needs the ASIN)
        amz_check.assert_called_once_with(amz)
        # Shopping path called with the product name string
        shp_check.assert_called_once_with(shp["product_name"])
        # Baseline seed happens for the Amazon row (baseline was None)
        set_baseline.assert_called_once()
        assert set_baseline.call_args[0][0] == amz["id"]

    def test_missing_source_defaults_to_shopping_path(self):
        """Older rows written before the source column existed have no source
        set. Dispatch must treat them as shopping, not skip or crash."""
        legacy = self._shopping_watch()
        legacy.pop("source")  # simulate a pre-migration row shape
        with patch("db.get_active_price_watches", return_value=[legacy]), \
             patch("amazon.check_price") as amz_check, \
             patch("shopping.check_price", return_value=None) as shp_check, \
             patch("db.set_price_watch_baseline"):
            shopping.run_price_watches()
        amz_check.assert_not_called()
        shp_check.assert_called_once_with(legacy["product_name"])

    def test_amazon_alert_uses_amazon_draft_alert(self):
        """When the alert fires on an Amazon row, the Amazon drafter (URL-inclusive)
        runs — not the shopping drafter (URL-less)."""
        amz = self._amazon_watch()
        amz["baseline_price"] = 60.0  # already seeded; a drop should alert
        current = {"price": 40.0, "title": "t", "merchant": "Amazon",
                   "url": "https://www.amazon.com/dp/B0ABC"}
        with patch("db.get_active_price_watches", return_value=[amz]), \
             patch("amazon.check_price", return_value=current), \
             patch("amazon.draft_alert", return_value="alert body https://www.amazon.com/dp/B0ABC") as amz_draft, \
             patch("shopping._draft_alert") as shop_draft, \
             patch("sms_util.ensure_sms", return_value=True), \
             patch("db.claim_price_watch_alert", return_value=True), \
             patch("db.update_price_watch_alerted"):
            shopping.run_price_watches()
        amz_draft.assert_called_once()
        shop_draft.assert_not_called()
