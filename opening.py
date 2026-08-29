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

Events are pulled in two windows: the near-term week everyone sees, and a
sparser long-lead pull out to LONG_LEAD_DAYS (a year) so a big touring act
going on sale for months out can surface too — see LONG_LEAD_DAYS. The
curation prompt is what keeps that from turning into a wall of tour dates: a
distant EVENT only earns a row when it is genuinely major, at most one per
pass. Curated rows keep their event date internally (never rendered) so that
_not_expired can drop a row the moment it has happened, rather than letting a
Friday concert ride out the rest of its weekly cache window into Sunday.
"""
from __future__ import annotations

import os
import threading
import urllib.parse
from datetime import date, datetime, timedelta

from llm import client, HAIKU_MODEL, _parse_json
from netutil import _http_get_json

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
TICKETMASTER_API_KEY = os.environ.get("TICKETMASTER_API_KEY", "")

TMDB_BASE = "https://api.themoviedb.org/3"
TM_BASE = "https://app.ticketmaster.com/discovery/v2/events.json"

# How many rows survive to the page. The section is a nudge, not a listings
# magazine — four is already more than anyone reads under a weather card.
# The section is a nudge, not a listings magazine. Five rows: up to three local
# things and up to two screens, each with its own reserved allowance rather than
# competing for one pool. They competed at first, and a good week locally pushed
# screens off the page entirely — which is not the section that was asked for.
MAX_LOCAL = 3
MAX_SCREENS = 2
# Followed shows. Two, because they take their slots from screens rather than
# adding to the row count, and a page that is all television is not the section.
MAX_EPISODES = 2
MAX_ROWS = MAX_LOCAL + MAX_SCREENS

# The three kinds of row, and the words a user reaches for when asking for or
# dropping one. A user says "I want movie openings too" or "stop the concerts";
# they never say "kind=screen".
ALL_KINDS = ("local", "event", "screen")
KIND_WORDS = {
    "restaurants": "local", "places": "local", "food": "local", "bars": "local",
    "events": "event", "concerts": "event", "festivals": "event", "shows": "event",
    "movies": "screen", "films": "screen", "streaming": "screen", "tv": "screen",
}
# Curate a slightly deeper pool than any one user will see. The pool is cached
# per metro and shared, so per-user filtering needs something left to draw from
# — a user who only wants restaurants should not come up empty because the
# three cached rows happened to all be concerts. Costs nothing extra: same one
# Haiku call, a few more output tokens.
CURATE_POOL = 6
# Radius for the events pull. 25 miles is a metro, not a neighbourhood, which
# is deliberate: nobody picks a concert by how close it is to their street.
EVENT_RADIUS_MI = 25

# The long-lead events pull runs alongside the near-term week, out to a year.
# Users want to hear about a big show on sale for months out, not just what's
# happening in the next seven days. It stays a separate, smaller pull (15 vs
# 20) rather than just widening the one query — a year of relevance-sorted
# results is mostly noise, and the curation prompt is the real filter, told to
# let a distant date through only when it is genuinely major.
LONG_LEAD_DAYS = 365
LONG_LEAD_SIZE = 15

# Coarse lat/lon bucket for the cache key, in degrees. 0.5 is roughly 35 miles,
# so Culver City (34.02, -118.40) and Woodland Hills (34.17, -118.61) land in
# one bucket and share a single fetch — which is the whole cost argument. A
# city -> metro lookup table would do the same job and would need maintaining
# forever; rounding does not.
BUCKET = 0.5

# Cached by COST, not by convenience. Only _local_candidates spends money (two
# Tavily searches); Ticketmaster is free at 5,000/day and TMDB is free. Keying
# all three weekly was protecting a cost that exists for one of them, and it
# froze every row Monday to Sunday — the same movie every day, and nothing but
# the current weekend.
#
#   _candidate_cache  paid   (bucket, ISO week)  the Tavily rows
#   _local_cache      free   (bucket, local day) curation over those + events
#   _screen_cache     free   (local day)         TMDB, national
_candidate_cache: dict[tuple, list[dict]] = {}
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
        _candidate_cache.clear()
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


def _local_candidates(metro: str) -> list[dict]:
    """New-opening coverage from local press.

    There is no structured API for "restaurants that opened this month", but
    it is a well-established local-media beat — Eater, Time Out, LAist and the
    city dailies all run it as a standing column. Those outlets are tier 2 in
    trusted_sources.json specifically so this search can pass trusted_only and
    still return something; before they were added it returned nothing at all.
    """
    from datafeeds import _search_raw
    from sources import canonical_domain
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


def _events(lat: float, lon: float, start_days: int = 0, end_days: int = 7,
            size: int = 20) -> list[dict]:
    """Concerts and festivals from Ticketmaster Discovery.

    Queried by latlong rather than city name on purpose. A city string makes
    small suburbs dead ends — the whole reason the SerpAPI events path failed
    for Culver City — while a coordinate plus a radius asks the question people
    actually mean: what is on near me.

    `start_days`/`end_days` let one function serve two different pulls: the
    near-term week everyone sees (the default), and a separate, sparser
    long-lead pull (see LONG_LEAD_DAYS below) for shows worth advance notice —
    a stadium tour going on sale for December is exactly what a weekly
    this-week-only scan would never surface.
    """
    if not TICKETMASTER_API_KEY:
        return []
    start = datetime.utcnow() + timedelta(days=start_days)
    end = datetime.utcnow() + timedelta(days=end_days)
    url = (f"{TM_BASE}?apikey={TICKETMASTER_API_KEY}"
           f"&latlong={lat:.4f},{lon:.4f}&radius={EVENT_RADIUS_MI}&unit=miles"
           f"&startDateTime={start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
           f"&endDateTime={end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
           f"&size={size}&sort=relevance,desc"
           # Music and Arts only. Unfiltered, a metro's next seven days are
           # mostly regular-season ball games — eight Cardinals fixtures crowded
           # out every concert in St. Louis — and a Tuesday home game is not
           # something opening. Filtering here rather than in the prompt keeps
           # the candidate list short and the curation call cheap.
           f"&segmentName=Music&segmentName=Arts%20%26%20Theatre")
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


# What a screen row has to clear to count as a recommendation.
#
# Ranking by vote_average with no floor is meaningless and it showed: "Toxic: A
# Fairy Tale for Grown-ups" scored 6.23 from THIRTEEN votes and went to every
# user on the system as a recommendation. A rating from thirteen people is not
# comparable to one from six hundred, and sorting the two together is noise
# wearing the shape of taste.
#
# Popularity is the better ranker for "new": a film released three days ago has
# not accumulated votes yet no matter how good it is, but TMDB's popularity
# already reflects that people are looking it up. The vote floor then removes
# the long tail that popularity alone still lets through.
MIN_VOTES = 150
# A film is "new in theaters" for weeks, not seven days. The old +/-7 day window
# is what forced the ranking down into the 13-vote tail: almost nothing good was
# left inside it.
SCREEN_WINDOW_DAYS = 30
# TV needs a smaller floor — a genuinely new series has fewer ratings than a
# wide theatrical release, and requiring 150 leaves nothing at all. Note this
# one is enforced by TMDB via vote_count.gte in the query rather than by us,
# unlike MIN_VOTES which now_playing gives no way to push server-side.
MIN_TV_VOTES = 50


def _screens() -> list[dict]:
    """Movies and shows actually worth telling someone about, from TMDB.

    National, so this cache is global rather than per-metro.

    Both halves used the wrong endpoint. `/tv/on_the_air` means *currently
    airing*, not new — it returns Ted Lasso, Reacher and Silo, running for
    years — and the only thing making those look new was a filter on
    first_air_date, which instead surfaced obscure foreign premieres and, one
    slot down, a Brazilian nightly news programme from 1969. `/discover/tv`
    with a real premiere window asks the question we actually meant.
    """
    if not TMDB_API_KEY:
        return []
    today = date.today()
    window_start = today - timedelta(days=SCREEN_WINDOW_DAYS)

    def _in_window(iso: str | None) -> bool:
        try:
            d = date.fromisoformat(iso)
        except (TypeError, ValueError):
            return False
        return window_start <= d <= today + timedelta(days=7)

    out = []
    movies = _http_get_json(
        f"{TMDB_BASE}/movie/now_playing?api_key={TMDB_API_KEY}&region=US&page=1", timeout=10)
    for m in (movies or {}).get("results", [])[:20]:
        if _in_window(m.get("release_date")) and (m.get("vote_count") or 0) >= MIN_VOTES:
            out.append({"title": m.get("title"), "kind": "movie",
                        "date": m.get("release_date"),
                        "blurb": (m.get("overview") or "")[:240],
                        "score": m.get("popularity") or 0,
                        "url": f"https://www.themoviedb.org/movie/{m.get('id')}"})

    params = urllib.parse.urlencode({
        "api_key": TMDB_API_KEY,
        "first_air_date.gte": window_start.isoformat(),
        "first_air_date.lte": today.isoformat(),
        "sort_by": "popularity.desc",
        "vote_count.gte": MIN_TV_VOTES,
        "with_original_language": "en",
    })
    shows = _http_get_json(f"{TMDB_BASE}/discover/tv?{params}", timeout=10)
    for sh in (shows or {}).get("results", [])[:10]:
        out.append({"title": sh.get("name"), "kind": "show",
                    "date": sh.get("first_air_date"),
                    "blurb": (sh.get("overview") or "")[:240],
                    "score": sh.get("popularity") or 0,
                    "url": f"https://www.themoviedb.org/tv/{sh.get('id')}"})

    out.sort(key=lambda r: r["score"], reverse=True)
    return out


_CURATE_PROMPT = """Today is {today}. Most candidates are dated this week or next; a few may be weeks or months further out — Ticketmaster lists shows as soon as they go on sale, sometimes up to a year ahead.

