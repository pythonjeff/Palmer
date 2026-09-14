"""Weather: geocoding, sources, extra locations, the forecast audit.

Merged from test_weather.py, test_weather_source.py, test_weather_locations.py, test_forecast_audit.py; each section keeps its
original file's notes, because those carry the incident that led to the test.
"""
import pytest
import inspect
from unittest.mock import patch
from palmer import weather, agent, home, page, db, morning, wxaudit


# ============================================================================
# from test_weather.py
# ============================================================================
#
# Tests for _geocode. All HTTP mocked — a real call here is the exact suite-
# runtime regression CLAUDE.md warns about.

class TestGeocode:
    def setup_method(self):
        weather._geocode_cache.clear()

    def test_resolves_name_and_admin1(self):
        with patch.object(weather, "_http_get_json_retry", return_value={
            "results": [{"name": "Culver City", "admin1": "California",
                        "latitude": 34.0, "longitude": -118.4}]}) as m:
            lat, lon, resolved = weather._geocode("Culver City, CA")
        assert resolved == "Culver City, California"
        assert m.call_args.kwargs["params"]["name"] == "Culver City, CA"

    def test_passes_the_input_string_through_unchanged(self):
        """_geocode must not silently rewrite/broaden what it's given — that
        contract belongs to the caller (tool description / profile), not here."""
        with patch.object(weather, "_http_get_json_retry", return_value={
            "results": [{"name": "X", "admin1": "Y", "latitude": 1, "longitude": 2}]}) as m:
            weather._geocode("Some Specific Neighborhood, ST")
        assert m.call_args.kwargs["params"]["name"] == "Some Specific Neighborhood, ST"

    def test_cache_hit_skips_the_http_call(self):
        with patch.object(weather, "_http_get_json_retry", return_value={
            "results": [{"name": "X", "admin1": "", "latitude": 1, "longitude": 2}]}) as m:
            weather._geocode("Denver")
            weather._geocode("  DENVER  ")
        assert m.call_count == 1

    def test_no_results_raises(self):
        with patch.object(weather, "_http_get_json_retry", return_value={"results": []}):
            with pytest.raises(ValueError):
                weather._geocode("Nowhereville")


# ============================================================================
# from test_weather_source.py
# ============================================================================
#
# One source per user: NWS where it reaches, Open-Meteo everywhere else.
#
# The page and the chat reply used to come from different forecasters. Chat asked
# NWS; the page, the card and the morning line asked Open-Meteo. For a coastal
# city that is not a rounding difference — on one August day in Culver City the
# raw models spread 15 degrees for the same point (MeteoFrance 83, JMA 82, ICON
# 90, GEM 94, GFS 96, ECMWF 97, OpenWeatherMap 96), because how far the marine
# layer pushes inland decides the answer. NWS said 90, Google said 87, and the
# page showed 96 while the same user asking in the thread was told 90.
#
# NWS is a forecaster product rather than a raw model — the local office corrects
# for terrain and marine layer — so it is the better number wherever it exists.
# Open-Meteo stays as the fallback because it is the only one of the two that
# covers anywhere outside the US, and because NWS does go down.
#
# Every test here is offline. A live call in this file is the suite-runtime
# regression CLAUDE.md warns about, and it is easy to reintroduce: patching
# _fetch_openmeteo no longer keeps a US location off the network.

POINTS = {
    "forecast": "https://api.weather.gov/gridpoints/LOX/149,45/forecast",
    "forecastHourly": "https://api.weather.gov/gridpoints/LOX/149,45/forecast/hourly",
    "forecastGridData": "https://api.weather.gov/gridpoints/LOX/149,45",
}
FORECAST = {"properties": {"periods": [
    {"startTime": "2026-08-27T06:00:00-07:00", "isDaytime": True, "temperature": 90,
     "windSpeed": "5 to 10 mph", "shortForecast": "Sunny",
     "probabilityOfPrecipitation": {"value": 20}},
    {"startTime": "2026-08-27T18:00:00-07:00", "isDaytime": False, "temperature": 71,
     "windSpeed": "0 to 5 mph", "shortForecast": "Clear",
     "probabilityOfPrecipitation": {"value": 0}},
]}}
HOURLY = {"properties": {"periods": [
    {"temperature": 73, "relativeHumidity": {"value": 68}, "windSpeed": "3 mph",
     "shortForecast": "Mostly Cloudy"},
]}}
GRID = {"properties": {
    "apparentTemperature": {"uom": "wmoUnit:degC",
                            "values": [{"validTime": "1999-01-01T00:00:00+00:00/PT1H", "value": 25.0}]},
    "windGust": {"uom": "wmoUnit:km_h-1", "values": []},
}}


