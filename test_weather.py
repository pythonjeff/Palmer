"""Tests for _geocode. All HTTP mocked — a real call here is the exact suite-
runtime regression CLAUDE.md warns about."""
from unittest.mock import patch

import pytest

import weather


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
