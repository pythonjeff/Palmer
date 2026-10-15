"""Which sources Palmer will repeat, and in what order.

Every news fact and every news link Palmer sends passes through here. The
helpers used to live in watches.py, but datafeeds.py sits below watches in the
import order and is where the search actually happens — filtering at the search
call is what gets every surface (watch alerts, the morning briefing, Palmer
Home, and conversation) the same bar without wiring each one up separately.

Three signals, cheapest first:

  is_blocked  — structural junk. Press-release wires and republishing
                aggregators. Dropped outright, never ranked.
  source_tier — 1 premier newsroom or official (.gov/.edu), 2 mainstream,
                3 everything else. Orders what survives.
  corroborated — >= 2 distinct domains agree, or one tier-1 confirms.

meets_score sits in front of all three as a relevance floor, relaxed for
trusted sources for the reason spelled out on the function.

The blocklist is deliberately structural rather than editorial. A press release
on globenewswire is a paid placement wearing a news layout, and an msn.com copy
of a Reuters story is a worse link than the Reuters story that is almost always
sitting next to it in the same result set. Neither judgment is about whether an
outlet is any good, which is the kind of call that ages badly in a JSON file.
"""
import json
from pathlib import Path
from urllib.parse import urlparse


def _load() -> tuple[set[str], set[str], set[str]]:
    with open(Path(__file__).parent / "trusted_sources.json") as f:
        data = json.load(f)
    tier1 = {d["domain"] for d in data["domains"] if d["tier"] == 1}
    tier2 = {d["domain"] for d in data["domains"] if d["tier"] == 2}
    blocked = {d["domain"] for d in data.get("blocked", [])}
    return tier1, tier2, blocked


_TIER1_DOMAINS, _TIER2_DOMAINS, _BLOCKED_DOMAINS = _load()


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().lstrip(".")
    except Exception:
        return ""


def _in(host: str, domains: set[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def canonical_domain(url: str) -> str:
    """Collapse subdomains to a canonical form for corroboration counting.
    Prefers a known domain if the host matches; otherwise last two labels."""
    host = _host(url)
    if not host:
        return ""
    for d in _TIER1_DOMAINS | _TIER2_DOMAINS | _BLOCKED_DOMAINS:
        if host == d or host.endswith("." + d):
            return d
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def source_tier(url: str) -> int:
    """1 = premier newsroom or official (.gov/.edu), 2 = mainstream, 3 = other."""
    host = _host(url)
    if not host:
        return 3
    if _in(host, _TIER1_DOMAINS):
        return 1
    if host.endswith(".gov") or host.endswith(".edu"):
        return 1
    if _in(host, _TIER2_DOMAINS):
        return 2
    return 3


def is_blocked(url: str) -> bool:
    """Structural junk: press-release wires and republishing aggregators."""
    host = _host(url)
    return bool(host) and _in(host, _BLOCKED_DOMAINS)


TRUSTED_SCORE_RELIEF = 0.15


def meets_score(url: str, score: float | None, min_score: float) -> bool:
    """Relevance floor, relaxed for trusted sources.

    Tavily's score measures how well a page matches the query text — which is
    the exact thing an SEO content farm is built to win. A single flat floor is
    applied before the tier sort, so it cuts the Reuters piece at 0.45 and keeps
    the farm at 0.90, and the tier sort never gets the chance to undo it. That
    ordering is how junk wins even with ranking in place.

    Trusted sources get 0.15 of slack. Tier 3 gets none: an unknown domain has
    nothing going for it except matching the query, so it had better match."""
    floor = min_score if source_tier(url) == 3 else max(0.0, min_score - TRUSTED_SCORE_RELIEF)
    return (score or 0) >= floor


def rank(results: list[dict], trusted_only: bool = False) -> list[dict]:
    """Drop junk, then order best-source-first.

    Sorting by (tier, -score) puts a Reuters piece above a higher-scoring blog,
    which is the whole point — Tavily's score measures how well a page matches
    the query, not whether the page is worth believing.

    `trusted_only` drops tier 3 entirely. Palmer Home passes it because an
    untrusted headline row is worse than no row: the page is a short curated
    list the user reads top to bottom, so one bad entry taints it. Conversation
    and the morning briefing leave it off — the user asked a direct question,
    and an obscure-but-real source beats "nothing found"."""
    kept = [r for r in results if not is_blocked(r.get("url", ""))]
    if trusted_only:
        kept = [r for r in kept if source_tier(r.get("url", "")) <= 2]
    kept.sort(key=lambda r: (source_tier(r.get("url", "")), -(r.get("score") or 0)))
    return kept


def corroborated(results: list[dict]) -> bool:
    """Do these results clear the shared news-quality bar?
    Pass if >= 2 distinct canonical domains agree OR a single tier-1 source appears.
    Single unknown-domain hits are how rumor/spam/fake alerts leak through."""
    if not results:
        return False
    if any(source_tier(r.get("url", "")) == 1 for r in results):
        return True
    domains = {canonical_domain(r.get("url", "")) for r in results}
    domains.discard("")
    return len(domains) >= 2
