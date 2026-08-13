import json
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta, date as _date

from agent import client, _search_raw, _sms_clean, _is_duplicate_subject, HAIKU_MODEL
from db import get_active_watches, update_watch_alerted, get_messages_after, claim_watch_alert

DAILY_ALERT_MAX = 4


def _load_trusted_sources() -> tuple[set[str], set[str]]:
    with open(Path(__file__).parent / "trusted_sources.json") as f:
        data = json.load(f)
    tier1 = {d["domain"] for d in data["domains"] if d["tier"] == 1}
    tier2 = {d["domain"] for d in data["domains"] if d["tier"] == 2}
    return tier1, tier2


_TIER1_DOMAINS, _TIER2_DOMAINS = _load_trusted_sources()


def _canonical_domain(url: str) -> str:
    """Collapse subdomains to a canonical form for corroboration counting.
    Prefers a known trusted domain if the host matches; otherwise last two labels."""
    try:
        host = (urlparse(url).hostname or "").lower().lstrip(".")
    except Exception:
        return ""
    if not host:
        return ""
    for d in _TIER1_DOMAINS | _TIER2_DOMAINS:
        if host == d or host.endswith("." + d):
            return d
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _source_tier(url: str) -> int:
    """1 = premier newsroom or official (.gov/.edu), 2 = mainstream, 3 = other."""
    try:
        host = (urlparse(url).hostname or "").lower().lstrip(".")
    except Exception:
        return 3
    if not host:
        return 3
    if any(host == d or host.endswith("." + d) for d in _TIER1_DOMAINS):
        return 1
    if host.endswith(".gov") or host.endswith(".edu"):
        return 1
    if any(host == d or host.endswith("." + d) for d in _TIER2_DOMAINS):
        return 2
    return 3


def _daily_ok(watch: dict) -> bool:
    """True if this watch is under the 4-alert daily cap (UTC date)."""
    today = _date.today().isoformat()
    if watch.get("daily_alert_date") != today:
        return True  # new day, count resets
    return watch.get("daily_alert_count", 0) < DAILY_ALERT_MAX


def _user_engaged(watch: dict) -> bool:
    """True if the user replied after the last alert — lowers the threshold next check."""
    last_alerted = watch.get("last_alerted")
    if not last_alerted:
        return False
    msgs = get_messages_after(watch["phone"], last_alerted)
    return any(m["role"] == "user" for m in msgs)


def _check_watch_hit(results: str, description: str, recent_summaries: list[str], engaged: bool) -> bool:
    """Ask Haiku if results contain a new development worth alerting on.
    High bar (engaged=False): major breaking events only.
    Lower bar (engaged=True): meaningful updates the user showed interest in."""
    dedup_block = ""
    if recent_summaries:
        lines = "\n".join(f'- "{s}"' for s in recent_summaries)
        dedup_block = f"\nAlready alerted on these — reply NO if results cover the same events:\n{lines}\n"

    if engaged:
        bar = (
            "Is there a notable new development — a meaningful update (trade, signing, deal, key result) "
            "that matches the watch and isn't already covered above? Skip only pure recap, analysis, or opinion with no new facts."
        )
    else:
        bar = (
            "Is there a MAJOR breaking development — something significant, genuinely new, and time-sensitive? "
            "The bar is high: only a truly critical new event qualifies. "
            "Reply NO for routine coverage, analysis, speculation, or anything not clearly a new critical event."
        )

    response = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=10,
        messages=[{"role": "user", "content": f"""Watch: "{description}"

Results:
{results[:2000]}
{dedup_block}
{bar}
Reply YES or NO."""}],
    )
    return response.content[0].text.strip().upper().startswith("YES")


def _url_reachable(url: str, timeout: int = 4) -> bool:
    """HEAD check that the URL resolves. Treats 405 (HEAD not allowed) as reachable."""
    try:
        req = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 400
    except urllib.error.HTTPError as e:
        return e.code == 405  # HEAD blocked but page exists
    except Exception:
        return False


