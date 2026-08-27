"""Opening — what is newly open or landing near the user this week.

Two halves, three sources, none of them SerpAPI:

    local    Tavily via datafeeds._search_raw   new restaurants, bars, festivals
    events   Ticketmaster Discovery (free)      concerts and festivals with dates
    screens  TMDB (free)                        movies and shows out this week

SerpAPI was the obvious first guess and both of its candidate engines failed.
`google_events` returns "Fully empty" for every query tried, including
SerpAPI's own documented Austin example with the `location` parameter.
`google_local` works but is a proximity search with no `opened_date` field —
asking it for "new restaurants" near Culver City returns Applebee's. Neither is
an openings feed, and the account's free tier (250 searches/month) could not
have carried a per-user daily fetch anyway.

**This is metro-scoped weekly content, not user-scoped daily content.** "New
restaurants in LA" is the same for every user in LA, and "movies out this week"
is the same for everyone alive. Both caches key on that rather than on the
phone number, which is what keeps the cost flat as users are added: two users
in one metro cost one fetch, not two. Same pattern as trends.py and rubrics.py,
and safe for the same reason — WEB_CONCURRENCY=1.

Every fetch degrades to nothing rather than raising. A missing key, a dead
upstream or a bad payload drops that one row class and keeps the others, and
the section simply does not render if all three come back empty.
"""
from __future__ import annotations

import os
import threading
from datetime import date, datetime, timedelta

from llm import client, HAIKU_MODEL, _parse_json
from netutil import _http_get_json

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
TICKETMASTER_API_KEY = os.environ.get("TICKETMASTER_API_KEY", "")

TMDB_BASE = "https://api.themoviedb.org/3"
TM_BASE = "https://app.ticketmaster.com/discovery/v2/events.json"

# How many rows survive to the page. The section is a nudge, not a listings
# magazine — four is already more than anyone reads under a weather card.
MAX_ROWS = 4
# Screens are national, so they are the part most likely to crowd out the local
# rows that make this section worth having. Two, hard.
MAX_SCREENS = 2
# Radius for the events pull. 25 miles is a metro, not a neighbourhood, which
# is deliberate: nobody picks a concert by how close it is to their street.
EVENT_RADIUS_MI = 25

# Coarse lat/lon bucket for the cache key, in degrees. 0.5 is roughly 35 miles,
# so Culver City (34.02, -118.40) and Woodland Hills (34.17, -118.61) land in
# one bucket and share a single fetch — which is the whole cost argument. A
# city -> metro lookup table would do the same job and would need maintaining
# forever; rounding does not.
BUCKET = 0.5

_local_cache: dict[tuple, list[dict]] = {}
_screen_cache: dict[str, list[dict]] = {}
_metro_cache: dict[str, str] = {}
_cache_lock = threading.Lock()


def _bucket(lat: float, lon: float) -> tuple[float, float]:
    return (round(lat / BUCKET) * BUCKET, round(lon / BUCKET) * BUCKET)


def _week_key(today: date | None = None) -> str:
    y, w, _ = (today or date.today()).isocalendar()
    return f"{y}-W{w:02d}"


def _clear_caches() -> None:
    """Tests only — the caches are process-lifetime by design."""
    with _cache_lock:
        _local_cache.clear()
        _screen_cache.clear()
        _metro_cache.clear()


def _metro(city: str) -> str:
    """The city name local press would use when covering this place.

    Suburbs are dead ends for news search exactly the way they were for the
    SerpAPI events engine: "new restaurants opening in Culver City" returns
    nothing, while the same query for Los Angeles returns the LA Times. Nobody
    writes an openings column for a suburb; they write it for the metro.

    Resolved by model rather than a lookup table, once per city and cached for
    the process. A hardcoded metro map is the same mistake `tickers.py` made
    twice with PRIVATE_COMPANIES — it encodes a snapshot, and there are tens of
    thousands of towns. Falls back to the city itself, which is correct for
    every city that is already its own metro.
    """
    key = (city or "").strip().lower()
    if not key:
        return city
    with _cache_lock:
        hit = _metro_cache.get(key)
    if hit:
        return hit
    metro = city
    try:
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=20,
            messages=[{"role": "user", "content":
                       f"Which metro area's local press covers {city}? Reply with just the "
                       f"city name a local news outlet would use (e.g. 'Los Angeles', "
                       f"'St. Louis'). If it is already a major city, repeat it back."}],
        )
        text = resp.content[0].text.strip()
        if text and len(text) < 40:
            metro = text
    except Exception as e:
        print(f"opening: metro lookup failed for {city!r}: {type(e).__name__}: {e}")
    with _cache_lock:
        _metro_cache[key] = metro
    return metro


