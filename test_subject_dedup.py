"""Tests for cross-job subject-dedup: userprofile._is_duplicate_subject and its wiring
into watches.py. Pure logic + mocked LLM/DB — no real network or LLM calls.
Run: pytest test_subject_dedup.py"""
from dotenv import load_dotenv
load_dotenv()

from unittest.mock import patch, MagicMock

import userprofile
import watches


def _haiku_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    return resp


class TestIsDuplicateSubject:
    def test_no_recent_messages_returns_false_without_llm_call(self):
        with patch("db.get_recent_assistant_messages", return_value=[]), \
             patch("userprofile.client") as mock_client:
            assert userprofile._is_duplicate_subject("+15551234567", "new message") is False
        mock_client.messages.create.assert_not_called()

    def test_haiku_says_yes_returns_true(self):
        with patch("db.get_recent_assistant_messages", return_value=["Hurts practice update"]), \
             patch("userprofile.client") as mock_client:
            mock_client.messages.create.return_value = _haiku_response("YES")
            assert userprofile._is_duplicate_subject("+15551234567", "Hurts camp footage") is True

    def test_haiku_says_no_returns_false(self):
        with patch("db.get_recent_assistant_messages", return_value=["weather update"]), \
             patch("userprofile.client") as mock_client:
            mock_client.messages.create.return_value = _haiku_response("NO")
            assert userprofile._is_duplicate_subject("+15551234567", "bitcoin price") is False

    def test_llm_failure_fails_open(self):
        with patch("db.get_recent_assistant_messages", return_value=["something"]), \
             patch("userprofile.client") as mock_client:
            mock_client.messages.create.side_effect = Exception("API down")
            assert userprofile._is_duplicate_subject("+15551234567", "new message") is False


def _watch(**overrides):
    base = {
        "id": 1, "phone": "+15551234567", "description": "Eagles Hurts injury",
        "queries": ["Jalen Hurts injury"], "cooldown_hours": 4,
        "last_alerted": None, "last_alert_summary": None,
        "daily_alert_count": 0, "daily_alert_date": None, "recent_summaries": [],
    }
    base.update(overrides)
    return base


_RAW_RESULTS = [
    {"title": "Hurts injury update", "url": "https://example1.com/a",
     "content": "details", "published_date": "2026-08-13", "score": 1.0},
    {"title": "Hurts injury update 2", "url": "https://example2.com/b",
     "content": "more details", "published_date": "2026-08-13", "score": 1.0},
]


class TestWatchesSubjectDedup:
    def test_skips_send_when_duplicate_subject(self):
        with patch("watches.get_active_watches", return_value=[_watch()]), \
             patch("watches._search_raw", return_value=_RAW_RESULTS), \
             patch("watches._check_watch_hit", return_value=True), \
             patch("watches._best_result", return_value=_RAW_RESULTS[0]), \
             patch("watches._is_duplicate_subject", return_value=True) as dedup, \
             patch("watches.claim_watch_alert") as mock_claim, \
             patch("watches.update_watch_alerted") as mock_update, \
             patch("sms_util.send_sms") as mock_send:
            watches.run_watches()
        dedup.assert_called_once()
        mock_claim.assert_not_called()
        mock_send.assert_not_called()
        mock_update.assert_not_called()

    def test_sends_normally_when_not_duplicate(self):
        with patch("watches.get_active_watches", return_value=[_watch()]), \
             patch("watches._search_raw", return_value=_RAW_RESULTS), \
             patch("watches._check_watch_hit", return_value=True), \
             patch("watches._best_result", return_value=_RAW_RESULTS[0]), \
             patch("watches._is_duplicate_subject", return_value=False), \
             patch("watches.claim_watch_alert", return_value=True), \
             patch("watches.update_watch_alerted") as mock_update, \
             patch("sms_util.send_sms") as mock_send:
            watches.run_watches()
        mock_send.assert_called_once()
        mock_update.assert_called_once()

    def test_alert_persists_the_fired_url_and_domain(self):
        """The 'Watching' page section links a watch to the article
        that fired it — update_watch_alerted must be called with that URL/domain,
        not just the dedup title."""
        with patch("watches.get_active_watches", return_value=[_watch()]), \
             patch("watches._search_raw", return_value=_RAW_RESULTS), \
             patch("watches._check_watch_hit", return_value=True), \
             patch("watches._best_result", return_value=_RAW_RESULTS[0]), \
             patch("watches._is_duplicate_subject", return_value=False), \
             patch("watches.claim_watch_alert", return_value=True), \
             patch("watches.update_watch_alerted") as mock_update, \
             patch("sms_util.send_sms"):
            watches.run_watches()
        _, kwargs = mock_update.call_args
        assert kwargs["url"] == "https://example1.com/a"
        assert kwargs["domain"] == "example1.com"
