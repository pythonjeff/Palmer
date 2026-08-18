"""Tests for the per-watch daily cap and marketplace-filter added to price watches.
The cap prevents a price oscillating across the drop threshold from re-firing
indefinitely on the 12-hour cadence."""
from datetime import date, timedelta
from unittest.mock import patch

from shopping import _daily_ok, _is_marketplace_thirdparty, PRICE_DAILY_ALERT_MAX, check_price


class TestDailyOk:
    def test_no_history_ok(self):
        assert _daily_ok({"daily_alert_count": 0, "daily_alert_date": None})

    def test_prior_day_resets(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        assert _daily_ok({"daily_alert_count": 99, "daily_alert_date": yesterday})

    def test_under_cap_today_ok(self):
        today = date.today().isoformat()
        assert _daily_ok({"daily_alert_count": PRICE_DAILY_ALERT_MAX - 1,
                          "daily_alert_date": today})

    def test_at_cap_today_blocked(self):
        today = date.today().isoformat()
        assert not _daily_ok({"daily_alert_count": PRICE_DAILY_ALERT_MAX,
                              "daily_alert_date": today})

    def test_over_cap_today_blocked(self):
        today = date.today().isoformat()
        assert not _daily_ok({"daily_alert_count": PRICE_DAILY_ALERT_MAX + 5,
                              "daily_alert_date": today})


class TestMarketplaceExtended:
    def test_walmart_marketplace_seller_filtered(self):
        assert _is_marketplace_thirdparty("Walmart - Greensole LLC")

    def test_walmart_bare_still_ok(self):
        assert not _is_marketplace_thirdparty("Walmart")

    def test_stockx_goat_flagged(self):
        assert _is_marketplace_thirdparty("StockX")
        assert _is_marketplace_thirdparty("GOAT")


class TestCheckPriceStripsMarketplace:
    """The AirPods-at-Poshmark case: cheapest listing is a resale, but check_price
    should feed only real retailers to _pick_best_match."""

    def test_poshmark_dropped_before_pick(self):
        results = [
            {"title": "AirPods Pro 2", "price": 75.0, "merchant": "Poshmark",
             "url": "https://example.com/1"},
            {"title": "Apple AirPods Pro 2", "price": 200.0, "merchant": "Apple",
             "url": "https://example.com/2"},
            {"title": "AirPods Pro 2", "price": 100.0, "merchant": "eBay - randomseller",
             "url": "https://example.com/3"},
        ]
        seen = []

        def _capture(*args, **kwargs):
            seen.extend(args[1] if len(args) > 1 else kwargs.get("results", []))
            return None

        with patch("shopping._serpapi_search", return_value=results), \
             patch("shopping._pick_best_match", side_effect=_capture) as mock_pick:
            check_price("AirPods Pro 2")

        mock_pick.assert_called_once()
        passed = mock_pick.call_args.args[1]
        merchants = {r["merchant"] for r in passed}
        assert merchants == {"Apple"}, f"expected only real retailer, got {merchants}"
