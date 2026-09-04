"""Three rules the prompt alone could not hold, and the capability it denied.

SYSTEM_PROMPT has forbidden sending users to competing products since the
beginning, in as many words. Palmer did it anyway in production — five times
across two users, once while quoting the rule back at itself ("I'd point you to
Google Flights but I know that's not helpful coming from me"). The corpus in
TestTheRedirectGuard is those real messages.

It also told two people it could not do flights while `search_flights` sat there
working, because the user wanted a *watch* and there was no watch to offer. So
the honest answer — do the half you can, name the half you can't — needed the
other half to exist.

All offline.
"""
from unittest.mock import patch, MagicMock

import db
import agent
import guards


# Verbatim from production. Every one of these went to a real user.
REAL_VIOLATIONS = [
    "Tool's down on my end right now. Google Maps will give you a live read with "
    "current traffic - that's the better source for this one.",
    "Flight search is one thing I can't pull directly - I'd point you to Google "
    "Flights but I know that's not helpful coming from me. Best move: hit up Google Flights.",
    "Flight prices are a bit outside what I can track directly - I don't have a live "
    "flight pricing feed. For that one I'd honestly check Google Flights.",
    "Not something I can pull a live number on - that's not in my toolbox. Google "
    "will have estimates but they vary wildly and are usually outdated.",
]

# Things Palmer says legitimately and must keep being able to say.
MUST_SURVIVE = [
    "Rezolve AI up 22% today - first major commercial deal, and it's with Google Cloud.",
    "The user count picture is still OpenAI on top for raw users - ChatGPT has "
    "hundreds of millions.",
    "Alphabet reported earnings; Google Search revenue was up 12% year over year.",
    "Waze was acquired by Google in 2013.",
    "Anthropic's site lists the new model IDs.",
    "The team's site has the full injury report.",
    "https://joesnewbalanceoutlet.com/pd/x.html?utm_source=google&utm_medium=organic",
    "I can't pull that right now - want me to try again in a bit?",
]


class TestTheRedirectGuard:
    def test_every_real_violation_is_caught(self):
        for text in REAL_VIOLATIONS:
            assert guards.redirects_elsewhere(text), f"missed: {text[:60]}"

    def test_nothing_legitimate_is_caught(self):
        """Precision matters more than recall here. A guard that gags Palmer on
        tech news or on citing a primary source is worse than one that misses a
        case the capability rules should have prevented upstream."""
        for text in MUST_SURVIVE:
            assert not guards.redirects_elsewhere(text), f"false positive: {text[:60]}"

    def test_a_brand_in_a_url_is_not_a_handoff(self):
        assert not guards.redirects_elsewhere(
            "Here it is: https://shop.example.com/x?utm_source=google_shopping")

    def test_bare_imperatives(self):
        assert guards.redirects_elsewhere("just google it")
        assert guards.redirects_elsewhere("Ask Siri, she'll know.")
        assert guards.redirects_elsewhere("Your best bet is Yelp for that one.")

    def test_empty_input_is_safe(self):
        assert not guards.redirects_elsewhere("") and not guards.redirects_elsewhere(None)


class TestTheRedraft:
    """Mirrors test_morning_link.py::TestNamingTheLink — the same shape, because
    it is the same problem: a rule the prompt states and the model breaks."""

    def _finalize(self, first, *retries):
        import agent
        calls = []

        def _create(**kw):
            calls.append(kw)
            text = retries[min(len(calls) - 1, len(retries) - 1)] if retries else first
            return MagicMock(content=[MagicMock(text=text)])

        with patch.object(agent.client.messages, "create", side_effect=_create):
            out, _ = agent._finalize(first, "sys", [{"role": "user", "content": "hi"}], None)
        return out, calls

    def test_a_clean_reply_costs_no_redraft(self):
        out, calls = self._finalize("90 today in Culver City, low 71.")
        assert out.startswith("90 today")
        assert calls == [], "a clean reply must not pay for a second call"

    def test_a_handoff_is_redrafted_once(self):
        out, calls = self._finalize(
            "I can't pull that - check Google Maps.",
            "I can't pull that right now. Want me to try again in a minute?")
        assert "Google" not in out
        assert len(calls) == 1, "exactly one redraft"

    def test_a_redraft_that_still_hands_off_is_not_preferred(self):
        """Take the better-formed of the two rather than a worse second try."""
        out, _ = self._finalize(
            "Tool's down - try Google Maps.", "honestly just google it")
        assert out == "Tool's down - try Google Maps."

    def test_a_failed_redraft_keeps_the_original(self):
        import agent
        with patch.object(agent.client.messages, "create", side_effect=RuntimeError("api down")):
            out, _ = agent._finalize("check Google Maps for that", "sys", [], None)
        assert out == "check Google Maps for that"

    def test_the_correction_names_what_palmer_can_do(self):
        """A redraft that only says "don't" invites another refusal."""
        assert "flights" in guards.REDIRECT_CORRECTION
        assert "Palmer is the product" in guards.REDIRECT_CORRECTION


