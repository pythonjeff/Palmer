"""Guards, flights, and the scheduler's own configuration.

Merged from test_guards_and_flights.py, test_scheduler_config.py; each section keeps its
original file's notes, because those carry the incident that led to the test.
"""
import importlib
import pytest
from unittest.mock import patch, MagicMock
from palmer import db, agent, guards
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from apscheduler.triggers.cron import CronTrigger


# ============================================================================
# from test_guards_and_flights.py
# ============================================================================
#
# Three rules the prompt alone could not hold, and the capability it denied.
#
# SYSTEM_PROMPT has forbidden sending users to competing products since the
# beginning, in as many words. Palmer did it anyway in production — five times
# across two users, once while quoting the rule back at itself ("I'd point you to
# Google Flights but I know that's not helpful coming from me"). The corpus in
# TestTheRedirectGuard is those real messages.
#
# It also told two people it could not do flights while `search_flights` sat there
# working, because the user wanted a *watch* and there was no watch to offer. So
# the honest answer — do the half you can, name the half you can't — needed the
# other half to exist.
#
# All offline.

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
        from palmer import agent
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
        from palmer import agent
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
        from palmer import flights
        with patch.object(flights, "SERP_API_KEY", ""):
            out = flights.search_flights("LAX", "MXP", "2026-09-18")
        assert "DO have flight search" in out
        assert not guards.redirects_elsewhere(out)

    def test_traffic_failure_asks_rather_than_redirects(self):
        from palmer import traffic
        out = traffic.get_travel_time("", "")
        assert "ask the user" in out.lower()
        assert not guards.redirects_elsewhere(out)

    def test_no_failure_string_names_a_competitor(self):
        from palmer import flights
        from palmer import hotels
        from palmer import serpapi
        from palmer import shopping
        from palmer import traffic
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
        from palmer import datafeeds
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
        from palmer import datafeeds
        ticker = MagicMock()
        ticker.fast_info.last_price = None
        with patch("yfinance.Ticker", return_value=ticker):
            out = datafeeds._get_price("NOTATICKER")
        assert "private" in out and "not evidence" in out
        assert not guards.redirects_elsewhere(out)

    def test_a_city_palmer_cannot_place_is_asked_about_not_disclaimed(self):
        from palmer import traffic
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
    from palmer import flightwatch as fw

    def test_first_sighting_is_a_baseline_not_news(self):
        from palmer import flightwatch
        assert flightwatch._should_alert({"baseline_price": None}, 800) is None

    def test_a_target_hit_fires(self):
        from palmer import flightwatch
        assert flightwatch._should_alert({"target_price": 800, "baseline_price": 900}, 780) == "target"

    def test_noise_below_the_bar_is_silent(self):
        """Fares wobble tens of dollars daily; the flat $2 product rule would
        page someone every morning."""
        from palmer import flightwatch
        assert flightwatch._should_alert({"baseline_price": 900}, 880) is None

    def test_a_real_move_fires_in_both_directions(self):
        from palmer import flightwatch
        assert flightwatch._should_alert({"baseline_price": 900}, 840) == "drop"
        assert flightwatch._should_alert({"baseline_price": 900}, 960) == "rise"

    def test_a_departed_flight_stops_costing_searches(self):
        from palmer import flightwatch
        assert flightwatch._expired({"outbound_date": "2020-01-01"})
        assert not flightwatch._expired({"outbound_date": "2099-01-01"})
        assert not flightwatch._expired({"outbound_date": None})

    def test_the_job_never_raises(self):
        from palmer import flightwatch
        with patch("palmer.db.get_active_flight_watches", side_effect=RuntimeError("db down")):
            flightwatch.run_flight_watches()

    def test_it_is_scheduled_on_cron(self):
        import inspect
        from palmer import main
        block = inspect.getsource(main).split("run_flight_watches,")[1][:120]
        assert '"cron"' in block and "misfire_grace_time" in block


class TestTopicOverlapIsRaisedNotEnforced:
    def test_an_overlap_is_reported_to_palmer_not_acted_on(self):
        """Semantic overlap has false positives — "NFL headlines" reads as a
        duplicate of "Philadelphia Eagles news" and is not — so silently
        dropping what someone asked for is the wrong failure."""
        import inspect
        from palmer import agent
        src = inspect.getsource(agent.get_reply)
        block = src.split('update_morning_briefing"')[1].split("elif b.name")[0]
        assert "topic_already_covered" in block
        assert "topics.append(item)" in block
        assert "Do not remove anything yourself" in block

    def test_the_page_will_not_render_one_article_twice(self):
        import inspect
        from palmer import home
        src = inspect.getsource(home._fetch_headlines)
        assert "seen_urls" in src


