"""Inbound and outbound SMS: chunking, links, reactions.

Merged from test_sms_chunking.py, test_link_integrity.py, test_tapback.py, test_reaction_interpret.py; each section keeps its
original file's notes, because those carry the incident that led to the test.
"""
import pytest
from unittest.mock import patch, MagicMock
from palmer.smstext import _sms_clean
from palmer.sms_util import _split_for_sms, send_sms
from palmer import sms_util, smstext, tapback, main
from tests.helpers import llm_reply


# ============================================================================
# from test_sms_chunking.py
# ============================================================================

class TestSmsCleanNoTruncation:
    def test_short_text_unchanged(self):
        assert _sms_clean("hello there") == "hello there"

    def test_long_text_not_truncated(self):
        text = "This is a sentence. " * 100  # ~2100 chars
        assert len(_sms_clean(text)) == len(text.strip())

    def test_strips_bullets_and_markdown(self):
        text = "- first point\n* second point\n**bold**\n# Header"
        cleaned = _sms_clean(text)
        assert "-" not in cleaned.split("\n")[0][:1]
        assert "*" not in cleaned
        assert "#" not in cleaned


class TestSplitForSms:
    def test_short_text_single_part(self):
        assert _split_for_sms("short message") == ["short message"]

    def test_paragraph_split_preferred(self):
        text = "First topic here.\n\nSecond topic here." + ("x" * 900)
        parts = _split_for_sms(text)
        assert len(parts) == 2
        assert parts[0] == "First topic here."

    def test_hard_chunk_fallback_when_no_breaks(self):
        text = "a" * 2000  # no paragraph breaks at all
        parts = _split_for_sms(text, max_chars=900)
        assert len(parts) == 3
        assert all(len(p) <= 900 for p in parts)
        assert "".join(parts) == text

    def test_exactly_at_limit_single_part(self):
        text = "a" * 900
        assert _split_for_sms(text, max_chars=900) == [text]


class TestSendSmsChunking:
    def test_long_body_sent_as_multiple_messages(self, monkeypatch):
        monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15550000000")
        long_body = ("Weather update here.\n\n"
                     "Traffic is normal today.\n\n"
                     "News: " + "x" * 900)
        with patch("palmer.sms_util._twilio") as mock_twilio:
            mock_twilio.messages.create.return_value = MagicMock()
            result = send_sms("+15551234567", long_body)
        assert result is True
        assert mock_twilio.messages.create.call_count == 3
        bodies = [c.kwargs["body"] for c in mock_twilio.messages.create.call_args_list]
        assert bodies[0] == "Weather update here."
        assert bodies[1] == "Traffic is normal today."
        assert "News:" in bodies[2]

    def test_short_body_sent_as_one_message(self, monkeypatch):
        monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15550000000")
        with patch("palmer.sms_util._twilio") as mock_twilio:
            mock_twilio.messages.create.return_value = MagicMock()
            result = send_sms("+15551234567", "quick note")
        assert result is True
        assert mock_twilio.messages.create.call_count == 1
        assert mock_twilio.messages.create.call_args.kwargs["body"] == "quick note"

    def test_no_content_lost_across_chunks(self, monkeypatch):
        monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15550000000")
        long_body = "\n\n".join(f"Topic {i}: some detail about it." for i in range(20))
        with patch("palmer.sms_util._twilio") as mock_twilio:
            mock_twilio.messages.create.return_value = MagicMock()
            send_sms("+15551234567", long_body)
        bodies = [c.kwargs["body"] for c in mock_twilio.messages.create.call_args_list]
        assert "\n\n".join(bodies) == long_body


# ============================================================================
# from test_link_integrity.py
# ============================================================================
#
# A URL must survive the SMS pipeline byte for byte, or not be sent at all.
#
# "Bad links" turned out to be three separate mechanical defects, none of them in
# the code that chooses a link:
#
#   - the markdown scrub rewrote `[text](url)` to `text`, DELETING the target;
#   - the ASCII fold dropped non-ASCII bytes out of the middle of a path, leaving
#     a URL that looks right and resolves to nothing;
#   - three truncating paths (`shorten_message`, the `body[:320]` fallback, the
#     hard chunker) all cut at a fixed offset, which lands mid-URL on exactly the
#     messages most likely to carry one.
#
# And the /sms-status retry fed a live message back through the first and third of
# those. morning.py opted out of that callback by hand; nothing else did.

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
        from palmer import flightwatch
        route = flightwatch._route(
            {"origin": "LAX", "destination": "MXP", "outbound_date": "2026-09-10"})
        assert "LAX to MXP" in route
        # The bug: an unmapped arrow was dropped, leaving "LAX  MXP".
        assert smstext._sms_clean(route) == route
        assert "  " not in smstext._sms_clean(route)

    def test_the_arrow_is_mapped_for_any_other_caller(self):
        assert smstext._sms_clean("LAX → MXP") == "LAX -> MXP"


