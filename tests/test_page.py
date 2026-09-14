"""Palmer Home and its card, and the tools that hand them out.

Merged from test_page.py, test_cards.py, test_my_page_tool.py, test_arrange_page_tool.py; each section keeps its
original file's notes, because those carry the incident that led to the test.
"""
import os
import re
import io
from unittest.mock import patch, MagicMock
from palmer import (
    page,
    cards,
    datafeeds,
    traffic as traffic_mod,
    weather as weather_mod,
    agent,
    prompts,
)
from PIL import Image
from tests.helpers import drive_tool, tool_by_name


# ============================================================================
# from test_page.py
# ============================================================================
#
# Tests for the interactive page, focused on the unknown-name fallback.
#
# A brand new user has no name yet, and an anonymous page is a dead end. The page
# has no auth and nothing to POST to, so the affordance is a pre-filled SMS back
# to Palmer — which is also the only zero-install way to collect it.

BASE = {"city": "Kirkwood, MO", "weather": {"temp_now": 81.0, "description": "Clear"},
        "fetched": {}, "tracking": {}}


def _render(**over):
    payload = dict(BASE)
    payload.update(over)
    return page.render(payload, token="t", image_url="i", page_url="p")


class TestNameKnown:
    def test_name_is_the_header(self):
        assert ">Jeff<" in _render(name="Jeff")

    def test_prompt_is_suppressed(self):
        assert "doesn't know your name" not in _render(name="Jeff")

    def test_city_still_shown(self):
        assert "Kirkwood, MO" in _render(name="Jeff")

    def test_whitespace_only_name_counts_as_missing(self):
        assert "doesn't know your name" in _render(name="   ")


class TestOpeningMetaSeparator:
    """The when/source line renders a middle dot between its two parts. A live
    page showed the literal text "&middot;" instead of "·" — e() ran on the
    already-built line, escaping the entity's own "&" into "&amp;", which the
    browser then displays as "&middot;" rather than decoding it."""

    def test_renders_an_actual_middle_dot_not_the_entity_name(self):
        html = _render(opening=[{"title": "Todd Rundgren", "when": "Friday",
                                 "source": "ticketmaster.com"}])
        assert "&middot;" in html, "the entity itself must still be emitted"
        assert "&amp;middot;" not in html, "double-escaping ships literal text instead of a dot"

    def test_the_untrusted_parts_are_still_escaped(self):
        html = _render(opening=[{"title": "x", "when": "<script>bad</script>", "source": "y"}])
        assert "<script>bad</script>" not in html


class TestPriceLinks:
    """The Markets row links out to coingecko.com / finance.yahoo.com. Those
    links must be built from the real coingecko id / Yahoo ticker (`symbol`),
    not the human-readable `label` — "S&P 500" and "Avalanche" are not the
    slugs those sites use, so a link built from the label 404s."""

    def test_crypto_link_uses_symbol_not_label(self):
        p = {"label": "Avalanche", "symbol": "avalanche-2", "is_crypto": True}
        assert page._price_link(p) == "https://www.coingecko.com/en/coins/avalanche-2"

    def test_stock_link_uses_symbol_not_label(self):
        p = {"label": "S&P 500", "symbol": "^GSPC", "is_crypto": False}
        assert page._price_link(p) == "https://finance.yahoo.com/quote/%5EGSPC"

    def test_falls_back_to_label_when_symbol_is_missing(self):
        """A payload cached before `symbol` existed still renders a link."""
        p = {"label": "NVDA", "is_crypto": False}
        assert page._price_link(p) == "https://finance.yahoo.com/quote/NVDA"


class TestPreviewTitle:
    """The og:title is the headline of the link preview in the thread — and the
    user-visible proof that Palmer stored the name rather than just reading it
    back out of the conversation."""

    def _title(self, **over):
        return re.search(r'og:title" content="(.*?)"', _render(**over)).group(1)

    def test_the_name_is_the_headline_when_known(self):
        assert self._title(name="Jeff") == "Jeff"

    def test_it_falls_back_to_the_weather_when_unknown(self):
        assert self._title() == "81\u00b0 in Kirkwood, MO"

    def test_the_browser_title_matches(self):
        assert "<title>Jeff</title>" in _render(name="Jeff")

    def test_a_blank_name_does_not_produce_an_empty_headline(self):
        assert self._title(name="   ").strip()


