"""Tests for cross-job subject-dedup: userprofile._is_duplicate_subject and its wiring
into watches.py and followup.py. Pure logic + mocked LLM/DB — no real network
or LLM calls. Run: pytest test_subject_dedup.py"""

from unittest.mock import patch, MagicMock

from palmer import userprofile
from palmer import watches
from palmer import followup


def _haiku_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    return resp


class TestIsDuplicateSubject:
    def test_no_recent_messages_returns_false_without_llm_call(self):
        with patch("palmer.db.get_recent_assistant_messages", return_value=[]), \
             patch("palmer.userprofile.client") as mock_client:
            assert userprofile._is_duplicate_subject("+15551234567", "new message") is False
        mock_client.messages.create.assert_not_called()

    def test_haiku_says_yes_returns_true(self):
        with patch("palmer.db.get_recent_assistant_messages", return_value=["Hurts practice update"]), \
             patch("palmer.userprofile.client") as mock_client:
            mock_client.messages.create.return_value = _haiku_response("YES")
            assert userprofile._is_duplicate_subject("+15551234567", "Hurts camp footage") is True

    def test_haiku_says_no_returns_false(self):
        with patch("palmer.db.get_recent_assistant_messages", return_value=["weather update"]), \
             patch("palmer.userprofile.client") as mock_client:
            mock_client.messages.create.return_value = _haiku_response("NO")
            assert userprofile._is_duplicate_subject("+15551234567", "bitcoin price") is False

    def test_llm_failure_fails_open(self):
        with patch("palmer.db.get_recent_assistant_messages", return_value=["something"]), \
             patch("palmer.userprofile.client") as mock_client:
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
        with patch("palmer.watches.get_active_watches", return_value=[_watch()]), \
             patch("palmer.watches._search_raw", return_value=_RAW_RESULTS), \
             patch("palmer.watches._check_watch_hit", return_value=True), \
             patch("palmer.watches._best_result", return_value=_RAW_RESULTS[0]), \
             patch("palmer.watches._is_duplicate_subject", return_value=True) as dedup, \
             patch("palmer.watches.claim_watch_alert") as mock_claim, \
             patch("palmer.watches.update_watch_alerted") as mock_update, \
             patch("palmer.sms_util.send_sms") as mock_send:
            watches.run_watches()
        dedup.assert_called_once()
        mock_claim.assert_not_called()
        mock_send.assert_not_called()
        mock_update.assert_not_called()

    def test_sends_normally_when_not_duplicate(self):
        with patch("palmer.watches.get_active_watches", return_value=[_watch()]), \
             patch("palmer.watches._search_raw", return_value=_RAW_RESULTS), \
             patch("palmer.watches._check_watch_hit", return_value=True), \
             patch("palmer.watches._best_result", return_value=_RAW_RESULTS[0]), \
             patch("palmer.watches._is_duplicate_subject", return_value=False), \
             patch("palmer.watches.claim_watch_alert", return_value=True), \
             patch("palmer.watches._draft_alert", return_value="Hurts is out. https://example.com/a"), \
             patch("palmer.watches.update_watch_alerted") as mock_update, \
             patch("palmer.sms_util.send_sms") as mock_send:
            watches.run_watches()
        mock_send.assert_called_once()
        mock_update.assert_called_once()
        # What goes out is the drafted line, not the raw headline.
        assert mock_send.call_args[0][1] == "Hurts is out. https://example.com/a"

    def test_alert_persists_the_fired_url_and_domain(self):
        """The 'Watching' page section links a watch to the article
        that fired it — update_watch_alerted must be called with that URL/domain,
        not just the dedup title."""
        with patch("palmer.watches.get_active_watches", return_value=[_watch()]), \
             patch("palmer.watches._search_raw", return_value=_RAW_RESULTS), \
             patch("palmer.watches._check_watch_hit", return_value=True), \
             patch("palmer.watches._best_result", return_value=_RAW_RESULTS[0]), \
             patch("palmer.watches._is_duplicate_subject", return_value=False), \
             patch("palmer.watches.claim_watch_alert", return_value=True), \
             patch("palmer.watches._draft_alert", return_value="drafted line https://example.com/a"), \
             patch("palmer.watches.update_watch_alerted") as mock_update, \
             patch("palmer.sms_util.send_sms"):
            watches.run_watches()
        _, kwargs = mock_update.call_args
        assert kwargs["url"] == "https://example1.com/a"
        assert kwargs["domain"] == "example1.com"


class TestFollowupSubjectDedup:
    def _profile(self):
        return {"morning_onboarded": True, "timezone": "America/Chicago",
                "ongoing_threads": ["a big life event"]}

    def test_skips_send_and_releases_claim_when_duplicate(self):
        with patch("palmer.followup.get_all_profiles",
                   return_value=[("+15551234567", self._profile())]), \
             patch("palmer.followup._should_send_followup", return_value=True), \
             patch("palmer.followup.claim_daily_guard", return_value=True), \
             patch("palmer.followup.get_history", return_value=[]), \
             patch("palmer.followup._candidates", return_value=[followup.Subject("thread", "a big life event")]), \
             patch("palmer.followup._pick_subject", return_value=followup.Subject("thread", "a big life event")), \
             patch("palmer.followup._draft_followup", return_value="hey how'd that go?"), \
             patch("palmer.followup._is_duplicate_subject", return_value=True) as dedup, \
             patch("palmer.followup.upsert_profile") as mock_upsert, \
             patch("palmer.followup._local_today") as mock_today, \
             patch("palmer.sms_util.send_sms") as mock_send:
            mock_today.return_value.isoformat.return_value = "2026-08-13"
            followup.run_followups()
        dedup.assert_called_once()
        mock_send.assert_not_called()
        assert any(
            c.args[1] == {"followup_sent_date": None} for c in mock_upsert.call_args_list
        ), "expected the daily claim to be released"

    def test_sends_normally_when_not_duplicate(self):
        with patch("palmer.followup.get_all_profiles",
                   return_value=[("+15551234567", self._profile())]), \
             patch("palmer.followup._should_send_followup", return_value=True), \
             patch("palmer.followup.claim_daily_guard", return_value=True), \
             patch("palmer.followup.get_history", return_value=[]), \
             patch("palmer.followup._candidates", return_value=[followup.Subject("thread", "a big life event")]), \
             patch("palmer.followup._pick_subject", return_value=followup.Subject("thread", "a big life event")), \
             patch("palmer.followup._draft_followup", return_value="hey how'd that go?"), \
             patch("palmer.followup._is_duplicate_subject", return_value=False), \
             patch("palmer.followup.save_message") as mock_save, \
             patch("palmer.followup._local_today") as mock_today, \
             patch("palmer.sms_util.send_sms") as mock_send:
            mock_today.return_value.isoformat.return_value = "2026-08-13"
            followup.run_followups()
        mock_send.assert_called_once()
        mock_save.assert_called_once()