# Categorical denials. Palmer has a tool for every one of these.
DENIALS = [
    "Not something I can pull a live number on - that's not in my toolbox.",
    "Flight prices are a bit outside what I can track directly.",
    "I don't have a live flight pricing feed.",
    "Flight search is one thing I can't pull directly.",
    "I can't pull hotel prices for you.",
    "Live scores aren't something I can do.",
    "I don't have access to real-time traffic.",
    "Watching a fare over time is outside my capabilities.",
    "I can't do weather, but I can help with other stuff.",
]

# A real gap, said the way a person says it. Palmer has no tool for any of these.
HONEST_GAPS = [
    "I can't send an email for you.",
    "I can't book it - you'll have to do that part.",
    "I can't see your calendar.",
    "I don't have a way to pay for it.",
]

# Somebody else's limits. Reporting these is Palmer's job.
THIRD_PARTY = [
    "The airline doesn't publish seat maps until 24 hours out.",
    "Ticketmaster hasn't posted prices for that show yet.",
    "Their site doesn't have a live feed for injuries.",
]

# The sanctioned failure line — what SYSTEM_PROMPT tells Palmer to say.
TRANSIENT = [
    "I can't pull that right now - want me to try again in a bit?",
    "Flight search didn't come back just now - want me to try again?",
    "Couldn't pull the drive time this second, trying again in a few.",
    "I can't get the weather at the moment. Try you again shortly.",
]


class TestTheCapabilityDenialGuard:
    """redirects_elsewhere only fires when the denial is ACCOMPANIED by a
    competitor. Strip the brand name from three of the four real violations
    and nothing was left to catch them."""

    def test_every_categorical_denial_is_caught(self):
        for text in DENIALS:
            assert guards.denies_capability(text), text

    def test_three_of_the_four_real_violations_are_denials_on_their_own(self):
        for text in REAL_VIOLATIONS[1:]:
            assert guards.denies_capability(text), text

    def test_a_tool_outage_belongs_to_the_redirect_guard_not_this_one(self):
        """"Tool's down on my end right now" is the sentence we asked for; the
        violation in it is the handoff that follows, which is already caught."""
        assert not guards.denies_capability(REAL_VIOLATIONS[0])
        assert guards.redirects_elsewhere(REAL_VIOLATIONS[0])

    def test_an_honest_gap_survives(self):
        """Palmer has no email tool. Saying so is honesty, not a false limit."""
        for text in HONEST_GAPS:
            assert not guards.denies_capability(text), text

    def test_a_third_partys_limits_survive(self):
        for text in THIRD_PARTY:
            assert not guards.denies_capability(text), text

    def test_a_transient_failure_survives(self):
        for text in TRANSIENT:
            assert not guards.denies_capability(text), text

    def test_nothing_legitimate_is_caught(self):
        for text in MUST_SURVIVE:
            assert not guards.denies_capability(text), text

    def test_the_whitelisted_line_is_excluded_twice_over(self):
        """Belt and braces, pinned: it carries a transient marker AND names no
        capability. A future edit to either half cannot silently start
        blocking Palmer's sanctioned failure sentence."""
        line = "I can't pull that right now - want me to try again in a bit?"
        assert line in MUST_SURVIVE
        assert guards._TRANSIENT.search(line)
        assert not guards._CAPABILITY.search(line)

    def test_the_inventory_tier_needs_no_object(self):
        """Nobody with a real gap says "email isn't in my toolbox"."""
        assert guards.denies_capability("that's not in my toolbox")
        assert guards.denies_capability("Fares aren't really in my wheelhouse.")

    def test_transience_is_judged_per_clause(self):
        """One real violation is damning twice over in two halves."""
        assert guards.denies_capability(
            "I can't pull the fares right now, and flight tracking isn't in my toolbox.")

    def test_a_url_cannot_trip_it(self):
        assert not guards.denies_capability(
            "https://example.com/i-dont-have-live-flight-data")

    def test_empty_input_is_safe(self):
        assert not guards.denies_capability("")
        assert not guards.denies_capability(None)

    def test_no_failure_string_in_the_codebase_trips_it(self):
        """The tool failure strings are what the model paraphrases. If one of
        them reads as a denial, the guard is policing a problem we wrote."""
        from palmer import flights
        from palmer import hotels
        from palmer import serpapi
        from palmer import shopping
        from palmer import traffic
        with patch.object(flights, "SERP_API_KEY", ""), \
             patch.object(hotels, "SERP_API_KEY", ""), \
             patch.object(serpapi, "API_KEY", ""):
            outs = [flights.search_flights("LAX", "MXP", "2026-09-18"),
                    hotels.search_hotels("Lisbon", "2026-09-18", "2026-09-20"),
                    shopping.search_shopping("wool coat"),
                    traffic.get_travel_time("", ""),
                    agent._tool_error("search_flights", RuntimeError("x"))]
        for o in outs:
            assert not guards.denies_capability(o), o


