"""A URL must survive the SMS pipeline byte for byte, or not be sent at all.

"Bad links" turned out to be three separate mechanical defects, none of them in
the code that chooses a link:

  - the markdown scrub rewrote `[text](url)` to `text`, DELETING the target;
  - the ASCII fold dropped non-ASCII bytes out of the middle of a path, leaving
    a URL that looks right and resolves to nothing;
  - three truncating paths (`shorten_message`, the `body[:320]` fallback, the
    hard chunker) all cut at a fixed offset, which lands mid-URL on exactly the
    messages most likely to carry one.

And the /sms-status retry fed a live message back through the first and third of
those. morning.py opted out of that callback by hand; nothing else did.
"""
import sms_util
import smstext


URL = "https://palmer.example/h/AbC123_xyz"


class TestMarkdownLinksKeepTheirTarget:
    def test_the_url_survives(self):
        out = smstext._sms_clean(f"here you go: [your page]({URL})")
        assert URL in out

    def test_the_label_survives_too(self):
        out = smstext._sms_clean(f"here you go: [your page]({URL})")
        assert "your page" in out

    def test_a_non_http_target_is_still_dropped(self):
        # `[x](#)` is markup, not a link worth texting.
        out = smstext._sms_clean("see [the notes](#anchor)")
        assert "the notes" in out
        assert "#anchor" not in out


class TestTheAsciiFoldDoesNotCorruptUrls:
    def test_a_plain_url_is_untouched(self):
        assert smstext._sms_clean(f"tap it {URL}").endswith(URL)

    def test_a_non_ascii_path_is_percent_encoded_not_truncated(self):
        out = smstext._sms_clean("https://example.com/café/menu")
        # The old fold silently deleted the accented byte, producing
        # https://example.com/caf/menu — a valid-looking URL to nowhere.
        assert "/caf/menu" not in out
        assert "caf%C3%A9" in out

    def test_query_strings_survive(self):
        u = "https://example.com/x?a=1&b=2#frag"
        assert u in smstext._sms_clean(f"here {u}")

    def test_emoji_beside_a_url_is_still_stripped(self):
        out = smstext._sms_clean(f"\U0001F600 {URL}")
        assert URL in out
        assert "\U0001F600" not in out


class TestTruncationNeverCutsAUrl:
    def test_a_url_is_kept_whole_or_dropped(self):
        text = "x" * 300 + " " + URL
        out = smstext.truncate_preserving_urls(text, 320)
        assert URL in out or URL[:20] not in out

    def test_a_url_alone_is_returned_when_it_cannot_fit_with_prose(self):
        out = smstext.truncate_preserving_urls(URL, 10)
        assert out == URL

    def test_short_text_is_untouched(self):
        assert smstext.truncate_preserving_urls("hi", 320) == "hi"

    def test_no_partial_scheme_ever_ships(self):
        for n in range(1, 60):
            out = smstext.truncate_preserving_urls("a " + URL, n)
            assert "http" not in out or URL in out


class TestChunkingKeepsUrlsIntact:
    def test_a_url_is_not_split_across_parts(self):
        body = ("word " * 400) + URL
        parts = sms_util._split_for_sms(body, max_chars=900)
        assert any(URL in p for p in parts), parts

    def test_nothing_is_lost(self):
        body = ("word " * 400) + URL
        joined = " ".join(sms_util._split_for_sms(body, max_chars=900))
        assert joined.count("word") == 400

    def test_short_text_is_one_part(self):
        assert sms_util._split_for_sms("hi") == ["hi"]


class TestTheStatusCallbackIsSuppressedForLinks:
    """The retry at /sms-status reruns shorten_message on the original body, so
    a message carrying a link must not opt into it."""

    def _sent(self, monkeypatch, body):
        seen = {}

        class _Msgs:
            def create(self, **kw):
                seen.update(kw)

        # send_sms reads the from-number from the environment at call time, so
        # without this the suite only passes on a machine holding live Twilio
        # credentials — a trap for CI and for anyone with a partial .env.
        monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15550000000")
        monkeypatch.setattr(sms_util, "_twilio", type("T", (), {"messages": _Msgs()})())
        monkeypatch.setattr(sms_util, "_STATUS_CALLBACK_URL", "https://app/sms-status")
        sms_util.send_sms("+15550001111", body)
        return seen

    def test_a_body_with_a_url_opts_out(self, monkeypatch):
        assert "status_callback" not in self._sent(monkeypatch, f"morning - 72 today {URL}")

    def test_a_body_without_a_url_keeps_it(self, monkeypatch):
        assert "status_callback" in self._sent(monkeypatch, "morning - 72 today")


class TestShortenMessageKeepsLinks:
    def test_the_link_survives_a_failed_model_call(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("no network")
        monkeypatch.setattr(smstext.client.messages, "create", _boom)
        out = smstext.shorten_message("blah " * 200 + URL, max_chars=320)
        assert URL in out
        assert len(out) <= 320

    def test_the_link_survives_a_model_that_drops_it(self, monkeypatch):
        # Haiku never sees the URL now, so it cannot lose it.
        class _R:
            content = [type("B", (), {"text": "short version, no link here"})()]
        monkeypatch.setattr(smstext.client.messages, "create", lambda *a, **k: _R())
        out = smstext.shorten_message("blah " * 200 + URL, max_chars=320)
        assert URL in out

    def test_the_url_is_last(self, monkeypatch):
        class _R:
            content = [type("B", (), {"text": "short version"})()]
        monkeypatch.setattr(smstext.client.messages, "create", lambda *a, **k: _R())
        out = smstext.shorten_message("blah " * 200 + URL, max_chars=320)
        assert out.endswith(URL)


class TestTheFlightRouteIsAscii:
    def test_no_glyph_is_deleted_by_the_fold(self):
        import flightwatch
        route = flightwatch._route(
            {"origin": "LAX", "destination": "MXP", "outbound_date": "2026-09-10"})
        assert "LAX to MXP" in route
        # The bug: an unmapped arrow was dropped, leaving "LAX  MXP".
        assert smstext._sms_clean(route) == route
        assert "  " not in smstext._sms_clean(route)

    def test_the_arrow_is_mapped_for_any_other_caller(self):
        assert smstext._sms_clean("LAX → MXP") == "LAX -> MXP"