# ============================================================================
# from test_tapback.py
# ============================================================================
#
# Tests for inbound reaction handling.
#
# The core requirement is negative — Palmer must send NOTHING when someone
# reacts — so the handler tests assert on calls that must not happen.
#
# main.py calls _scheduler.start() at import, and send_due_reminders runs every
# minute and sends real SMS. Neutralize it before importing main; production code
# stays untouched.

class TestAppleTapbacks:
    @pytest.mark.parametrize("body,kind,sentiment", [
        ('Liked "The audacity of it."', "liked", "positive"),
        ('Loved "Peno on Clayton."', "loved", "positive"),
        ('Laughed at "Airport beer or airport spiral."', "laughed", "positive"),
        ('Emphasized "Get the short rib."', "emphasized", "positive"),
        ('Disliked "Every six months, same speech."', "disliked", "negative"),
        ('Questioned "It\'s Wednesday."', "questioned", "neutral"),
    ])
    def test_all_six_parse(self, body, kind, sentiment):
        r = tapback.parse_reaction(body)
        assert r is not None, f"{body!r} should parse as a reaction"
        assert r["kind"] == kind
        assert r["sentiment"] == sentiment

    def test_quoted_text_is_captured(self):
        r = tapback.parse_reaction('Liked "The audacity of it. Every single week."')
        assert r["quoted"] == "The audacity of it. Every single week."

    def test_curly_quotes(self):
        r = tapback.parse_reaction('Liked “the audacity of it”')
        assert r is not None and r["quoted"] == "the audacity of it"

    def test_truncated_original_keeps_text(self):
        r = tapback.parse_reaction('Liked "this is a long message that got cut off…"')
        assert r is not None
        assert not r["quoted"].endswith("…")

    def test_multiline_quote(self):
        r = tapback.parse_reaction('Liked "line one\nline two"')
        assert r is not None and "line two" in r["quoted"]

    def test_long_quote_is_truncated(self):
        r = tapback.parse_reaction('Liked "' + "x" * 500 + '"')
        assert len(r["quoted"]) <= tapback._QUOTE_TRUNCATE


class TestEmojiReactions:
    def test_ios18_reacted_form(self):
        r = tapback.parse_reaction('Reacted \U0001f602 to "that traffic take"')
        assert r["kind"] == "emoji"
        assert r["sentiment"] == "positive"
        assert r["quoted"] == "that traffic take"

    def test_google_messages_form(self):
        r = tapback.parse_reaction('\U0001f44d to "see you then"')
        assert r["kind"] == "emoji" and r["sentiment"] == "positive"

    def test_bare_emoji(self):
        r = tapback.parse_reaction("\U0001f44d")
        assert r["kind"] == "emoji" and r["quoted"] == ""

    def test_bare_negative_emoji(self):
        assert tapback.parse_reaction("\U0001f44e")["sentiment"] == "negative"

    def test_multiple_emoji_only(self):
        assert tapback.parse_reaction("\U0001f525\U0001f4af") is not None

    def test_emoji_with_variation_selector(self):
        assert tapback.parse_reaction("❤️") is not None


class TestNotReactions:
    """False positives are the expensive failure — a real message met with silence."""

    @pytest.mark.parametrize("body", [
        "Liked it",                                  # no quoted original
        "I liked that restaurant you mentioned",     # lowercase, mid-sentence
        "Loved the game last night",
        'He said "hello" to me',
        "Disliked by everyone apparently",
        "what's the weather",
        "\U0001f44d sounds good to me",              # emoji plus real content
        "thanks \U0001f602",
        "",
        "   ",
    ])
    def test_normal_messages_are_not_reactions(self, body):
        assert tapback.parse_reaction(body) is None, f"{body!r} wrongly parsed as a reaction"

    def test_none_body(self):
        assert tapback.parse_reaction(None) is None

    def test_is_emoji_only_rejects_empty(self):
        assert not tapback.is_emoji_only("")
        assert not tapback.is_emoji_only("   ")