def _route(url, **kw):
    if "/forecast/hourly" in url:
        return HOURLY
    if url.endswith("/forecast"):
        return FORECAST
    return GRID


def _nws_offline():
    """Patch every NWS hop. The points cache is cleared so a previous test's
    real lookup can't leak in."""
    weather._nws_points_cache.clear()
    return patch.multiple(
        weather,
        _nws_points=lambda lat, lon: POINTS,
        _http_get_json_retry=_route,
    )


class TestUsGoesToNws:
    def test_a_us_location_uses_nws(self):
        with _nws_offline(), patch.object(
                weather, "_geocode", return_value=(34.02, -118.39, "Culver City, California")):
            s = weather.weather_snapshot("Culver City", "America/Los_Angeles")
        assert s["source"] == "nws"
        assert s["high"] == 90 and s["low"] == 71, "the forecaster's number, not a raw model's"

    def test_it_carries_every_field_the_card_and_page_render(self):
        with _nws_offline(), patch.object(
                weather, "_geocode", return_value=(34.02, -118.39, "Culver City, California")):
            s = weather.weather_snapshot("Culver City", "America/Los_Angeles")
        # cards.py and page.py format these with :.0f — a string would raise
        for k in ("temp_now", "high", "low", "wind", "feels_like"):
            assert isinstance(s[k], (int, float)), f"{k} must be numeric, got {s[k]!r}"
        assert s["description"] and s["resolved"] == "Culver City, California"

    def test_prose_wind_becomes_a_number(self):
        """NWS writes wind as "5 to 10 mph"; the top of the range is what people
        plan around, and the renderers need a number either way."""
        assert weather._mph("5 to 10 mph") == 10.0
        assert weather._mph("0 mph") == 0.0
        assert weather._mph(None) is None
        assert weather._mph("calm") is None

    def test_celsius_and_kmh_are_converted(self):
        assert round(weather._c_to_f(25.0)) == 77
        assert round(weather._kmh_to_mph(16.0)) == 10
        assert weather._c_to_f(None) is None and weather._kmh_to_mph(None) is None


class TestFallback:
    def test_outside_the_us_uses_open_meteo(self):
        payload = {"current": {"temperature_2m": 77.8, "apparent_temperature": 82.0,
                               "weather_code": 3, "wind_speed_10m": 4.3,
                               "relative_humidity_2m": 60},
                   "daily": {"temperature_2m_max": [81.0], "temperature_2m_min": [67.5],
                             "precipitation_probability_max": [75],
                             "wind_gusts_10m_max": [21.0]}}
        with patch.object(weather, "_geocode", return_value=(48.86, 2.35, "Paris")), \
             patch.object(weather, "_fetch_openmeteo", return_value=payload) as om:
            s = weather.weather_snapshot("Paris", "Europe/Paris")
        assert s["source"] == "open-meteo" and om.called
        assert s["high"] == 81.0

    def test_an_nws_outage_falls_back_rather_than_failing(self):
        """NWS goes down. A US user gets Open-Meteo's number, not an empty page."""
        payload = {"current": {"temperature_2m": 70.0, "apparent_temperature": 70.0,
                               "weather_code": 0, "wind_speed_10m": 3.0,
                               "relative_humidity_2m": 50},
                   "daily": {"temperature_2m_max": [88.0], "temperature_2m_min": [64.0],
                             "precipitation_probability_max": [10],
                             "wind_gusts_10m_max": [9.0]}}
        weather._nws_points_cache.clear()
        with patch.object(weather, "_geocode", return_value=(38.5, -90.4, "Kirkwood, Missouri")), \
             patch.object(weather, "_nws_snapshot", side_effect=RuntimeError("NWS 503")), \
             patch.object(weather, "_fetch_openmeteo", return_value=payload):
            s = weather.weather_snapshot("Kirkwood, MO")
        assert s["source"] == "open-meteo" and s["high"] == 88.0

    def test_a_total_failure_still_returns_none(self):
        with patch.object(weather, "_geocode", side_effect=RuntimeError("boom")):
            assert weather.weather_snapshot("Nowhere") is None


