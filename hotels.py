"""Google Hotels search via SerpAPI.

One-shot browse — user asks for hotels in a place for dates, Palmer replies
with a couple of options in prose. Watch/tracking layer is separate.

Never raises. Returns a plain-string message on any failure so tool dispatch
stays clean (same discipline as shopping.py / flights.py / traffic.py).
"""
import os
import urllib.parse

from netutil import _http_get_json

SERP_API_KEY = os.environ.get("SERP_API_KEY", "")
_SERPAPI_BASE = "https://serpapi.com/search.json"
_SERPAPI_TIMEOUT = 15

# SerpAPI's min_rating is a discrete code, not a raw star value.
_RATING_CODE = {3.5: "7", 4.0: "8", 4.5: "9"}


def _rating_code(min_rating: float | None) -> str | None:
    if min_rating is None:
        return None
    # Snap to the nearest supported floor (3.5 / 4.0 / 4.5); never overshoot user intent.
    for threshold, code in sorted(_RATING_CODE.items(), reverse=True):
        if min_rating >= threshold:
            return code
    return None


def _summarize(prop: dict) -> str | None:
    """One SMS-shaped line per property. None if malformed."""
    rate = (prop.get("rate_per_night") or {}).get("extracted_lowest")
    name = prop.get("name")
    if not name or not isinstance(rate, (int, float)):
        return None
    bits = [f"${int(rate)}/night", name]
    rating = prop.get("overall_rating")
    reviews = prop.get("reviews")
    if isinstance(rating, (int, float)):
        rating_str = f"{rating:.1f}★"
        if isinstance(reviews, int) and reviews > 0:
            rating_str += f" ({reviews} reviews)"
        bits.append(rating_str)
    hotel_class = prop.get("hotel_class")
    if hotel_class:
        bits.append(str(hotel_class))
    return " - ".join([bits[0], ", ".join(bits[1:])])


def _serpapi_hotels_search(
    location: str, check_in_date: str, check_out_date: str,
    max_price: float | None = None, min_rating: float | None = None,
    adults: int = 2,
) -> list[dict]:
    if not SERP_API_KEY or not location or not check_in_date or not check_out_date:
        return []
    params = {
        "engine": "google_hotels",
        "q": location,
        "check_in_date": check_in_date,
        "check_out_date": check_out_date,
        "adults": str(adults),
        "currency": "USD",
        "sort_by": "3",  # 3 = lowest price
        "hl": "en",
        "gl": "us",
        "api_key": SERP_API_KEY,
    }
    if max_price is not None:
        params["max_price"] = str(int(max_price))
    code = _rating_code(min_rating)
    if code:
        params["rating"] = code
    url = f"{_SERPAPI_BASE}?{urllib.parse.urlencode(params)}"
    data = _http_get_json(url, timeout=_SERPAPI_TIMEOUT)
    if not data:
        return []
    return data.get("properties") or []


def search_hotels(
    location: str, check_in_date: str, check_out_date: str,
    max_price: float | None = None, min_rating: float | None = None,
    limit: int = 3,
) -> str:
    """Cheapest hotel options for a location and date range. Returns a readable
    summary Sonnet can weave into a reply. Prices are per-night lows.

    location: city or neighborhood as you'd search on Google Maps
             ('Shoreditch, London', 'Times Square, New York').
    check_in_date / check_out_date: YYYY-MM-DD.
    max_price: optional per-night USD cap.
    min_rating: optional floor (3.5 / 4.0 / 4.5); other values snap down.
    """
    if not SERP_API_KEY:
        return ("Hotel search failed to run just now. Say you couldn't pull hotels this "
                "second and offer to try again — you DO have hotel search. Do not name "
                "another site.")
    props = _serpapi_hotels_search(location, check_in_date, check_out_date, max_price, min_rating)
    if not props:
        return f"No hotels found in {location} {check_in_date} to {check_out_date}."
    lines = []
    for prop in props:
        line = _summarize(prop)
        if line:
            lines.append(line)
        if len(lines) >= limit:
            break
    if not lines:
        return f"No hotels found in {location} {check_in_date} to {check_out_date}."
    return "\n".join(lines)