class TestRecording:
    def test_appends_and_caps(self):
        stored = {}

        def _upsert(phone, updates):
            stored.update(updates)

        with patch.object(tapback, "get_profile", side_effect=lambda p: dict(stored)), \
             patch.object(tapback, "upsert_profile", side_effect=_upsert):
            for i in range(tapback.MAX_STORED_REACTIONS + 5):
                tapback.record_reaction("+15550001111", {
                    "kind": "liked", "sentiment": "positive",
                    "quoted": f"msg {i}", "emoji": "",
                })

        log = stored["reactions"]
        assert len(log) == tapback.MAX_STORED_REACTIONS, "rolling log must be capped"
        assert log[-1]["quoted"] == f"msg {tapback.MAX_STORED_REACTIONS + 4}", "keeps newest"

    def test_db_failure_is_swallowed(self):
        """A bookkeeping failure must never bubble up and trigger a reply."""
        with patch.object(tapback, "get_profile", side_effect=RuntimeError("db down")):
            tapback.record_reaction("+15550001111", {
                "kind": "liked", "sentiment": "positive", "quoted": "x", "emoji": "",
            })


class TestReactionBlock:
    def test_empty_without_reactions(self):
        assert tapback.reaction_block({}) == ""
        assert tapback.reaction_block({"reactions": []}) == ""
        assert tapback.reaction_block(None) == ""

    def test_renders_recent_reactions(self):
        block = tapback.reaction_block({"reactions": [
            {"kind": "liked", "sentiment": "positive", "quoted": "the audacity of it", "emoji": ""},
            {"kind": "disliked", "sentiment": "negative", "quoted": "long all-hands take", "emoji": ""},
        ]})
        assert "the audacity of it" in block
        assert "long all-hands take" in block

    def test_surfaced_by_build_system(self):
        from palmer import agent
        profile = {"name": "Mike", "reactions": [
            {"kind": "liked", "sentiment": "positive", "quoted": "the audacity of it", "emoji": ""},
        ]}
        with patch.object(agent, "get_profile", return_value=profile), \
             patch.object(agent, "get_user_watches", return_value=[]), \
             patch.object(agent, "get_user_price_watches", return_value=[]):
            out = agent._build_system("+15550001111")
        assert "HOW THEY'VE REACTED" in out
        assert "the audacity of it" in out


class TestHandlerSendsNothing:
    """The actual requirement: a reaction produces no outbound message at all."""

    def _run(self, body, media_url=None):
        with patch.object(main, "ensure_sms") as ensure, \
             patch.object(main, "send_sms") as send, \
             patch.object(main, "get_reply") as get_reply, \
             patch.object(main, "save_message"), \
             patch.object(main, "get_history", return_value=[]), \
             patch.object(main, "get_profile", return_value={"intro_sent": True}), \
             patch.object(main, "upsert_profile") as upsert, \
             patch.object(main, "record_reaction") as record:
            main._handle_sms("+15550001111", body, media_url)
            return ensure, send, get_reply, record, upsert

    def test_tapback_sends_nothing(self):
        ensure, send, get_reply, record, _ = self._run('Liked "the audacity of it"')
        ensure.assert_not_called()
        send.assert_not_called()
        get_reply.assert_not_called()
        record.assert_called_once()

    def test_no_fallback_sms_on_reaction(self):
        """_handle_sms fires FALLBACK_SMS when the inner call returns falsy."""
        ensure, _, _, _, _ = self._run("\U0001f44d")
        ensure.assert_not_called()

    def test_reaction_does_not_mark_intro_sent(self):
        _, _, _, _, upsert = self._run('Liked "hi"')
        upsert.assert_not_called()

    def test_normal_message_still_replies(self):
        with patch.object(main, "ensure_sms", return_value=True) as ensure, \
             patch.object(main, "get_reply", return_value=("sure thing", None)) as get_reply, \
             patch.object(main, "save_message"), \
             patch.object(main, "save_assistant_turn"), \
             patch.object(main, "get_history", return_value=[]), \
             patch.object(main, "get_profile", return_value={"intro_sent": True}), \
             patch.object(main, "upsert_profile"):
            main._handle_sms("+15550001111", "what's the weather", None)
        get_reply.assert_called_once()
        ensure.assert_called_once()

    def test_photo_is_never_a_reaction(self):
        """An emoji caption on an MMS must not swallow the photo."""
        _, _, get_reply, record, _ = self._run("\U0001f44d", media_url="http://x/img.jpg")
        record.assert_not_called()
        get_reply.assert_called_once()


