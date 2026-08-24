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
