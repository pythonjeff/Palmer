"""Tests for price-watch alert logic. Pure logic only — no SerpAPI / Anthropic
calls. Run: pytest test_price_watches.py"""
from dotenv import load_dotenv
load_dotenv()

from datetime import datetime, timezone, timedelta

from shopping import (
    _cooldown_ok, _should_alert, DROP_THRESHOLD, _filter_and_sort, _shorten_url,
    _is_marketplace_thirdparty, _brand_tokens,
)


def _watch(**kwargs) -> dict:
    """Build a watch dict with sensible defaults; override fields per-test."""
    base = {
        "id": 1,
        "phone": "+15551234567",
        "product_name": "Nike Pegasus 40",
        "target_price": None,
        "currency": "USD",
        "baseline_price": None,
        "last_seen_price": None,
        "cooldown_hours": 12,
        "last_alerted": None,
    }
    base.update(kwargs)
    return base


class TestShouldAlert:
    def test_no_baseline_no_target_never_alerts(self):
        assert _should_alert(_watch(), 50.0) == ""

    def test_target_hit_alerts(self):
        assert _should_alert(_watch(target_price=100.0), 100.0) == "target"
        assert _should_alert(_watch(target_price=100.0), 99.99) == "target"

    def test_target_not_hit_no_alert(self):
        assert _should_alert(_watch(target_price=100.0), 100.01) == ""

    def test_drop_threshold_hit_alerts(self):
        # 15% drop from $200 → alert at $170 or below
        assert _should_alert(_watch(baseline_price=200.0), 170.0) == "drop"
        assert _should_alert(_watch(baseline_price=200.0), 150.0) == "drop"

    def test_drop_threshold_not_hit_no_alert(self):
        # 10% drop from $200 → $180, not enough
        assert _should_alert(_watch(baseline_price=200.0), 180.0) == ""

    def test_target_takes_precedence_over_drop(self):
        # Both would trigger; expect 'target' since that's the user's explicit ask
        assert _should_alert(_watch(target_price=150.0, baseline_price=200.0), 140.0) == "target"

    def test_drop_threshold_constant_is_reasonable(self):
        assert 0.5 < DROP_THRESHOLD < 1.0  # sanity: must be a discount fraction