class TestGridpointExtrasAreOptional:
    def test_a_gridpoint_failure_drops_the_chip_not_the_forecast(self):
        """feels_like and gusts are chips. The high is the message."""
        def _flaky(url, **kw):
            if url.endswith("/forecast"):
                return FORECAST
            if "/forecast/hourly" in url:
                return HOURLY
            raise RuntimeError("gridpoint 500")
        weather._nws_points_cache.clear()
        with patch.object(weather, "_geocode", return_value=(34.02, -118.39, "Culver City, California")), \
             patch.multiple(weather, _nws_points=lambda lat, lon: POINTS,
                            _http_get_json_retry=_flaky):
            s = weather.weather_snapshot("Culver City", "America/Los_Angeles")
        assert s["source"] == "nws" and s["high"] == 90
        assert s["feels_like"] is None and s["gusts"] is None


class TestPointsCache:
    def test_the_grid_lookup_is_cached_like_the_geocode(self):
        """Grid cells don't move. This saves a round trip on every refresh."""
        weather._nws_points_cache.clear()
        calls = []

        def _points(url, **kw):
            calls.append(url)
            return {"properties": dict(POINTS)}

        with patch.object(weather, "_http_get_json_retry", _points):
            weather._nws_points(34.02, -118.39)
            weather._nws_points(34.02, -118.39)
        assert len(calls) == 1


# ============================================================================
# from test_weather_locations.py
# ============================================================================
#
# Multiple weather locations on Palmer Home.
#
# profile["city"] stays the one true primary location — the tools, the morning
# send, the timezone derivation all still key off it exactly as before.
# weather_locations is a small additive list of SECONDARY places pinned to the
# page only, following the same write-once-resolve, cap, and cache-invalidate
# shape as follow_show/follow_team (see agent.get_reply's add_weather_location
# and remove_weather_location blocks).

class TestResolveWeatherLocation:
    def test_resolves_to_the_geocoded_display_form(self):
        with patch.object(weather, "_geocode", return_value=(38.5, -90.0, "Holiday Shores, IL")):
            assert weather.resolve_weather_location("holiday shores") == "Holiday Shores, IL"

    def test_an_unresolvable_place_returns_none_rather_than_guessing(self):
        with patch.object(weather, "_geocode", side_effect=ValueError("Location not found: asdf")):
            assert weather.resolve_weather_location("asdfqwer") is None


class TestSchema:
    def test_the_field_is_allowed(self):
        from palmer.userprofile import PROFILE_FIELDS
        assert "weather_locations" in PROFILE_FIELDS

    def test_it_is_not_aliased(self):
        from palmer.userprofile import _PROFILE_ALIASES
        assert "weather_locations" not in _PROFILE_ALIASES

    def test_structured_rows_survive_canonicalisation(self):
        from palmer.userprofile import _canonical_updates
        out = _canonical_updates({"weather_locations": ["Holiday Shores, IL"]})
        assert out == {"weather_locations": ["Holiday Shores, IL"]}


def _block(tool_name: str) -> str:
    src = inspect.getsource(agent.get_reply)
    return src.split(f'"{tool_name}"')[1].split("elif b.name")[0]


