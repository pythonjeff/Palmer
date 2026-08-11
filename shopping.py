"""Price watches for products the user asked Palmer to track.

Pipeline per watch:
  1. SerpAPI Google Shopping query on the product_name.
  2. Haiku picks the cheapest genuine match from the top candidates (guards
     against firing on unrelated cheap accessories/refurbs).
  3. If no baseline yet, record it silently and move on.
  4. Otherwise alert if the target price is hit or the current price is at
     least 15% below baseline. Cooldown gates repeat alerts.

Returns None on any API failure so the scheduler tick silently skips —
never surfaces a "shopping tool failed" line to the user (same discipline
as traffic.py).
"""
import os
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from agent import client, _sms_clean, HAIKU_MODEL

SERP_API_KEY = os.environ.get("SERP_API_KEY", "")
_SERPAPI_BASE = "https://serpapi.com/search.json"
_SERPAPI_TIMEOUT = 12
DROP_THRESHOLD = 0.85  # alert when current <= baseline * DROP_THRESHOLD (i.e. >=15% off)


def _http_get_json(url: str) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Palmer/1.0"})
        with urllib.request.urlopen(req, timeout=_SERPAPI_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"SerpAPI request failed: {e}")
        return None


def _serpapi_search(query: str) -> list[dict]:
    if not SERP_API_KEY or not query:
        return []
    params = {
        "engine": "google_shopping",
        "q": query,
        "api_key": SERP_API_KEY,
        "num": "20",
        "hl": "en",
        "gl": "us",
    }
    url = f"{_SERPAPI_BASE}?{urllib.parse.urlencode(params)}"
    data = _http_get_json(url)
    if not data:
        return []
    results = []
    for item in data.get("shopping_results", [])[:20]:
        price = item.get("extracted_price")
        if not isinstance(price, (int, float)):
            continue
        results.append({
            "title": item.get("title") or "",
            "price": float(price),
            "merchant": item.get("source") or "",
            "url": item.get("link") or item.get("product_link") or "",
        })
    return results


def _pick_best_match(product_name: str, results: list[dict]) -> dict | None:
    """Ask Haiku to pick the cheapest genuine match for product_name from candidates.
    Returns the picked result or None if nothing plausibly matches — guards
    against alerting on an unrelated $12 accessory when watching a $150 product."""
    if not results:
        return None
    candidates = results[:10]
    numbered = "\n".join(
        f"{i}. ${c['price']:.2f} - {c['title']} ({c['merchant']})"
        for i, c in enumerate(candidates)
    )
    prompt = (
        f"User is tracking: {product_name}\n\n"
        f"Google Shopping returned these candidates. Pick the CHEAPEST one that is genuinely "
        f"the product they're tracking - same item, not an accessory, refurb, case, replacement "
        f"part, or wildly different product. Reply with just the index number (0-9) or NONE if "
        f"nothing here is actually the product they meant.\n\n{numbered}"
    )
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.upper().startswith("NONE"):
            return None
        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits:
            return None
        idx = int(digits)
        if 0 <= idx < len(candidates):
            return candidates[idx]
    except Exception as e:
        print(f"_pick_best_match failed: {e}")
    return None


def check_price(product_name: str) -> dict | None:
    """Return {price, title, merchant, url} for the cheapest genuine match, or None."""
    results = _serpapi_search(product_name)
    return _pick_best_match(product_name, results)


def _filter_and_sort(results: list[dict], max_price: float | None,
                     min_price: float | None, limit: int) -> list[dict]:
    filtered = [
        r for r in results
        if (max_price is None or r["price"] <= float(max_price))
        and (min_price is None or r["price"] >= float(min_price))
    ]
    filtered.sort(key=lambda r: r["price"])
    return filtered[:limit]


