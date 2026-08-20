"""Generic city traffic for morning briefings.

Pipeline per city:
  1. Geocode city name to (lat/lng + bbox) via TomTom Search — cached in-process.
  2. Fetch Traffic Flow at city center + Traffic Incidents in the bbox in parallel.
  3. Haiku summarizes it into one factual line; callers voice it.

Returns None on any hard failure (no key, no city, geocode/API error) so
morning briefing can silently skip the section — never surface a broken
"traffic tool failed" message to the user.
"""
import os
import concurrent.futures
import urllib.parse

from llm import client, HAIKU_MODEL
from netutil import _http_get_json

TOMTOM_API_KEY = os.environ.get("TOMTOM_API_KEY", "")
_TOMTOM_BASE = "https://api.tomtom.com"
_TOMTOM_TIMEOUT = 8

# Icon categories worth reporting; ignore weather-condition icons (2,3,4,5,10,11).
_NOTABLE_ICONS = {1, 6, 7, 8, 9, 14}  # accident, jam, lane closed, road closed, roadworks, broken vehicle

# City -> geocode result cache. Cities don't move; safe to keep for the process lifetime.
_city_geo_cache: dict[str, dict] = {}


def _geocode_city(city: str) -> dict | None:
    """Return {lat, lng, min_lat, max_lat, min_lng, max_lng} or None."""
    key = city.strip().lower()
    if not key or not TOMTOM_API_KEY:
        return None
    if key in _city_geo_cache:
        return _city_geo_cache[key]
    q = urllib.parse.quote(city)
    url = f"{_TOMTOM_BASE}/search/2/geocode/{q}.json?limit=1&key={TOMTOM_API_KEY}"
    data = _http_get_json(url, timeout=_TOMTOM_TIMEOUT)
    if not data or not data.get("results"):
        return None
    r = data["results"][0]
    pos = r.get("position") or {}
    vp = r.get("viewport") or {}
    tl = vp.get("topLeftPoint") or {}
    br = vp.get("btmRightPoint") or {}
    if not pos.get("lat") or not tl.get("lat") or not br.get("lat"):
        return None
    result = {
        "lat": pos["lat"],
        "lng": pos["lon"],
        "min_lat": br["lat"],
        "max_lat": tl["lat"],
        "min_lng": tl["lon"],
        "max_lng": br["lon"],
    }
    _city_geo_cache[key] = result
    return result


def _get_flow(lat: float, lng: float) -> dict | None:
    """TomTom Traffic Flow at a point. Returns dict with currentSpeed/freeFlowSpeed or None."""
    url = (
        f"{_TOMTOM_BASE}/traffic/services/4/flowSegmentData/absolute/10/json"
        f"?point={lat},{lng}&key={TOMTOM_API_KEY}"
    )
    data = _http_get_json(url, timeout=_TOMTOM_TIMEOUT)
    if not data:
        return None
    return data.get("flowSegmentData")


def _get_incidents(bbox: dict) -> list[dict]:
    """TomTom Traffic Incidents in the bbox. Returns raw incident list."""
    bbox_str = f"{bbox['min_lng']},{bbox['min_lat']},{bbox['max_lng']},{bbox['max_lat']}"
    fields = (
        "{incidents{properties{iconCategory,magnitudeOfDelay,delay,"
        "roadNumbers,from,to,events{description,code,iconCategory}}}}"
    )
    url = (
        f"{_TOMTOM_BASE}/traffic/services/5/incidentDetails"
        f"?bbox={bbox_str}&fields={urllib.parse.quote(fields)}&language=en-US&key={TOMTOM_API_KEY}"
    )
    data = _http_get_json(url, timeout=_TOMTOM_TIMEOUT)
    if not data:
        return []
    return data.get("incidents", []) or []


def _summarize_incident(inc: dict) -> str:
    """Compact one-line summary of an incident for the Haiku prompt."""
    p = inc.get("properties") or {}
    events = p.get("events") or []
    event_descs = "; ".join(e.get("description", "") for e in events if e.get("description"))
    roads = p.get("roadNumbers") or []
    from_road = p.get("from") or ""
    to_road = p.get("to") or ""
    delay = p.get("delay")
    parts = []
    if event_descs:
        parts.append(event_descs)
    if roads:
        parts.append(f"on {'/'.join(roads)}")
    if from_road and to_road:
        parts.append(f"{from_road} → {to_road}")
    elif from_road:
        parts.append(f"near {from_road}")
    if delay and delay > 60:
        parts.append(f"~{int(delay / 60)} min delay")
    return ", ".join(parts) if parts else "incident (no detail)"