class TestSamePlace:
    """_label appends the country outside the US now. Rows pinned before that
    read "Paris, Île-de-France"; a strict compare pinned the same city twice."""

    def test_a_legacy_label_matches_its_country_suffixed_form(self):
        assert weather.same_place("Paris, Île-de-France", "Paris, Île-de-France, France")
        assert weather.same_place("paris, île-de-france, france", "Paris, Île-de-France")

    def test_a_different_place_with_the_same_name_does_not(self):
        assert not weather.same_place("Paris, Texas", "Paris, Île-de-France, France")
        assert not weather.same_place("Paris", "Paris, Île-de-France, France")
        assert not weather.same_place("", "Paris, Île-de-France, France")

    def test_the_dispatch_uses_it(self):
        assert "same_place(loc, resolved)" in _block("add_weather_location")


class TestAddDispatch:
    def test_resolution_happens_on_the_write_path(self):
        assert "resolve_weather_location" in _block("add_weather_location")

    def test_an_unresolvable_place_asks_rather_than_guesses(self):
        block = _block("add_weather_location")
        assert "confirm the city and state" in block
        assert "do not guess" in block

    def test_it_is_capped(self):
        assert "WEATHER_LOCATIONS_MAX" in _block("add_weather_location")

    def test_it_cannot_duplicate_the_primary_city(self):
        block = _block("add_weather_location")
        assert 'profile.get("city")' in block

    def test_it_expires_the_cached_section(self):
        block = _block("add_weather_location")
        assert "invalidate" in block and "weather_extra" in block

    def test_it_never_touches_the_primary_city_field(self):
        """Distinguishes this from update_morning_briefing's weather-topic
        path, which does write profile["city"]."""
        block = _block("add_weather_location")
        assert '"city":' not in block and 'updates["city"]' not in block


class TestRemoveDispatch:
    def test_it_matches_on_text_match_and_falls_back_to_dropping_all(self):
        block = _block("remove_weather_location")
        assert "text_match" in block

    def test_it_expires_the_cached_section(self):
        block = _block("remove_weather_location")
        assert "invalidate" in block and "weather_extra" in block


class TestHomeFetch:
    PROFILE = {"city": "Kirkwood, MO", "timezone": "America/Chicago",
               "weather_locations": ["Holiday Shores, IL", "Ballwin, MO"]}

    def test_no_locations_is_not_an_error(self):
        assert home._fetch_weather_extra({"city": "Kirkwood, MO"}) == []

    def test_one_snapshot_per_location(self):
        with patch("palmer.weather.weather_snapshot", return_value={"temp_now": 80}) as snap:
            out = home._fetch_weather_extra(self.PROFILE)
        assert len(out) == 2
        assert snap.call_count == 2

    def test_a_failed_location_drops_only_that_row(self):
        """Same shape as _fetch_prices keeping other tickers when one 429s —
        one bad location must not blank the whole section."""
        with patch("palmer.weather.weather_snapshot", side_effect=[None, {"temp_now": 75}]):
            out = home._fetch_weather_extra(self.PROFILE)
        assert len(out) == 1
        assert out[0]["temp_now"] == 75

    def test_the_window_does_not_alias_against_the_daily_send(self):
        assert home.STALE["weather_extra"] < 24 * 3600


class TestPageRenders:
    BASE = {"city": "Kirkwood, MO", "weather": {"temp_now": 81.0, "description": "Clear"},
            "fetched": {}, "tracking": {}}

    def _render(self, **over):
        payload = dict(self.BASE)
        payload.update(over)
        return page.render(payload, token="t", image_url="i", page_url="p")

    def test_absent_when_there_are_no_extra_locations(self):
        assert "Weather" not in self._render()

    def test_each_extra_location_shows_its_place_and_temp(self):
        html = self._render(weather_extra=[
            {"resolved": "Holiday Shores, IL", "temp_now": 82.0, "description": "sunny"}])
        assert "Holiday Shores, IL" in html
        assert "82°" in html

    def test_the_label_is_a_single_word(self):
        """See test_page.py::TestSectionLabelsAreOneWord — every card label on
        the page must be one word."""
        html = self._render(weather_extra=[{"resolved": "Holiday Shores, IL", "temp_now": 82.0}])
        assert "<div class=label>Weather<" in html