You are picking rows for a section called "Opening" on someone's personal page. It answers two questions: what is newly open or worth catching near them this week, and separately, what big or notable thing further out is worth telling them about now.

Their area: {city} — venues anywhere in this metro count as near them, including neighbouring towns and the central city.

CANDIDATES (JSON):
{candidates}

Return JSON: {{"rows": [{{"title": "...", "subtitle": "...", "when": "...", "url": "...", "kind": "local|event|screen", "date": "..."}}]}}

Pick up to {max_rows}, best first. If that many clear the bar, return that many — under-filling is as wrong as padding, and a metro with three good things happening should show three.

Order matters: something happening in the next few days outranks something weeks away, which outranks a dated event months out; any dated event outranks an article about a place. A named act at a named venue this week is one of the strongest rows this section can have.

A dated EVENT more than about three weeks from {today} only earns a row if it is genuinely major — a well-known touring artist, a marquee festival, something someone would want advance notice of to plan around or buy tickets before it sells out. Allow at most one such row per pass; a handful of distant tour dates must never crowd out what is actually happening near {today}.

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
- Tribute acts and cover bands — "A Tribute to", "The Music of", "performs the hits of". A touring artist under their own name at a real venue is NOT this. That is one of the best rows this section can have, and it being on sale is not a mark against it.
- Anything you cannot name specifically. If the source only says "several new restaurants", drop it.
- Anything already past relative to {today}, or PLACE coverage older than about a month. Judge that against the date at the top of this prompt and nothing else. A dated EVENT further out is fine on its own terms — see the major-event rule above — as long as it has not already happened.