# ============================================================================
# from test_reaction_interpret.py
# ============================================================================
#
# Tests for reaction interpretation and the reply/silence decision.
#
# Haiku is mocked throughout — these test the plumbing, the validation, and above
# all the failure behavior. The rule that matters: anything that goes wrong must
# land on silence, never on an unwanted text.
#
# Live model behavior (does Haiku actually call a thumbs-up on a question an
# "answer"?) is verified separately against the real API, not here.

LIKED = {"kind": "liked", "sentiment": "positive", "quoted": "want me to add that?", "emoji": ""}
THUMBS = {"kind": "emoji", "sentiment": "positive", "quoted": "", "emoji": "\U0001f44d"}


class TestInterpretVerdicts:
    @pytest.mark.parametrize("function,needs_reply", [
        ("answer", True),
        ("closer", False),
        ("applause", False),
        ("objection", False),
        ("emotional", False),
    ])
    def test_only_answer_needs_reply(self, function, needs_reply):
        payload = f'{{"function": "{function}", "sentiment": "positive", "about": "mornings"}}'
        with patch.object(tapback.client.messages, "create", return_value=llm_reply(payload)):
            v = tapback.interpret_reaction(THUMBS, "want me to add that to your morning?", {})
        assert v["function"] == function
        assert v["needs_reply"] is needs_reply

    def test_sentiment_is_taken_from_the_model_not_the_emoji_map(self):
        """A skull is 'neutral' to the static map; in context it can be praise."""
        skull = {"kind": "emoji", "sentiment": "neutral", "quoted": "", "emoji": "\U0001f480"}
        payload = '{"function": "applause", "sentiment": "positive", "about": "the joke"}'
        with patch.object(tapback.client.messages, "create", return_value=llm_reply(payload)):
            v = tapback.interpret_reaction(skull, "They always pick the day before the weekend.", {})
        assert v["sentiment"] == "positive"

    def test_about_is_captured_and_truncated(self):
        payload = '{"function": "objection", "sentiment": "negative", "about": "' + "x" * 200 + '"}'
        with patch.object(tapback.client.messages, "create", return_value=llm_reply(payload)):
            v = tapback.interpret_reaction(LIKED, "bitcoin is up 12%", {})
        assert len(v["about"]) <= 40


class TestInterpretFailsSafe:
    """Every failure path must produce silence."""

    def test_api_exception(self):
        with patch.object(tapback.client.messages, "create", side_effect=RuntimeError("boom")):
            v = tapback.interpret_reaction(THUMBS, "want me to add that?", {})
        assert v["needs_reply"] is False and v["function"] == "closer"

    def test_unparseable_response(self):
        with patch.object(tapback.client.messages, "create", return_value=llm_reply("not json at all")):
            v = tapback.interpret_reaction(THUMBS, "want me to add that?", {})
        assert v["needs_reply"] is False

    def test_invalid_function_value(self):
        with patch.object(tapback.client.messages, "create",
                          return_value=llm_reply('{"function": "vibes", "sentiment": "positive"}')):
            v = tapback.interpret_reaction(THUMBS, "want me to add that?", {})
        assert v["function"] == "closer" and v["needs_reply"] is False

    def test_invalid_sentiment_falls_back_to_parsed(self):
        with patch.object(tapback.client.messages, "create",
                          return_value=llm_reply('{"function": "closer", "sentiment": "spicy"}')):
            v = tapback.interpret_reaction(THUMBS, "hi", {})
        assert v["sentiment"] in ("positive", "negative", "neutral")

    def test_no_last_assistant_skips_the_model_entirely(self):
        """Nothing to answer means no question was asked — don't pay for a call."""
        with patch.object(tapback.client.messages, "create") as create:
            v = tapback.interpret_reaction(THUMBS, "", {})
        create.assert_not_called()
        assert v["needs_reply"] is False

    def test_empty_reaction(self):
        assert tapback.interpret_reaction({}, "hi", {})["needs_reply"] is False


