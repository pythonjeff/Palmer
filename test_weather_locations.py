"""Multiple weather locations on Palmer Home.

profile["city"] stays the one true primary location — the tools, the morning
send, the timezone derivation all still key off it exactly as before.
weather_locations is a small additive list of SECONDARY places pinned to the
page only, following the same write-once-resolve, cap, and cache-invalidate
shape as follow_show/follow_team (see agent.get_reply's add_weather_location
and remove_weather_location blocks).
"""
import inspect
from unittest.mock import patch

import agent
import home
import page
import weather


class TestResolveWeatherLocation:
    def test_resolves_to_the_geocoded_display_form(self):
        with patch.object(weather, "_geocode", return_value=(38.5, -90.0, "Holiday Shores, IL")):
            assert weather.resolve_weather_location("holiday shores") == "Holiday Shores, IL"

    def test_an_unresolvable_place_returns_none_rather_than_guessing(self):
        with patch.object(weather, "_geocode", side_effect=ValueError("Location not found: asdf")):
            assert weather.resolve_weather_location("asdfqwer") is None


class TestSchema:
    def test_the_field_is_allowed(self):
        from userprofile import PROFILE_FIELDS
        assert "weather_locations" in PROFILE_FIELDS

    def test_it_is_not_aliased(self):
        from userprofile import _PROFILE_ALIASES
        assert "weather_locations" not in _PROFILE_ALIASES

    def test_structured_rows_survive_canonicalisation(self):
        from userprofile import _canonical_updates
        out = _canonical_updates({"weather_locations": ["Holiday Shores, IL"]})
        assert out == {"weather_locations": ["Holiday Shores, IL"]}


def _block(tool_name: str) -> str:
    src = inspect.getsource(agent.get_reply)
    return src.split(f'"{tool_name}"')[1].split("elif b.name")[0]


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
        with patch("weather.weather_snapshot", return_value={"temp_now": 80}) as snap:
            out = home._fetch_weather_extra(self.PROFILE)
        assert len(out) == 2
        assert snap.call_count == 2

    def test_a_failed_location_drops_only_that_row(self):
        """Same shape as _fetch_prices keeping other tickers when one 429s —
        one bad location must not blank the whole section."""
        with patch("weather.weather_snapshot", side_effect=[None, {"temp_now": 75}]):
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
