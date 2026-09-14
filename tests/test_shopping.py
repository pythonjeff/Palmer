"""Price watches: Google Shopping, Amazon, the alert drafter, the daily cap.

Merged from test_price_watches.py, test_price_daily_cap.py, test_price_alert.py, test_amazon_watches.py; each section keeps its
original file's notes, because those carry the incident that led to the test.
"""
from datetime import datetime, timezone, timedelta, date
from unittest.mock import patch, MagicMock
from palmer.shopping import (
    _cooldown_ok,
    _should_alert,
    MOVE_MIN_ABS,
    _filter_and_sort,
    _is_marketplace_thirdparty,
    _brand_tokens,
    browse_shop,
    _daily_ok,
    PRICE_DAILY_ALERT_MAX,
    check_price,
)
from palmer import price_alert, amazon, shopping
from tests.helpers import llm_reply


# ============================================================================
# from test_price_watches.py
# ============================================================================
#
# Tests for price-watch alert logic. Pure logic only — no SerpAPI / Anthropic
# calls. Run: pytest test_price_watches.py

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
        with patch("palmer.serpapi._http_get_json", return_value=payload):
            out = browse_shop("Madewell mens tee shirts")
        assert "madewell.com" in out

    def test_brand_organic_beats_top_ranked_listicle(self):
        payload = self._serp(organic=[
            {"title": "Best T-Shirts 2026", "link": "https://www.buzzfeed.com/tees"},
            {"title": "Men's T-Shirts | Madewell", "link": "https://www.madewell.com/mens/tshirts"},
        ])
        with patch("palmer.serpapi._http_get_json", return_value=payload):
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
        with patch("palmer.serpapi._http_get_json", return_value=payload):
            out = browse_shop("mens tee shirts")
        assert "uniqlo.com" in out
        assert "pinterest" not in out
        assert "reddit" not in out

    def test_amazon_search_page_skipped_product_page_allowed(self):
        search_page = self._serp(organic=[
            {"title": "Amazon search", "link": "https://www.amazon.com/s?k=wool+coat"},
            {"title": "Wool coat", "link": "https://www.amazon.com/dp/B0XXXX"},
        ])
        with patch("palmer.serpapi._http_get_json", return_value=search_page):
            out = browse_shop("wool coat")
        assert "/dp/B0XXXX" in out
        assert "/s?k=" not in out

    def test_falls_back_to_top_valid_organic_when_no_brand_match(self):
        payload = self._serp(organic=[
            {"title": "Random shop", "link": "https://www.someshop.com/x"},
            {"title": "Another shop", "link": "https://www.othershop.com/y"},
        ])
        with patch("palmer.serpapi._http_get_json", return_value=payload):
            out = browse_shop("wool coat")
        assert "someshop.com" in out

    def test_empty_organic_returns_no_result_string(self):
        """Empty, and said as an empty result rather than a dead end.

        This used to assert the literal "No browse result found", which told
        the model nothing about what to do next — the shape that gets
        paraphrased into a refusal and then into another store."""
        from palmer import guards
        with patch("palmer.serpapi._http_get_json", return_value=self._serp()):
            out = browse_shop("Madewell tees")
        assert "DO have this" in out
        assert "confirm the brand" in out
        assert not guards.redirects_elsewhere(out)

    def test_serpapi_failure_returns_no_result_string(self):
        from palmer import guards
        with patch("palmer.serpapi._http_get_json", return_value=None):
            out = browse_shop("Madewell tees")
        assert "DO have this" in out
        assert not guards.redirects_elsewhere(out)

    def test_missing_api_key_returns_unavailable(self):
        with patch("palmer.serpapi.API_KEY", ""):
            out = browse_shop("Madewell tees")
        # The string must not imply the capability does not exist — that is what
        # the model paraphrased into "I can't do that, try somewhere else".
        low = out.lower()
        assert "failed to run" in low and "try again" in low
        assert "do have product search" in low
        for rival in ("google", "amazon.com", "another store"):
            assert f"try {rival}" not in low


class TestPriceWatchSchedule:
    """run_price_watches must be on a fixed-hour cron, not an interval.

    An interval job's first run is start + interval, and the clock restarts on
    every dyno boot — i.e. every deploy. At a 12h cadence that meant the job
    only ever ran on days production was left alone for 12 straight hours; on a
    day with four deploys it never ran at all, and a tick that finds nothing
    logs nothing, so it failed silently. The property under test is that the
    fire times are a function of the clock, not of when the process started."""

    def _trigger(self):
        from palmer import main
        from palmer.shopping import run_price_watches
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


