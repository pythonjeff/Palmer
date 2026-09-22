"""One brand across the card, the page, the preview and the contact card.

The palette used to exist twice — `--paper:#f7f5ef` in page.py and
`PAPER = (247, 245, 239)` in cards.py — under a standing rule that the two
renderers must not disagree. These tests are that rule, enforced.
"""
import inspect
import re

import pytest

import brand


class TestPaletteHasOneDefinition:
    def test_the_card_draws_brand_colours(self):
        import cards
        assert cards.PAPER == brand.rgb(brand.PAPER)
        assert cards.INK == brand.rgb(brand.INK)
        assert cards.MUTED == brand.rgb(brand.INK2)
        assert cards.RULE == brand.rgb(brand.RULE_SOLID)
        assert (cards.UP, cards.DOWN) == (brand.rgb(brand.UP), brand.rgb(brand.DOWN))
        assert cards.WARM == brand.rgb(brand.WARM)
        assert cards.COOL == brand.rgb(brand.COOL)

    def test_the_page_css_declares_brand_colours(self):
        import page
        root = page.CSS.split(":root{")[1].split("}")[0]
        for var, value in (("--paper", brand.PAPER), ("--ink", brand.INK),
                           ("--ink2", brand.INK2), ("--warm", brand.WARM),
                           ("--cool", brand.COOL), ("--up", brand.UP),
                           ("--down", brand.DOWN), ("--amber", brand.AMBER)):
            assert f"{var}:{value};" in root, var

    def test_no_module_hardcodes_the_paper_or_ink_hex(self):
        """The whole point. A second literal is a second source of truth."""
        import pathlib
        offenders = []
        for path in sorted(pathlib.Path(".").glob("*.py")):
            if path.name in ("brand.py",) or path.name.startswith("test_"):
                continue
            text = path.read_text()
            for literal in (brand.PAPER, "(247, 245, 239)"):
                if literal in text:
                    offenders.append(f"{path.name}: {literal}")
        assert not offenders, "import these from brand.py:\n" + "\n".join(offenders)

    def test_rgb_round_trips(self):
        assert brand.rgb("#f7f5ef") == (247, 245, 239)
        assert brand.rgb("161510") == (22, 21, 16)


class TestTheMark:
    def test_svg_is_well_formed_and_carries_the_palette(self):
        svg = brand.mark_svg(64)
        assert svg.startswith("<svg") and svg.endswith("</svg>")
        assert brand.PAPER in svg and brand.INK in svg
        assert 'viewBox="0 0 64 64"' in svg

    def test_svg_has_no_unescaped_quote_breaking_an_attribute(self):
        """It is inlined into href="data:image/svg+xml,...", so a bare double
        quote would end the attribute and silently drop the favicon."""
        svg = brand.mark_svg(32)
        assert '&quot;' in svg  # the font stack's inner quotes are entity-escaped
        assert re.search(r'font-family="[^"]*"', svg)

    def test_png_renders_at_the_rbm_logo_size(self):
        png = brand.mark_png(224)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        # RCS Business Messaging caps an agent logo at 50KB, which is why 224
        # is the default — one drawing serves every job.
        assert len(png) < 50 * 1024

    def test_png_is_square_at_the_requested_size(self):
        import io
        from PIL import Image
        for size in (64, 180, 512):
            img = Image.open(io.BytesIO(brand.mark_png(size)))
            assert img.size == (size, size)

    def test_the_mark_is_drawn_not_committed(self):
        """Generated from the palette, like the card itself, so it cannot fall
        out of step with it."""
        import pathlib
        assert not list(pathlib.Path(".").glob("*.png"))

    def test_it_survives_a_box_with_no_fonts(self, monkeypatch):
        """A fontless container must still get the square and the rule rather
        than a 500 on the icon route."""
        monkeypatch.setattr(brand, "_MARK_DIRS", ("/nonexistent",))
        brand.mark_png.cache_clear()   # the render is memoised; this one differs
        try:
            png = brand.mark_png(64)
            assert png[:8] == b"\x89PNG\r\n\x1a\n"
        finally:
            brand.mark_png.cache_clear()

    def test_the_render_is_memoised(self):
        """/icon.png is public and unauthenticated, and a render costs ~12ms on
        the single worker that also answers Twilio's inbound webhooks."""
        brand.mark_png.cache_clear()
        first = brand.mark_png(180)
        assert brand.mark_png(180) is first

    def test_every_offered_size_is_on_the_ladder(self):
        """snap_size bounds the cache. An arbitrary ?s= would not."""
        assert brand.snap_size(1) == 32
        assert brand.snap_size(180) == 180
        assert brand.snap_size(181) == 224
        assert brand.snap_size(99999) == 512
        assert brand.mark_png.cache_info().maxsize == len(brand.MARK_SIZES)


