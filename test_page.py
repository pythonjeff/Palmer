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