class TestConsolidation:
    def _profile(self, n):
        return {"reactions": [
            {"kind": "liked", "sentiment": "positive", "quoted": f"m{i}", "about": ""}
            for i in range(n)
        ]}

    def test_does_not_fire_below_threshold(self):
        with patch.object(tapback, "get_profile",
                          return_value=self._profile(tapback.CONSOLIDATE_EVERY - 1)), \
             patch.object(tapback.client.messages, "create") as create, \
             patch.object(tapback, "upsert_profile") as upsert:
            tapback.maybe_consolidate("+15550001111")
        create.assert_not_called()
        upsert.assert_not_called()

    def test_fires_at_threshold_and_updates_style(self):
        stored = {}
        with patch.object(tapback, "get_profile", return_value=self._profile(tapback.CONSOLIDATE_EVERY)), \
             patch.object(tapback.client.messages, "create",
                          return_value=llm_reply('{"communication_style": "likes the dry stuff"}')), \
             patch.object(tapback, "upsert_profile", side_effect=lambda p, u: stored.update(u)):
            tapback.maybe_consolidate("+15550001111")
        assert stored["communication_style"] == "likes the dry stuff"
        assert stored["reactions_folded_count"] == tapback.CONSOLIDATE_EVERY

    def test_does_not_refire_until_another_batch(self):
        prof = self._profile(tapback.CONSOLIDATE_EVERY)
        prof["reactions_folded_count"] = tapback.CONSOLIDATE_EVERY
        with patch.object(tapback, "get_profile", return_value=prof), \
             patch.object(tapback.client.messages, "create") as create:
            tapback.maybe_consolidate("+15550001111")
        create.assert_not_called()

    def test_failure_is_swallowed(self):
        with patch.object(tapback, "get_profile", side_effect=RuntimeError("db down")):
            tapback.maybe_consolidate("+15550001111")


class TestHandlerRouting:
    """End-to-end through _handle_sms with the verdict controlled."""

    def _run(self, body, verdict, last_assistant="want me to add that to your morning?"):
        history = [{"role": "assistant", "content": last_assistant}] if last_assistant else []
        with patch.object(main, "ensure_sms", return_value=True) as ensure, \
             patch.object(main, "get_reply", return_value=("done, added", None)) as get_reply, \
             patch.object(main, "interpret_reaction", return_value=verdict), \
             patch.object(main, "record_reaction") as record, \
             patch.object(main, "learn_from_reactions") as consolidate, \
             patch.object(main, "save_message"), \
             patch.object(main, "save_assistant_turn"), \
             patch.object(main, "get_history", return_value=history), \
             patch.object(main, "get_profile", return_value={"intro_sent": True}), \
             patch.object(main, "upsert_profile"):
            main._handle_sms("+15550001111", body, None)
            return ensure, get_reply, record, consolidate

    def test_closer_stays_silent(self):
        ensure, get_reply, record, consolidate = self._run(
            'Liked "the audacity of it"',
            {"function": "closer", "sentiment": "positive", "about": "", "needs_reply": False},
        )
        ensure.assert_not_called()
        get_reply.assert_not_called()
        record.assert_called_once()
        consolidate.assert_called_once()

    def test_answer_gets_exactly_one_reply(self):
        ensure, get_reply, record, _ = self._run(
            "\U0001f44d",
            {"function": "answer", "sentiment": "positive", "about": "mornings", "needs_reply": True},
        )
        get_reply.assert_called_once()
        ensure.assert_called_once()
        record.assert_called_once()

    def test_answer_is_relabelled_for_the_model(self):
        """Palmer must see 'this is their answer', not a bare emoji."""
        with patch.object(main, "ensure_sms", return_value=True), \
             patch.object(main, "get_reply", return_value=("done", None)) as get_reply, \
             patch.object(main, "interpret_reaction",
                          return_value={"function": "answer", "sentiment": "positive",
                                        "about": "", "needs_reply": True}), \
             patch.object(main, "record_reaction"), \
             patch.object(main, "learn_from_reactions"), \
             patch.object(main, "save_message"), \
             patch.object(main, "save_assistant_turn"), \
             patch.object(main, "get_history",
                          return_value=[{"role": "assistant", "content": "want me to add that?"}]), \
             patch.object(main, "get_profile", return_value={"intro_sent": True}), \
             patch.object(main, "upsert_profile"):
            main._handle_sms("+15550001111", "\U0001f44d", None)
        sent = get_reply.call_args.kwargs["message"]
        assert "this is their answer" in sent
        assert "want me to add that?" in sent

    def test_objection_stays_silent(self):
        ensure, get_reply, _, _ = self._run(
            'Disliked "bitcoin is up 12%"',
            {"function": "objection", "sentiment": "negative", "about": "bitcoin", "needs_reply": False},
        )
        ensure.assert_not_called()
        get_reply.assert_not_called()


