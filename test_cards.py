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
OPENING = [{"kind": "event", "title": "Todd Rundgren", "when": "Friday", "source": "t.com"},
           {"kind": "event", "title": "The Wallflowers", "when": "Saturday", "source": "t.com"},
           {"kind": "screen", "title": "Colony", "when": "in theaters", "source": "tmdb.org"}]


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


class TestOpeningBand:
    """Opening draws in the left column between the weather chips and the news
    rule — the one band of the card that was empty."""

    def test_the_card_renders_with_opening(self):
        img = Image.open(io.BytesIO(_render(opening=OPENING)))
        assert img.size == (cards.W, cards.H)

    def test_it_changes_the_pixels(self):
        """A section that draws nothing is a section that isn't there."""
        assert _render(opening=OPENING) != _render(opening=None)

    def test_absent_opening_is_fine(self):
        assert _render(opening=None) and _render(opening=[])

    def test_more_rows_than_fit_do_not_overflow_into_the_news_band(self):
        many = [{"kind": "event", "title": f"Act number {i}", "when": "Friday"}
                for i in range(9)]
        a = _render(opening=many)
        b = _render(opening=many[:cards.CARD_OPENING_ROWS])
        assert a == b, "rows past the cap must not be drawn at all"

    def test_a_very_long_title_is_clipped_rather_than_running_under_markets(self):
        long = [{"kind": "local", "title": "A restaurant with an absurdly long name " * 4,
                 "when": "Friday"}]
        assert _render(opening=long)


class TestCardCacheKey:
    """The card image is memoised. It used to key on `built_at`, which only
    advances inside home.rebuild() — and ensure_fresh calls rebuild only when
    there is no payload at all. So after a user's first build the key never
    changed again and the card froze on that morning's weather while the page
    beside it stayed live."""

    PAYLOAD = {"city": "Kirkwood, MO", "weather": WEATHER, "traffic": TRAFFIC,
               "prices": PRICES, "opening": OPENING,
               "headlines": [{"title": h} for h in HEADS], "built_at": 1}

    def _fp(self, **over):
        import artifacts
        return artifacts._card_fingerprint(dict(self.PAYLOAD, **over))

    def test_identical_content_is_a_cache_hit(self):
        assert self._fp() == self._fp()

    def test_a_stale_built_at_no_longer_freezes_the_card(self):
        """Same built_at, different weather — the old key could not tell these
        apart, which is the whole bug."""
        warmer = dict(WEATHER, temp_now=42.0)
        assert self._fp(weather=warmer) != self._fp()

    def test_every_drawn_section_moves_the_key(self):
        for field, value in (("city", "Denver, CO"),
                             ("traffic", dict(TRAFFIC, live_min=99)),
                             ("prices", []),
                             ("opening", []),
                             ("headlines", [{"title": "something else"}])):
            assert self._fp(**{field: value}) != self._fp(), f"{field} must re-key"

    def test_something_not_drawn_does_not_move_the_key(self):
        """tracking and the token never reach the renderer, so they must not
        cost a re-render."""
        assert self._fp(tracking={"topics": ["new"]}) == self._fp()

    def test_render_png_reuses_the_image_for_identical_content(self):
        import artifacts
        artifacts._png_cache.clear()
        a = artifacts.render_png("tok", dict(self.PAYLOAD))
        b = artifacts.render_png("tok", dict(self.PAYLOAD))
        assert a is b

    def test_render_png_redraws_when_the_content_moves(self):
        import artifacts
        artifacts._png_cache.clear()
        a = artifacts.render_png("tok", dict(self.PAYLOAD))
        b = artifacts.render_png("tok", dict(self.PAYLOAD, weather=dict(WEATHER, temp_now=12.0)))
        assert a != b

    def test_opening_reaches_the_renderer(self):
        import artifacts
        assert "opening" in artifacts._card_inputs(self.PAYLOAD)


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

    def test_price_snapshot_crypto_symbol_is_the_real_coingecko_id(self):
        """`asset` here is the alias a topic matched on ("avax"), not the
        coingecko id. The page builds coingecko.com links from `symbol`, so it
        must carry the resolved id ("avalanche-2"), not the alias — a link
        built from the alias 404s."""
        resp = MagicMock()
        resp.json.return_value = {"avalanche-2": {"usd": 20.0, "usd_24h_change": 1.0}}
        resp.raise_for_status = lambda: None
        with patch.object(datafeeds, "_requests") as rq:
            rq.get.side_effect = [resp, RuntimeError("chart down")]
            s = datafeeds.price_snapshot("avax", "Avalanche")
        assert s["symbol"] == "avalanche-2"

    def test_price_snapshot_stock_symbol_is_the_real_ticker(self):
        """An index's `label` is a human string ("S&P 500"); `symbol` must stay
        the actual Yahoo ticker ("^GSPC") so the page's link works."""
        ticker = MagicMock()
        ticker.fast_info.last_price = 5000.0
        ticker.history.return_value.empty = True
        with patch("yfinance.Ticker", return_value=ticker):
            s = datafeeds.price_snapshot("^GSPC", "S&P 500")
        assert s["symbol"] == "^GSPC" and s["label"] == "S&P 500"

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