Writing the row:
- title: the name of the place, event or title. Nothing else. No city, no verb.
- subtitle: at most 8 words on why it is worth knowing. Concrete, not promotional. Never "a must-visit" or "don't miss". For an EVENT this is usually just the venue name, nothing else — the day already goes in `when`, so do not repeat "Friday" or a date here too. For a PLACE, say what makes it distinct (a chef, a neighborhood, a dish) rather than restating the venue name from the title.
- when: short and human — a day name for something dated within the next couple weeks ("Friday"), a short date for anything further out ("Oct 12") so it reads as advance notice rather than this week's plans, or "opened this week", "in theaters", "streaming now" for undated rows. Pick ONE form, not both — "Friday" on its own is enough; do not also add the date ("Friday, August 29").
- url: copy verbatim from the candidate. Never invent one.
- date: for an EVENT with a known date, copy it verbatim in YYYY-MM-DD form from the candidate data. Empty string for a PLACE, a screen, or anything undated.
- Plain ASCII. No emoji, no markdown, no exclamation marks.
- subtitle and when must never say the same thing twice between them — if you catch yourself writing the same day, date, or venue in both fields, cut it from subtitle.

If nothing clears the bar, return {{"rows": []}}. An empty section is correct and normal; a padded one is not."""


def _curate(city: str, candidates: list[dict]) -> list[dict]:
    """The taste gate. One Haiku call, on the write path only.

    Everything upstream of this is a firehose: local press runs listicles and
    restaurant-week promos alongside real openings, and Ticketmaster will
    happily return every cover band within 25 miles. Filtering that with
    keywords was never going to work — the difference between "Mamele's opened
    on Washington" and "15 best brunch spots in LA" is editorial, not lexical.

    The prompt states today's date, and that is load-bearing rather than
    decorative. Without it the model dates events against its training cutoff:
    handed a concert on 2026-08-29 it called it "over a year away" and dropped
    it under the stale-content rule, rejecting all seventeen candidates for a
    metro whose week held Todd Rundgren, The Wallflowers and Ray LaMontagne.
    It read as a taste problem and was a calendar problem.

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
                today=date.today().strftime("%A, %B %d, %Y"), city=city,
                candidates=json.dumps(candidates)[:9000], max_rows=CURATE_POOL)}],
        )
        parsed = _parse_json(resp.content[0].text) or {}
    except Exception as e:
        print(f"opening: curation failed for {city!r}: {type(e).__name__}: {e}")
        return []

    rows = []
    for r in (parsed.get("rows") or [])[:CURATE_POOL]:
        title = (r.get("title") or "").strip()
        if not title:
            continue
        # "screen" is reserved for TMDB rows, which are built in code below and
        # never pass through here. Letting the model pick it meant a live
        # theatre listing from Ticketmaster was tagged as a screen — and
        # kind == "screen" is what puts TMDB's attribution on the page, so the
        # page would have credited TMDB for data TMDB never supplied.
        kind = r.get("kind") if r.get("kind") in ("local", "event") else "local"
        rows.append({
            "kind": kind,
            "title": title[:80],
            "subtitle": (r.get("subtitle") or "").strip()[:90],
            "when": (r.get("when") or "").strip()[:32],
            "url": r.get("url") or None,
            "source": _source_of(r.get("url") or ""),
            # Kept only to expire the row once its date passes — see
            # opening_snapshot. Not rendered; "when" is what the page shows.
            "date": _valid_iso_date(r.get("date")),
        })
    return rows