class TestNameMissing:
    def test_neutral_header_instead_of_blank(self):
        assert "Your briefing" in _render()

    def test_prompts_for_the_name(self):
        assert "doesn't know your name" in _render()

    def test_offers_a_prefilled_sms_link(self):
        with patch.dict(os.environ, {"TWILIO_PHONE_NUMBER": "+15550001234"}):
            html = _render()
        m = re.search(r'href="(sms:[^"]+)"', html)
        assert m, "expected a tappable sms: link"
        assert "+15550001234" in m.group(1)
        assert "body=My%20name%20is" in m.group(1), "the text should be pre-written"

    def test_the_prefilled_body_uses_percent_escapes_not_plusses(self):
        """The sms: scheme has no form encoding, so "+" is a literal plus.
        quote_plus put people into Messages with "My+name+is+" already typed,
        and that is exactly the text Palmer received back."""
        with patch.dict(os.environ, {"TWILIO_PHONE_NUMBER": "+15550001234"}):
            html = _render()
        body = re.search(r'body=([^"]+)"', html).group(1)
        assert "+" not in body, f"literal plus in prefilled body: {body!r}"

    def test_degrades_without_a_configured_number(self):
        """No number is a config gap, not a reason to render a broken link."""
        with patch.dict(os.environ, {"TWILIO_PHONE_NUMBER": ""}):
            html = _render()
        assert "Text Palmer your name" in html
        assert 'href=""' not in html and "sms:?" not in html

    def test_title_avoids_the_placeholder_city(self):
        html = page.render({"fetched": {}, "tracking": {}}, token="t", image_url="i", page_url="p")
        assert "Today briefing" not in html

    def test_name_is_escaped(self):
        assert "<script>" not in _render(name="<script>alert(1)</script>")


class TestThePreviewImageCanChange:
    """Link-preview scrapers cache og:images by URL and have no reason to
    refetch one they have seen. The URL was a fixed /h/{token}.png, so every
    morning's message showed whatever card was scraped the first time — the
    server was rendering today's card faithfully and nobody was asking for it.
    """

    def test_the_image_url_carries_a_content_stamp(self):
        import inspect
        from palmer import main
        src = inspect.getsource(main.home_page)
        assert "_card_fingerprint" in src and "?v=" in src

    def test_the_png_answers_a_revalidating_cache(self):
        import inspect
        from palmer import main
        src = inspect.getsource(main.home_png)
        assert "ETag" in src

    def test_the_stamp_moves_when_the_card_would_look_different(self):
        from palmer import artifacts
        base = {"city": "Kirkwood, MO", "weather": {"temp_now": 70, "high": 80, "low": 60},
                "prices": [], "headlines": [], "opening": []}
        warmer = dict(base, weather={"temp_now": 95, "high": 100, "low": 70})
        assert artifacts._card_fingerprint(base) != artifacts._card_fingerprint(warmer)


class TestSectionLabelsAreOneWord:
    """Every card label on the page is a single word.

    "Today" and "Palmer is watching" used to sit beside "Commute" and "Markets",
    so the column read as a mix of headings and a sentence. One word each is the
    rule now — it is a masthead, not prose, and the card image (cards.py) uses
    the same words so the MMS preview and the page read as one publication.
    """

    def _labels(self) -> list[str]:
        import inspect
        import re
        from palmer import page as page_mod
        # Labels are written straight into the markup, so read them from source
        # rather than rendering every possible payload permutation.
        src = inspect.getsource(page_mod)
        return [m.strip() for m in re.findall(r"<div class=label>([^<{\']*)", src) if m.strip()]

    def test_the_page_actually_has_labels_to_check(self):
        assert len(self._labels()) >= 4, "regex stopped matching the markup"

    def test_every_label_is_a_single_word(self):
        for label in self._labels():
            assert " " not in label, f"section label {label!r} must be one word"

    def test_the_renamed_sections_use_the_new_words(self):
        labels = self._labels()
        assert "News" in labels and "Watching" in labels
        assert "Today" not in labels

    def test_the_card_image_uses_the_same_words(self):
        """cards.py and page.py render from one payload and must not disagree
        about what a section is called."""
        import inspect
        from palmer import cards
        src = inspect.getsource(cards)
        assert '"NEWS"' in src and '"TODAY"' not in src