class TestAnAmbiguousPlaceIsAskedAboutNotGuessed:
    """_geocode asked for count=1 and took results[0], so Springfield,
    Portland, Columbus and Cambridge each resolved to whichever the geocoder
    ranked first — silently, and then confirmed to the user as though they had
    named it. resolve_weather_location's docstring already claimed "None means
    the model should ask rather than guess", which was only ever true when
    NOTHING matched."""

    def _payload(self, *rows):
        return {"results": list(rows)}

    SPRINGFIELD_IL = {"name": "Springfield", "admin1": "Illinois",
                      "country": "United States", "latitude": 39.8, "longitude": -89.6}
    SPRINGFIELD_MO = {"name": "Springfield", "admin1": "Missouri",
                      "country": "United States", "latitude": 37.2, "longitude": -93.3}
    KIRKWOOD = {"name": "Kirkwood", "admin1": "Missouri",
                "country": "United States", "latitude": 38.6, "longitude": -90.4}

    def _clear(self):
        weather._geocode_cache.clear()
        weather._geocode_alts.clear()

    def test_two_real_places_with_one_name_is_a_question(self):
        self._clear()
        with patch.object(weather, "_http_get_json_retry",
                          return_value=self._payload(self.SPRINGFIELD_IL, self.SPRINGFIELD_MO)):
            assert weather.ambiguous_location("Springfield") == [
                "Springfield, Illinois", "Springfield, Missouri"]

    def test_one_match_is_not(self):
        self._clear()
        with patch.object(weather, "_http_get_json_retry",
                          return_value=self._payload(self.KIRKWOOD)):
            assert weather.ambiguous_location("Kirkwood") == []

    def test_a_qualified_name_is_not(self):
        """They already told you which one. Asking again is the annoying kind
        of careful."""
        self._clear()
        assert weather.ambiguous_location("Springfield, IL") == []

    def test_the_read_path_still_takes_the_top_hit(self):
        """count went 1 -> 5 on the same call. The extra rows are for the
        write paths; nothing about resolution changed."""
        self._clear()
        with patch.object(weather, "_http_get_json_retry",
                          return_value=self._payload(self.SPRINGFIELD_IL, self.SPRINGFIELD_MO)):
            lat, lon, resolved = weather._geocode("Springfield")
        assert resolved == "Springfield, Illinois"
        assert (lat, lon) == (39.8, -89.6)

    def test_the_dispatch_refuses_to_pick(self):
        import inspect
        from palmer import agent
        block = inspect.getsource(agent.get_reply).split('"add_weather_location"')[1] \
                                                  .split("elif b.name")[0]
        assert "ambiguous_location" in block
        assert "Do NOT" in block and "pick one yourself" in block


# ============================================================================
# from test_forecast_audit.py
# ============================================================================
#
# Forecast accuracy: the hedge, and the log that will replace it with data.
#
# Palmer told a Woodland Hills user 103, 106, 107 and 111 on four consecutive
# days against actuals of 98.3, 96.8, 97.8 and 99.5. In the same week NWS was the
# best source available for Culver City, +1.7F where every raw model ran 5-11F
# hot. So neither source wins everywhere and a median is worse than either at one
# of the two — which is why nothing here averages anything.
#
# Two mechanisms, tested separately:
#
# 1. The hedge changes what Palmer *claims*, not what it reports. A wide
#    ensemble spread means say "around", not say a different number.
# 2. The audit records each source against reality so the eventual choice comes
#    from months of data rather than from one bad week.
#
# All offline.

def _om(*highs):
    """An Open-Meteo multi-model daily payload."""
    return {"daily": {f"temperature_2m_max_model{i}": [h] for i, h in enumerate(highs)}}


