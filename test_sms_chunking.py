from dotenv import load_dotenv
load_dotenv()

from unittest.mock import patch, MagicMock

from agent import _sms_clean
from sms_util import _split_for_sms, send_sms, FALLBACK_SMS


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
        with patch("sms_util._twilio") as mock_twilio:
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
        with patch("sms_util._twilio") as mock_twilio:
            mock_twilio.messages.create.return_value = MagicMock()
            result = send_sms("+15551234567", "quick note")
        assert result is True
        assert mock_twilio.messages.create.call_count == 1
        assert mock_twilio.messages.create.call_args.kwargs["body"] == "quick note"

    def test_no_content_lost_across_chunks(self, monkeypatch):
        monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15550000000")
        long_body = "\n\n".join(f"Topic {i}: some detail about it." for i in range(20))
        with patch("sms_util._twilio") as mock_twilio:
            mock_twilio.messages.create.return_value = MagicMock()
            send_sms("+15551234567", long_body)
        bodies = [c.kwargs["body"] for c in mock_twilio.messages.create.call_args_list]
        assert "\n\n".join(bodies) == long_body
