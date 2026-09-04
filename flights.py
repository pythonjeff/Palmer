"""Google Flights search via SerpAPI.

One-shot browse — user asks for flights on a route/date and Palmer replies
with a couple of options in prose. Not a persistent watch yet; that can be
layered on later using the same pattern as shopping's add_price_watch.

Never raises. Returns a plain-string message on any failure so tool dispatch
stays clean (same discipline as shopping.py / traffic.py).
"""
import os
import urllib.parse
from datetime import datetime

from netutil import _http_get_json

SERP_API_KEY = os.environ.get("SERP_API_KEY", "")
_SERPAPI_BASE = "https://serpapi.com/search.json"
_SERPAPI_TIMEOUT = 15


def _fmt_time(iso_local: str) -> str:
    """SerpAPI returns local times like '2026-11-15 08:00'. Format as '8:00a'."""
    try:
        dt = datetime.strptime(iso_local, "%Y-%m-%d %H:%M")
    except Exception:
        return iso_local
    ampm = "a" if dt.hour < 12 else "p"
    h12 = dt.hour % 12 or 12
    return f"{h12}:{dt.minute:02d}{ampm}"


def _fmt_duration(minutes) -> str:
    if not isinstance(minutes, int) or minutes <= 0:
        return ""
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h {m}m"
    return f"{h}h" if h else f"{m}m"


def _summarize(entry: dict) -> str | None:
    """One SMS-shaped line per flight option. None if the entry is malformed."""
    legs = entry.get("flights") or []
    price = entry.get("price")
    if not legs or not isinstance(price, (int, float)):
        return None
    airlines = sorted({leg.get("airline", "") for leg in legs if leg.get("airline")})
    airline_str = " / ".join(airlines) if airlines else "airline unknown"
    stops = len(legs) - 1
    if stops == 0:
        route = "nonstop"
    else:
        vias = [leg.get("arrival_airport", {}).get("id") for leg in legs[:-1]]
        vias = [v for v in vias if v]
        route = f"via {'/'.join(vias)} ({stops} stop{'s' if stops > 1 else ''})"
    dep = _fmt_time((legs[0].get("departure_airport") or {}).get("time", ""))
    arr = _fmt_time((legs[-1].get("arrival_airport") or {}).get("time", ""))
    dur = _fmt_duration(entry.get("total_duration"))
    dur_str = f", {dur}" if dur else ""
    return f"${int(price)} - {airline_str} {route}, dep {dep} arr {arr}{dur_str}"


def _serpapi_flights_search(
    origin: str, destination: str, outbound_date: str,
    return_date: str | None = None,
) -> list[dict]:
    if not SERP_API_KEY or not origin or not destination or not outbound_date:
        return []
    params = {
        "engine": "google_flights",
        "departure_id": origin.upper(),
        "arrival_id": destination.upper(),
        "outbound_date": outbound_date,
        "type": "1" if return_date else "2",  # 1 = round trip, 2 = one-way
        "currency": "USD",
        "hl": "en",
        "api_key": SERP_API_KEY,
    }
    if return_date:
        params["return_date"] = return_date
    url = f"{_SERPAPI_BASE}?{urllib.parse.urlencode(params)}"
    data = _http_get_json(url, timeout=_SERPAPI_TIMEOUT)
    if not data:
        return []
    return (data.get("best_flights") or []) + (data.get("other_flights") or [])


def search_flights(
    origin: str, destination: str, outbound_date: str,
    return_date: str | None = None, limit: int = 3,
) -> str:
    """Cheapest flight options for a route and date. Returns a readable summary
    Sonnet can weave into a reply. Prices are round-trip totals when
    return_date is given, one-way otherwise.

    origin / destination: IATA airport codes (e.g. 'BOS', 'LAX').
    outbound_date / return_date: YYYY-MM-DD.
    """
    if not SERP_API_KEY:
        return ("Flight search failed to run just now. Tell the user plainly you "
                "couldn't pull flights this second and offer to try again — you DO "
                "have flight search, so never say you cannot do flights. Do not "
                "name another site.")
    results = _serpapi_flights_search(origin, destination, outbound_date, return_date)
    if not results:
        trip = "round trip" if return_date else "one way"
        return (f"No flights returned for {origin} → {destination} {outbound_date} ({trip}). "
                "Ask the user to confirm the airports and dates, or offer nearby dates. "
                "Do not send them to another site.")
    results.sort(key=lambda r: r.get("price") or float("inf"))
    lines = []
    for entry in results:
        line = _summarize(entry)
        if line:
            lines.append(line)
        if len(lines) >= limit:
            break
    if not lines:
        # The no-results branch above got the full treatment and this one — the
        # same outcome reached through a malformed payload — did not.
        trip = "round trip" if return_date else "one way"
        return (f"Nothing usable came back for {origin} → {destination} {outbound_date} "
                f"({trip}). That is an empty result, not a broken tool — you DO have "
                f"flight search. Ask them to confirm the airports and dates, or offer "
                f"nearby dates. Do not send them to another site.")
    return "\n".join(lines)