def get_city_traffic(city: str) -> str | None:
    """Return a short natural-language traffic update for the city, or None on failure.

    Always produces a line when the API responds (even 'roads are clear') so the
    morning briefing has a consistent section. Returns None only when we truly
    have nothing to say (missing key/city, geocode fail, API error)."""
    if not city or not TOMTOM_API_KEY:
        return None
    geo = _geocode_city(city)
    if not geo:
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        flow_f = ex.submit(_get_flow, geo["lat"], geo["lng"])
        inc_f = ex.submit(_get_incidents, geo)
        try:
            flow = flow_f.result(timeout=_TOMTOM_TIMEOUT + 2)
            incidents = inc_f.result(timeout=_TOMTOM_TIMEOUT + 2)
        except Exception as e:
            print(f"Traffic fetch failed for {city!r}: {e}")
            return None

    # Congestion ratio: current/freeflow. <0.7 = notably slow, <0.85 = mild.
    flow_state = "unknown"
    if flow:
        cur = flow.get("currentSpeed")
        free = flow.get("freeFlowSpeed")
        if cur and free:
            ratio = cur / free
            if ratio < 0.7:
                flow_state = "slower than usual"
            elif ratio < 0.85:
                flow_state = "slightly slow"
            else:
                flow_state = "normal"

    notable = [i for i in incidents if (i.get("properties") or {}).get("iconCategory") in _NOTABLE_ICONS]
    # Rank by delay desc so the biggest problems land in the prompt.
    notable.sort(key=lambda i: (i.get("properties") or {}).get("delay") or 0, reverse=True)
    incident_lines = [_summarize_incident(i) for i in notable[:4]]

    # Deliberately NOT written in Palmer's voice. Both callers — morning.py's
    # briefing draft and the get_city_traffic tool inside get_reply — feed this
    # into a Sonnet call that already carries the real system prompt, so voicing
    # it here would just be a fourth, uncalibrated impression of Palmer.
    # This is a factual summarizer; the callers do the talking.
    prompt = f"""Summarize the traffic situation in {city} in ONE line. Max two sentences, under 200 characters. Plain factual reporting — this is source data for another writer, not a message to a user.

Overall flow near city center: {flow_state}
Notable incidents ({len(notable)}):
{chr(10).join(f'- {line}' for line in incident_lines) if incident_lines else '- none'}

Format examples:
- "Traffic around St. Louis is slower than usual. Multiple accidents on highway 40 near the 270 merger are slowing all directions."
- "Roads around Boston are clear."
- "I-5 south into Seattle is slow, with a jam near the Ship Canal Bridge adding about 15 minutes."

Rules:
- If flow is normal and no notable incidents: say the roads are clear in one short sentence.
- If incidents exist: name the specific roads/exits from the data, don't invent details.
- No commentary, no jokes, no greeting, no preamble. Plain ASCII only, no emoji."""

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        text = (response.content[0].text or "").strip()
        return text or None
    except Exception as e:
        print(f"Traffic summarization failed for {city!r}: {e}")
        return None


def _geocode_address(address: str) -> tuple[float, float] | None:
    """Geocode any free-form address (not just cities) to (lat, lng)."""
    if not address or not TOMTOM_API_KEY:
        return None
    q = urllib.parse.quote(address)
    url = f"{_TOMTOM_BASE}/search/2/geocode/{q}.json?limit=1&key={TOMTOM_API_KEY}"
    data = _http_get_json(url, timeout=_TOMTOM_TIMEOUT)
    if not data or not data.get("results"):
        return None
    pos = (data["results"][0].get("position") or {})
    lat, lng = pos.get("lat"), pos.get("lon")
    if lat is None or lng is None:
        return None
    return (lat, lng)


def get_travel_time(origin: str, destination: str) -> str:
    """Return a natural-language travel-time line between two addresses using TomTom
    Routing with live traffic. Always returns a string (an error message on failure)
    so the tool result is readable by the agent."""
    if not TOMTOM_API_KEY:
        return "Traffic API is not configured."
    if not origin or not destination:
        return "Need both an origin and destination address to route."

    orig = _geocode_address(origin)
    if not orig:
        return f"Couldn't find that starting address: {origin!r}. Ask the user to clarify or provide a more specific address."
    dest = _geocode_address(destination)
    if not dest:
        return f"Couldn't find that destination: {destination!r}. Ask the user to clarify or provide a more specific address."

    locations = f"{orig[0]},{orig[1]}:{dest[0]},{dest[1]}"
    url = (
        f"{_TOMTOM_BASE}/routing/1/calculateRoute/{locations}/json"
        f"?traffic=true&travelMode=car&computeTravelTimeFor=all&key={TOMTOM_API_KEY}"
    )
    data = _http_get_json(url, timeout=_TOMTOM_TIMEOUT)
    if not data or not data.get("routes"):
        return f"Routing failed for {origin!r} → {destination!r}."

    summary = data["routes"][0].get("summary") or {}
    live = summary.get("travelTimeInSeconds")  # with live traffic
    free = summary.get("noTrafficTravelTimeInSeconds")  # ideal free-flow
    delay = summary.get("trafficDelayInSeconds") or 0
    dist = summary.get("lengthInMeters")

    if not live:
        return f"Routing returned no travel time for {origin!r} → {destination!r}."

    parts = [f"{round(live / 60)} min with current traffic"]
    if free:
        parts.append(f"free-flow {round(free / 60)} min")
    if delay >= 60:
        parts.append(f"delay ~{round(delay / 60)} min above free-flow")
    if dist:
        parts.append(f"{round(dist / 1609.34, 1)} miles")
    return f"{origin} → {destination}: " + "; ".join(parts) + "."