# ============================================================================
# from test_price_daily_cap.py
# ============================================================================
#
# Tests for the per-watch daily cap and marketplace-filter added to price watches.
# The cap prevents a price oscillating across the drop threshold from re-firing
# indefinitely on the 12-hour cadence.

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

        with patch("palmer.shopping._serpapi_search", return_value=results), \
             patch("palmer.shopping._pick_best_match", side_effect=_capture) as mock_pick:
            check_price("AirPods Pro 2")

        mock_pick.assert_called_once()
        passed = mock_pick.call_args.args[1]
        merchants = {r["merchant"] for r in passed}
        assert merchants == {"Apple"}, f"expected only real retailer, got {merchants}"


# ============================================================================
# from test_price_alert.py
# ============================================================================
#
# Tests for the shared price-alert drafter.
#
# shopping.py and amazon.py used to carry near-identical copies of this. These
# pin the behavior that has to stay identical across both sources, and the two
# places they legitimately differ (the price line, and whether a URL is appended).

CURRENT = {"price": 42.0, "merchant": "Zappos", "url": "https://www.amazon.com/dp/B0PROT"}
WATCH = {"phone": "+15550001111", "target_price": None, "baseline_price": 60.0}


class TestContextFacts:
    def test_drop_states_dollars_and_percentage(self):
        # Dollars lead — the $2 materiality rule is denominated in dollars.
        ctx = price_alert._context("Protein", CURRENT, WATCH, "drop", None)
        assert "Down $18.00 (about 30%)" in ctx and "$60.00" in ctx

    def test_rise_says_up_not_down(self):
        # A rise must never be described as a drop; $60 -> $63.50 is +$3.50.
        ctx = price_alert._context("Protein", {"price": 63.50, "merchant": "Zappos"},
                                   WATCH, "rise", None)
        assert "Up $3.50 (about 6%)" in ctx
        assert "Down" not in ctx

    def test_target_hit(self):
        watch = dict(WATCH, target_price=45.0)
        ctx = price_alert._context("Protein", CURRENT, watch, "target", None)
        assert "at or under $45.00" in ctx

    def test_shopping_price_line_names_the_merchant(self):
        ctx = price_alert._context("Protein", CURRENT, WATCH, "drop", None)
        assert "Zappos" in ctx

    def test_amazon_price_line_names_the_source(self):
        ctx = price_alert._context("Protein", CURRENT, WATCH, "drop", "Amazon")
        assert "Amazon price: $42.00" in ctx

    def test_missing_merchant_does_not_blow_up(self):
        ctx = price_alert._context("Protein", {"price": 42.0}, WATCH, "drop", None)
        assert "unknown seller" in ctx

    def test_no_baseline_no_percentage(self):
        watch = dict(WATCH, baseline_price=None)
        ctx = price_alert._context("Protein", CURRENT, watch, "drop", None)
        assert "Down" not in ctx

    def test_rise_prompt_does_not_claim_the_watch_hit(self):
        # "your price watch just hit" on a price INCREASE reads as good news.
        from unittest.mock import patch, MagicMock
        seen = {}
        def _capture(**kw):
            seen.update(kw)
            m = MagicMock(); m.content = [MagicMock(text="shake went up to 63.50")]
            return m
        with patch("palmer.price_alert.client.messages.create", side_effect=_capture), \
             patch("palmer.price_alert._build_system", return_value="sys"):
            price_alert.draft_price_alert("Protein", {"price": 63.50, "merchant": "Z"},
                                          WATCH, "rise")
        prompt = seen["messages"][0]["content"]
        assert "just hit" not in prompt
        assert "went UP" in prompt