def _neg(topic, n):
    return [{"kind": "disliked", "sentiment": "negative", "function": "objection",
             "about": topic, "quoted": ""} for _ in range(n)]


class TestPacingFactor:
    def test_normal_with_no_reactions(self):
        assert tapback.pacing_factor({}) == 1.0
        assert tapback.pacing_factor({"reactions": []}) == 1.0

    def test_positives_do_not_slow_palmer_down(self):
        prof = {"reactions": [{"kind": "liked", "sentiment": "positive", "function": "applause"}] * 6}
        assert tapback.pacing_factor(prof) == 1.0

    def test_negatives_increase_the_factor(self):
        assert tapback.pacing_factor({"reactions": _neg("crypto", 2)}) > 1.0

    def test_capped(self):
        assert tapback.pacing_factor({"reactions": _neg("crypto", 50)}) == tapback.MAX_PACING_FACTOR

    def test_decays_as_positives_push_negatives_out(self):
        """The rolling log is the decay mechanism — no reset job needed."""
        heavy = tapback.pacing_factor({"reactions": _neg("crypto", 4)})
        recovered = tapback.pacing_factor({"reactions": _neg("crypto", 1)})
        assert recovered < heavy

    def test_followup_gap_stretches(self):
        from palmer import followup
        from datetime import datetime, timedelta
        base = {"morning_onboarded": True, "timezone": "America/Chicago",
                "ongoing_threads": ["job offer"]}
        short = followup.GAP_DAYS - 1
        recent = (datetime.now() - timedelta(days=short)).date().isoformat()
        with patch.object(followup, "_local_now",
                          return_value=datetime.now().replace(hour=15)):
            calm = dict(base, followup_sent_date=recent, reactions=[])
            noisy = dict(base, followup_sent_date=recent, reactions=_neg("checkins", 4))
            assert followup._should_send_followup(calm) is False
            assert followup._should_send_followup(noisy) is False

        due = (datetime.now() - timedelta(days=followup.GAP_DAYS + 1)).date().isoformat()
        with patch.object(followup, "_local_now",
                          return_value=datetime.now().replace(hour=15)):
            calm = dict(base, followup_sent_date=due, reactions=[])
            noisy = dict(base, followup_sent_date=due, reactions=_neg("checkins", 4))
            assert followup._should_send_followup(calm) is True, "normal user is due"
            assert followup._should_send_followup(noisy) is False, "backed-off user is not"


class TestPreferenceLearning:
    def _run(self, log, existing_prefs=None, topics=("crypto", "sports", "weather")):
        stored = {}
        prof = {"reactions": log, "morning_topics": list(topics)}
        if existing_prefs is not None:
            prof["morning_prefs"] = existing_prefs
        with patch.object(tapback, "get_profile", return_value=prof), \
             patch.object(tapback, "upsert_profile", side_effect=lambda p, u: stored.update(u)):
            tapback.maybe_learn_preferences("+15550001111")
        return stored

    def test_one_dislike_changes_nothing(self):
        assert self._run(_neg("crypto", 1)) == {}

    def test_two_dislikes_change_nothing(self):
        assert self._run(_neg("crypto", 2)) == {}

    def test_threshold_adds_to_avoid(self):
        stored = self._run(_neg("crypto", tapback.NEGATIVE_STREAK_FOR_AVOID))
        assert "crypto" in stored["morning_prefs"]["avoid"]

    def test_sets_a_notice_so_it_is_not_silent(self):
        stored = self._run(_neg("crypto", tapback.NEGATIVE_STREAK_FOR_AVOID))
        assert stored["pending_preference_notice"] == "crypto"

    def test_preserves_existing_avoid_entries(self):
        stored = self._run(_neg("crypto", tapback.NEGATIVE_STREAK_FOR_AVOID),
                           existing_prefs={"avoid": ["sports"]})
        assert set(stored["morning_prefs"]["avoid"]) == {"sports", "crypto"}

    def test_does_not_duplicate(self):
        assert self._run(_neg("crypto", tapback.NEGATIVE_STREAK_FOR_AVOID),
                         existing_prefs={"avoid": ["crypto"]}) == {}

    def test_spread_across_topics_does_not_trigger(self):
        log = _neg("crypto", 1) + _neg("sports", 1) + _neg("weather", 1)
        assert self._run(log) == {}

    def test_blank_topic_ignored(self):
        assert self._run(_neg("", 5)) == {}

    def test_topic_not_in_the_briefing_is_ignored(self):
        """Only things Palmer actually sends can be dropped from what he sends."""
        assert self._run(_neg("someone's dog", tapback.NEGATIVE_STREAK_FOR_AVOID)) == {}

    def test_no_briefing_topics_means_nothing_to_drop(self):
        assert self._run(_neg("crypto", tapback.NEGATIVE_STREAK_FOR_AVOID), topics=()) == {}

    def test_matching_is_case_insensitive(self):
        stored = self._run(_neg("CRYPTO", tapback.NEGATIVE_STREAK_FOR_AVOID))
        assert "crypto" in [a.lower() for a in stored["morning_prefs"]["avoid"]]

    def test_stores_the_original_topic_casing(self):
        """morning.py matches against morning_topics, so casing must round-trip."""
        stored = self._run(_neg("bitcoin", tapback.NEGATIVE_STREAK_FOR_AVOID),
                           topics=("Bitcoin",))
        assert "Bitcoin" in stored["morning_prefs"]["avoid"]

    def test_failure_is_swallowed(self):
        with patch.object(tapback, "get_profile", side_effect=RuntimeError("db down")):
            tapback.maybe_learn_preferences("+15550001111")


