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
        assert "body=My+name+is" in m.group(1), "the text should be pre-written"

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
