"""Every public URL has one owner, and it survives a host change.

The point of links.py is that the shape is edited in one place. These tests
guard both halves of that: the builders are correct, and no caller has quietly
grown its own copy of the f-string again.
"""
import inspect
import pathlib
import re

import pytest

import links


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No ambient host. main.py's load_dotenv() puts a local .env into
    os.environ for the whole session, so a test that reads a host must set it."""
    monkeypatch.delenv("APP_URL", raising=False)
    monkeypatch.delenv("LINK_DOMAIN", raising=False)


class TestPublicBase:
    def test_it_falls_back_to_the_app(self, monkeypatch):
        monkeypatch.setenv("APP_URL", "https://palmer-ai.herokuapp.com")
        assert links.public_base() == "https://palmer-ai.herokuapp.com"

    def test_link_domain_wins_when_set(self, monkeypatch):
        monkeypatch.setenv("APP_URL", "https://palmer-ai.herokuapp.com")
        monkeypatch.setenv("LINK_DOMAIN", "https://palmr.at")
        assert links.public_base() == "https://palmr.at"

    def test_a_bare_domain_is_promoted_to_https(self, monkeypatch):
        """iMessage refuses to load a preview image over http, so a link that
        lost its scheme would resolve and still never draw a card."""
        monkeypatch.setenv("LINK_DOMAIN", "palmr.at")
        assert links.public_base() == "https://palmr.at"

    def test_a_trailing_slash_does_not_double_up(self, monkeypatch):
        monkeypatch.setenv("LINK_DOMAIN", "https://palmr.at/")
        assert links.page_url("tok") == "https://palmr.at/h/tok"

    def test_whitespace_is_not_a_domain(self, monkeypatch):
        monkeypatch.setenv("APP_URL", "https://app.test")
        monkeypatch.setenv("LINK_DOMAIN", "   ")
        assert links.public_base() == "https://app.test"

    def test_no_host_at_all_yields_a_relative_path(self, monkeypatch):
        """Callers gate on startswith('http') to fall back to a text briefing
        rather than handing someone a link to nothing."""
        assert not links.page_url("tok").startswith("http")
        assert links.configured() is False


class TestTheShapes:
    def test_page(self, monkeypatch):
        monkeypatch.setenv("APP_URL", "https://x.test")
        assert links.page_url("tok") == "https://x.test/h/tok"

    def test_image_carries_the_stamp(self, monkeypatch):
        monkeypatch.setenv("APP_URL", "https://x.test")
        assert links.image_url("tok", "abc") == "https://x.test/h/tok.png?v=abc"

    def test_image_without_a_stamp_is_still_valid(self, monkeypatch):
        monkeypatch.setenv("APP_URL", "https://x.test")
        assert links.image_url("tok") == "https://x.test/h/tok.png"

    def test_vcard_and_icon_are_not_per_user(self, monkeypatch):
        """Palmer's contact card is the same for everyone and says nothing
        about whoever fetched it."""
        monkeypatch.setenv("APP_URL", "https://x.test")
        assert links.vcard_url() == "https://x.test/palmer.vcf"
        assert "tok" not in links.vcard_url()
        assert links.icon_url() == "https://x.test/icon.png"


class TestSmsLink:
    def test_it_prefills_the_body(self, monkeypatch):
        monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15551234567")
        assert links.sms_link("My name is ") == "sms:+15551234567?&body=My%20name%20is%20"

    def test_the_plus_is_not_form_encoded(self, monkeypatch):
        """quote_plus once sent people into Messages with "My+name+is+" already
        typed, and that is exactly what Palmer received."""
        monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15551234567")
        assert "+" not in links.sms_link("a b c").split("body=")[1]

    def test_the_separator_both_platforms_parse(self, monkeypatch):
        """iOS wants `&body=`, Android wants `?body=`; `?&` is the one spelling
        both accept. It looks like a typo and is load-bearing."""
        monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15551234567")
        assert "?&body=" in links.sms_link("hi")

    def test_no_number_means_no_dead_tap_target(self, monkeypatch):
        monkeypatch.setenv("TWILIO_PHONE_NUMBER", "")
        assert links.sms_link("hi") is None


class TestNobodyBuildsTheirOwn:
    """The six hand-written f-strings are the reason this module exists. This
    fails if a seventh appears."""

    def test_no_module_spells_the_page_path_itself(self):
        offenders = []
        pattern = re.compile(r'f"\{[A-Za-z_]*(?:app_url|APP_URL|base)[^"]*\}/h/')
        for path in sorted(pathlib.Path(".").glob("*.py")):
            if path.name in ("links.py",) or path.name.startswith("test_"):
                continue
            for n, line in enumerate(path.read_text().split("\n"), 1):
                if pattern.search(line):
                    offenders.append(f"{path.name}:{n}: {line.strip()}")
        assert not offenders, (
            "build these through links.py instead:\n" + "\n".join(offenders))

    def test_the_dead_c_routes_are_gone(self):
        """artifacts.load accepted only kind='briefing' and home.save writes
        kind='home', so nothing had written a row /c/ could read in a long time
        and every request to it 404'd."""
        import main
        src = inspect.getsource(main)
        assert '"/c/{token}"' not in src and '"/c/{token}.png"' not in src

    def test_the_short_domain_contract_is_written_down(self):
        """A redirecting shortener in front of the page breaks the og preview,
        which is most of the value of the morning send. If that paragraph goes,
        someone will point LINK_DOMAIN at Bitly."""
        doc = links.__doc__ or ""
        assert "redirect" in doc.lower() and "alias" in doc.lower()
