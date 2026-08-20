"""Tests for _user_already_covered — suppresses proactive sends when the user
already brought up the same story themselves in their recent messages."""
from unittest.mock import patch, MagicMock

import userprofile


def _haiku_reply(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


class TestUserAlreadyCovered:
    def test_no_recent_user_messages_returns_false_without_haiku(self):
        with patch("db.get_recent_user_messages", return_value=[]), \
             patch("userprofile.client") as mock_client:
            assert userprofile._user_already_covered("+15550000000", "Iran launched strikes") is False
            mock_client.messages.create.assert_not_called()

    def test_yes_reply_suppresses(self):
        with patch("db.get_recent_user_messages", return_value=[
            "did you see the Iran thing?",
            "wild what's happening over there",
        ]), patch("userprofile.client") as mock_client:
            mock_client.messages.create.return_value = _haiku_reply("YES")
            assert userprofile._user_already_covered(
                "+15550000000",
                "Iran launched missiles at a US base in Iraq."
            ) is True

    def test_no_reply_allows_send(self):
        with patch("db.get_recent_user_messages", return_value=[
            "grocery list: milk, bread",
            "what's the weather tomorrow?",
        ]), patch("userprofile.client") as mock_client:
            mock_client.messages.create.return_value = _haiku_reply("NO")
            assert userprofile._user_already_covered(
                "+15550000000",
                "Cardinals move into first place with 4-2 win."
            ) is False

    def test_haiku_failure_fails_open(self):
        """Fail-open semantics — a broken Haiku call must NOT silently suppress
        real alerts. Better to send a possibly-duplicate than to drop a real one."""
        with patch("db.get_recent_user_messages", return_value=["did you see that"]), \
             patch("userprofile.client") as mock_client:
            mock_client.messages.create.side_effect = RuntimeError("boom")
            assert userprofile._user_already_covered("+15550000000", "some alert") is False

    def test_prompt_shape_includes_recent_and_candidate(self):
        captured = []

        def _create(**kwargs):
            captured.append(kwargs["messages"][0]["content"])
            return _haiku_reply("NO")

        with patch("db.get_recent_user_messages",
                   return_value=["did you see the Iran thing", "crazy"]), \
             patch("userprofile.client") as mock_client:
            mock_client.messages.create.side_effect = _create
            userprofile._user_already_covered("+15550000000", "Iran launched missiles.")
        prompt = captured[0]
        assert "Iran launched missiles" in prompt
        assert "Iran thing" in prompt
        # Prompt should distinguish specific-event awareness from general-topic mention
        low = prompt.lower()
        assert "did you see" in low or "have you heard" in low
        assert "general topic" in low or "background chatter" in low

    def test_window_hours_default_is_12(self):
        """The 12h default is intentional — user mentioning a story in the morning
        should suppress an afternoon alert on the same story."""
        # Check the signature default without invoking Haiku
        import inspect
        sig = inspect.signature(userprofile._user_already_covered)
        assert sig.parameters["window_hours"].default == 12
