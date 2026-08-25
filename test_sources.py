"""Tests for sources.py — the one place that decides which news Palmer repeats.

Every news surface (watch alerts, the morning briefing, Palmer Home, and the
conversation search) reaches this module through datafeeds._search_raw, so a
regression here is a regression everywhere at once. The integration class at
the bottom pins that wiring; the rest pin the gate itself.

trusted_sources.json is meant to be hand-edited without touching code, which
makes TestSourceListIntegrity load-bearing rather than pedantic — a stray
"https://" or a domain listed in both the allowlist and the blocklist would
otherwise fail silently at runtime.
"""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import sources
import datafeeds


def _fresh(hours_ago: float = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _r(url: str, score: float = 0.7, hours_ago: float = 1) -> dict:
    return {"url": url, "title": "t", "content": "c", "score": score,
            "published_date": _fresh(hours_ago)}


class TestIsBlocked:
    def test_press_release_wire_blocked(self):
        assert sources.is_blocked("https://www.prnewswire.com/news-releases/thing-123.html")
        assert sources.is_blocked("https://globenewswire.com/x")

    def test_republishing_aggregator_blocked(self):
        assert sources.is_blocked("https://www.msn.com/en-us/news/other/story")
        assert sources.is_blocked("https://biztoc.com/x/abc")
        assert sources.is_blocked("https://news.google.com/articles/xyz")

    def test_subdomain_of_blocked_host_blocked(self):
        assert sources.is_blocked("https://ir.globenewswire.com/x")

    def test_real_newsroom_not_blocked(self):
        assert not sources.is_blocked("https://www.reuters.com/world/x")

    def test_lookalike_suffix_not_blocked(self):
        """notmsn.com must not match msn.com — suffix matching is on a dot boundary."""
        assert not sources.is_blocked("https://notmsn.com/x")

    def test_malformed_and_empty_are_not_blocked(self):
        assert not sources.is_blocked("")
        assert not sources.is_blocked("not a url")


class TestSourceTier:
    def test_tier1_newsroom(self):
        assert sources.source_tier("https://www.reuters.com/world/x") == 1

    def test_tier2_outlet(self):
        assert sources.source_tier("https://www.theverge.com/x") == 2

    def test_unknown_is_tier3(self):
        assert sources.source_tier("https://contentfarm.example/x") == 3

    def test_gov_and_edu_are_tier1(self):
        assert sources.source_tier("https://www.weather.gov/alert/1") == 1
        assert sources.source_tier("https://news.mit.edu/2026/x") == 1

    def test_subdomain_inherits_tier(self):
        assert sources.source_tier("https://feeds.bbc.co.uk/news/x") == 1

    def test_lookalike_suffix_does_not_inherit_tier(self):
        """A domain ending in the trusted string but not on a dot boundary is
        tier 3 — otherwise notreuters.com would launder itself into tier 1."""
        assert sources.source_tier("https://notreuters.com/x") == 3
        assert sources.source_tier("https://reuters.com.evil.example/x") == 3

    def test_blocked_domain_still_reports_tier3(self):
        """is_blocked is the gate; tier is only an ordering. They are separate
        so a blocked host can never be ranked as merely-untrusted by accident."""
        assert sources.source_tier("https://www.msn.com/x") == 3

    def test_malformed_url_is_tier3(self):
        assert sources.source_tier("") == 3
        assert sources.source_tier("garbage") == 3


class TestCanonicalDomain:
    def test_collapses_subdomain_to_known_domain(self):
        assert sources.canonical_domain("https://www.reuters.com/x") == "reuters.com"
        assert sources.canonical_domain("https://feeds.bbc.co.uk/n") == "bbc.co.uk"

    def test_unknown_domain_falls_back_to_last_two_labels(self):
        assert sources.canonical_domain("https://a.b.contentfarm.example/x") == "contentfarm.example"

    def test_blocked_domains_are_canonicalized_too(self):
        """Blocked hosts are normally dropped before counting, but corroboration
        must not treat two msn.com copies as two independent sources if one ever
        reaches it."""
        assert sources.canonical_domain("https://www.msn.com/en-us/x") == "msn.com"

    def test_empty_on_garbage(self):
        assert sources.canonical_domain("") == ""
        assert sources.canonical_domain("not a url") == ""


class TestRank:
    def test_drops_blocked_results(self):
        out = sources.rank([_r("https://www.msn.com/a"), _r("https://www.reuters.com/b")])
        assert [r["url"] for r in out] == ["https://www.reuters.com/b"]

    def test_tier_beats_score(self):
        """A wire report at score 0.5 outranks a content farm at 0.99 — Tavily's
        score measures query match, not whether the page is worth believing."""
        out = sources.rank([
            _r("https://contentfarm.example/a", score=0.99),
            _r("https://www.reuters.com/b", score=0.50),
        ])
        assert [r["url"] for r in out] == ["https://www.reuters.com/b",
                                           "https://contentfarm.example/a"]

    def test_score_breaks_ties_within_a_tier(self):
        out = sources.rank([
            _r("https://www.reuters.com/low", score=0.4),
            _r("https://apnews.com/high", score=0.9),
        ])
        assert [r["url"] for r in out] == ["https://apnews.com/high",
                                           "https://www.reuters.com/low"]

    def test_missing_score_does_not_raise(self):
        out = sources.rank([{"url": "https://www.reuters.com/a"}])
        assert len(out) == 1

    def test_trusted_only_drops_tier3(self):
        out = sources.rank([
            _r("https://contentfarm.example/a", score=0.99),
            _r("https://www.theverge.com/b", score=0.1),
        ], trusted_only=True)
        assert [r["url"] for r in out] == ["https://www.theverge.com/b"]

    def test_trusted_only_can_return_nothing(self):
        """Palmer Home would rather show no row than an untrusted one."""
        assert sources.rank([_r("https://contentfarm.example/a")], trusted_only=True) == []

    def test_default_keeps_tier3_as_last_resort(self):
        """Conversation and the morning briefing answer a real question, so an
        obscure-but-real source beats 'nothing found'."""
        out = sources.rank([_r("https://contentfarm.example/a")])
        assert len(out) == 1

    def test_empty_input(self):
        assert sources.rank([]) == []


class TestMeetsScore:
    """The relevance floor runs before the tier sort, so a flat floor lets an
    SEO-tuned content farm survive a cut that a real newsroom does not."""

    def test_tier3_gets_no_slack(self):
        assert not sources.meets_score("https://contentfarm.example/a", 0.45, 0.5)
        assert sources.meets_score("https://contentfarm.example/a", 0.50, 0.5)

    def test_trusted_source_clears_a_lower_bar(self):
        assert sources.meets_score("https://www.reuters.com/a", 0.45, 0.5)
        assert sources.meets_score("https://www.theverge.com/a", 0.40, 0.5)

    def test_trusted_slack_is_bounded(self):
        """Relaxed, not waived — an off-topic wire story is still off-topic."""
        assert not sources.meets_score("https://www.reuters.com/a", 0.20, 0.5)

    def test_missing_score_never_passes_a_positive_floor(self):
        assert not sources.meets_score("https://www.reuters.com/a", None, 0.5)

    def test_floor_never_goes_negative(self):
        assert sources.meets_score("https://www.reuters.com/a", 0.0, 0.1)


class TestSourceListIntegrity:
    """trusted_sources.json is edited by hand and read at import. These pin the
    shape so a typo fails the suite instead of silently demoting a newsroom."""

    @classmethod
    def setup_class(cls):
        with open(Path(__file__).parent / "trusted_sources.json") as f:
            cls.data = json.load(f)

    def test_every_allowlist_entry_has_domain_and_valid_tier(self):
        for d in self.data["domains"]:
            assert d.get("domain"), f"entry missing domain: {d}"
            assert d.get("tier") in (1, 2), f"{d['domain']} has tier {d.get('tier')}"

    def test_every_blocked_entry_explains_why(self):
        """The 'why' is the guardrail against the list drifting from structural
        junk into editorial opinion about who reports well."""
        for d in self.data["blocked"]:
            assert d.get("domain"), f"blocked entry missing domain: {d}"
            assert d.get("why"), f"{d['domain']} blocked with no reason given"

    def test_domains_are_bare_hosts(self):
        for d in self.data["domains"] + self.data["blocked"]:
            host = d["domain"]
            assert host == host.lower(), f"{host} is not lowercase"
            assert "://" not in host, f"{host} includes a scheme"
            assert "/" not in host, f"{host} includes a path"
            assert not host.startswith("www."), f"{host} should not carry a www. prefix"
            assert "." in host, f"{host} is not a domain"

    def test_no_duplicate_domains(self):
        hosts = [d["domain"] for d in self.data["domains"]]
        assert len(hosts) == len(set(hosts)), \
            f"duplicates: {sorted({h for h in hosts if hosts.count(h) > 1})}"

    def test_allowlist_and_blocklist_do_not_overlap(self):
        allowed = {d["domain"] for d in self.data["domains"]}
        blocked = {d["domain"] for d in self.data["blocked"]}
        assert not (allowed & blocked), f"listed as both trusted and blocked: {allowed & blocked}"

    def test_loaded_sets_match_the_file(self):
        assert len(sources._TIER1_DOMAINS) + len(sources._TIER2_DOMAINS) == len(self.data["domains"])
        assert len(sources._BLOCKED_DOMAINS) == len(self.data["blocked"])


class TestCorroboratedIsShared:
    """test_alerts_corroboration.py imports corroborated from watches. It moved
    here; this pins that watches still exposes the same object so the gate can
    never fork into two implementations."""

    def test_watches_reexports_the_same_function(self):
        import watches
        assert watches.corroborated is sources.corroborated


class TestSearchRawAppliesTheGate:
    """The wiring that matters: every news surface goes through _search_raw, so
    the gate has to be applied there rather than by each caller."""

    def _tavily_returning(self, results):
        fake = MagicMock()
        fake.search.return_value = {"results": results}
        return fake

    def test_blocked_results_never_reach_callers(self):
        fake = self._tavily_returning([
            _r("https://www.prnewswire.com/a", score=0.99),
            _r("https://www.reuters.com/b", score=0.6),
        ])
        with patch.object(datafeeds, "_tavily", fake):
            out = datafeeds._search_raw("some topic")
        assert [r["url"] for r in out] == ["https://www.reuters.com/b"]

    def test_results_come_back_source_ranked(self):
        fake = self._tavily_returning([
            _r("https://contentfarm.example/a", score=0.99),
            _r("https://apnews.com/b", score=0.6),
        ])
        with patch.object(datafeeds, "_tavily", fake):
            out = datafeeds._search_raw("some topic")
        assert [r["url"] for r in out] == ["https://apnews.com/b",
                                           "https://contentfarm.example/a"]

    def test_trusted_only_is_forwarded(self):
        fake = self._tavily_returning([_r("https://contentfarm.example/a")])
        with patch.object(datafeeds, "_tavily", fake):
            assert datafeeds._search_raw("t", trusted_only=True) == []
            assert len(datafeeds._search_raw("t")) == 1

    def test_recency_and_score_filters_still_apply_first(self):
        fake = self._tavily_returning([
            _r("https://apnews.com/stale", hours_ago=48),
            _r("https://apnews.com/weak", score=0.1),  # under the floor even with slack
            _r("https://apnews.com/good"),
        ])
        with patch.object(datafeeds, "_tavily", fake):
            out = datafeeds._search_raw("t", max_age_hours=12, min_score=0.5)
        assert [r["url"] for r in out] == ["https://apnews.com/good"]

    def test_trusted_source_survives_a_cut_that_drops_a_content_farm(self):
        """The end-to-end shape of the bug: the farm scores higher because it is
        built to, and a flat floor would have left it as the only survivor."""
        fake = self._tavily_returning([
            _r("https://contentfarm.example/a", score=0.55),
            _r("https://www.reuters.com/b", score=0.42),
        ])
        with patch.object(datafeeds, "_tavily", fake):
            out = datafeeds._search_raw("t", min_score=0.5)
        assert [r["url"] for r in out] == ["https://www.reuters.com/b",
                                           "https://contentfarm.example/a"]

    def test_pulls_ten_candidates(self):
        """The recency window throws most of a page away. Five candidates left
        the tier sort nothing to choose between, which is how a lone content
        farm became the best available source. Tavily bills per search, not per
        result, so the wider pull is free."""
        fake = self._tavily_returning([])
        with patch.object(datafeeds, "_tavily", fake):
            datafeeds._search_raw("t")
        assert fake.search.call_args.kwargs["max_results"] == 10

    def test_search_failure_returns_empty(self):
        fake = MagicMock()
        fake.search.side_effect = RuntimeError("tavily down")
        with patch.object(datafeeds, "_tavily", fake):
            assert datafeeds._search_raw("t") == []


class TestConversationSearchLabelsSources:
    """_search feeds the drafting model directly. It had no provenance at all,
    so Palmer could not tell a wire report from a content farm."""

    def test_domain_is_labelled_and_junk_is_dropped(self):
        fake = MagicMock()
        fake.search.return_value = {"results": [
            _r("https://www.msn.com/junk"),
            _r("https://www.reuters.com/real"),
        ]}
        with patch.object(datafeeds, "_tavily", fake):
            out = datafeeds._search("what happened")
        assert "[reuters.com]" in out
        assert "msn.com" not in out

    def test_no_results_message_when_everything_is_filtered(self):
        fake = MagicMock()
        fake.search.return_value = {"results": [_r("https://biztoc.com/a")]}
        with patch.object(datafeeds, "_tavily", fake):
            assert datafeeds._search("q") == "No results found."
