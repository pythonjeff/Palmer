"""Tests for the news-quality gate in watches.corroborated(). Every unprompted
news text — a watch alert, a headline on the page — has to clear it."""
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
