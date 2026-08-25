"""Tests for price-watch alert logic. Pure logic only — no SerpAPI / Anthropic
calls. Run: pytest test_price_watches.py"""
from dotenv import load_dotenv
load_dotenv()

from datetime import datetime, timezone, timedelta

from unittest.mock import patch

from shopping import (
    _cooldown_ok, _should_alert, MOVE_MIN_ABS, _filter_and_sort,
    _is_marketplace_thirdparty, _brand_tokens, browse_shop,
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

    def test_drop_over_the_bar_alerts(self):
        assert _should_alert(_watch(baseline_price=50.98), 47.98) == "drop"
        assert _should_alert(_watch(baseline_price=200.0), 150.0) == "drop"

    def test_move_under_the_bar_is_silent(self):
        assert _should_alert(_watch(baseline_price=50.98), 49.50) == ""
        assert _should_alert(_watch(baseline_price=200.0), 198.50) == ""
        assert _should_alert(_watch(baseline_price=200.0), 201.50) == ""

    def test_bar_is_flat_not_proportional(self):
        # The rule is "$2 on any product", so an expensive item is NOT held to a
        # bigger move. A percentage bar (briefly max(5%, $2)) needed $10 here.
        assert _should_alert(_watch(baseline_price=200.0), 197.50) == "drop"
        assert _should_alert(_watch(baseline_price=1200.0), 1197.50) == "drop"
        # ...and a cheap item clears it on the same absolute move.
        assert _should_alert(_watch(baseline_price=12.00), 9.50) == "drop"

    def test_rise_over_the_bar_alerts(self):
        # A material move UP is worth a text too — it's the cue to buy now.
        assert _should_alert(_watch(baseline_price=50.98), 53.98) == "rise"
        assert _should_alert(_watch(baseline_price=200.0), 202.50) == "rise"

    def test_boundary_is_inclusive_both_ways(self):
        assert _should_alert(_watch(baseline_price=100.0), 98.00) == "drop"
        assert _should_alert(_watch(baseline_price=100.0), 102.00) == "rise"
        assert _should_alert(_watch(baseline_price=100.0), 98.01) == ""
        assert _should_alert(_watch(baseline_price=100.0), 101.99) == ""

    def test_target_takes_precedence_over_drop(self):
        # Both would trigger; expect 'target' since that's the user's explicit ask
        assert _should_alert(_watch(target_price=150.0, baseline_price=200.0), 140.0) == "target"

    def test_a_rise_never_reports_as_a_target_hit(self):
        # target is a ceiling: a price above it must not read as "you got it".
        assert _should_alert(_watch(target_price=45.0, baseline_price=50.0), 55.0) == "rise"

    def test_move_constant_is_reasonable(self):
        assert 0.0 < MOVE_MIN_ABS < 10.0  # a materiality floor, in dollars


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
        # After baseline at $200, tick 2 sees $199.20 — an $0.80 wobble, under
        # the flat $2 bar. ($195 used to count as noise here; under a flat bar a
        # $5 move on any product is material, which is the point of the change.)
        w = _watch(baseline_price=200.0)
        assert _should_alert(w, 199.20) == ""

    def test_alerted_price_becomes_the_new_baseline(self):
        # update_price_watch_alerted re-baselines to the alerted price, so the
        # same discount does not re-qualify on the next tick — only a further
        # drop does. Simulated at the dict level, like the rest of this class.
        w = _watch(baseline_price=200.0)
        assert _should_alert(w, 150.0) == "drop"
        w["baseline_price"] = 150.0          # what the alert write now does
        assert _should_alert(w, 150.0) == ""  # still cheap, but already told
        assert _should_alert(w, 147.0) == "drop"  # a further $3 move does fire


class TestBrowseShop:
    """Pure-logic coverage for browse_shop's ranking + aggregator-skip rules.
    _http_get_json is mocked so no network calls."""

    def _serp(self, organic=None, knowledge_graph=None) -> dict:
        return {
            "organic_results": organic or [],
            "knowledge_graph": knowledge_graph or {},
        }

    def test_knowledge_graph_wins_when_brand_matches(self):
        payload = self._serp(
            organic=[{"title": "Buzzfeed roundup", "link": "https://www.buzzfeed.com/best-tees"}],
            knowledge_graph={"title": "Madewell", "website": "https://www.madewell.com"},
        )
        with patch("serpapi._http_get_json", return_value=payload):
            out = browse_shop("Madewell mens tee shirts")
        assert "madewell.com" in out

    def test_brand_organic_beats_top_ranked_listicle(self):
        payload = self._serp(organic=[
            {"title": "Best T-Shirts 2026", "link": "https://www.buzzfeed.com/tees"},
            {"title": "Men's T-Shirts | Madewell", "link": "https://www.madewell.com/mens/tshirts"},
        ])
        with patch("serpapi._http_get_json", return_value=payload):
            out = browse_shop("Madewell mens tee shirts")
        assert "madewell.com/mens/tshirts" in out
        assert "buzzfeed" not in out

    def test_aggregators_skipped(self):
        # No brand token match; walk past pinterest/reddit/google to real store
        payload = self._serp(organic=[
            {"title": "Pinterest pins", "link": "https://www.pinterest.com/x"},
            {"title": "Reddit thread", "link": "https://www.reddit.com/r/malefashionadvice/x"},
            {"title": "Actual store", "link": "https://www.uniqlo.com/us/en/men/tops/t-shirts"},
        ])
        with patch("serpapi._http_get_json", return_value=payload):
            out = browse_shop("mens tee shirts")
        assert "uniqlo.com" in out
        assert "pinterest" not in out
        assert "reddit" not in out

    def test_amazon_search_page_skipped_product_page_allowed(self):
        search_page = self._serp(organic=[
            {"title": "Amazon search", "link": "https://www.amazon.com/s?k=wool+coat"},
            {"title": "Wool coat", "link": "https://www.amazon.com/dp/B0XXXX"},
        ])
        with patch("serpapi._http_get_json", return_value=search_page):
            out = browse_shop("wool coat")
        assert "/dp/B0XXXX" in out
        assert "/s?k=" not in out

    def test_falls_back_to_top_valid_organic_when_no_brand_match(self):
        payload = self._serp(organic=[
            {"title": "Random shop", "link": "https://www.someshop.com/x"},
            {"title": "Another shop", "link": "https://www.othershop.com/y"},
        ])
        with patch("serpapi._http_get_json", return_value=payload):
            out = browse_shop("wool coat")
        assert "someshop.com" in out

    def test_empty_organic_returns_no_result_string(self):
        with patch("serpapi._http_get_json", return_value=self._serp()):
            out = browse_shop("Madewell tees")
        assert out.startswith("No browse result found")

    def test_serpapi_failure_returns_no_result_string(self):
        with patch("serpapi._http_get_json", return_value=None):
            out = browse_shop("Madewell tees")
        assert out.startswith("No browse result found")

    def test_missing_api_key_returns_unavailable(self):
        with patch("serpapi.API_KEY", ""):
            out = browse_shop("Madewell tees")
        assert "unavailable" in out.lower()


class TestPriceWatchSchedule:
    """run_price_watches must be on a fixed-hour cron, not an interval.

    An interval job's first run is start + interval, and the clock restarts on
    every dyno boot — i.e. every deploy. At a 12h cadence that meant the job
    only ever ran on days production was left alone for 12 straight hours; on a
    day with four deploys it never ran at all, and a tick that finds nothing
    logs nothing, so it failed silently. The property under test is that the
    fire times are a function of the clock, not of when the process started."""

    def _trigger(self):
        from unittest.mock import patch
        with patch("apscheduler.schedulers.background.BackgroundScheduler.start"):
            import main
        from shopping import run_price_watches
        jobs = [j for j in main._scheduler.get_jobs() if j.func is run_price_watches]
        assert len(jobs) == 1, "expected exactly one run_price_watches job"
        return jobs[0].trigger

    def test_is_a_cron_trigger(self):
        from apscheduler.triggers.cron import CronTrigger
        assert isinstance(self._trigger(), CronTrigger)

    def test_phase_is_independent_of_process_start(self):
        # The regression an interval trigger would reintroduce: boot at two
        # different moments and the schedule must not move.
        from datetime import datetime, timedelta, timezone
        trigger = self._trigger()
        boot_a = datetime(2026, 8, 25, 3, 17, tzinfo=timezone.utc)
        boot_b = boot_a + timedelta(hours=5, minutes=42)  # a later deploy
        assert (trigger.get_next_fire_time(None, boot_a)
                == trigger.get_next_fire_time(None, boot_b))

    def test_runs_exactly_twice_a_day(self):
        # The SerpAPI budget is denominated in runs per day, not in evenness of
        # spacing — these slots are 16h and 8h apart on purpose (see main.py).
        from datetime import datetime, timedelta, timezone
        trigger = self._trigger()
        start = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
        fire, fires = None, []
        while True:
            fire = trigger.get_next_fire_time(fire, start if fire is None else fire + timedelta(seconds=1))
            if fire >= start + timedelta(days=1):
                break
            fires.append(fire)
        assert len(fires) == 2, fires

    def test_fires_during_waking_hours_for_served_timezones(self):
        # These are unprompted texts. Both user timezones on record must land in
        # daytime, or the cadence is correct and the experience still bad.
        from datetime import datetime, timedelta, timezone
        from zoneinfo import ZoneInfo
        trigger = self._trigger()
        fire = None
        for _ in range(4):
            start = datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc) if fire is None else fire + timedelta(seconds=1)
            fire = trigger.get_next_fire_time(fire, start)
            for tz in ("America/Chicago", "America/Los_Angeles"):
                hour = fire.astimezone(ZoneInfo(tz)).hour
                assert 8 <= hour <= 21, f"{fire} is {hour}:00 in {tz}"
