"""One source per user: NWS where it reaches, Open-Meteo everywhere else.

The page and the chat reply used to come from different forecasters. Chat asked
NWS; the page, the card and the morning line asked Open-Meteo. For a coastal
city that is not a rounding difference — on one August day in Culver City the
raw models spread 15 degrees for the same point (MeteoFrance 83, JMA 82, ICON
90, GEM 94, GFS 96, ECMWF 97, OpenWeatherMap 96), because how far the marine
layer pushes inland decides the answer. NWS said 90, Google said 87, and the
page showed 96 while the same user asking in the thread was told 90.

NWS is a forecaster product rather than a raw model — the local office corrects
for terrain and marine layer — so it is the better number wherever it exists.
Open-Meteo stays as the fallback because it is the only one of the two that
covers anywhere outside the US, and because NWS does go down.

Every test here is offline. A live call in this file is the suite-runtime
regression CLAUDE.md warns about, and it is easy to reintroduce: patching
_fetch_openmeteo no longer keeps a US location off the network.
"""
from unittest.mock import patch

import weather


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