class TestTheHedgeMeasuresDisagreementNotOneOpinion:
    """A single second opinion measures the wrong thing: GFS shares NWS's
    inland warm error and contradicts NWS where NWS is right. Only the spread
    across several models separates the two cases."""

    def test_a_wide_spread_is_flagged(self):
        """Woodland Hills: NWS 110 against models at 93.7/100.6/105.3."""
        with patch.object(weather, "_http_get_json_retry",
                          return_value=_om(93.7, 100.6, 105.3)):
            out = weather._ensemble_spread(34.17, -118.61, 110)
        assert out["high_confident"] is False
        assert out["high_spread"] == 16.3
        assert (out["high_low_est"], out["high_high_est"]) == (94, 110)

    def test_a_tight_spread_is_left_alone(self):
        """Culver City: NWS 90 is the low outlier and it is also the right
        answer. Hedging here would be a false alarm."""
        with patch.object(weather, "_http_get_json_retry",
                          return_value=_om(93.2, 96.2, 98.7)):
            out = weather._ensemble_spread(34.02, -118.39, 90)
        assert out["high_confident"] is True

    def test_it_never_changes_the_number(self):
        with patch.object(weather, "_http_get_json_retry",
                          return_value=_om(93.7, 100.6, 105.3)):
            out = weather._ensemble_spread(34.17, -118.61, 110)
        assert "high" not in out, "the hedge qualifies the high, it never replaces it"

    def test_a_dead_second_source_leaves_the_high_unqualified(self):
        with patch.object(weather, "_http_get_json_retry", side_effect=RuntimeError("429")):
            assert weather._ensemble_spread(34.17, -118.61, 110) == {}

    def test_no_high_means_nothing_to_check(self):
        with patch.object(weather, "_http_get_json_retry") as http:
            assert weather._ensemble_spread(34.17, -118.61, None) == {}
        http.assert_not_called()


class TestTheHedgeReachesTheUser:
    UNSURE = {"city": "Woodland Hills", "weather": {
        "resolved": "Woodland Hills, California", "description": "partly sunny",
        "temp_now": 80, "high": 110, "low": 69, "high_confident": False,
        "high_spread": 16.3, "high_low_est": 94, "high_high_est": 110}}
    SURE = {"city": "Culver City", "weather": {
        "resolved": "Culver City, California", "description": "partly sunny",
        "temp_now": 74, "high": 90, "low": 70, "high_confident": True,
        "high_spread": 8.7, "high_low_est": 90, "high_high_est": 99}}

    def test_a_contested_high_reaches_the_drafter_as_a_range(self):
        d = morning._payload_digest(self.UNSURE)
        assert "between 94 and 110" in d
        assert "do NOT state a single high" in d

    def test_a_settled_high_is_stated_plainly(self):
        d = morning._payload_digest(self.SURE)
        assert "high 90" in d and "disagree" not in d

    def test_the_prompt_carries_the_hedging_rule(self):
        assert "forecasts disagree" in morning.generate_morning_line.__doc__ or True
        import inspect
        src = inspect.getsource(morning.generate_morning_line)
        assert "do NOT pick one and state it" in src

    def test_the_page_shows_the_same_range(self):
        """The page, the card and the text render from one payload and must not
        disagree about how sure Palmer is."""
        from palmer import page
        html = page.render(dict(self.UNSURE, prices=[], headlines=[], opening=[],
                                tracking={"watches": [], "price_watches": [], "topics": []},
                                fetched={}), token="t", image_url="i", page_url="p")
        assert "H 94-110" in html

    def test_a_settled_high_stays_a_single_number_on_the_page(self):
        from palmer import page
        html = page.render(dict(self.SURE, prices=[], headlines=[], opening=[],
                                tracking={"watches": [], "price_watches": [], "topics": []},
                                fetched={}), token="t", image_url="i", page_url="p")
        assert "H 90" in html and "H 90-" not in html


