"""Tests for the news-quality gate in watches.corroborated() and its use in
alerts.run_alert_checks. Same shared gate as watches.py; unprompted daily
pushes have to clear it just like user-created watches do."""
from unittest.mock import patch

import alerts
from watches import corroborated


def _r(url: str, score: float = 0.7) -> dict:
    return {"url": url, "title": "t", "content": "c", "score": score,
            "published_date": "Tue, 18 Aug 2026 12:00:00 GMT"}


class TestCorroborated:
    def test_two_distinct_domains_pass(self):
        results = [_r("https://something.example/a"), _r("https://other.example/b")]
        assert corroborated(results)

    def test_single_tier1_passes_alone(self):
        # apnews.com is tier 1 in trusted_sources.json
        assert corroborated([_r("https://apnews.com/story/xyz")])

    def test_gov_domain_counts_as_tier1(self):
        # .gov is tier-1 via _source_tier
        assert corroborated([_r("https://weather.gov/warning/123")])

    def test_single_unknown_domain_fails(self):
        assert not corroborated([_r("https://rumor.example/x")])

    def test_multiple_urls_same_canonical_domain_fails(self):
        results = [
            _r("https://rumor.example/one"),
            _r("https://rumor.example/two"),
            _r("https://sub.rumor.example/three"),
        ]
        assert not corroborated(results)

    def test_empty_fails(self):
        assert not corroborated([])


class TestAlertsGate:
    """alerts.run_alert_checks calls corroborated() before scoring; if the gate
    fails, no Haiku significance call, no send, and the daily claim is released."""

    def _profile(self):
        return {"morning_onboarded": True, "timezone": "America/Chicago"}

    def _run_with(self, raw):
        with patch("alerts.get_all_profiles",
                   return_value=[("+15551234567", self._profile())]), \
             patch("alerts._in_alert_window", return_value=True), \
             patch("alerts.claim_daily_guard", return_value=True), \
             patch("alerts._get_alert_queries", return_value=["some query"]), \
             patch("alerts._search_raw", return_value=raw), \
             patch("alerts._check_significance") as mock_sig, \
             patch("alerts.upsert_profile") as mock_upsert, \
             patch("sms_util.send_sms") as mock_send:
            alerts.run_alert_checks()
        return mock_sig, mock_upsert, mock_send

    def test_single_unknown_source_blocks_send_and_releases_claim(self):
        mock_sig, mock_upsert, mock_send = self._run_with([_r("https://rumor.example/x")])
        mock_sig.assert_not_called()
        mock_send.assert_not_called()
        assert any(c.args[1] == {"alert_sent_date": None} for c in mock_upsert.call_args_list), \
            "daily claim should be released so retry can happen tomorrow"

    def test_tier1_passes_gate_and_reaches_significance(self):
        mock_sig, _, _ = self._run_with([_r("https://apnews.com/story/big")])
        mock_sig.assert_called_once()

    def test_two_domains_pass_gate(self):
        mock_sig, _, _ = self._run_with([
            _r("https://a.example/x"),
            _r("https://b.example/y"),
        ])
        mock_sig.assert_called_once()
