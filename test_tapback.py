"""Tests for inbound reaction handling.

The core requirement is negative — Palmer must send NOTHING when someone
reacts — so the handler tests assert on calls that must not happen.

main.py calls _scheduler.start() at import, and send_due_reminders runs every
minute and sends real SMS. Neutralize it before importing main; production code
stays untouched.
"""
from unittest.mock import patch

import pytest

import tapback

with patch("apscheduler.schedulers.background.BackgroundScheduler.start"):
    import main


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
        import agent
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