class TestFailureStringsDoNotDisclaimCapability:
    """flights.py used to return "Flight search is unavailable right now", which
    the model paraphrased into "I can't do flights" and then a competitor."""

    def test_flight_failure_says_the_capability_exists(self):
        import flights
        with patch.object(flights, "SERP_API_KEY", ""):
            out = flights.search_flights("LAX", "MXP", "2026-09-18")
        assert "DO have flight search" in out
        assert not guards.redirects_elsewhere(out)

    def test_traffic_failure_asks_rather_than_redirects(self):
        import traffic
        out = traffic.get_travel_time("", "")
        assert "ask the user" in out.lower()
        assert not guards.redirects_elsewhere(out)

    def test_no_failure_string_names_a_competitor(self):
        import flights, hotels, serpapi, shopping, traffic
        # shopping gates on serpapi.API_KEY, not a local constant — patching the
        # wrong one lets the test out to the live network.
        with patch.object(flights, "SERP_API_KEY", ""), \
             patch.object(hotels, "SERP_API_KEY", ""), \
             patch.object(serpapi, "API_KEY", ""):
            outs = [flights.search_flights("LAX", "MXP", "2026-09-18"),
                    hotels.search_hotels("Lisbon", "2026-09-18", "2026-09-20"),
                    shopping.search_shopping("wool coat"),
                    traffic.get_travel_time("", "")]
        for o in outs:
            assert not guards.redirects_elsewhere(o), o

    def test_an_empty_result_is_not_a_broken_tool(self):
        """The commonest failure string in the system was "No results found."

        A bare dead end is what the drafting model turns into "I can't find
        news on that", and from there into a competitor. `_search` returns
        empty often and by design — the recency window and the source floor
        throw most of a page away — so this is the string that has to be right.
        """
        import datafeeds
        fake = MagicMock()
        fake.search.return_value = {"results": []}
        with patch.object(datafeeds, "_tavily", fake):
            empty_search = datafeeds._search("something nobody wrote about")
        outs = [empty_search, agent._tool_error("web_search", RuntimeError("boom"))]
        for o in outs:
            assert not guards.redirects_elsewhere(o), o
            assert "DO have" in o or "not a missing capability" in o, o

    def test_a_failed_price_lookup_does_not_teach_the_model_the_company_is_private(self):
        """The failed lookup used to confirm the model's own stale prior, and
        Palmer told a user SpaceX was private while SPCX was trading."""
        import datafeeds
        ticker = MagicMock()
        ticker.fast_info.last_price = None
        with patch("yfinance.Ticker", return_value=ticker):
            out = datafeeds._get_price("NOTATICKER")
        assert "private" in out and "not evidence" in out
        assert not guards.redirects_elsewhere(out)

    def test_a_city_palmer_cannot_place_is_asked_about_not_disclaimed(self):
        import traffic
        with patch.object(traffic, "_geocode_city", return_value=None):
            line, why = traffic.city_traffic("Nowheresville")
        assert line is None and why == "unknown_city"