class TestWatchingSection:
    """'Watching' is a row of keyword chips, each linked to a source when one
    is known — a watch's last-fired article, a price watch's last-seen merchant
    page, or (for topics) the matching 'News' headline."""

    def test_absent_when_nothing_is_tracked(self):
        html = _render(tracking={"watches": [], "price_watches": [], "topics": []})
        assert ">Watching" not in html

    def test_watch_with_a_url_renders_as_a_linked_chip(self):
        html = _render(tracking={"watches": [{"description": "Iran and US strikes",
                                               "url": "https://apnews.com/x", "source": "apnews.com"}],
                                  "price_watches": [], "topics": []})
        assert 'href="https://apnews.com/x"' in html
        assert 'class="chip link"' in html

    def test_watch_without_a_url_renders_as_a_plain_chip(self):
        html = _render(tracking={"watches": [{"description": "brand new watch", "url": None}],
                                  "price_watches": [], "topics": []})
        assert "brand new watch" in html
        assert 'class="chip link"' not in html

    def test_watch_description_is_truncated(self):
        long_desc = "x" * 80
        html = _render(tracking={"watches": [{"description": long_desc, "url": None}],
                                  "price_watches": [], "topics": []})
        assert long_desc not in html
        assert "…" in html

    def test_watches_are_capped(self):
        many = [{"description": f"watch{i}", "url": None} for i in range(10)]
        html = _render(tracking={"watches": many, "price_watches": [], "topics": []})
        assert html.count("class=chip>watch") == page.WATCH_CHIP_CAP

    def test_price_watch_shows_current_price_when_seen(self):
        html = _render(tracking={"watches": [], "topics": [],
                                  "price_watches": [{"product": "AirPods Pro", "last_seen": 220.0,
                                                     "target": 199.0, "url": "https://amazon.com/x"}]})
        assert "$220.00" in html
        assert 'href="https://amazon.com/x"' in html

    def test_topic_links_to_its_matching_headline(self):
        html = _render(tracking={"watches": [], "price_watches": [], "topics": ["SpaceX news"]},
                       headlines=[{"title": "Starship launch", "url": "https://apnews.com/y",
                                   "topic": "SpaceX news"}])
        assert 'href="https://apnews.com/y"' in html
        assert "SpaceX news" in html

    def test_topic_without_a_matching_headline_is_unlinked(self):
        html = _render(tracking={"watches": [], "price_watches": [], "topics": ["some obscure topic"]},
                       headlines=[])
        assert "some obscure topic" in html
        assert 'class="chip link"' not in html

    def test_morning_time_moves_to_the_label_annotation(self):
        html = _render(tracking={"watches": [], "price_watches": [], "topics": ["news"],
                                  "morning_time": "7:00 AM"})
        assert "7:00 AM" in html
        assert "in your morning update" not in html, "old per-row phrasing should be gone"


ARRANGE_BASE = dict(
    BASE,
    traffic={"live_min": 17, "delay_min": 0, "ratio": 1.0},
    prices=[{"label": "NVDA", "price": 214.0, "pct_24h": -5.0}],
    headlines=[{"title": "a story", "url": "https://example.com/s", "source": "example.com"}],
)


def _order_of(html):
    labels = [("commute", ">Commute<"), ("markets", ">Markets<"),
              ("news", ">News<"), ("opening", ">Opening<")]
    found = [(html.index(tag), name) for name, tag in labels if tag in html]
    return [name for _, name in sorted(found)]


class TestArrangement:
    """arrange_page's order and visibility, honoured at render from
    payload["page_prefs"]. Sections the user named come first in their order;
    anything unnamed keeps its default position after them, so a partial
    instruction never silently drops a section."""

    def test_default_order_without_prefs(self):
        assert _order_of(_render(**ARRANGE_BASE)) == ["commute", "markets", "news"]

    def test_a_full_order_is_honoured(self):
        html = _render(**ARRANGE_BASE,
                       page_prefs={"section_order": ["news", "markets", "commute"]})
        assert _order_of(html) == ["news", "markets", "commute"]

    def test_a_partial_order_keeps_unnamed_sections_in_default_order_after(self):
        """'put markets first' is section_order=["markets"] and nothing else
        moves or vanishes."""
        html = _render(**ARRANGE_BASE, page_prefs={"section_order": ["markets"]})
        assert _order_of(html) == ["markets", "commute", "news"]

    def test_a_hidden_section_does_not_render(self):
        html = _render(**ARRANGE_BASE, page_prefs={"hidden_sections": ["commute"]})
        assert _order_of(html) == ["markets", "news"]

    def test_an_unknown_name_in_the_order_is_ignored(self):
        """Stored prefs outlive code changes; a stale name must not break the
        page or eat a section."""
        html = _render(**ARRANGE_BASE, page_prefs={"section_order": ["bogus", "news"]})
        assert _order_of(html) == ["news", "commute", "markets"]