class TestNoticeSurfacedAndCleared:
    def test_notice_appears_in_prompt(self):
        from palmer import agent
        with patch.object(agent, "get_profile",
                          return_value={"pending_preference_notice": "crypto"}), \
             patch.object(agent, "get_user_watches", return_value=[]), \
             patch.object(agent, "get_user_price_watches", return_value=[]):
            out = agent._build_system("+15550001111")
        assert "crypto" in out
        assert "thumbs-downed" in out

    def test_notice_absent_when_unset(self):
        from palmer import agent
        with patch.object(agent, "get_profile", return_value={"name": "Mike"}), \
             patch.object(agent, "get_user_watches", return_value=[]), \
             patch.object(agent, "get_user_price_watches", return_value=[]):
            out = agent._build_system("+15550001111")
        assert "thumbs-downed" not in out

    def test_notice_is_cleared_after_being_shown(self):
        """Otherwise Palmer mentions the dropped topic every single turn."""
        from palmer import agent
        stored = {}
        with patch.object(agent, "_update_profile"), \
             patch.object(agent, "_consolidate_history"), \
             patch.object(agent, "upsert_profile", side_effect=lambda p, u: stored.update(u)), \
             patch.object(agent, "get_profile", return_value={}):
            agent._profile_and_consolidate("+15550001111", "ok", "sure", None, "crypto")
        assert stored["pending_preference_notice"] is None


class TestWatchCapLookupIsPerUser:
    """run_watches covers every watch for every user and get_profile opens a
    fresh DB connection per call — the cap lookup must not be inside the loop."""

    def test_profile_read_once_per_user_not_per_watch(self):
        from palmer import watches
        rows = [{"phone": "+1555000111" + str(i % 2), "id": i, "last_alerted": None,
                 "cooldown_hours": 4, "daily_alert_date": None, "daily_alert_count": 0,
                 "queries": "[]", "description": "x", "genre": None, "story_state": None}
                for i in range(6)]  # 6 watches spread across 2 users

        with patch.object(watches, "get_active_watches", return_value=rows), \
             patch.object(watches, "get_profile", return_value={}) as gp, \
             patch.object(watches, "_search_raw", return_value=[]):
            watches.run_watches()

        assert gp.call_count == 2, f"expected 1 profile read per user, got {gp.call_count}"

    def test_profile_failure_falls_back_to_default_cap(self):
        from palmer import watches
        rows = [{"phone": "+15550001111", "id": 1, "last_alerted": None,
                 "cooldown_hours": 4, "daily_alert_date": None, "daily_alert_count": 0,
                 "queries": "[]", "description": "x", "genre": None, "story_state": None}]
        with patch.object(watches, "get_active_watches", return_value=rows), \
             patch.object(watches, "get_profile", side_effect=RuntimeError("db down")), \
             patch.object(watches, "_search_raw", return_value=[]):
            watches.run_watches()  # must not raise