class TestFlightWatches:
    def _fresh(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "_DB_PATH", tmp_path / "fw.db")
        db.init_db()

    def test_a_watch_saves_and_lists(self, tmp_path, monkeypatch):
        self._fresh(tmp_path, monkeypatch)
        assert db.save_flight_watch("+1555", "lax", "mxp", "2026-09-18", "2026-09-26", 800)
        w = db.get_user_flight_watches("+1555")[0]
        assert (w["origin"], w["destination"]) == ("LAX", "MXP"), "codes normalise to upper"

    def test_the_same_route_is_not_watched_twice(self, tmp_path, monkeypatch):
        self._fresh(tmp_path, monkeypatch)
        db.save_flight_watch("+1555", "LAX", "MXP", "2026-09-18")
        assert db.save_flight_watch("+1555", "lax", "mxp", "2026-09-18") is None

    def test_the_cap_holds(self, tmp_path, monkeypatch):
        """Each active watch costs ~30 SerpAPI searches a month against a 250
        plan, so the cap is a budget control, not tidiness."""
        self._fresh(tmp_path, monkeypatch)
        for i in range(db.FLIGHT_WATCH_MAX):
            assert db.save_flight_watch("+1555", "LAX", f"MX{i}", "2026-09-18")
        assert db.save_flight_watch("+1555", "LAX", "JFK", "2026-09-18") is None

    def test_cancelling_by_airport(self, tmp_path, monkeypatch):
        self._fresh(tmp_path, monkeypatch)
        db.save_flight_watch("+1555", "LAX", "MXP", "2026-09-18")
        db.save_flight_watch("+1555", "JFK", "LHR", "2026-10-01")
        assert db.cancel_flight_watches("+1555", "lax") == 1
        assert len(db.get_user_flight_watches("+1555")) == 1

    def test_an_alert_rebaselines(self, tmp_path, monkeypatch):
        """Otherwise the next check measures from a fare the user was never told."""
        self._fresh(tmp_path, monkeypatch)
        db.save_flight_watch("+1555", "LAX", "MXP", "2026-09-18")
        wid = db.get_user_flight_watches("+1555")[0]["id"]
        db.update_flight_watch_price(wid, 900.0, baseline=True)
        db.update_flight_watch_price(wid, 700.0, alerted=True)
        w = db.get_user_flight_watches("+1555")[0]
        assert w["baseline_price"] == 700.0 and w["last_alerted"]


class TestFlightAlertThresholds:
    import flightwatch as fw

    def test_first_sighting_is_a_baseline_not_news(self):
        import flightwatch
        assert flightwatch._should_alert({"baseline_price": None}, 800) is None

    def test_a_target_hit_fires(self):
        import flightwatch
        assert flightwatch._should_alert({"target_price": 800, "baseline_price": 900}, 780) == "target"

    def test_noise_below_the_bar_is_silent(self):
        """Fares wobble tens of dollars daily; the flat $2 product rule would
        page someone every morning."""
        import flightwatch
        assert flightwatch._should_alert({"baseline_price": 900}, 880) is None

    def test_a_real_move_fires_in_both_directions(self):
        import flightwatch
        assert flightwatch._should_alert({"baseline_price": 900}, 840) == "drop"
        assert flightwatch._should_alert({"baseline_price": 900}, 960) == "rise"

    def test_a_departed_flight_stops_costing_searches(self):
        import flightwatch
        assert flightwatch._expired({"outbound_date": "2020-01-01"})
        assert not flightwatch._expired({"outbound_date": "2099-01-01"})
        assert not flightwatch._expired({"outbound_date": None})

    def test_the_job_never_raises(self):
        import flightwatch
        with patch("db.get_active_flight_watches", side_effect=RuntimeError("db down")):
            flightwatch.run_flight_watches()

    def test_it_is_scheduled_on_cron(self):
        import inspect, main
        block = inspect.getsource(main).split("run_flight_watches,")[1][:120]
        assert '"cron"' in block and "misfire_grace_time" in block


class TestTopicOverlapIsRaisedNotEnforced:
    def test_an_overlap_is_reported_to_palmer_not_acted_on(self):
        """Semantic overlap has false positives — "NFL headlines" reads as a
        duplicate of "Philadelphia Eagles news" and is not — so silently
        dropping what someone asked for is the wrong failure."""
        import inspect, agent
        src = inspect.getsource(agent.get_reply)
        block = src.split('update_morning_briefing"')[1].split("elif b.name")[0]
        assert "topic_already_covered" in block
        assert "topics.append(item)" in block
        assert "Do not remove anything yourself" in block

    def test_the_page_will_not_render_one_article_twice(self):
        import inspect, home
        src = inspect.getsource(home._fetch_headlines)
        assert "seen_urls" in src