class TestTheContactCard:
    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setenv("APP_URL", "https://palmer-ai.test")
        monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15551234567")
        monkeypatch.delenv("LINK_DOMAIN", raising=False)
        from fastapi.testclient import TestClient
        import main
        return TestClient(main.app)

    def test_it_parses_as_a_vcard(self, client):
        body = client.get("/palmer.vcf").text
        assert body.startswith("BEGIN:VCARD\r\n") and body.rstrip().endswith("END:VCARD")
        assert "VERSION:3.0" in body

    def test_it_carries_the_name_number_and_photo(self, client):
        body = client.get("/palmer.vcf").text
        assert f"FN:{brand.NAME}" in body
        assert "TEL;TYPE=CELL:+15551234567" in body
        assert "PHOTO;VALUE=URI:https://palmer-ai.test/icon.png" in body

    def test_lines_end_crlf(self, client):
        """vCard is CRLF-delimited; a bare LF is what makes a card import as
        one mangled field on iOS."""
        body = client.get("/palmer.vcf").text
        assert "\r\n" in body
        assert not re.search(r"(?<!\r)\n", body)

    def test_it_is_served_as_a_contact_not_a_download_of_text(self, client):
        r = client.get("/palmer.vcf")
        assert r.headers["content-type"].startswith("text/vcard")
        assert "palmer.vcf" in r.headers.get("content-disposition", "")

    def test_it_says_nothing_about_who_fetched_it(self, client):
        """One card for everyone. No token, no phone but Palmer's own."""
        body = client.get("/palmer.vcf").text
        assert "h/" not in body

    def test_no_number_means_no_card(self, client, monkeypatch):
        monkeypatch.setenv("TWILIO_PHONE_NUMBER", "")
        assert client.get("/palmer.vcf").status_code == 404

    def test_the_icon_route_serves_a_png(self, client):
        r = client.get("/icon.png?s=180")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_the_icon_size_is_clamped(self, client):
        """?s= is user input on an unauthenticated route; an unbounded value is
        an invitation to render a 30000px image on a 512MB dyno."""
        import io
        from PIL import Image
        assert Image.open(io.BytesIO(client.get("/icon.png?s=99999").content)).size == (512, 512)
        assert Image.open(io.BytesIO(client.get("/icon.png?s=1").content)).size == (32, 32)


class TestWhereTheContactCardGoesOut:
    """It is offered on the page and never texted.

    Same rule and same shape as test_onboard.TestWhereTheLinkGoesOut: sending
    an unprompted attachment to someone who asked for nothing is the mistake
    the bare-greeting intro rule exists to prevent. A user who has opened their
    page has opted into Palmer; a stranger on message one has not.
    """

    def test_the_page_offers_it(self, monkeypatch):
        monkeypatch.setenv("APP_URL", "https://palmer-ai.test")
        monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15551234567")
        import page
        import links
        html = page.render({"name": "Drew", "fetched": {}, "tracking": {}},
                           token="tok", image_url="i", page_url="p")
        assert links.vcard_url() in html

    def test_no_sender_reaches_for_it(self):
        """The guard that matters. If a proactive job ever attaches the vCard,
        this fails."""
        import pathlib
        senders = ("morning.py", "followup.py", "watches.py", "shopping.py",
                   "flightwatch.py", "scorewatch.py", "send_reminders.py",
                   "alerts.py", "agent.py", "onboard.py")
        offenders = []
        for name in senders:
            path = pathlib.Path(name)
            if not path.exists():
                continue
            if "vcard" in path.read_text().lower():
                offenders.append(name)
        assert not offenders, (
            "the contact card is offered on the page, never texted: " + ", ".join(offenders))

    def test_the_intro_path_does_not_mention_it(self):
        """main._handle_sms_inner is where a first reply is assembled."""
        import main
        src = inspect.getsource(main._handle_sms_inner)
        assert "vcf" not in src.lower() and "vcard" not in src.lower()


class TestThePreviewNamesTheSender:
    """The masthead is the reader's own name, deliberately — so without
    og:site_name nothing anywhere in the link preview said who sent it."""

    def _head(self, monkeypatch):
        monkeypatch.setenv("APP_URL", "https://palmer-ai.test")
        monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15551234567")
        import page
        html = page.render({"name": "Drew", "fetched": {}, "tracking": {}},
                           token="tok", image_url="i", page_url="p")
        return html.split("</head>")[0]

    def test_og_site_name_is_the_brand(self, monkeypatch):
        assert f'property="og:site_name" content="{brand.NAME}"' in self._head(monkeypatch)

    def test_the_preview_image_has_alt_text(self, monkeypatch):
        assert 'property="og:image:alt"' in self._head(monkeypatch)

    def test_there_is_a_favicon_and_a_touch_icon(self, monkeypatch):
        head = self._head(monkeypatch)
        assert 'rel="icon"' in head and 'rel="apple-touch-icon"' in head

    def test_the_favicon_is_inline(self, monkeypatch):
        """page.py's docstring promises no external requests — it opens on a
        cell connection and that is the only place it is ever opened."""
        head = self._head(monkeypatch)
        assert 'rel="icon" href="data:image/svg+xml,' in head