class TestTmdbNoticeFollowsVisibility:
    """TMDB's terms require the notice wherever their data APPEARS — so it
    tracks the rendered page, not the payload: a screen row in a section the
    user hid shows no TMDB data and gets no unexplained third-party notice."""

    SCREEN = [{"title": "A Film", "kind": "screen"}]

    def test_notice_shown_when_a_screen_row_renders(self):
        assert "TMDB" in _render(**ARRANGE_BASE, opening=self.SCREEN)

    def test_notice_suppressed_when_opening_is_hidden(self):
        html = _render(**ARRANGE_BASE, opening=self.SCREEN,
                       page_prefs={"hidden_sections": ["opening"]})
        assert "TMDB" not in html


class TestArrangeAffordance:
    """The 'edit button' is a pre-filled text back to Palmer — same mechanics
    as the name ask, and for the same reason: the page has no auth and nothing
    to POST to."""

    def test_tap_target_present_with_a_number(self):
        with patch.dict(os.environ, {"TWILIO_PHONE_NUMBER": "+15550001111"}):
            html = _render(name="Jeff")
        assert "Want this arranged differently?" in html
        assert 'href="sms:+15550001111?&amp;body=Arrange%20my%20page%3A%20"' in html

    def test_body_uses_quote_not_quote_plus(self):
        """sms: URIs have no form encoding — a '+' is a literal plus, and
        quote_plus would put 'Arrange+my+page' in the user's Messages draft."""
        with patch.dict(os.environ, {"TWILIO_PHONE_NUMBER": "+15550001111"}):
            html = _render(name="Jeff")
        assert "Arrange+my" not in html

    def test_absent_without_a_number(self):
        with patch.dict(os.environ, {}, clear=True):
            assert "Want this arranged differently?" not in _render(name="Jeff")


class TestScoresSection:
    """One row per followed team: yesterday's result and today's game, from
    the same payload rows the morning text is drafted from."""

    ROW = {"team": "St. Louis Cardinals", "abbrev": "STL", "league": "mlb",
           "last": {"id": "1", "state": "post", "detail": "Final",
                    "home": {"abbrev": "STL", "name": "St. Louis Cardinals", "score": 5},
                    "away": {"abbrev": "CHC", "name": "Chicago Cubs", "score": 2}},
           "today": {"id": "2", "state": "pre", "detail": "7:15 PM CT",
                     "home": {"abbrev": "STL", "name": "St. Louis Cardinals", "score": 0},
                     "away": {"abbrev": "CHC", "name": "Chicago Cubs", "score": 0}}}

    def _html(self, **over):
        payload = {"city": "Kirkwood, MO", "scores": [self.ROW]}
        payload.update(over)
        return page.render(payload, token="t", image_url="i", page_url="p")

    def test_it_renders_both_lines_from_the_teams_side(self):
        html = self._html()
        assert ">Scores<" in html
        assert "beat Chicago Cubs 5-2" in html
        assert "play Chicago Cubs, 7:15 PM CT" in html

    def test_no_rows_no_section(self):
        assert ">Scores<" not in self._html(scores=[])

    def test_it_is_arrangeable_by_the_words_people_use(self):
        for word in ("scores", "sports", "games", "my team"):
            assert page.SECTION_WORDS[word] == "scores"
        assert "scores" in page.DEFAULT_SECTION_ORDER

    def test_it_can_be_hidden(self):
        html = self._html(page_prefs={"hidden_sections": ["scores"], "section_order": []})
        assert ">Scores<" not in html


# ============================================================================
# from test_cards.py
# ============================================================================
#
# Tests for the dashboard renderer and the structured snapshots behind it.
#
# The card is enrichment layered on top of a briefing that must still arrive if
# rendering fails, so most of these assert on graceful degradation rather than on
# pixels. Visual quality is judged by looking at the output, not here.

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


def _render_card(**kw):
    args = dict(city="Kirkwood, MO", weather=WEATHER, traffic=TRAFFIC,
                prices=PRICES, headlines=HEADS)
    args.update(kw)
    return cards.render_dashboard(**args)