class TestTheDenialRedraft:
    def test_a_denial_is_redrafted_once(self):
        calls = []

        def _create(**kw):
            calls.append(kw)
            return MagicMock(content=[MagicMock(text="pulling those fares now - one sec.")])

        with patch.object(agent.client.messages, "create", side_effect=_create):
            out, _ = agent._finalize(
                "Flight search is one thing I can't pull directly.", "sys", [], None)
        assert len(calls) == 1
        assert out == "pulling those fares now - one sec."

    def test_the_correction_names_what_palmer_can_do(self):
        """A redraft that only says "don't" invites another refusal."""
        for word in ("flights", "hotels", "weather", "traffic", "reminders"):
            assert word in guards.DENIAL_CORRECTION

    def test_the_correction_separates_transient_from_categorical(self):
        assert "different sentence" in guards.DENIAL_CORRECTION

    def test_the_correction_leaves_room_for_a_true_limit(self):
        """The guard cannot tell "I can't track hotel prices" (true — there is
        no hotel watch) from "I can't do flights" (false). These trip it and
        must NOT be redrafted into a promise:"""
        for text in ("I can't track hotel prices - only flights and products.",
                     "I can't do weather that far out - forecasts only run ten days."):
            assert guards.denies_capability(text), text   # the false positive is real
        assert "genuine limit" in guards.DENIAL_CORRECTION
        assert "Never promise a watch" in guards.DENIAL_CORRECTION
        # And it is exact about which watches exist, so it cannot invite one.
        assert "product price watches" in guards.DENIAL_CORRECTION
        assert "hotel or stock price WATCH" in guards.DENIAL_CORRECTION

    def test_the_correction_does_not_trade_one_guard_for_the_other(self):
        assert "another product" in guards.DENIAL_CORRECTION

    def test_a_clean_reply_costs_nothing(self):
        calls = []
        with patch.object(agent.client.messages, "create",
                          side_effect=lambda **kw: calls.append(kw)):
            out, _ = agent._finalize("90 today in Culver City, low 71.", "sys", [], None)
        assert calls == []


# ============================================================================
# from test_scheduler_config.py
# ============================================================================
#
# Scheduler registration properties that are invisible at runtime.
#
# Every job here failed, or could fail, the same way: an APScheduler *interval*
# job schedules its first run at `start + interval`, and that clock restarts on
# every dyno boot — which means every deploy. A job whose period is long relative
# to the gap between deploys therefore runs on a cadence set by deploy history
# rather than by the clock, and since a tick that finds nothing to do logs
# nothing, it fails silently. run_price_watches at 12h only ran on days
# production was left alone; run_followups at 4h had 1.5 ticks of margin against
# a 6h delivery window before the reset was even considered.
#
# The property under test throughout is phase-independence: fire times must not
# move when the process starts at a different moment.

def _scheduler():
    from palmer import main
    return main._scheduler


def _trigger_for(func):
    jobs = [j for j in _scheduler().get_jobs() if j.func is func]
    assert len(jobs) == 1, f"expected exactly one job for {func.__name__}"
    return jobs[0].trigger


def _fire_times(trigger, start, count):
    out, fire = [], None
    for _ in range(count):
        fire = trigger.get_next_fire_time(fire, start if fire is None else fire + timedelta(seconds=1))
        out.append(fire)
    return out


# Jobs whose period is an hour or longer must be phase-stable. The short jobs
# (reminders 1m, morning 5m, watches 30m) stay on interval deliberately —
# losing up to 30 minutes to a deploy is immaterial there.
def _long_period_jobs():
    from palmer.followup import run_followups
    from palmer.morning import send_missing_data_asks
    from palmer.shopping import run_price_watches
    from palmer.flightwatch import run_flight_watches
    from palmer.wxaudit import run_forecast_audit
    return [run_followups, send_missing_data_asks, run_price_watches,
            run_flight_watches, run_forecast_audit]


@pytest.mark.parametrize("func", _long_period_jobs(), ids=lambda f: f.__name__)
def test_long_period_jobs_are_cron(func):
    assert isinstance(_trigger_for(func), CronTrigger)