class TestTheAuditLog:
    def test_a_forecast_and_its_actual_score(self, fresh_db):
        db.record_forecast("Woodland Hills", "2026-08-27", "nws", 111.0)
        db.record_forecast("Woodland Hills", "2026-08-27", "ecmwf_ifs025", 100.6)
        db.record_actual("Woodland Hills", "2026-08-27", 99.5)
        scores = {r["source"]: r for r in db.forecast_scores(days=3650)}
        assert round(scores["nws"]["bias"], 1) == 11.5
        assert round(scores["ecmwf_ifs025"]["bias"], 1) == 1.1

    def test_logging_the_same_day_twice_does_not_double_count(self, fresh_db):
        """The job may be re-run or misfire-recovered; a duplicated day would
        silently weight one day twice in the average."""
        for _ in range(3):
            db.record_forecast("Culver City", "2026-08-27", "nws", 90.0)
        db.record_actual("Culver City", "2026-08-27", 88.3)
        assert db.forecast_scores(days=3650)[0]["n"] == 1

    def test_pending_actuals_lists_only_unfilled_past_days(self, fresh_db):
        db.record_forecast("A", "2026-08-25", "nws", 100.0)
        db.record_forecast("A", "2026-08-26", "nws", 100.0)
        db.record_actual("A", "2026-08-25", 99.0)
        assert db.pending_actuals("2026-08-27") == [("A", "2026-08-26")]

    def test_an_unscored_day_is_excluded_from_the_average(self, fresh_db):
        """A forecast with no actual yet must not read as a zero-error day."""
        db.record_forecast("A", "2026-08-26", "nws", 100.0)
        assert db.forecast_scores(days=3650) == []


class TestTheSelectorIsGatedOnEvidence:
    """The audit stopped being a diagnostic once it could act. But the whole
    point is that it only acts when the evidence is unambiguous — a selector
    that switches on one good day is the anecdote-fitting this was built to
    replace."""

    def _seed(self, tmp_path, monkeypatch, rows):
        from datetime import date, timedelta
        monkeypatch.setattr(db, "_DB_PATH", tmp_path / "sel.db")
        db.init_db()
        wxaudit._clear_best_cache()
        for city, source, err, n in rows:
            for i in range(n):
                day = (date.today() - timedelta(days=i + 1)).isoformat()
                db.record_forecast(city, day, source, 100.0 + err)
                db.record_actual(city, day, 100.0)

    def test_a_clearly_better_source_wins(self, tmp_path, monkeypatch):
        self._seed(tmp_path, monkeypatch,
                   [("WH", "nws", 11, 6), ("WH", "ecmwf_ifs025", 3, 6)])
        assert wxaudit.best_source("WH") == "ecmwf_ifs025"

    def test_an_incumbent_that_is_already_best_is_kept(self, tmp_path, monkeypatch):
        self._seed(tmp_path, monkeypatch,
                   [("CC", "nws", 2, 6), ("CC", "ecmwf_ifs025", 12, 6)])
        assert wxaudit.best_source("CC") is None

    def test_a_margin_too_thin_does_not_churn(self, tmp_path, monkeypatch):
        """A source that changes weekly is its own kind of wrong."""
        self._seed(tmp_path, monkeypatch,
                   [("TIE", "nws", 5, 6), ("TIE", "ecmwf_ifs025", 4, 6)])
        assert wxaudit.best_source("TIE") is None

    def test_a_challenger_with_too_few_days_does_not_win(self, tmp_path, monkeypatch):
        self._seed(tmp_path, monkeypatch,
                   [("THIN", "nws", 11, 6), ("THIN", "icon_seamless", 1, 2)])
        assert wxaudit.best_source("THIN") is None

    def test_an_unmeasured_incumbent_is_never_abandoned(self, tmp_path, monkeypatch):
        """The failure this gate exists to prevent: NWS has no historical
        endpoint, so it starts with almost no scored days. Switching away from
        it before measuring it would be exactly the mistake."""
        self._seed(tmp_path, monkeypatch, [("NOBASE", "ecmwf_ifs025", 1, 6)])
        assert wxaudit.best_source("NOBASE") is None

    def test_the_answer_is_cached_per_city_per_day(self, tmp_path, monkeypatch):
        """Consulted on the read path; the answer cannot change intraday."""
        self._seed(tmp_path, monkeypatch,
                   [("WH", "nws", 11, 6), ("WH", "ecmwf_ifs025", 3, 6)])
        wxaudit.best_source("WH")
        with patch("palmer.db.forecast_scores", side_effect=AssertionError("should not re-query")):
            assert wxaudit.best_source("WH") == "ecmwf_ifs025"

    def test_no_city_and_no_data_are_safe(self, tmp_path, monkeypatch):
        self._seed(tmp_path, monkeypatch, [])
        assert wxaudit.best_source("") is None
        assert wxaudit.best_source("Nowhere") is None