class TestRender:
    def test_produces_a_png_at_og_spec(self):
        """1200x630 so one asset serves both the MMS card and the og:image."""
        img = Image.open(io.BytesIO(_render_card()))
        assert img.format == "PNG"
        assert img.size == (cards.W, cards.H) == (1200, 630)

    def test_not_blank(self):
        img = Image.open(io.BytesIO(_render_card())).convert("RGB")
        assert len(img.getcolors(maxcolors=1_000_000) or []) > 50, "render looks empty"

    def test_under_mms_size_limit(self):
        assert len(_render_card()) < 5_000_000

    def test_deterministic(self):
        from datetime import datetime
        when = datetime(2026, 8, 23, 7, 15)
        assert _render_card(when=when) == _render_card(when=when)


class TestDegradesSection_by_section:
    """A missing snapshot drops its panel; it must never fail the render, because
    a briefing without a card is fine and a briefing that doesn't send is not."""

    def test_each_section_may_be_absent(self):
        for missing in ("weather", "traffic", "prices", "headlines"):
            assert _render_card(**{missing: None})

    def test_everything_absent(self):
        assert _render_card(weather=None, traffic=None, prices=None, headlines=None)

    def test_partial_weather(self):
        assert _render_card(weather={"description": "Overcast"})

    def test_price_without_series(self):
        assert _render_card(prices=[{"label": "X", "price": 1.5, "pct_24h": -2.0, "series": []}])

    def test_long_headline_is_clipped_not_crashed(self):
        assert _render_card(headlines=["x" * 400])

    def test_missing_city(self):
        assert _render_card(city="")


class TestOpeningBand:
    """Opening draws in the left column between the weather chips and the news
    rule — the one band of the card that was empty."""

    def test_the_card_renders_with_opening(self):
        img = Image.open(io.BytesIO(_render_card(opening=OPENING)))
        assert img.size == (cards.W, cards.H)

    def test_it_changes_the_pixels(self):
        """A section that draws nothing is a section that isn't there."""
        assert _render_card(opening=OPENING) != _render_card(opening=None)

    def test_absent_opening_is_fine(self):
        assert _render_card(opening=None) and _render_card(opening=[])

    def test_more_rows_than_fit_do_not_overflow_into_the_news_band(self):
        many = [{"kind": "event", "title": f"Act number {i}", "when": "Friday"}
                for i in range(9)]
        a = _render_card(opening=many)
        b = _render_card(opening=many[:cards.CARD_OPENING_ROWS])
        assert a == b, "rows past the cap must not be drawn at all"

    def test_a_very_long_title_is_clipped_rather_than_running_under_markets(self):
        long = [{"kind": "local", "title": "A restaurant with an absurdly long name " * 4,
                 "when": "Friday"}]
        assert _render_card(opening=long)


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
        from palmer import artifacts
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
        from palmer import artifacts
        artifacts._png_cache.clear()
        a = artifacts.render_png("tok", dict(self.PAYLOAD))
        b = artifacts.render_png("tok", dict(self.PAYLOAD))
        assert a is b

    def test_render_png_redraws_when_the_content_moves(self):
        from palmer import artifacts
        artifacts._png_cache.clear()
        a = artifacts.render_png("tok", dict(self.PAYLOAD))
        b = artifacts.render_png("tok", dict(self.PAYLOAD, weather=dict(WEATHER, temp_now=12.0)))
        assert a != b

    def test_opening_reaches_the_renderer(self):
        from palmer import artifacts
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


# ============================================================================
# from test_my_page_tool.py
# ============================================================================
#
# get_my_page — Palmer hands over the user's link mid-conversation.
#
# The page is only useful if it can be asked for. Before this, the URL went out
# once a day with the morning update and there was no way to get it back short of
# scrolling the thread.
#
# The tool returns the URL and the model writes the sentence around it, so the
# tests that matter are: the tool exists and is routed, dispatch returns a live
# URL rather than a stale or invented one, and the no-APP_URL case tells the model
# to shut up about the page instead of promising a link it can't send.

URL = "https://palmer.example.com/h/AbC123xyz"