@pytest.mark.parametrize("func", _long_period_jobs(), ids=lambda f: f.__name__)
def test_long_period_jobs_fire_on_round_clock_positions(func):
    """Phase-independence is structural once the trigger is cron — a CronTrigger
    holds no boot-derived state, where an IntervalTrigger's grid is anchored to
    the start_date it was given at add_job time. What is checkable here is the
    visible consequence: fire times sit on round clock positions rather than on
    whatever minute the dyno happened to boot at."""
    trigger = _trigger_for(func)
    for fire in _fire_times(trigger, datetime(2026, 8, 25, 3, 17, tzinfo=timezone.utc), 6):
        utc = fire.astimezone(timezone.utc)
        assert utc.second == 0 and utc.microsecond == 0
        assert utc.minute in (0, 30), f"{func.__name__} fires at :{utc.minute:02d}"


@pytest.mark.parametrize("func", _long_period_jobs(), ids=lambda f: f.__name__)
def test_long_period_jobs_pin_their_timezone(func):
    """A bare BackgroundScheduler() inherits the PROCESS timezone. That is
    Etc/UTC on the dyno and the developer's own zone locally, so an unpinned
    cron grid means local runs disagree with production while both look right —
    and a TZ config var would rotate the live schedule with nothing to show for
    it. Caught exactly this: three jobs registered as America/Chicago on a
    laptop and Etc/UTC in prod."""
    assert str(_trigger_for(func).timezone) in ("UTC", "Etc/UTC")


@pytest.mark.parametrize("func", _long_period_jobs(), ids=lambda f: f.__name__)
def test_long_period_jobs_survive_a_delayed_tick(func):
    """APScheduler's default misfire_grace_time is 1 second, which drops a tick
    delayed behind a slow job instead of running it late."""
    jobs = [j for j in _scheduler().get_jobs() if j.func is func]
    assert jobs[0].misfire_grace_time and jobs[0].misfire_grace_time >= 600


class TestFollowupGrid:
    """run_followups gates on a 13:00-19:00 window in the USER's timezone, so the
    grid has to land inside that window for every timezone served — not just for
    whichever one the UTC hours were picked against."""

    SERVED = ("America/Chicago", "America/Los_Angeles")

    def _ticks_in_window(self, tz_name):
        trigger = _trigger_for(importlib.import_module("palmer.followup").run_followups)
        start = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
        fires = _fire_times(trigger, start, 24)  # a full day at 2h spacing
        return [f for f in fires
                if 13 <= f.astimezone(ZoneInfo(tz_name)).hour < 19
                and f < start + timedelta(days=1)]

    @pytest.mark.parametrize("tz_name", SERVED)
    def test_window_gets_multiple_chances(self, tz_name):
        # The old 4h interval gave 1.5 ticks of margin against this 6h window.
        assert len(self._ticks_in_window(tz_name)) >= 3

    def test_grid_is_timezone_agnostic(self):
        # Both zones get the same coverage — the property that keeps working as
        # users are added in zones nobody picked hours for.
        counts = {tz: len(self._ticks_in_window(tz)) for tz in self.SERVED}
        assert len(set(counts.values())) == 1, counts


class TestNoUnpromptedSenderComesBack:
    """A daily "a friend would text this" news alert used to text people on
    Palmer's own initiative. It is gone; run_followups is the one paced
    check-in, and run_score_alerts only texts teams a user set a live level
    on. This pins that nothing quietly comes back."""

    def test_the_retired_modules_are_gone(self):
        import importlib
        for name in ("palmer.alerts",):
            try:
                importlib.import_module(name)
            except ModuleNotFoundError:
                continue
            raise AssertionError(f"{name} is back")

    def test_the_job_list_is_exactly_this(self):
        allowed = {"send_due_reminders", "send_morning_messages", "run_watches",
                   "send_missing_data_asks", "run_followups", "run_price_watches",
                   "run_forecast_audit", "run_flight_watches", "run_score_alerts"}
        names = {j.func.__name__ for j in _scheduler().get_jobs()}
        assert names == allowed, names


class TestShortJobsStayOnInterval:
    """Not everything should be cron. These are frequent enough that a deploy
    reset costs less than the added rigidity is worth."""

    def test_short_jobs_are_interval(self):
        from apscheduler.triggers.interval import IntervalTrigger
        from palmer.reminders import send_due_reminders
        from palmer.morning import send_morning_messages
        from palmer.watches import run_watches
        for func in (send_due_reminders, send_morning_messages, run_watches):
            assert isinstance(_trigger_for(func), IntervalTrigger), func.__name__