class TestDrafting:
    def _draft(self, **kw):
        with patch.object(price_alert, "_build_system", return_value="SYS"), \
             patch.object(price_alert.client.messages, "create",
                          return_value=llm_reply("protein's down to $42")) as create:
            body = price_alert.draft_price_alert("Protein", CURRENT, WATCH, "drop", **kw)
        return body, create

    def test_no_link_by_default(self):
        body, _ = self._draft()
        assert body == "protein's down to $42"

    def test_link_appended_when_given(self):
        body, _ = self._draft(link=CURRENT["url"])
        assert body.endswith(CURRENT["url"])

    def test_uses_sonnet_not_haiku(self):
        """User-facing drafting belongs on Sonnet per CLAUDE.md routing."""
        _, create = self._draft()
        assert create.call_args.kwargs["model"] == price_alert.SONNET_MODEL

    def test_uses_the_real_system_prompt(self):
        """The whole point: price alerts sound like the Palmer they talk to."""
        _, create = self._draft()
        assert create.call_args.kwargs["system"] == "SYS"

    def test_prompt_suppresses_url_when_one_is_appended(self):
        _, create = self._draft(link=CURRENT["url"])
        assert "Do NOT include a URL" in create.call_args.kwargs["messages"][0]["content"]

    def test_falls_back_to_the_base_prompt_without_a_phone(self):
        """A malformed row shouldn't cost the user their alert."""
        with patch.object(price_alert, "_build_system") as bs, \
             patch.object(price_alert.client.messages, "create", return_value=llm_reply("x")) as create:
            price_alert.draft_price_alert("Protein", CURRENT, {"baseline_price": 60.0}, "drop")
        bs.assert_not_called()
        # Used to assert NO system prompt at all, which meant a failed profile
        # read dropped the voice, the calibration and every NEVER rule — the
        # anti-redirect one included — from a message that still went out.
        sys = create.call_args.kwargs.get("system") or ""
        assert "Palmer" in sys, "must still sound like Palmer"
        assert "{profile_block}" not in sys, "the template must be formatted, not raw"


class TestFailsSafe:
    def test_model_failure_falls_back_to_facts(self):
        with patch.object(price_alert, "_build_system", return_value="SYS"), \
             patch.object(price_alert.client.messages, "create", side_effect=RuntimeError("boom")):
            body = price_alert.draft_price_alert("Protein", CURRENT, WATCH, "drop")
        assert "42" in body and "Protein" in body

    def test_fallback_still_appends_the_link(self):
        with patch.object(price_alert, "_build_system", return_value="SYS"), \
             patch.object(price_alert.client.messages, "create", side_effect=RuntimeError("boom")):
            body = price_alert.draft_price_alert("Protein", CURRENT, WATCH, "drop",
                                                 link=CURRENT["url"], source_label="Amazon")
        assert body.endswith(CURRENT["url"])
        assert "Amazon" in body

    def test_empty_model_output_falls_back(self):
        with patch.object(price_alert, "_build_system", return_value="SYS"), \
             patch.object(price_alert.client.messages, "create", return_value=llm_reply("   ")):
            body = price_alert.draft_price_alert("Protein", CURRENT, WATCH, "drop")
        assert body.strip(), "must never send an empty alert"

    def test_build_system_failure_does_not_lose_the_alert(self):
        with patch.object(price_alert, "_build_system", side_effect=RuntimeError("db down")), \
             patch.object(price_alert.client.messages, "create", return_value=llm_reply("x")):
            body = price_alert.draft_price_alert("Protein", CURRENT, WATCH, "drop")
        assert body.strip()


class TestBothSourcesDelegate:
    def test_shopping_passes_no_link(self):
        from palmer import shopping
        with patch("palmer.price_alert.draft_price_alert", return_value="line") as d:
            shopping._draft_alert("Protein", CURRENT, WATCH, "drop")
        assert d.call_args.kwargs.get("link") is None

    def test_amazon_passes_link_and_label(self):
        from palmer import amazon
        with patch("palmer.price_alert.draft_price_alert", return_value="line") as d:
            amazon.draft_alert("Protein", CURRENT, WATCH, "drop")
        assert d.call_args.kwargs["link"] == CURRENT["url"]
        assert d.call_args.kwargs["source_label"] == "Amazon"