class TestSchema:
    def test_the_tool_exists(self):
        assert tool_by_name("get_my_page") is not None

    def test_it_takes_no_arguments(self):
        """The caller is the user. There is nothing to pass and nothing to
        get wrong — in particular no phone number the model could invent."""
        schema = tool_by_name("get_my_page")["input_schema"]
        assert schema["properties"] == {} and schema["required"] == []

    def test_the_description_covers_how_people_actually_ask(self):
        d = tool_by_name("get_my_page")["description"].lower()
        for phrase in ("send me my page", "link", "dashboard", "resend"):
            assert phrase in d

    def test_the_description_pins_the_url_to_the_end(self):
        d = tool_by_name("get_my_page")["description"].lower()
        assert "end" in d and "preview" in d

    def test_it_is_routed_in_the_system_prompt(self):
        """Tool routing is strict in this codebase — a tool the prompt does not
        name is a tool the model will not reliably reach for."""
        assert "get_my_page" in prompts.SYSTEM_PROMPT

    def test_the_prompt_forbids_typing_a_url_from_memory(self):
        block = prompts.SYSTEM_PROMPT.split("get_my_page:")[1].split("\n")[0].lower()
        assert "never type" in block or "from memory" in block


def _drive_my_page(reply="here you go", url=URL):
    """Run get_reply through one get_my_page tool call and capture what the
    model was handed back."""
    text, result, (ensure,) = drive_tool(
        "get_my_page", {}, message="send me my page", reply=reply,
        profile={"timezone": "America/Chicago"},
        patches=[patch("palmer.home.ensure_fresh", return_value=url)])
    return text, result, ensure


class TestDispatch:
    def test_it_returns_the_live_url(self):
        _, result, _ = _drive_my_page()
        assert URL in result

    def test_it_refreshes_the_page_for_this_caller(self):
        """ensure_fresh, never a bare URL builder — a link to a 404 or to yesterday's data
        is worse than no link."""
        _, _, ensure = _drive_my_page()
        ensure.assert_called_once_with("+1555")

    def test_it_tells_the_model_where_to_put_the_url(self):
        _, result, _ = _drive_my_page()
        assert "end of your reply" in result.lower()

    def test_the_reply_reaches_the_user(self):
        text, _, _ = _drive_my_page(reply=f"all yours {URL}")
        assert text.endswith(URL)

    def test_no_app_url_does_not_promise_a_link(self):
        _, result, _ = _drive_my_page(url="/h/tok")
        assert URL not in result
        assert "not mention a page" in result.lower()

    def test_no_app_url_still_answers_instead_of_erroring(self):
        text, _, _ = _drive_my_page(reply="not much going on", url="/h/tok")
        assert text == "not much going on"


class TestPriceTopicNormalization:
    """"add Nvidia to my site" has to end up as something the Markets section
    can resolve. The drafting model often writes the ticker itself and
    sometimes doesn't, and the failure was silent — the topic appeared under
    "Watching" with no price anywhere."""

    def test_a_topic_the_map_already_covers_is_untouched(self):
        """No model call: it already resolves."""
        with patch("palmer.llm.client") as client:
            assert agent._normalize_price_topic("Nvidia stock") == "Nvidia stock"
        client.messages.create.assert_not_called()

    def test_an_unmapped_company_gains_its_ticker(self):
        with patch("palmer.tickers.resolve_company_ticker", return_value="LULU"):
            assert agent._normalize_price_topic("Lululemon shares") == "Lululemon shares (LULU)"

    def test_an_unverifiable_company_gains_nothing(self):
        """No ticker is appended when nothing tradeable can be confirmed."""
        with patch("palmer.tickers.resolve_company_ticker", return_value=None):
            assert agent._normalize_price_topic("Stripe stock") == "Stripe stock"

    def test_a_news_topic_never_pays_for_a_lookup(self):
        with patch("palmer.llm.client") as client:
            for t in ("AI news", "St. Louis Cardinals", "Kirkwood weather"):
                assert agent._normalize_price_topic(t) == t
        client.messages.create.assert_not_called()

    def test_an_unresolvable_topic_is_left_alone(self):
        with patch("palmer.tickers.resolve_company_ticker", return_value=None):
            assert agent._normalize_price_topic("some obscure stock") == "some obscure stock"

    def test_empty_input_is_safe(self):
        assert agent._normalize_price_topic("") == ""

    def test_it_runs_on_the_add_path(self):
        """Normalization has to happen where topics are SAVED, since the read
        path runs on every page view and must stay free."""
        import inspect
        src = inspect.getsource(agent.get_reply)
        block = src.split('update_morning_briefing"')[1].split("elif b.name")[0]
        assert "_normalize_price_topic" in block