def _local_candidates(city: str) -> list[dict]:
    """New-opening coverage from local press.

    There is no structured API for "restaurants that opened this month", but
    it is a well-established local-media beat — Eater, Time Out, LAist and the
    city dailies all run it as a standing column. Those outlets are tier 2 in
    trusted_sources.json specifically so this search can pass trusted_only and
    still return something; before they were added it returned nothing at all.
    """
    from datafeeds import _search_raw
    from sources import canonical_domain
    metro = _metro(city)
    out = []
    for query in (f"new restaurant openings {metro}",
                  f"festivals and events happening in {metro} this week"):
        try:
            for r in _search_raw(query, days=14, max_age_hours=24 * 14,
                                 min_score=0.4, trusted_only=True)[:4]:
                out.append({"title": (r.get("title") or "")[:140],
                            "url": r.get("url"),
                            "source": canonical_domain(r.get("url", "")),
                            "blurb": (r.get("content") or "")[:300]})
        except Exception as e:
            print(f"opening: local search failed for {query!r}: {type(e).__name__}: {e}")
    return out


def _events(lat: float, lon: float) -> list[dict]:
    """Concerts and festivals from Ticketmaster Discovery.

    Queried by latlong rather than city name on purpose. A city string makes
    small suburbs dead ends — the whole reason the SerpAPI events path failed
    for Culver City — while a coordinate plus a radius asks the question people
    actually mean: what is on near me.
    """
    if not TICKETMASTER_API_KEY:
        return []
    start = datetime.utcnow()
    end = start + timedelta(days=7)
    url = (f"{TM_BASE}?apikey={TICKETMASTER_API_KEY}"
           f"&latlong={lat:.4f},{lon:.4f}&radius={EVENT_RADIUS_MI}&unit=miles"
           f"&startDateTime={start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
           f"&endDateTime={end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
           f"&size=20&sort=relevance,desc")
    data = _http_get_json(url, timeout=10)
    out = []
    for ev in ((data or {}).get("_embedded") or {}).get("events") or []:
        venues = ((ev.get("_embedded") or {}).get("venues") or [{}])
        cls = (ev.get("classifications") or [{}])[0]
        out.append({
            "title": (ev.get("name") or "")[:120],
            "venue": venues[0].get("name") if venues else None,
            "date": ((ev.get("dates") or {}).get("start") or {}).get("localDate"),
            "genre": ((cls.get("genre") or {}).get("name")
                      or (cls.get("segment") or {}).get("name")),
            "url": ev.get("url"),
        })
    return out


def _screens() -> list[dict]:
    """Movies and shows landing this week, from TMDB.

    National, so this cache is global rather than per-metro — one fetch a week
    serves every user there will ever be.
    """
    if not TMDB_API_KEY:
        return []
    today = date.today()
    window_start, window_end = today - timedelta(days=7), today + timedelta(days=7)

    def _recent(iso: str | None) -> bool:
        try:
            d = date.fromisoformat(iso)
        except (TypeError, ValueError):
            return False
        return window_start <= d <= window_end

    out = []
    movies = _http_get_json(
        f"{TMDB_BASE}/movie/now_playing?api_key={TMDB_API_KEY}&region=US&page=1", timeout=10)
    for m in (movies or {}).get("results", [])[:20]:
        if _recent(m.get("release_date")):
            out.append({"title": m.get("title"), "kind": "movie",
                        "date": m.get("release_date"),
                        "blurb": (m.get("overview") or "")[:240],
                        "score": m.get("vote_average") or 0,
                        "url": f"https://www.themoviedb.org/movie/{m.get('id')}"})
    shows = _http_get_json(f"{TMDB_BASE}/tv/on_the_air?api_key={TMDB_API_KEY}&page=1", timeout=10)
    for s in (shows or {}).get("results", [])[:20]:
        if _recent(s.get("first_air_date")):
            out.append({"title": s.get("name"), "kind": "show",
                        "date": s.get("first_air_date"),
                        "blurb": (s.get("overview") or "")[:240],
                        "score": s.get("vote_average") or 0,
                        "url": f"https://www.themoviedb.org/tv/{s.get('id')}"})
    out.sort(key=lambda r: r["score"], reverse=True)
    return out


_CURATE_PROMPT = """You are picking rows for a section called "Opening" on someone's personal page. It answers one question: what is newly open or worth catching near them this week.

Their city: {city}

CANDIDATES (JSON):
{candidates}

Return JSON: {{"rows": [{{"title": "...", "subtitle": "...", "when": "...", "url": "...", "kind": "local|event|screen"}}]}}

Pick at most {max_rows}, best first. Include at most 2 screens, so the section stays local.

What earns a row:
- A specific place that actually opened recently, named. A chef, a neighbourhood, a thing that distinguishes it.
- A festival, show or one-off with a date, that a person would tell a friend about.
- A film or series people are actually talking about this week.

Apply the right test to each candidate before anything else.

For a PLACE (restaurant, bar, shop): would a local paper have run this under the headline "X opens"? If the piece is really about something else and merely mentions the place, drop it. A TV segment cooking with a restaurant, a promotional appearance, and a dining-week feature all fail this test even though a real restaurant is named.

For an EVENT (concert, festival, one-off): is it a specific named thing, on a date, at a named venue, that someone would actually go to? A dated event is a valid row even though nothing is "opening" — that is what this section is for. Drop anything undated or with no venue. An annual festival in its Nth year is fine — recurring is what festivals do — and a sponsor's name attached to a real event does not make it an ad.

What does not, ever:
- Chains and franchises. Applebee's opening a location is not news.
- Roundups and listicles — "15 best brunch spots", "your guide to". Those are not openings.
- Promotional tie-ins and dining weeks — "Dine LA", "Restaurant Week", a segment where a chef visits a studio. The restaurant is real; the opening is not. This is about the piece being an ad, not about a sponsor being named.
- Anywhere outside the metro named above. A different city's opening is noise no matter how good it is.
- Ticket spam, tribute acts, bar covers bands, anything whose draw is that tickets exist.
- Anything you cannot name specifically. If the source only says "several new restaurants", drop it.
- Anything already closed, past, or older than about a month.

Writing the row:
- title: the name of the place, event or title. Nothing else. No city, no verb.
- subtitle: at most 8 words on why it is worth knowing. Concrete, not promotional. Never "a must-visit" or "don't miss".
- when: short and human — "opened this week", "Friday", "in theaters", "streaming now". Empty string if you genuinely don't know.
- url: copy verbatim from the candidate. Never invent one.
- Plain ASCII. No emoji, no markdown, no exclamation marks.

If nothing clears the bar, return {{"rows": []}}. An empty section is correct and normal; a padded one is not."""