def _best_result(results: list[dict]) -> dict | None:
    """Return the highest-trust reachable result. Ranks by (tier, -score) so a
    tier-1 newsroom beats a higher-scoring blog."""
    ranked = sorted(
        results,
        key=lambda r: (_source_tier(r.get("url", "")), -(r.get("score") or 0)),
    )
    for r in ranked:
        url = r.get("url", "")
        if url and _url_reachable(url):
            return r
    return None


def _format_alert(result: dict) -> str:
    """Format a raw search result as a headline + link SMS."""
    title = (result.get("title") or "").strip()
    url = (result.get("url") or "").strip()
    if title and url:
        return _sms_clean(f"{title}\n{url}")
    return _sms_clean(title or url)


def run_watches():
    """Check all active watches and send alerts if triggered. Called every 30 minutes by APScheduler."""
    from sms_util import send_sms
    from db import save_message

    watches = get_active_watches()
    now = datetime.now(timezone.utc)

    for watch in watches:
        try:
            # Cooldown: respect per-watch minimum gap between alerts
            if watch["last_alerted"]:
                last = datetime.fromisoformat(watch["last_alerted"])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if now - last < timedelta(hours=watch["cooldown_hours"]):
                    continue

            # Daily cap: max 4 alerts per watch per day
            if not _daily_ok(watch):
                continue

            # Collect raw results across all queries, deduped by URL
            all_raw: list[dict] = []
            seen_urls: set[str] = set()
            for query in watch["queries"]:
                for r in _search_raw(query, days=1, max_age_hours=12):
                    url = r.get("url", "")
                    if url not in seen_urls:
                        seen_urls.add(url)
                        all_raw.append(r)

            if not all_raw:
                continue

            # Corroboration gate: require >= 2 distinct sources OR >= 1 tier-1 source.
            # Single unknown-domain hits are how rumor/spam/fake alerts leak through.
            domains = {_canonical_domain(r.get("url", "")) for r in all_raw}
            domains.discard("")
            has_tier1 = any(_source_tier(r.get("url", "")) == 1 for r in all_raw)
            if len(domains) < 2 and not has_tier1:
                print(f"Watch {watch['id']}: no corroboration ({len(domains)} domain(s), no tier-1), skipping")
                continue

            # Build combined text for hit-check (Haiku reads title + snippet, not full content)
            combined = "\n\n".join(
                f"{r['title']}\nPublished: {r.get('published_date', 'unknown')}\n{r.get('content', '')[:300]}"
                for r in all_raw
            )

            engaged = _user_engaged(watch)
            if not _check_watch_hit(combined, watch["description"], watch["recent_summaries"], engaged):
                continue

            # Pick best reachable result — falls through to next if top URL is dead
            top = _best_result(all_raw)
            if not top:
                print(f"Watch {watch['id']}: all URLs unreachable, skipping alert")
                continue

            alert = _format_alert(top)
            if not alert:
                continue

            if _is_duplicate_subject(watch["phone"], alert):
                print(f"Watch {watch['id']}: subject already covered by a recent message, skipping")
                continue

            if not claim_watch_alert(watch["id"], watch["cooldown_hours"]):
                print(f"Watch {watch['id']}: already claimed by another process, skipping")
                continue

            send_sms(watch["phone"], alert)
            save_message(watch["phone"], "assistant", alert)

            # Use title for dedup context (shorter than full alert with URL)
            title = (top.get("title") or alert)[:120]
            recent = (watch["recent_summaries"] + [title])[-3:]
            update_watch_alerted(watch["id"], title, recent)
            print(f"Watch {watch['id']} triggered for {watch['phone']} (engaged={engaged}): {alert[:80]}")

        except Exception as e:
            print(f"Watch {watch['id']} check failed: {e}")