# ============================================================================
# from test_amazon_watches.py
# ============================================================================
#
# Tests for Amazon price-watch logic. Pure logic + mocked SerpAPI/Haiku —
# no real network or LLM calls. Run: pytest test_amazon_watches.py

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
        with patch("palmer.serpapi.API_KEY", ""):
            assert amazon._serpapi_search("protein") == []

    def test_empty_query_returns_empty(self):
        with patch("palmer.serpapi.API_KEY", "fake"):
            assert amazon._serpapi_search("") == []

    def test_http_failure_returns_empty(self):
        with patch("palmer.serpapi.API_KEY", "fake"), \
             patch("palmer.serpapi._http_get_json", return_value=None):
            assert amazon._serpapi_search("protein") == []

    def test_skips_items_without_asin(self):
        payload = self._payload([
            {"title": "no asin here", "extracted_price": 20.0},
            {"asin": "B01", "title": "has asin", "extracted_price": 25.0},
        ])
        with patch("palmer.serpapi.API_KEY", "fake"), \
             patch("palmer.serpapi._http_get_json", return_value=payload):
            out = amazon._serpapi_search("thing")
        assert len(out) == 1
        assert out[0]["asin"] == "B01"

    def test_skips_items_without_price(self):
        payload = self._payload([
            {"asin": "B01", "title": "priced", "extracted_price": 20.0},
            {"asin": "B02", "title": "no price"},
        ])
        with patch("palmer.serpapi.API_KEY", "fake"), \
             patch("palmer.serpapi._http_get_json", return_value=payload):
            out = amazon._serpapi_search("thing")
        assert [r["asin"] for r in out] == ["B01"]

    def test_falls_back_to_dp_url_when_missing_link(self):
        payload = self._payload([
            {"asin": "B0ABC123", "title": "t", "extracted_price": 10.0},
        ])
        with patch("palmer.serpapi.API_KEY", "fake"), \
             patch("palmer.serpapi._http_get_json", return_value=payload):
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
        with patch("palmer.amazon.client") as mock_client:
            mock_client.messages.create.return_value = self._haiku_reply("0")
            out = amazon._pick_best_match("Optimum Nutrition 5lb", self._candidates())
        assert out["asin"] == "B01"

    def test_none_reply_returns_none(self):
        with patch("palmer.amazon.client") as mock_client:
            mock_client.messages.create.return_value = self._haiku_reply("NONE")
            assert amazon._pick_best_match("random", self._candidates()) is None

    def test_out_of_range_index_returns_none(self):
        with patch("palmer.amazon.client") as mock_client:
            mock_client.messages.create.return_value = self._haiku_reply("99")
            assert amazon._pick_best_match("q", self._candidates()) is None

    def test_haiku_exception_returns_none(self):
        with patch("palmer.amazon.client") as mock_client:
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
        with patch("palmer.amazon._resolve_short_url",
                   return_value="https://www.amazon.com/dp/B0XYZ12345?ref=short"):
            assert amazon._extract_asin("https://a.co/d/08q64W9B") == "B0XYZ12345"

    def test_amzn_to_short_url_follows_redirect(self):
        with patch("palmer.amazon._resolve_short_url",
                   return_value="https://www.amazon.com/gp/product/B0AMZTOAAA"):
            assert amazon._extract_asin("here: https://amzn.to/3abcXYZ") == "B0AMZTOAAA"

    def test_short_url_redirect_failure_returns_none(self):
        with patch("palmer.amazon._resolve_short_url", return_value=None):
            assert amazon._extract_asin("https://a.co/d/broken") is None

    def test_short_url_redirects_to_non_product_page(self):
        # a.co could resolve to an amazon.com homepage / cart / list URL —
        # if there's no /dp/<ASIN> in the final URL, return None.
        with patch("palmer.amazon._resolve_short_url",
                   return_value="https://www.amazon.com/hz/wishlist/ls/XYZ"):
            assert amazon._extract_asin("https://a.co/d/anythg") is None


class TestResolveAsin:
    def _search_result(self):
        return [{"asin": "B0ABC", "title": "thing", "price": 42.0, "url": "https://amazon.com/dp/B0ABC"}]

    def test_no_match_returns_none(self):
        with patch("palmer.amazon._extract_asin", return_value=None), \
             patch("palmer.amazon._serpapi_search", return_value=[]), \
             patch("palmer.amazon._pick_best_match", return_value=None):
            assert amazon.resolve_asin("nonsense") is None

    def test_pick_returns_watchable_dict(self):
        with patch("palmer.amazon._extract_asin", return_value=None), \
             patch("palmer.amazon._serpapi_search", return_value=self._search_result()), \
             patch("palmer.amazon._pick_best_match", return_value=self._search_result()[0]):
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
        with patch("palmer.amazon._extract_asin", return_value="B000QSNYGI"), \
             patch("palmer.amazon._amazon_product", return_value=product) as amp, \
             patch("palmer.amazon._serpapi_search") as search, \
             patch("palmer.amazon._pick_best_match") as pick:
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
        with patch("palmer.amazon._extract_asin", return_value="B0DEADBEEF"), \
             patch("palmer.amazon._amazon_product", return_value=None), \
             patch("palmer.amazon._serpapi_search") as search:
            assert amazon.resolve_asin("https://www.amazon.com/dp/B0DEADBEEF") is None
        search.assert_not_called()