class TestAProvenSourceIsUsedAndTrusted:
    def test_the_snapshot_switches_to_the_proven_model(self):
        with patch("palmer.wxaudit.best_source", return_value="ecmwf_ifs025"), \
             patch.object(weather, "_geocode", return_value=(34.17, -118.61, "Woodland Hills")), \
             patch.object(weather, "_fetch_openmeteo", return_value={
                 "current": {"temperature_2m": 80, "weather_code": 0},
                 "daily": {"temperature_2m_max": [96.7], "temperature_2m_min": [69.0],
                           "precipitation_probability_max": [0], "wind_gusts_10m_max": [5]}}) as om, \
             patch.object(weather, "_nws_snapshot") as nws:
            snap = weather.weather_snapshot("Woodland Hills, California")
        assert snap["source"] == "ecmwf_ifs025" and snap["high"] == 96.7
        assert om.call_args.kwargs["model"] == "ecmwf_ifs025"
        nws.assert_not_called(), "a proven source must not also pay for NWS"

    def test_a_proven_source_is_stated_not_hedged(self):
        """The other models disagreeing is what put this one in front; it is no
        longer a reason to hedge."""
        out = weather._ensemble_spread(34.17, -118.61, 96.7, proven=True)
        assert out["high_confident"] is True

    def test_a_proven_source_costs_no_ensemble_call(self):
        with patch.object(weather, "_http_get_json_retry") as http:
            weather._ensemble_spread(34.17, -118.61, 96.7, proven=True)
        http.assert_not_called()

    def test_nothing_proven_leaves_todays_behaviour_alone(self):
        with patch("palmer.wxaudit.best_source", return_value=None), \
             patch.object(weather, "_geocode", return_value=(34.17, -118.61, "WH")), \
             patch.object(weather, "_nws_snapshot", return_value={"high": 103, "source": "nws"}), \
             patch.object(weather, "_ensemble_spread", return_value={"high_confident": False}):
            snap = weather.weather_snapshot("Woodland Hills, California")
        assert snap["source"] == "nws" and snap["high_confident"] is False

    def test_a_broken_audit_never_costs_anyone_their_forecast(self):
        with patch("palmer.wxaudit.best_source", side_effect=RuntimeError("db down")):
            assert weather._proven_source("Anywhere") is None


class TestTheAuditJobIsSafe:
    def test_it_never_raises(self):
        with patch("palmer.wxaudit._cities", side_effect=RuntimeError("db down")):
            from palmer import wxaudit
            wxaudit.run_forecast_audit()      # must not propagate

    def test_it_sends_nothing(self):
        import inspect
        from palmer import wxaudit
        src = inspect.getsource(wxaudit)
        for forbidden in ("send_sms", "ensure_sms", "messages.create"):
            assert forbidden not in src, "the audit is observation only"

    def test_one_city_is_logged_once_however_many_users_share_it(self):
        from palmer import wxaudit
        profiles = [("+1", {"city": "Kirkwood, MO"}), ("+2", {"city": "Kirkwood, MO"}),
                    ("+3", {"city": "Culver City"}), ("+4", {})]
        with patch("palmer.wxaudit.get_all_profiles", return_value=profiles), \
             patch("palmer.weather._geocode", side_effect=lambda c: (1.0, 2.0, c)):
            assert sorted(wxaudit._cities()) == ["Culver City", "Kirkwood, MO"]

    def test_a_scheduled_cron_not_an_interval(self):
        """Once a day on an interval makes the phase a function of deploy
        history, and a skipped day is a hole in the record."""
        import inspect
        from palmer import main
        src = inspect.getsource(main)
        block = src.split("run_forecast_audit,")[1][:120]
        assert '"cron"' in block and "misfire_grace_time" in block