class TestAddingToTheSiteRefreshesIt:
    """The morning list and the page are one list, so a topic change has to be
    visible on the page immediately — not after the 5-minute price cooldown."""

    def test_the_add_path_expires_the_price_cache(self):
        import inspect
        src = inspect.getsource(agent.get_reply)
        block = src.split('update_morning_briefing"')[1].split("elif b.name")[0]
        assert "invalidate" in block, "a topic change must expire the cached prices"

    def test_a_failure_to_invalidate_does_not_break_the_reply(self):
        import inspect
        src = inspect.getsource(agent.get_reply)
        block = src.split('update_morning_briefing"')[1].split("elif b.name")[0]
        assert "except" in block, "the reply matters more than the cache stamp"

    def test_the_tool_description_covers_site_vocabulary(self):
        from palmer.tools_def import TOOLS
        d = next(t for t in TOOLS if t["name"] == "update_morning_briefing")["description"].lower()
        for word in ("markets", "site", "page", "morning"):
            assert word in d, f"users say {word!r} and mean this tool"

    def test_the_prompt_separates_asking_a_price_from_tracking_one(self):
        from palmer import prompts
        block = prompts.SYSTEM_PROMPT.lower()
        assert "one-off" in block and "update_morning_briefing" in block


class TestPalmerCanSeeTheReminders:
    """Watches and price watches were both listed in the system prompt.
    Reminders — the one thing the user explicitly asked to happen at a named
    time — were the table the model could not read at all. So "what have I got
    on today" had nothing to answer from, and "cancel my 4pm one" was a guess
    against twenty messages of history, against a tool that deletes."""

    PHONE = "+15550009999"

    def _system(self, pending):
        from unittest.mock import patch
        from palmer import agent
        with patch.object(agent, "get_profile",
                          return_value={"timezone": "America/Chicago"}), \
             patch("palmer.db.get_pending_reminders", return_value=pending), \
             patch.object(agent, "get_user_watches", return_value=[]), \
             patch.object(agent, "get_user_price_watches", return_value=[]):
            return agent._build_system(self.PHONE)

    def test_pending_reminders_reach_the_prompt(self):
        out = self._system([{"id": 1, "text": "move the car",
                             "due_at": "2026-09-04T21:00:00+00:00", "recurrence": None}])
        assert "move the car" in out

    def test_they_are_shown_on_the_users_clock(self):
        """21:00Z is 4pm in Chicago. Showing UTC is how a confirmation ends up
        naming an hour the user never said."""
        out = self._system([{"id": 1, "text": "move the car",
                             "due_at": "2026-09-04T21:00:00+00:00", "recurrence": None}])
        assert "4:00 PM" in out
        assert "21:00" not in out

    def test_a_repeat_says_so(self):
        out = self._system([{"id": 1, "text": "take the bins out",
                             "due_at": "2026-09-04T21:00:00+00:00", "recurrence": "weekly"}])
        assert "repeats weekly" in out

    def test_the_bulk_cancel_hazard_is_stated(self):
        out = self._system([{"id": 1, "text": "move the car",
                             "due_at": "2026-09-04T21:00:00+00:00", "recurrence": None}])
        assert "no text_match takes" in out
        assert "ask which" in out

    def test_no_reminders_adds_nothing(self):
        assert "Reminders they have set" not in self._system([])

    def test_a_failed_read_does_not_break_the_turn(self):
        from unittest.mock import patch
        from palmer import agent
        with patch.object(agent, "get_profile", return_value={}), \
             patch("palmer.db.get_pending_reminders", side_effect=RuntimeError("db down")), \
             patch.object(agent, "get_user_watches", return_value=[]), \
             patch.object(agent, "get_user_price_watches", return_value=[]):
            assert agent._build_system(self.PHONE)


class TestACancelSaysWhatItTook:
    """cancel_reminders returned a bare count on a tool whose no-argument form
    deletes every pending reminder, and whose text_match is a substring — so
    "call" takes "call mom" and "call the vet" together."""

    PHONE = "+15550008888"

    def _seed(self):
        from palmer import db
        db.cancel_reminders(self.PHONE)
        for text in ("call mom", "call the vet", "move the car"):
            db.save_reminder(self.PHONE, text, "2099-01-01T12:00:00+00:00")

    def test_it_returns_the_texts_that_went(self):
        from palmer import db
        self._seed()
        gone = db.cancel_reminders_named(self.PHONE, "call")
        assert sorted(gone) == ["call mom", "call the vet"]

    def test_the_unmatched_one_survives(self):
        from palmer import db
        self._seed()
        db.cancel_reminders_named(self.PHONE, "call")
        left = [r["text"] for r in db.get_pending_reminders(self.PHONE)]
        assert left == ["move the car"]

    def test_the_count_form_still_works(self):
        from palmer import db
        self._seed()
        assert db.cancel_reminders(self.PHONE) == 3

    def test_the_dispatch_names_them(self):
        import inspect
        from palmer import agent
        block = inspect.getsource(agent.get_reply).split('"cancel_reminders"')[1] \
                                                  .split("elif b.name")[0]
        assert "cancel_reminders_named" in block
        assert "Say which ones went" in block


