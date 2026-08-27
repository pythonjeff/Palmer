"""Tests for the dashboard renderer and the structured snapshots behind it.

The card is enrichment layered on top of a briefing that must still arrive if
rendering fails, so most of these assert on graceful degradation rather than on
pixels. Visual quality is judged by looking at the output, not here.
"""
from unittest.mock import patch, MagicMock

from PIL import Image
import io

import cards
import datafeeds
import traffic as traffic_mod
import weather as weather_mod

WEATHER = {"resolved": "Kirkwood, Missouri", "temp_now": 81.0, "feels_like": 84.0,
           "humidity": 55, "wind": 7.0, "weather_code": 3, "description": "Overcast",
           "high": 83.0, "low": 64.0, "rain_pct": 20, "gusts": None}
TRAFFIC = {"live_min": 17, "free_min": 16, "delay_min": 0, "miles": 13.7, "ratio": 1.024}
PRICES = [{"label": "Bitcoin", "price": 77752.0, "pct_24h": 1.1, "pct_7d": 0.4,
           "series": [76000, 76500, 77000, 77752], "is_crypto": True}]
HEADS = ["Cards blown out 12-3", "SpaceX hits 100 launches"]


def _render(**kw):
    args = dict(city="Kirkwood, MO", weather=WEATHER, traffic=TRAFFIC,
                prices=PRICES, headlines=HEADS)
    args.update(kw)
    return cards.render_dashboard(**args)


class TestRender:
    def test_produces_a_png_at_og_spec(self):
        """1200x630 so one asset serves both the MMS card and the og:image."""
        img = Image.open(io.BytesIO(_render()))
        assert img.format == "PNG"
        assert img.size == (cards.W, cards.H) == (1200, 630)

    def test_not_blank(self):
        img = Image.open(io.BytesIO(_render())).convert("RGB")
        assert len(img.getcolors(maxcolors=1_000_000) or []) > 50, "render looks empty"

    def test_under_mms_size_limit(self):
        assert len(_render()) < 5_000_000

    def test_deterministic(self):
        from datetime import datetime
        when = datetime(2026, 8, 23, 7, 15)
        assert _render(when=when) == _render(when=when)


class TestDegradesSection_by_section:
    """A missing snapshot drops its panel; it must never fail the render, because
    a briefing without a card is fine and a briefing that doesn't send is not."""

    def test_each_section_may_be_absent(self):
        for missing in ("weather", "traffic", "prices", "headlines"):
            assert _render(**{missing: None})

    def test_everything_absent(self):
        assert _render(weather=None, traffic=None, prices=None, headlines=None)

    def test_partial_weather(self):
        assert _render(weather={"description": "Overcast"})

    def test_price_without_series(self):
        assert _render(prices=[{"label": "X", "price": 1.5, "pct_24h": -2.0, "series": []}])

    def test_long_headline_is_clipped_not_crashed(self):
        assert _render(headlines=["x" * 400])

    def test_missing_city(self):
        assert _render(city="")


class TestMeter:
    def _ratio_colour(self, ratio):
        img = Image.new("RGBA", (200, 60), (0, 0, 0, 255))
        return cards._meter(img, 10, 20, 160, ratio)

    def test_free_flowing_is_green(self):
        assert self._ratio_colour(1.02) == cards.UP

    def test_moderate_is_amber(self):
        assert self._ratio_colour(1.25) == cards.WARM

    def test_heavy_is_red(self):
        assert self._ratio_colour(1.5) == cards.DOWN

    def test_extreme_ratio_clamps(self):
        assert self._ratio_colour(9.0) == cards.DOWN


class TestSparkline:
    def test_too_few_points_is_a_noop(self):
        img = Image.new("RGB", (100, 40))
        before = img.tobytes()
        cards._sparkline(cards.ImageDraw.Draw(img), 0, 0, 90, 30, [1.0], cards.UP)
        assert img.tobytes() == before

    def test_flat_series_does_not_divide_by_zero(self):
        img = Image.new("RGB", (100, 40))
        cards._sparkline(cards.ImageDraw.Draw(img), 0, 0, 90, 30, [5.0, 5.0, 5.0], cards.UP)