class TestCooldownOk:
    def test_never_alerted_ok(self):
        assert _cooldown_ok(_watch(last_alerted=None))

    def test_recent_alert_blocked(self):
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        assert not _cooldown_ok(_watch(last_alerted=recent, cooldown_hours=12))

    def test_stale_alert_ok(self):
        stale = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        assert _cooldown_ok(_watch(last_alerted=stale, cooldown_hours=12))

    def test_exactly_at_cooldown_ok(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        past = (now - timedelta(hours=12)).isoformat()
        assert _cooldown_ok(_watch(last_alerted=past, cooldown_hours=12), now=now)

    def test_malformed_timestamp_treated_as_ok(self):
        # Defensive: don't block forever on bad data
        assert _cooldown_ok(_watch(last_alerted="not-a-date"))

    def test_z_suffix_iso_parses(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        assert not _cooldown_ok(_watch(last_alerted=past, cooldown_hours=12))


class TestFilterAndSort:
    def _sample(self) -> list[dict]:
        return [
            {"title": "Reebok Classic Leather", "price": 92.0, "merchant": "Zappos", "url": ""},
            {"title": "Reebok Nano X4", "price": 140.0, "merchant": "reebok.com", "url": ""},
            {"title": "Reebok Club C 85", "price": 75.0, "merchant": "Amazon", "url": ""},
            {"title": "Reebok Floatride", "price": 180.0, "merchant": "Nordstrom", "url": ""},
            {"title": "Reebok kids shoe", "price": 35.0, "merchant": "Target", "url": ""},
        ]

    def test_sorts_cheapest_first(self):
        out = _filter_and_sort(self._sample(), None, None, 10)
        prices = [r["price"] for r in out]
        assert prices == sorted(prices)

    def test_max_price_filter(self):
        out = _filter_and_sort(self._sample(), max_price=100.0, min_price=None, limit=10)
        assert all(r["price"] <= 100.0 for r in out)
        assert len(out) == 3  # 35, 75, 92

    def test_min_price_filter(self):
        out = _filter_and_sort(self._sample(), max_price=None, min_price=100.0, limit=10)
        assert all(r["price"] >= 100.0 for r in out)
        assert len(out) == 2  # 140, 180

    def test_band_filter(self):
        out = _filter_and_sort(self._sample(), max_price=150.0, min_price=70.0, limit=10)
        assert [r["price"] for r in out] == [75.0, 92.0, 140.0]

    def test_limit_caps_results(self):
        out = _filter_and_sort(self._sample(), None, None, 2)
        assert len(out) == 2
        assert [r["price"] for r in out] == [35.0, 75.0]

    def test_empty_input(self):
        assert _filter_and_sort([], None, None, 5) == []


class TestShortenUrl:
    def test_empty_string_passes_through(self):
        assert _shorten_url("") == ""

    def test_non_http_scheme_passes_through(self):
        # Guards against feeding TinyURL a garbage string; also protects against
        # accidentally shortening tel:/mailto: etc.
        assert _shorten_url("not-a-url") == "not-a-url"
        assert _shorten_url("ftp://example.com/x") == "ftp://example.com/x"

    def test_short_urls_pass_through_no_shortening(self):
        # Under threshold — return raw so user doesn't get a redirect hop
        url = "https://www.madewell.com/p/the-rockaway-tee/OA219/"
        assert _shorten_url(url) == url


class TestMarketplaceThirdparty:
    def test_ebay_individual_seller_is_thirdparty(self):
        assert _is_marketplace_thirdparty("eBay - dabondo1")
        assert _is_marketplace_thirdparty("eBay - sli00uo0rtoi")

    def test_named_marketplaces_are_thirdparty(self):
        for m in ("Poshmark", "Mercari", "Depop", "Vinted", "Grailed"):
            assert _is_marketplace_thirdparty(m), m

    def test_real_retailers_not_thirdparty(self):
        for m in ("Madewell", "Nordstrom Rack", "Zappos", "Amazon", "shopbop.com", "Allen Edmonds"):
            assert not _is_marketplace_thirdparty(m), m

    def test_link_mode_sort_pushes_thirdparty_last(self):
        # Google-ordered results: cheap eBay listing shouldn't win link mode
        results = [
            {"merchant": "eBay - randomseller", "price": 100.0},
            {"merchant": "Nordstrom Rack", "price": 180.0},
            {"merchant": "shopbop.com", "price": 212.0},
        ]
        ranked = sorted(results, key=lambda r: _is_marketplace_thirdparty(r["merchant"]))
        # Original order preserved for non-third-party; eBay falls to end
        assert ranked[0]["merchant"] == "Nordstrom Rack"
        assert ranked[-1]["merchant"].startswith("eBay - ")


class TestBrandTokens:
    def test_extracts_multi_word_brand(self):
        assert "allen" in _brand_tokens("Allen Edmonds Newman Penny Loafer")
        assert "edmonds" in _brand_tokens("Allen Edmonds Newman Penny Loafer")

    def test_short_words_ignored(self):
        # 'the', 'a', 'is' should not become tokens
        tokens = _brand_tokens("the Nike Air")
        assert "the" not in tokens
        assert "nike" in tokens

    def test_non_alpha_ignored(self):
        tokens = _brand_tokens("WH-1000XM5 Sony headphones")
        assert "sony" in tokens
        assert "headphones" in tokens
        # WH-1000XM5 contains non-alpha, should be dropped
        assert not any("1000" in t for t in tokens)

    def test_empty_query(self):
        assert _brand_tokens("") == []


class TestBaselineWorkflow:
    """The baseline-then-alert two-phase flow: first check silently records,
    later checks fire. We simulate the state machine at the dict level."""

    def test_first_check_no_alert_after_baseline_set(self):
        w = _watch(baseline_price=None, target_price=None)
        # No baseline, no target → run_price_watches would set baseline and NOT alert.
        assert _should_alert(w, 200.0) == ""

    def test_second_check_alerts_on_meaningful_drop(self):
        # After baseline was set at $200 on tick 1, tick 2 sees $150 → alert
        w = _watch(baseline_price=200.0)
        assert _should_alert(w, 150.0) == "drop"

    def test_second_check_no_alert_on_noise(self):
        # After baseline at $200, tick 2 sees $195 → nope
        w = _watch(baseline_price=200.0)
        assert _should_alert(w, 195.0) == ""