def _curate(city: str, candidates: list[dict]) -> list[dict]:
    """The taste gate. One Haiku call, on the write path only.

    Everything upstream of this is a firehose: local press runs listicles and
    restaurant-week promos alongside real openings, and Ticketmaster will
    happily return every cover band within 25 miles. Filtering that with
    keywords was never going to work — the difference between "Mamele's opened
    on Washington" and "15 best brunch spots in LA" is editorial, not lexical.

    Runs once per metro per week behind the cache, never on a page view.
    """
    if not candidates:
        return []
    import json
    try:
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=900,
            messages=[{"role": "user", "content": _CURATE_PROMPT.format(
                city=city, candidates=json.dumps(candidates)[:9000], max_rows=MAX_ROWS)}],
        )
        parsed = _parse_json(resp.content[0].text) or {}
    except Exception as e:
        print(f"opening: curation failed for {city!r}: {type(e).__name__}: {e}")
        return []

    rows = []
    for r in (parsed.get("rows") or [])[:MAX_ROWS]:
        title = (r.get("title") or "").strip()
        if not title:
            continue
        rows.append({
            "kind": r.get("kind") or "local",
            "title": title[:80],
            "subtitle": (r.get("subtitle") or "").strip()[:90],
            "when": (r.get("when") or "").strip()[:32],
            "url": r.get("url") or None,
            "source": _source_of(r.get("url") or ""),
        })
    return rows


def _first_clause(text: str, limit: int = 90) -> str:
    """A TMDB overview trimmed to something that fits under a title. Cut at a
    sentence if there is one early enough, otherwise at a word boundary."""
    t = " ".join((text or "").split())
    if not t:
        return ""
    head = t.split(". ")[0]
    if len(head) <= limit:
        return head
    return t[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _source_of(url: str) -> str:
    from sources import canonical_domain
    return canonical_domain(url) if url else ""


def opening_snapshot(profile: dict) -> list[dict]:
    """Rows for the Opening section. Never raises; [] when there is nothing.

    Returns [] immediately for a user with no city — a cityless user must cost
    nothing, and a section about what is near you cannot mean anything without
    a "near".
    """
    city = (profile or {}).get("city")
    if not city:
        return []
    try:
        from weather import _geocode
        lat, lon, _resolved = _geocode(city)
    except Exception as e:
        print(f"opening: geocode failed for {city!r}: {type(e).__name__}: {e}")
        return []

    week = _week_key()
    key = (*_bucket(lat, lon), week)
    with _cache_lock:
        hit = _local_cache.get(key)
    if hit is None:
        candidates = _local_candidates(city)
        for ev in _events(lat, lon):
            candidates.append({"title": ev["title"], "url": ev.get("url"),
                               "source": "ticketmaster.com",
                               "blurb": " ".join(str(x) for x in
                                                 (ev.get("genre"), ev.get("venue"), ev.get("date"))
                                                 if x)})
        hit = _curate(city, candidates)
        with _cache_lock:
            _local_cache[key] = hit

    with _cache_lock:
        screens = _screen_cache.get(week)
    if screens is None:
        # No model call here on purpose. TMDB is already structured and already
        # ranked by vote_average, so there is no firehose to filter — and the
        # curation prompt is written for local openings, which means running
        # screens through it threw away every title for being "outside the
        # metro". A taste gate that rejects the whole input is not a gate.
        screens = [{"kind": "screen",
                    "title": (r["title"] or "")[:80],
                    "subtitle": _first_clause(r.get("blurb") or ""),
                    "when": "in theaters" if r["kind"] == "movie" else "new season",
                    "url": r.get("url"),
                    "source": "themoviedb.org"}
                   for r in _screens()[:MAX_SCREENS]]
        with _cache_lock:
            _screen_cache[week] = screens

    return (hit + screens)[:MAX_ROWS]