# Beyond this many days out, a row is advance notice rather than a plan for the
# weekend, and it competes in its own reserved slot instead of against tonight.
FAR_HORIZON_DAYS = 10


def _is_far(row: dict, today: date) -> bool:
    d = row.get("date")
    if not d:
        return False           # undated rows are places and screens, never "far"
    try:
        return (date.fromisoformat(d) - today).days > FAR_HORIZON_DAYS
    except (TypeError, ValueError):
        return False


def _rotate(rows: list, today: date, take: int) -> list:
    """`take` rows, starting at a different offset each day.

    Same deterministic trick as morning._rotated_topics: a retry inside one day
    picks the same rows, and tomorrow picks different ones, with nothing stored.
    Without it the screens list served its top two by score every day of the
    week — "Colony" and "Coyote vs. Acme" to every user, while four other
    candidates were never shown at all."""
    if not rows or take <= 0:
        return []
    if len(rows) <= take:
        return rows
    offset = today.toordinal() % len(rows)
    return (rows[offset:] + rows[:offset])[:take]


def _valid_iso_date(s) -> str | None:
    try:
        date.fromisoformat(s)
        return s
    except (TypeError, ValueError):
        return None


def _not_expired(row: dict, today: date) -> bool:
    """A cached row is live until the date on it says otherwise.

    The metro cache lasts a week (see BUCKET/_week_key), but an event's date
    does not respect that boundary — a Friday concert cached on Monday is
    still a valid row on Wednesday and a stale one on Saturday. Filtering here,
    on every read, is what keeps a passed show from riding out the rest of its
    cache window; undated rows (a restaurant opening, a screen) have nothing
    to expire against and are always live.
    """
    d = row.get("date")
    if not d:
        return True
    try:
        return date.fromisoformat(d) >= today
    except (TypeError, ValueError):
        return True


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


def wanted_kinds(profile: dict) -> tuple[str, ...]:
    """Which kinds of row this user wants. All three unless they have said
    otherwise — flexibility is opt-out, not opt-in, so a new user gets the whole
    section and trims it down by asking."""
    prefs = (profile or {}).get("morning_prefs") or {}
    chosen = prefs.get("opening_kinds")
    if not isinstance(chosen, list):
        return ALL_KINDS
    return tuple(k for k in ALL_KINDS if k in chosen)


