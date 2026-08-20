"""Amazon price watches — user-declared Amazon listings tracked via SerpAPI.

Pipeline:
  1. resolve_asin(query): two paths — if the query is (or contains) an Amazon
     URL, extract the ASIN directly (following a.co / amzn.to shorteners via
     HTTP redirect) and fetch the product; otherwise fall back to
     search + Haiku match. Returns {asin, title, price, url, merchant} or
     None if nothing plausibly matches.
  2. check_price(watch): every scheduler tick, look up the stored ASIN directly
     via the amazon_product engine (more reliable than re-searching the phrase
     — sellers and rankings drift over time).
  3. draft_alert(...): Palmer-voice one-liner that INCLUDES the amazon.com/dp/ASIN
     URL (Google Shopping alerts omit URLs because those links are aggregator
     redirects — Amazon permalinks are clean and open in the Amazon app on iOS).

Silent-skip on any API failure so the scheduler tick never surfaces
"amazon tool failed" to the user (same discipline as shopping.py, traffic.py).
"""
import re

import requests as _requests

import serpapi
from llm import client, HAIKU_MODEL


# /dp/<ASIN> and /gp/product/<ASIN> are the two canonical Amazon product paths.
# ASINs are always 10 chars, uppercase alphanumeric.
_ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")
# Amazon short-URL hosts. These 301-redirect to the full amazon.com/dp/... URL,
# so we resolve via HTTP before applying the ASIN regex.
_SHORT_URL_RE = re.compile(r"https?://(?:a\.co|amzn\.to|amzn\.com)/\S+", re.IGNORECASE)


def _amazon_url(asin: str) -> str:
    return f"https://www.amazon.com/dp/{asin}"


def _resolve_short_url(url: str) -> str | None:
    """Follow HTTP redirects on a.co / amzn.to short URLs and return the final
    URL. Uses GET with stream=True so we don't download the product page body."""
    try:
        resp = _requests.get(
            url, allow_redirects=True, timeout=6, stream=True,
            headers={"User-Agent": "Palmer/1.0"},
        )
        final = resp.url
        resp.close()
        return final
    except Exception as e:
        print(f"amazon._resolve_short_url failed for {url}: {e}")
        return None


def _extract_asin(text: str) -> str | None:
    """Pull an Amazon ASIN out of a message. Handles:
      - Direct URLs: https://www.amazon.com/dp/B0XXXXXXXX (with any query/slug)
      - Short URLs: https://a.co/d/… and https://amzn.to/… (via redirect)
    Returns None if the message contains no Amazon URL."""
    if not text:
        return None
    m = _ASIN_RE.search(text)
    if m:
        return m.group(1)
    short = _SHORT_URL_RE.search(text)
    if short:
        final = _resolve_short_url(short.group(0))
        if final:
            m2 = _ASIN_RE.search(final)
            if m2:
                return m2.group(1)
    return None


def _extract_price(obj: dict) -> float | None:
    """SerpAPI's amazon endpoints return price as either a number
    (`extracted_price`) or a `$12.99`-style string (`price`). Handle both."""
    if not isinstance(obj, dict):
        return None
    p = obj.get("extracted_price")
    if isinstance(p, (int, float)):
        return float(p)
    p = obj.get("price")
    if isinstance(p, (int, float)):
        return float(p)
    if isinstance(p, str):
        try:
            return float(p.replace("$", "").replace(",", "").strip())
        except ValueError:
            return None
    return None


def _serpapi_search(query: str) -> list[dict]:
    if not query:
        return []
    data = serpapi.search({
        "engine": "amazon",
        "amazon_domain": "amazon.com",
        "k": query,
    })
    if not data:
        return []
    results = []
    for item in data.get("organic_results", [])[:10]:
        asin = item.get("asin") or ""
        price = _extract_price(item)
        if not asin or price is None:
            continue
        results.append({
            "asin": asin,
            "title": item.get("title") or "",
            "price": price,
            "url": item.get("link") or _amazon_url(asin),
        })
    return results


def _pick_best_match(query: str, candidates: list[dict]) -> dict | None:
    """Haiku picks the genuine match from top candidates. Guards against firing
    on accessories/refills/replacement parts."""
    if not candidates:
        return None
    numbered = "\n".join(
        f"{i}. ${c['price']:.2f} - {c['title']}"
        for i, c in enumerate(candidates)
    )
    prompt = (
        f"User wants to track this on Amazon: {query}\n\n"
        f"Amazon returned these listings. Pick the one that is genuinely the "
        f"product they mean — same item, not an accessory, refill, case, "
        f"replacement part, or a wildly different product. Prefer the listing "
        f"that most naturally matches the user's phrasing (right size, flavor, "
        f"count if mentioned). Reply with just the index number (0-9) or NONE "
        f"if nothing here is actually the product they meant.\n\n{numbered}"
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
        print(f"amazon._pick_best_match failed: {e}")
    return None


def _amazon_product(asin: str) -> dict | None:
    """Fetch {title, price} for an ASIN via SerpAPI amazon_product. Returns None
    on any failure (missing key, HTTP error, no price found in either
    product_results.extracted_price or buybox_winner)."""
    if not asin:
        return None
    data = serpapi.search({
        "engine": "amazon_product",
        "amazon_domain": "amazon.com",
        "asin": asin,
    })
    if not data:
        return None
    product = data.get("product_results") or {}
    price = _extract_price(product)
    if price is None:
        price = _extract_price(data.get("buybox_winner") or {})
    if price is None:
        return None
    return {"title": product.get("title") or "", "price": price}


def resolve_asin(query: str) -> dict | None:
    """Return {asin, title, price, url, merchant} for the best Amazon match,
    or None. Two paths:
      1. If the query contains an Amazon URL (direct or a.co / amzn.to short),
         extract the ASIN and fetch the product directly. No search needed.
      2. Otherwise, SerpAPI search + Haiku match from the top candidates.
    Called once at watch-creation time; the returned price seeds the baseline
    so the first scheduler tick already has a comparison point."""
    asin_from_url = _extract_asin(query)
    if asin_from_url:
        product = _amazon_product(asin_from_url)
        if product:
            return {
                "asin": asin_from_url,
                "title": product["title"] or "Amazon item",
                "price": product["price"],
                "url": _amazon_url(asin_from_url),
                "merchant": "Amazon",
            }
        # URL parsed but the product lookup failed — don't fall back to search
        # on the URL string, it'll return junk. Better to return None so the
        # handler tells the user something went wrong with THIS specific item.
        return None
    picked = _pick_best_match(query, _serpapi_search(query))
    if not picked:
        return None
    return {
        "asin": picked["asin"],
        "title": picked["title"],
        "price": picked["price"],
        "url": picked["url"] or _amazon_url(picked["asin"]),
        "merchant": "Amazon",
    }


def check_price(watch: dict) -> dict | None:
    """Return {price, title, merchant, url} for a stored watch by looking up its
    ASIN directly via amazon_product. Called every scheduler tick."""
    asin = watch.get("asin")
    product = _amazon_product(asin)
    if not product:
        return None
    return {
        "price": product["price"],
        "title": product["title"] or watch.get("product_name") or "",
        "merchant": "Amazon",
        "url": _amazon_url(asin),
    }


def draft_alert(product_name: str, current: dict, watch: dict, reason: str) -> str:
    """Amazon price hit. Shared drafter, plus the dp/ASIN URL — those are clean
    permalinks worth sending."""
    from price_alert import draft_price_alert
    return draft_price_alert(product_name, current, watch, reason,
                             link=current["url"], source_label="Amazon")
