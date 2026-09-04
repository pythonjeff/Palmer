"""Tests for cross-job subject-dedup: userprofile._is_duplicate_subject and its wiring
into watches.py, alerts.py, and followup.py. Pure logic + mocked LLM/DB — no real
network or LLM calls. Run: pytest test_subject_dedup.py"""
from dotenv import load_dotenv
load_dotenv()

from unittest.mock import patch, MagicMock

import userprofile
import watches
import alerts
import followup


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
             patch("watches._draft_alert", return_value="Hurts is out. https://example.com/a"), \
             patch("watches.update_watch_alerted") as mock_update, \
             patch("sms_util.send_sms") as mock_send:
            watches.run_watches()
        mock_send.assert_called_once()
        mock_update.assert_called_once()
        # What goes out is the drafted line, not the raw headline.
        assert mock_send.call_args[0][1] == "Hurts is out. https://example.com/a"

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
             patch("watches._draft_alert", return_value="drafted line https://example.com/a"), \
             patch("watches.update_watch_alerted") as mock_update, \
             patch("sms_util.send_sms"):
            watches.run_watches()
        _, kwargs = mock_update.call_args
        assert kwargs["url"] == "https://example1.com/a"
        assert kwargs["domain"] == "example1.com"


class TestAlertsSubjectDedup:
    def _profile(self):
        return {"morning_onboarded": True, "timezone": "America/Chicago"}

    def test_skips_send_and_releases_claim_when_duplicate(self):
        with patch("alerts.get_all_profiles",
                   return_value=[("+15551234567", self._profile())]), \
             patch("alerts._in_alert_window", return_value=True), \
             patch("alerts.claim_daily_guard", return_value=True), \
             patch("alerts._get_alert_queries", return_value=["some query"]), \
             patch("alerts._search_raw", return_value=[
                 {"url": "https://apnews.com/x", "title": "t", "content": "c", "published_date": "Tue, 18 Aug 2026 12:00:00 GMT"},
                 {"url": "https://reuters.com/y", "title": "t", "content": "c", "published_date": "Tue, 18 Aug 2026 12:00:00 GMT"},
             ]), \
             patch("alerts._check_significance", return_value=(9, "Big breaking news")), \
             patch("alerts._draft_alert", return_value="Breaking: big news happened"), \
             patch("alerts._is_duplicate_subject", return_value=True) as dedup, \
             patch("alerts.upsert_profile") as mock_upsert, \
             patch("sms_util.send_sms") as mock_send:
            alerts.run_alert_checks()
        dedup.assert_called_once()
        mock_send.assert_not_called()
        assert any(
            c.args[1] == {"alert_sent_date": None} for c in mock_upsert.call_args_list
        ), "expected the daily claim to be released"

    def test_sends_normally_when_not_duplicate(self):
        with patch("alerts.get_all_profiles",
                   return_value=[("+15551234567", self._profile())]), \
             patch("alerts._in_alert_window", return_value=True), \
             patch("alerts.claim_daily_guard", return_value=True), \
             patch("alerts._get_alert_queries", return_value=["some query"]), \
             patch("alerts._search_raw", return_value=[
                 {"url": "https://apnews.com/x", "title": "t", "content": "c", "published_date": "Tue, 18 Aug 2026 12:00:00 GMT"},
                 {"url": "https://reuters.com/y", "title": "t", "content": "c", "published_date": "Tue, 18 Aug 2026 12:00:00 GMT"},
             ]), \
             patch("alerts._check_significance", return_value=(9, "Big breaking news")), \
             patch("alerts._draft_alert", return_value="Breaking: big news happened"), \
             patch("alerts._is_duplicate_subject", return_value=False), \
             patch("alerts.save_message") as mock_save, \
             patch("sms_util.send_sms") as mock_send:
            alerts.run_alert_checks()
        mock_send.assert_called_once()
        mock_save.assert_called_once()


class TestFollowupSubjectDedup:
    def _profile(self):
        return {"morning_onboarded": True, "timezone": "America/Chicago",
                "ongoing_threads": ["a big life event"]}

    def test_skips_send_and_releases_claim_when_duplicate(self):
        with patch("followup.get_all_profiles",
                   return_value=[("+15551234567", self._profile())]), \
             patch("followup._should_send_followup", return_value=True), \
             patch("followup.claim_daily_guard", return_value=True), \
             patch("followup.get_history", return_value=[]), \
             patch("followup._pick_thread", return_value="a big life event"), \
             patch("followup._draft_followup", return_value="hey how'd that go?"), \
             patch("followup._is_duplicate_subject", return_value=True) as dedup, \
             patch("followup.upsert_profile") as mock_upsert, \
             patch("followup._local_today") as mock_today, \
             patch("sms_util.send_sms") as mock_send:
            mock_today.return_value.isoformat.return_value = "2026-08-13"
            followup.run_followups()
        dedup.assert_called_once()
        mock_send.assert_not_called()
        assert any(
            c.args[1] == {"followup_sent_date": None} for c in mock_upsert.call_args_list
        ), "expected the daily claim to be released"

    def test_sends_normally_when_not_duplicate(self):
        with patch("followup.get_all_profiles",
                   return_value=[("+15551234567", self._profile())]), \
             patch("followup._should_send_followup", return_value=True), \
             patch("followup.claim_daily_guard", return_value=True), \
             patch("followup.get_history", return_value=[]), \
             patch("followup._pick_thread", return_value="a big life event"), \
             patch("followup._draft_followup", return_value="hey how'd that go?"), \
             patch("followup._is_duplicate_subject", return_value=False), \
             patch("followup.save_message") as mock_save, \
             patch("followup._local_today") as mock_today, \
             patch("sms_util.send_sms") as mock_send:
            mock_today.return_value.isoformat.return_value = "2026-08-13"
            followup.run_followups()
        mock_send.assert_called_once()
        mock_save.assert_called_once()
