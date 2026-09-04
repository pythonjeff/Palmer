"""Tests for the interactive page, focused on the unknown-name fallback.

A brand new user has no name yet, and an anonymous page is a dead end. The page
has no auth and nothing to POST to, so the affordance is a pre-filled SMS back
to Palmer — which is also the only zero-install way to collect it.
"""
import os
import re
from unittest.mock import patch

import page

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
        with patch.dict(os.environ, {"TWILIO_PHONE_NUMBER": "+17312525071"}):
            html = _render()
        m = re.search(r'href="(sms:[^"]+)"', html)
        assert m, "expected a tappable sms: link"
        assert "+17312525071" in m.group(1)
        assert "body=My%20name%20is" in m.group(1), "the text should be pre-written"

    def test_the_prefilled_body_uses_percent_escapes_not_plusses(self):
        """The sms: scheme has no form encoding, so "+" is a literal plus.
        quote_plus put people into Messages with "My+name+is+" already typed,
        and that is exactly the text Palmer received back."""
        with patch.dict(os.environ, {"TWILIO_PHONE_NUMBER": "+17312525071"}):
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
        import main
        src = inspect.getsource(main.home_page)
        assert "_card_fingerprint" in src and "?v=" in src

    def test_the_png_answers_a_revalidating_cache(self):
        import inspect
        import main
        src = inspect.getsource(main.home_png)
        assert "ETag" in src

    def test_the_stamp_moves_when_the_card_would_look_different(self):
        import artifacts
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
        import inspect, re
        import page as page_mod
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
        import cards
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
    the same payload rows the morning and evening texts are drafted from."""

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