def search_shopping(query: str, max_price: float | None = None,
                    min_price: float | None = None, limit: int = 5) -> str:
    """One-shot Google Shopping search. Returns a readable summary Sonnet can
    weave into prose. Distinct from add_price_watch (persistent monitoring):
    this is a right-now browse answer, no watch created.

    Never raises; returns a plain message on any failure so tool dispatch stays clean.
    """
    if not SERP_API_KEY:
        return "Shopping search is unavailable right now."
    results = _serpapi_search(query)
    if not results:
        return f"No shopping results found for {query!r}."
    matches = _filter_and_sort(results, max_price, min_price, limit)
    if not matches:
        bound = ""
        if max_price is not None and min_price is not None:
            bound = f" between ${float(min_price):.0f} and ${float(max_price):.0f}"
        elif max_price is not None:
            bound = f" under ${float(max_price):.0f}"
        elif min_price is not None:
            bound = f" over ${float(min_price):.0f}"
        return f"No matches for {query!r}{bound}."
    lines = [
        f"${r['price']:.2f} - {r['title'][:80]} ({r['merchant'] or 'unknown seller'})"
        for r in matches
    ]
    return "\n".join(lines)


def _cooldown_ok(watch: dict, now: datetime | None = None) -> bool:
    """True if enough time has passed since the last alert on this watch."""
    last = watch.get("last_alerted")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except Exception:
        return True
    hours = watch.get("cooldown_hours") or 12
    now = now or datetime.now(timezone.utc)
    return (now - last_dt).total_seconds() >= hours * 3600


def _should_alert(watch: dict, current_price: float) -> str:
    """Return the alert reason ('target' | 'drop') or '' if no alert.
    Assumes cooldown was already checked."""
    target = watch.get("target_price")
    baseline = watch.get("baseline_price")
    if target is not None and current_price <= float(target):
        return "target"
    if baseline is not None and current_price <= float(baseline) * DROP_THRESHOLD:
        return "drop"
    return ""


def _draft_alert(product_name: str, current: dict, watch: dict, reason: str) -> str:
    """Palmer-voice one-liner announcing the hit. Falls back to a plain string."""
    target = watch.get("target_price")
    baseline = watch.get("baseline_price")
    context_lines = [
        f"Product: {product_name}",
        f"Now: ${current['price']:.2f} at {current['merchant'] or 'unknown seller'}",
    ]
    if reason == "target" and target is not None:
        context_lines.append(f"They wanted it at or under ${float(target):.2f} - done.")
    elif reason == "drop" and baseline:
        pct = (1 - current["price"] / float(baseline)) * 100
        context_lines.append(f"Down about {pct:.0f}% from ${float(baseline):.2f}.")
    ctx = "\n".join(context_lines)
    prompt = (
        "You're Palmer, a dry, sharp texting friend. Tell the user their price watch just hit. "
        "One short line. No emoji, no markdown, no bullets, no URL. Don't say 'alert' or "
        "'notification' - you're a friend, not an app. Include the merchant. Under 200 characters.\n\n"
        f"{ctx}"
    )
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        return _sms_clean(response.content[0].text.strip())
    except Exception as e:
        print(f"_draft_alert failed: {e}")
        return _sms_clean(
            f"{product_name} is at ${current['price']:.2f} at {current['merchant'] or 'unknown seller'}."
        )


def run_price_watches():
    """Scheduler job. Every 6h: check each active price watch, alert on target-hit
    or >=15% drop from baseline. Silent-skip on any per-watch failure."""
    from db import get_active_price_watches, set_price_watch_baseline, update_price_watch_alerted
    from sms_util import ensure_sms

    watches = get_active_price_watches()
    for w in watches:
        try:
            if not _cooldown_ok(w):
                continue
            current = check_price(w["product_name"])
            if not current:
                continue
            if w.get("baseline_price") is None:
                set_price_watch_baseline(w["id"], current["price"], current["url"], current["merchant"])
                continue
            reason = _should_alert(w, current["price"])
            if not reason:
                continue
            body = _draft_alert(w["product_name"], current, w, reason)
            if ensure_sms(w["phone"], body):
                update_price_watch_alerted(
                    w["id"], current["price"], current["url"], current["merchant"], body
                )
                print(f"Price watch {w['id']} fired ({reason}) at ${current['price']:.2f}")
        except Exception as e:
            print(f"price watch {w.get('id')} failed: {e}")
