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
from datetime import datetime, timezone

from agent import client, _sms_clean, _http_get_json, HAIKU_MODEL

SERP_API_KEY = os.environ.get("SERP_API_KEY", "")
_SERPAPI_BASE = "https://serpapi.com/search.json"
_SERPAPI_TIMEOUT = 12
DROP_THRESHOLD = 0.85  # alert when current <= baseline * DROP_THRESHOLD (i.e. >=15% off)


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
    data = _http_get_json(url, timeout=_SERPAPI_TIMEOUT)
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
            "page_token": item.get("immersive_product_page_token") or "",
        })
    return results


def _is_marketplace_thirdparty(merchant: str) -> bool:
    """True if the merchant string names a third-party marketplace listing
    (individual sellers, resale sites). We deprioritize these in link mode
    because 'buy it now' should land on a real retailer, not a random eBay
    seller with 12 reviews."""
    m = (merchant or "").lower().strip()
    if m.startswith("ebay - "):  # "eBay - dabondo1" style individual sellers
        return True
    return m in {"poshmark", "mercari", "depop", "vinted", "grailed"}


def _brand_tokens(query: str) -> list[str]:
    """Extract likely brand words from a shopping query. Words >= 4 chars, alpha
    only. Used to prefer the brand's own store when picking a merchant URL."""
    return [t.lower() for t in query.split() if len(t) >= 4 and t.isalpha()]


def _resolve_merchant_url(page_token: str, query: str = "") -> str | None:
    """Hit SerpAPI's google_immersive_product engine and return a direct
    merchant URL from product_results.stores[].link.

    Preference order:
      1. Store whose name or domain matches a brand token from query
         (so 'Madewell Rockaway Tee' lands on madewell.com, not the first
         reseller Google happens to rank).
      2. First store with a real URL.

    Google Shopping's per-result `link` field is empty on modern responses,
    and Google retired the standalone google_product engine —
    google_immersive_product is SerpAPI's replacement.
    """
    if not page_token or not SERP_API_KEY:
        return None
    params = {
        "engine": "google_immersive_product",
        "page_token": page_token,
        "api_key": SERP_API_KEY,
    }
    url = f"{_SERPAPI_BASE}?{urllib.parse.urlencode(params)}"
    data = _http_get_json(url, timeout=_SERPAPI_TIMEOUT)
    if not data:
        return None
    stores = (data.get("product_results") or {}).get("stores") or []
    if not stores:
        return None

    def _has_url(store: dict) -> bool:
        link = store.get("link") or ""
        return isinstance(link, str) and link.startswith("http")

    tokens = _brand_tokens(query)
    if tokens:
        for store in stores:
            if not _has_url(store):
                continue
            name = (store.get("name") or "").lower()
            link = (store.get("link") or "").lower()
            if any(t in name or t in link for t in tokens):
                return store["link"]
    for store in stores:
        if _has_url(store):
            return store["link"]
    return None


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
                    min_price: float | None = None, limit: int = 5,
                    include_link: bool = False) -> str:
    """One-shot Google Shopping search. Returns a readable summary Sonnet can
    weave into prose. Distinct from add_price_watch (persistent monitoring):
    this is a right-now browse answer, no watch created.

    include_link=False (default): pure browse. Cheap — 1 SerpAPI call, no URL
    work. Result lines contain price, title, and merchant only.

    include_link=True: link mode. Forces limit=1 and resolves the direct
    merchant URL via SerpAPI's google_immersive_product engine (bypassing
    Google's aggregator page). URL is returned raw — no shortener redirect.
    Costs 1 shopping call + 1 product call. Use when the user has explicitly
    asked to buy or for a link to a specific product.

    Never raises; returns a plain message on any failure so tool dispatch
    stays clean.
    """
    if not SERP_API_KEY:
        return "Shopping search is unavailable right now."
    if include_link:
        limit = 1  # link mode always returns exactly one row
    results = _serpapi_search(query)
    if not results:
        return f"No shopping results found for {query!r}."
    if include_link:
        # Respect Google's ranking (quality signal), but push third-party
        # marketplace listings to the bottom. Sorting by price would pick a
        # random eBay reseller over a real retailer just because it's a few
        # bucks cheaper.
        ranked = sorted(results, key=lambda r: _is_marketplace_thirdparty(r.get("merchant", "")))
        matches = ranked[:1]
    else:
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
    lines = []
    for r in matches:
        base = f"${r['price']:.2f} - {r['title'][:80]} ({r['merchant'] or 'unknown seller'})"
        if include_link:
            merchant_url = _resolve_merchant_url(r.get("page_token", ""), query=query)
            if merchant_url:
                base += f" | {merchant_url}"
            else:
                base += " | (direct link unavailable — try the merchant site)"
        lines.append(base)
    return "\n".join(lines)