# ============================================================================
# from test_arrange_page_tool.py
# ============================================================================
#
# arrange_page dispatch: presentation prefs merge by set arithmetic (a delta,
# never a model-restated whole set), unknown section words are surfaced rather
# than guessed, and the prices cache is expired only when the SORT changed —
# order and visibility are render-time and need no invalidate.

def _drive_arrange(tool_input, profile=None):
    _, result, (upsert, invalidate) = drive_tool(
        "arrange_page", tool_input, message="arrange my page", profile=profile or {},
        patches=[patch.object(agent, "upsert_profile"), patch("palmer.home.invalidate")])
    saved = upsert.call_args[0][1]["morning_prefs"] if upsert.called else None
    return result, saved, invalidate


class TestMarketsSort:
    def test_movers_is_stored_and_expires_the_prices_cache(self):
        result, saved, invalidate = _drive_arrange({"markets_sort": "movers"})
        assert saved["markets_sort"] == "movers"
        invalidate.assert_called_once_with("+1555", ("prices",))

    def test_added_clears_the_key_rather_than_storing_a_default(self):
        """Absent means topic order already; a stored default is prompt noise."""
        _, saved, invalidate = _drive_arrange({"markets_sort": "added"},
                                      {"morning_prefs": {"markets_sort": "movers"}})
        assert "markets_sort" not in saved
        invalidate.assert_called_once_with("+1555", ("prices",))

    def test_restating_the_current_sort_does_not_expire_the_cache(self):
        _, saved, invalidate = _drive_arrange({"markets_sort": "movers"},
                                      {"morning_prefs": {"markets_sort": "movers"}})
        invalidate.assert_not_called()


class TestOrderAndVisibility:
    def test_order_words_are_canonicalized(self):
        _, saved, invalidate = _drive_arrange({"section_order": ["stocks", "headlines"]})
        assert saved["section_order"] == ["markets", "news"]

    def test_order_changes_do_not_expire_any_cache(self):
        """Render-time — carried onto the payload on every view."""
        _, _, invalidate = _drive_arrange({"section_order": ["markets"]})
        invalidate.assert_not_called()

    def test_hide_then_show_round_trips(self):
        _, saved, _ = _drive_arrange({"hide": ["traffic"]})
        assert saved["hidden_sections"] == ["commute"]
        _, saved, _ = _drive_arrange({"show": ["commute"]},
                             {"morning_prefs": {"hidden_sections": ["commute"]}})
        assert saved["hidden_sections"] == []

    def test_hide_is_a_delta_not_a_restatement(self):
        """An existing hidden section survives a hide it wasn't named in."""
        _, saved, _ = _drive_arrange({"hide": ["news"]},
                             {"morning_prefs": {"hidden_sections": ["commute"]}})
        assert set(saved["hidden_sections"]) == {"commute", "news"}

    def test_other_prefs_survive_the_merge(self):
        _, saved, _ = _drive_arrange({"hide": ["news"]},
                             {"morning_prefs": {"episode_alerts": True,
                                                "opening_kinds": ["local"]}})
        assert saved["episode_alerts"] is True
        assert saved["opening_kinds"] == ["local"]


class TestUnknownWords:
    def test_an_unknown_word_is_surfaced_not_guessed(self):
        result, saved, _ = _drive_arrange({"hide": ["horoscope"]})
        assert "horoscope" in result
        assert "ask" in result.lower()
        assert saved is None, "nothing recognizable, nothing written"

    def test_kind_words_do_not_hide_the_opening_section(self):
        """'movies' and 'concerts' are Opening KINDS (opening_remove's job);
        mapping them here would let 'hide movies' silently hide the whole
        section instead of trimming a kind."""
        result, saved, _ = _drive_arrange({"hide": ["movies"]})
        assert saved is None
        assert "movies" in result