class TestFontResolution:
    def test_returns_a_font(self):
        assert cards._font(24) is not None

    def test_falls_back_when_no_font_dir_exists(self):
        cards._font_cache.clear()
        with patch.object(cards, "_FONT_DIRS", ("/nonexistent",)):
            assert cards._font(24, True) is not None
        cards._font_cache.clear()


class TestSnapshots:
    """Structured returns are additive — the prose paths must be untouched."""

    def test_weather_snapshot_shape(self):
        """The Open-Meteo branch, reached via non-US coords — US locations go to
        NWS now (see test_weather_source.py). Patching _fetch_openmeteo alone is
        not enough to keep this offline: with US coords it would route to NWS and
        make a real call."""
        payload = {"current": {"temperature_2m": 81.0, "apparent_temperature": 84.0,
                               "weather_code": 3, "wind_speed_10m": 7.0,
                               "relative_humidity_2m": 55},
                   "daily": {"temperature_2m_max": [83.0], "temperature_2m_min": [64.0],
                             "precipitation_probability_max": [20],
                             "wind_gusts_10m_max": [12.0]}}
        with patch.object(weather_mod, "_geocode", return_value=(48.86, 2.35, "Paris")), \
             patch.object(weather_mod, "_fetch_openmeteo", return_value=payload):
            s = weather_mod.weather_snapshot("Paris")
        assert s["temp_now"] == 81.0 and s["high"] == 83.0 and s["weather_code"] == 3
        assert s["description"]

    def test_weather_snapshot_returns_none_on_failure(self):
        with patch.object(weather_mod, "_geocode", side_effect=RuntimeError("boom")):
            assert weather_mod.weather_snapshot("Nowhere") is None

    def test_traffic_snapshot_ratio(self):
        payload = {"routes": [{"summary": {"travelTimeInSeconds": 1020,
                                           "noTrafficTravelTimeInSeconds": 960,
                                           "trafficDelayInSeconds": 60,
                                           "lengthInMeters": 22000}}]}
        with patch.object(traffic_mod, "_geocode_address", return_value=(38.5, -90.4)), \
             patch.object(traffic_mod, "_http_get_json", return_value=payload):
            s = traffic_mod.traffic_snapshot("a street", "b street")
        assert s["live_min"] == 17 and s["free_min"] == 16
        assert round(s["ratio"], 2) == 1.06, "ratio is what the meter renders"

    def test_traffic_snapshot_none_on_bad_route(self):
        with patch.object(traffic_mod, "_geocode_address", return_value=(1, 2)), \
             patch.object(traffic_mod, "_http_get_json", return_value={"routes": []}):
            assert traffic_mod.traffic_snapshot("a", "b") is None

    def test_price_snapshot_crypto(self):
        resp = MagicMock()
        resp.json.return_value = {"bitcoin": {"usd": 77752.0, "usd_24h_change": 1.1,
                                              "usd_7d_change": 0.4}}
        resp.raise_for_status = lambda: None
        chart = MagicMock()
        chart.json.return_value = {"prices": [[0, 76000], [1, 77752]]}
        chart.raise_for_status = lambda: None
        with patch.object(datafeeds, "_requests") as rq:
            rq.get.side_effect = [resp, chart]
            s = datafeeds.price_snapshot("bitcoin")
        assert s["price"] == 77752.0 and s["is_crypto"] is True
        assert len(s["series"]) == 2

    def test_price_snapshot_survives_missing_sparkline(self):
        resp = MagicMock()
        resp.json.return_value = {"bitcoin": {"usd": 100.0, "usd_24h_change": 0.0}}
        resp.raise_for_status = lambda: None
        with patch.object(datafeeds, "_requests") as rq:
            rq.get.side_effect = [resp, RuntimeError("chart down")]
            s = datafeeds.price_snapshot("bitcoin")
        assert s["price"] == 100.0 and s["series"] == [], "sparkline is a bonus, not a dependency"

    def test_price_snapshot_none_on_failure(self):
        with patch.object(datafeeds, "_requests") as rq:
            rq.get.side_effect = RuntimeError("boom")
            assert datafeeds.price_snapshot("bitcoin") is None