def opening_snapshot(profile: dict) -> list[dict]:
    """Rows for the Opening section. Never raises; [] when there is nothing.

    Returns [] immediately for a user with no city — a cityless user must cost
    nothing, and a section about what is near you cannot mean anything without
    a "near".
    """
    city = (profile or {}).get("city")
    if not city:
        return []
    kinds = wanted_kinds(profile)
    if not kinds:
        return []          # they removed every kind — same as switching it off
    try:
        from weather import _geocode
        lat, lon, _resolved = _geocode(city)
    except Exception as e:
        print(f"opening: geocode failed for {city!r}: {type(e).__name__}: {e}")
        return []

    from timeutil import local_today
    today = local_today(profile.get("timezone"))
    bucket = _bucket(lat, lon)
    week = _week_key(today)

    # The paid half, still weekly. Local-press coverage of new openings turns
    # over on the order of weeks, so re-searching it daily would spend four
    # times as much to find the same articles.
    ckey = (*bucket, week)
    with _cache_lock:
        candidates = _candidate_cache.get(ckey)
    metro = None
    if candidates is None:
        # Resolve the metro INSIDE the miss branch. It costs a model call, and
        # opening_snapshot runs on page views.
        #
        # It is used for BOTH the search and the curation prompt. Handing the
        # raw city to the prompt made the model reject its own metro: told
        # "their city: Kirkwood, MO", it correctly dropped every venue in
        # St. Louis as somewhere else, which is all of them.
        metro = _metro(city)
        candidates = _local_candidates(metro)
        with _cache_lock:
            _candidate_cache[ckey] = candidates

    # The free half, daily. Ticketmaster allows 5,000 calls a day and we make
    # two per metro, so there is no reason for a user to look at Tuesday's page
    # and see Sunday's events.
    dkey = (*bucket, today.isoformat())
    with _cache_lock:
        hit = _local_cache.get(dkey)
    if hit is None:
        near = _events(lat, lon)
        far = _events(lat, lon, start_days=7, end_days=LONG_LEAD_DAYS,
                      size=LONG_LEAD_SIZE)
        pool = list(candidates)
        for ev in near + far:
            pool.append({"title": ev["title"], "url": ev.get("url"),
                         "source": "ticketmaster.com",
                         "blurb": " ".join(str(x) for x in
                                           (ev.get("genre"), ev.get("venue"), ev.get("date"))
                                           if x)})
        hit = _curate(metro or _metro(city), pool)
        with _cache_lock:
            _local_cache[dkey] = hit

    with _cache_lock:
        screens = _screen_cache.get(today.isoformat())
    if screens is None:
        # No model call here on purpose. TMDB is already structured and already
        # ranked by vote_average, so there is no firehose to filter — and the
        # curation prompt is written for local openings, which means running
        # screens through it threw away every title for being "outside the
        # metro". A taste gate that rejects the whole input is not a gate.
        #
        # The full candidate list is cached and rotated at read time, NOT
        # trimmed to MAX_SCREENS here — trimming at fetch time is what served
        # the same top two every day and buried the other four.
        screens = [{"kind": "screen",
                    "title": (r["title"] or "")[:80],
                    "subtitle": _first_clause(r.get("blurb") or ""),
                    # "new season" was wrong once /discover/tv replaced
                    # /tv/on_the_air: these are series that PREMIERED inside the
                    # window, not returning ones.
                    "when": "in theaters" if r["kind"] == "movie" else "new series",
                    "url": r.get("url"),
                    "source": "themoviedb.org"}
                   for r in _screens()]
        with _cache_lock:
            _screen_cache[today.isoformat()] = screens

    # Expire past-dated rows on every read. Against the READER's date, not the
    # server's: the dyno runs UTC, so from 5pm Pacific date.today() is already
    # tomorrow and tonight's show would vanish hours before it starts.
    live = [r for r in hit if _not_expired(r, today)]

    # Filter per user HERE, at the end — never at fetch time. The caches are
    # keyed by metro and shared across every user in it, which is the whole cost
    # model; narrowing a fetch to one user's taste would make the cache
    # unshareable and turn N users back into N fetches.
    # Followed shows first. They are the only rows on this page someone asked
    # for by name, so they outrank discovery — and they DISPLACE screens rather
    # than adding to the total, because a show you actually watch is worth more
    # than a film chosen for you. They also bypass `kinds`: that setting is
    # about which kinds of discovery you want, and a followed show is not
    # discovery. Unfollowing is its control, not opening_remove.
    try:
        from shows import episode_rows
        episodes = episode_rows(profile, today)[:MAX_EPISODES]
    except Exception as e:
        print(f"opening: episode rows unavailable: {type(e).__name__}: {e}")
        episodes = []
    screen_allowance = max(MAX_SCREENS - len(episodes), 0)
    picked_screens = _rotate(screens, today, screen_allowance) if "screen" in kinds else []
    local_allowance = MAX_ROWS - len(picked_screens) - len(episodes)
    mine = [r for r in live if r.get("kind") in kinds]

    # Reserve the last local slot for something further out. Every candidate in
    # the next seven days outranks everything beyond them, so with a busy metro
    # the long-lead pull — Kacey Musgraves in twelve days, Zac Brown in eleven —
    # never won a slot and the section read as this weekend, forever.
    soon = [r for r in mine if not _is_far(r, today)]
    later = [r for r in mine if _is_far(r, today)]
    picked_local = soon[:max(local_allowance - 1, 0)] if later else soon[:local_allowance]
    if later and len(picked_local) < local_allowance:
        picked_local = picked_local + _rotate(later, today, local_allowance - len(picked_local))

    return episodes + picked_local + picked_screens