# Domains that dominate general search results but aren't the brand-site the
# user wanted to open. Aggregators/social/roundup content — we skip them and
# keep walking organic_results looking for the real store.
_BROWSE_AGGREGATOR_HOSTS = {
    "google.com", "shopping.google.com", "pinterest.com", "reddit.com",
    "youtube.com", "buzzfeed.com", "pricegrabber.com", "shopzilla.com",
    "nextag.com",
}


def _browse_result_ok(link: str) -> bool:
    """Reject aggregator/social domains and Amazon search pages; keep everything
    else including Amazon product pages (/dp/, /gp/product/)."""
    if not link or not link.startswith(("http://", "https://")):
        return False
    try:
        parsed = urllib.parse.urlparse(link)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    for bad in _BROWSE_AGGREGATOR_HOSTS:
        if host == bad or host.endswith("." + bad):
            return False
    if host.endswith("amazon.com") and parsed.path.startswith("/s"):
        return False  # Amazon search results page, not a product/category
    return True


def _host_matches_tokens(link: str, tokens: list[str]) -> bool:
    try:
        host = urllib.parse.urlparse(link).netloc.lower()
    except Exception:
        return False
    return any(t in host for t in tokens)


def browse_shop(query: str) -> str:
    """Return ONE clean URL for a brand or retailer page the user can open
    themselves. Distinct from search_shopping: this is "take me to the store,"
    not "pull specific products."

    Hits SerpAPI's regular google engine (not google_shopping). Preference:
      1. knowledge_graph.website when its host matches a brand token.
      2. First organic result whose host matches a brand token.
      3. First organic result that isn't an aggregator/social/search page.

    URL is returned raw. Never raises."""
    if not SERP_API_KEY:
        return "Shopping search is unavailable right now."
    if not query:
        return "No browse result found."
    params = {
        "engine": "google",
        "q": query,
        "api_key": SERP_API_KEY,
        "num": "10",
        "hl": "en",
        "gl": "us",
    }
    data = _http_get_json(f"{_SERPAPI_BASE}?{urllib.parse.urlencode(params)}", timeout=_SERPAPI_TIMEOUT)
    if not data:
        return f"No browse result found for {query!r}."

    tokens = _brand_tokens(query)

    kg = data.get("knowledge_graph") or {}
    kg_site = kg.get("website") or ""
    if tokens and _browse_result_ok(kg_site) and _host_matches_tokens(kg_site, tokens):
        title = kg.get("title") or query
        return f"{title} — {kg_site}"

    organics = data.get("organic_results") or []
    valid = [r for r in organics if _browse_result_ok(r.get("link") or "")]

    if tokens:
        for r in valid:
            if _host_matches_tokens(r["link"], tokens):
                return f"{(r.get('title') or query)} — {r['link']}"

    if valid:
        r = valid[0]
        return f"{(r.get('title') or query)} — {r['link']}"

    return f"No browse result found for {query!r}."


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
    """Scheduler job. Every 12h: check each active price watch, alert on target-hit
    or >=15% drop from baseline. Silent-skip on any per-watch failure. Dispatches
    to shopping (Google Shopping) or amazon (Amazon by ASIN) based on w['source']."""
    from db import (
        get_active_price_watches, set_price_watch_baseline, update_price_watch_alerted,
        claim_price_watch_alert, release_price_watch_claim,
    )
    from sms_util import ensure_sms
    import amazon

    watches = get_active_price_watches()
    for w in watches:
        try:
            if not _cooldown_ok(w):
                continue
            if w.get("source") == "amazon":
                current = amazon.check_price(w)
            else:
                current = check_price(w["product_name"])
            if not current:
                continue
            if w.get("baseline_price") is None:
                set_price_watch_baseline(w["id"], current["price"], current["url"], current["merchant"])
                continue
            reason = _should_alert(w, current["price"])
            if not reason:
                continue
            if not claim_price_watch_alert(w["id"], w.get("cooldown_hours") or 12):
                print(f"Price watch {w['id']}: already claimed by another process, skipping")
                continue
            if w.get("source") == "amazon":
                body = amazon.draft_alert(w["product_name"], current, w, reason)
            else:
                body = _draft_alert(w["product_name"], current, w, reason)
            if ensure_sms(w["phone"], body):
                update_price_watch_alerted(
                    w["id"], current["price"], current["url"], current["merchant"], body
                )
                print(f"Price watch {w['id']} fired ({reason}) at ${current['price']:.2f}")
            else:
                release_price_watch_claim(w["id"])  # allow retry next tick
        except Exception as e:
            print(f"price watch {w.get('id')} failed: {e}")