class TestCheckPrice:
    """check_price hits amazon_product with the stored ASIN, not the original query."""

    def _watch(self, **kw):
        base = {"id": 1, "asin": "B0PROT", "product_name": "Protein"}
        base.update(kw)
        return base

    def test_missing_asin_returns_none(self):
        with patch("palmer.serpapi.API_KEY", "fake"):
            assert amazon.check_price(self._watch(asin=None)) is None

    def test_missing_key_returns_none(self):
        with patch("palmer.serpapi.API_KEY", ""):
            assert amazon.check_price(self._watch()) is None

    def test_http_failure_returns_none(self):
        with patch("palmer.serpapi.API_KEY", "fake"), \
             patch("palmer.serpapi._http_get_json", return_value=None):
            assert amazon.check_price(self._watch()) is None

    def test_reads_product_results_price(self):
        payload = {"product_results": {"title": "Full title", "extracted_price": 42.0}}
        with patch("palmer.serpapi.API_KEY", "fake"), \
             patch("palmer.serpapi._http_get_json", return_value=payload):
            out = amazon.check_price(self._watch())
        assert out["price"] == 42.0
        assert out["title"] == "Full title"
        assert out["merchant"] == "Amazon"
        assert out["url"] == "https://www.amazon.com/dp/B0PROT"

    def test_falls_back_to_buybox_when_product_results_priceless(self):
        payload = {"product_results": {"title": "T"}, "buybox_winner": {"extracted_price": 39.99}}
        with patch("palmer.serpapi.API_KEY", "fake"), \
             patch("palmer.serpapi._http_get_json", return_value=payload):
            out = amazon.check_price(self._watch())
        assert out["price"] == 39.99

    def test_no_price_anywhere_returns_none(self):
        payload = {"product_results": {"title": "T"}}
        with patch("palmer.serpapi.API_KEY", "fake"), \
             patch("palmer.serpapi._http_get_json", return_value=payload):
            assert amazon.check_price(self._watch()) is None

    def test_uses_asin_not_query(self):
        """Regression: check_price MUST send the ASIN, not the product name.
        Rebuilding on the phrase would drift over time as Amazon's ranking changes."""
        captured = {}
        def _capture(url, timeout):
            captured["url"] = url
            return {"product_results": {"extracted_price": 10.0}}
        with patch("palmer.serpapi.API_KEY", "fake"), \
             patch("palmer.serpapi._http_get_json", side_effect=_capture):
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

    # Drafting moved to price_alert (shared with shopping), so the model client
    # to patch lives there — patching amazon.client here silently let these make
    # real API calls.
    def test_appends_url_even_on_draft_failure(self):
        with patch("palmer.price_alert.client") as mock_client:
            mock_client.messages.create.side_effect = RuntimeError("boom")
            body = amazon.draft_alert("Protein", self._current(), self._watch(), "drop")
        assert body.endswith("https://www.amazon.com/dp/B0PROT")

    def test_appends_url_on_success(self):
        block = MagicMock()
        block.text = "your protein just dropped to $42, was $60"
        resp = MagicMock()
        resp.content = [block]
        with patch("palmer.price_alert.client") as mock_client:
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
        with patch("palmer.db.get_active_price_watches", return_value=[amz, shp]), \
             patch("palmer.amazon.check_price", return_value=current) as amz_check, \
             patch("palmer.shopping.check_price", return_value=None) as shp_check, \
             patch("palmer.db.set_price_watch_baseline") as set_baseline, \
             patch("palmer.sms_util.ensure_sms", return_value=True):
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
        with patch("palmer.db.get_active_price_watches", return_value=[legacy]), \
             patch("palmer.amazon.check_price") as amz_check, \
             patch("palmer.shopping.check_price", return_value=None) as shp_check, \
             patch("palmer.db.set_price_watch_baseline"):
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
        with patch("palmer.db.get_active_price_watches", return_value=[amz]), \
             patch("palmer.amazon.check_price", return_value=current), \
             patch("palmer.amazon.draft_alert", return_value="alert body https://www.amazon.com/dp/B0ABC") as amz_draft, \
             patch("palmer.shopping._draft_alert") as shop_draft, \
             patch("palmer.sms_util.ensure_sms", return_value=True), \
             patch("palmer.db.claim_price_watch_alert", return_value=True), \
             patch("palmer.db.update_price_watch_alerted"):
            shopping.run_price_watches()
        amz_draft.assert_called_once()
        shop_draft.assert_not_called()
