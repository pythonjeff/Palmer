"""Scores for the teams a user follows, read on a schedule rather than live.

ESPN's `site.api.espn.com` scoreboard is what every guide recommends and it
**403s from a datacenter** — verified from the dyno, not just locally, so it is
ESPN blocking Heroku rather than a sandbox quirk. `site.web.api.espn.com` is the
same shape, unblocked, and returns a whole league in one call. The core API
works too but is reference-based: seven HTTP calls for a single game's score.

Free, keyless and undocumented, which is a deliberate starting position rather
than an oversight. Everything ESPN-shaped lives behind `scoreboard()` and
`find_teams()`, so swapping to a paid, supported feed is a change to this
module and nothing else.

WHAT THIS IS NOT: a live alert feed. Palmer used to poll every two minutes
during a game and text on lead changes, late scores and the final. That was a
pager by construction — three texts a game, more on a Sunday with two teams —
and it is gone. A followed team now surfaces in exactly three places: the
morning update (last night's result, tonight's game), the evening update (how
today's game went, or stands), and the Scores section of the page.
`team_day()` is the one read all three share.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

BASE = "https://site.web.api.espn.com/apis/site/v2/sports"
_UA = {"User-Agent": "Mozilla/5.0 (compatible; PalmerSMS/1.0)"}

# The leagues worth supporting. Adding one is a line here; nothing else changes.
LEAGUES = {
    "nfl": "football/nfl",
    "mlb": "baseball/mlb",
    "nba": "basketball/nba",
    "nhl": "hockey/nhl",
    "ncaaf": "football/college-football",
    "mls": "soccer/usa.1",
}

# Per user. Small on purpose — every followed team is a row in two daily
# updates, and four is already a lot of sport for one text.
FOLLOW_MAX = 4

# How long a fetched board is served before it is refetched. One speed now:
# nothing polls during a game any more, so the board only has to be fresh
# enough for a page view or a `get_score` question, and two minutes is that.
BOARD_TTL_SECONDS = 120

_board_cache: dict[tuple[str, str | None], tuple[float, list[dict]]] = {}
_team_cache: dict[str, list[dict]] = {}
_cache_lock = threading.Lock()


def _get(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=12) as r:
            return json.load(r)
    except Exception as e:
        print(f"sports: {url.rsplit('/', 2)[-2:]} failed: {type(e).__name__}: {e}")
        return None


def _clear_cache() -> None:
    """Tests only."""
    with _cache_lock:
        _board_cache.clear()
        _team_cache.clear()


def _parse_game(ev: dict, league: str) -> dict | None:
    comp = (ev.get("competitions") or [{}])[0]
    status = (comp.get("status") or {})
    stype = status.get("type") or {}
    sides = {}
    for c in comp.get("competitors") or []:
        team = c.get("team") or {}
        try:
            score = int(c.get("score"))
        except (TypeError, ValueError):
            score = 0
        sides[c.get("homeAway")] = {
            "abbrev": team.get("abbreviation"),
            "name": team.get("displayName") or team.get("name"),
            "score": score,
        }
    if "home" not in sides or "away" not in sides:
        return None
    return {
        "id": str(ev.get("id")),
        "league": league,
        "short": ev.get("shortName"),
        "state": stype.get("state"),          # pre | in | post
        "detail": stype.get("shortDetail") or stype.get("detail") or "",
        "period": status.get("period") or 0,
        "clock": status.get("clock") or 0,    # seconds remaining in the period
        "date": (ev.get("date") or "")[:10],  # ISO day of the game, ESPN's clock
        "home": sides["home"],
        "away": sides["away"],
    }


def scoreboard(league: str, ttl: float = BOARD_TTL_SECONDS,
               day: date | None = None) -> list[dict]:
    """Every game in a league — today's by default, or one calendar day's when
    `day` is given. One HTTP call, cached briefly and shared across users: two
    people following the same league on the same day cost one fetch.

    `day` maps to ESPN's `dates=YYYYMMDD` parameter. Without it the NFL board
    carries the whole current week, which is what made a Tuesday follow open
    with Sunday's final; with it, the board is exactly that day's games."""
    path = LEAGUES.get(league)
    if not path:
        return []
    key = (league, day.isoformat() if day else None)
    now = time.time()
    with _cache_lock:
        hit = _board_cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    url = f"{BASE}/{path}/scoreboard"
    if day:
        url += f"?dates={day.strftime('%Y%m%d')}"
    data = _get(url)
    if data is None:
        # Never cache a failure. Doing so served an empty board for the full
        # TTL, and a blank board reads as "no game today" to every caller.
        return hit[1] if hit else []
    games = [g for g in (_parse_game(e, league) for e in data.get("events") or []) if g]
    with _cache_lock:
        _board_cache[key] = (now, games)
    return games


def _teams(league: str) -> list[dict]:
    """Every team in a league. Rosters change once a year, so this is cached for
    the life of the dyno."""
    with _cache_lock:
        hit = _team_cache.get(league)
    if hit is not None:
        return hit
    data = _get(f"{BASE}/{LEAGUES[league]}/teams")
    if data is None:
        # A transient blip must not be cached for the life of the dyno. It was:
        # one failed fetch on the first `follow_team` after a deploy and Palmer
        # answered "no team matches 'Eagles'" to everyone until the next restart.
        return []
    group = ((data.get("sports") or [{}])[0].get("leagues") or [{}])[0]
    out = []
    for entry in group.get("teams") or []:
        t = entry.get("team") or {}
        out.append({"league": league, "abbrev": t.get("abbreviation"),
                    "name": t.get("displayName"),
                    "_match": {str(t.get(k) or "").lower()
                               for k in ("displayName", "name", "location",
                                         "abbreviation", "nickname")} - {""}})
    if out:
        with _cache_lock:
            _team_cache[league] = out
    return out


def _warm_teams() -> None:
    """Populate the team cache for every league at once.

    `find_teams` has to consult all six leagues to know whether a name is
    ambiguous, and this runs on the inbound reply path with the per-phone lock
    held. Serially, a cold cache against a slow ESPN was six 12-second timeouts
    back to back; concurrently the worst case is one."""
    cold = [lg for lg in LEAGUES if lg not in _team_cache]
    if len(cold) < 2:
        return
    with ThreadPoolExecutor(max_workers=len(cold)) as pool:
        list(pool.map(_teams, cold))


def find_teams(query: str) -> list[dict]:
    """Every team matching a name, across every league.

    Returns a LIST because "Cardinals" is three teams and "Rangers" is two —
    naming a team is genuinely ambiguous in a way that naming a TV show is not.
    The caller asks which one rather than guessing; guessing here would sign
    someone up for updates about a team in another sport."""
    q = (query or "").strip().lower()
    if not q:
        return []
    _warm_teams()
    out = []
    for league in LEAGUES:
        for t in _teams(league):
            if q in t["_match"]:
                out.append({k: v for k, v in t.items() if k != "_match"})
    if out:
        return out
    # Nothing exact — try a contained match, so "philadelphia eagles" and
    # "the eagles" both land.
    for league in LEAGUES:
        for t in _teams(league):
            if any(q in n or n in q for n in t["_match"] if len(n) > 3):
                out.append({k: v for k, v in t.items() if k != "_match"})
    return out


def _game_for(team: dict, games: list[dict]) -> dict | None:
    abbrev = (team or {}).get("abbrev")
    for g in games:
        if abbrev in (g["home"]["abbrev"], g["away"]["abbrev"]):
            return g
    return None


def team_game(team: dict, ttl: float = BOARD_TTL_SECONDS) -> dict | None:
    """The game this team is in today, or None. The `get_score` read."""
    return _game_for(team, scoreboard((team or {}).get("league") or "", ttl))


def team_day(team: dict, today: date) -> dict:
    """What the morning and evening updates say about one team.

    Two boards, both keyed on the READER's calendar day:

      last   yesterday's game, if it has finished — "the Cards lost 5-2"
      today  today's game in whatever state it is in — "play the Cubs at 7",
             "up 3-1 in the sixth", or "beat the Cubs 5-2" once it ends

    A team with nothing on either day yields both None, and the callers show
    nothing: a followed team between games is not a permanent row, the same
    rule `shows.py` applies to a series between seasons."""
    league = (team or {}).get("league") or ""
    last = _game_for(team, scoreboard(league, day=today - timedelta(days=1)))
    if last and last.get("state") != "post":
        last = None
    return {"last": last, "today": _game_for(team, scoreboard(league, day=today))}


def describe(game: dict) -> str:
    """One plain line: who is winning and where the game is."""
    h, a = game["home"], game["away"]
    if game["state"] == "pre":
        return f"{a['abbrev']} at {h['abbrev']}, {game['detail']}"
    line = f"{a['abbrev']} {a['score']}, {h['abbrev']} {h['score']}"
    return f"{line} - {game['detail']}" if game.get("detail") else line


def leader(game: dict) -> str | None:
    """"home", "away", or None when it is tied."""
    h, a = game["home"]["score"], game["away"]["score"]
    if h == a:
        return None
    return "home" if h > a else "away"


def side_of(game: dict, abbrev: str) -> str | None:
    """Which side of this game a team is on."""
    for side in ("home", "away"):
        if game[side]["abbrev"] == abbrev:
            return side
    return None


def result_line(game: dict, team: dict) -> str:
    """The game from this team's point of view, as a fact with no adjectives:
    "beat the Cubs 5-2", "lost to the Cubs 2-5", "up 3-1 on the Cubs, Top 6th",
    "play the Cubs, 7:15 PM ET". Source data for a drafter that already carries
    the system prompt, so the voice is deliberately absent here."""
    side = side_of(game, team.get("abbrev")) or "home"
    other = "away" if side == "home" else "home"
    mine, theirs = game[side]["score"], game[other]["score"]
    opp = game[other].get("name") or game[other].get("abbrev") or "them"
    detail = game.get("detail") or ""
    if game["state"] == "pre":
        return f"play {opp}" + (f", {detail}" if detail else "")
    if game["state"] == "post":
        if mine > theirs:
            return f"beat {opp} {mine}-{theirs}"
        if mine < theirs:
            return f"lost to {opp} {mine}-{theirs}"
        return f"drew {opp} {mine}-{theirs}"
    standing = ("up" if mine > theirs else "down" if mine < theirs else "level")
    return f"{standing} {mine}-{theirs} vs {opp}" + (f", {detail}" if detail else "")
