"""Live scores, and the small number of moments in a game worth a text.

ESPN's `site.api.espn.com` scoreboard is what every guide recommends and it
**403s from a datacenter** — verified from the dyno, not just locally, so it is
ESPN blocking Heroku rather than a sandbox quirk. `site.web.api.espn.com` is the
same shape, unblocked, and returns a whole league in one call. The core API
works too but is reference-based: seven HTTP calls for a single game's score.

Free, keyless and undocumented, which is a deliberate starting position rather
than an oversight. Everything ESPN-shaped lives behind `scoreboard()` and
`find_team()`, so swapping to a paid, supported feed is a change to this module
and nothing else.

WHAT EARNS A TEXT is the whole design. A scoring feed is a pager by
construction — an NFL game has six to ten scoring plays, and a user following
two teams on a Sunday could take twenty texts in an afternoon. Palmer spends
most of its code rationing exactly this. So an alert fires on the moments that
carry weight, not on every score:

  * the lead changes hands,
  * someone scores inside the last five minutes,
  * the game ends.

Two or three texts a game rather than ten, and `MAX_ALERTS_PER_GAME` is the
backstop when a game is genuinely wild.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

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

# Per user. Small on purpose — every followed team is a potential interruption.
FOLLOW_MAX = 4
# However wild the game, this is the most it may ever send.
MAX_ALERTS_PER_GAME = 4
# "Late" for the purposes of a scoring alert.
LATE_CLOCK_SECONDS = 5 * 60

# Two speeds. Polling every couple of minutes around the clock would be
# thousands of calls a day against an unofficial API to learn that nothing is
# happening; polling slowly during a game misses the moments entirely.
LIVE_POLL_SECONDS = 110
IDLE_POLL_SECONDS = 15 * 60

_board_cache: dict[str, tuple[float, list[dict]]] = {}
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
        "home": sides["home"],
        "away": sides["away"],
    }


def scoreboard(league: str, ttl: float = LIVE_POLL_SECONDS) -> list[dict]:
    """Every game in a league today. One HTTP call, cached briefly and shared
    across users — two people following the same league cost one fetch."""
    path = LEAGUES.get(league)
    if not path:
        return []
    now = time.time()
    with _cache_lock:
        hit = _board_cache.get(league)
    if hit and now - hit[0] < ttl:
        return hit[1]
    data = _get(f"{BASE}/{path}/scoreboard")
    if data is None:
        # Never cache a failure. Doing so served an empty board for the full
        # TTL, and `scorewatch` recomputes "is this league live" from the board
        # it gets back — so one flaky request during a game demoted the league
        # to the idle poll and lost every lead change for the next 15 minutes.
        return hit[1] if hit else []
    games = [g for g in (_parse_game(e, league) for e in data.get("events") or []) if g]
    with _cache_lock:
        _board_cache[league] = (now, games)
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
    someone up for alerts about a team in another sport."""
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


def team_game(team: dict, ttl: float = LIVE_POLL_SECONDS) -> dict | None:
    """The game this team is in today, or None."""
    abbrev = (team or {}).get("abbrev")
    for g in scoreboard((team or {}).get("league") or "", ttl):
        if abbrev in (g["home"]["abbrev"], g["away"]["abbrev"]):
            return g
    return None


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


# The last period of regulation, per league. Innings and halves are not
# quarters, and assuming they were is what made "late" mean nothing for half
# these sports.
FINAL_PERIOD = {"nfl": 4, "ncaaf": 4, "nba": 4, "nhl": 3, "mlb": 9, "mls": 2}
# Baseball has innings and no clock at all, so the inning IS the signal.
# Soccer has a clock that counts UP toward ~90 minutes rather than down to
# zero, so it needs a floor rather than a ceiling — treating the whole second
# half as "late" would make a 45-minute window the closing stretch.
CLOCKLESS = {"mlb"}
COUNTS_UP = {"mls": 80 * 60}


def _is_late(game: dict) -> bool:
    """Is this the closing stretch — the point where a score changes the game?

    Two questions, not one: are we in the final period, and if the sport has a
    countdown, is it nearly done. Extra time counts, which is why the period
    test is `>=`."""
    league = game.get("league") or ""
    if (game.get("period") or 0) < FINAL_PERIOD.get(league, 4):
        return False
    if league in CLOCKLESS:
        return True
    clock = game.get("clock") or 0
    floor = COUNTS_UP.get(league)
    if floor is not None:
        return clock >= floor
    return 0 < clock <= LATE_CLOCK_SECONDS


def alert_reason(prev: dict | None, game: dict) -> str | None:
    """Why this moment deserves a text, or None for the many that do not.

    `prev` is the last state this user was told about. The comparison is
    against what they were TOLD, not against the last poll — otherwise a score
    that arrives in the same tick as a lead change reads as two events."""
    if game["state"] == "pre":
        return None
    if not prev:
        # First sighting is a baseline whatever state it is in. ESPN's NFL
        # board carries the whole current week, so following the Eagles on a
        # Tuesday used to open with "Final: CIN 17, PHI 21" for Sunday's game.
        return None
    if game["state"] == "post":
        return None if prev.get("state") == "post" else "final"
    scored = (game["home"]["score"] != prev.get("home_score")
              or game["away"]["score"] != prev.get("away_score"))
    if not scored:
        return None
    now = leader(game)
    if now != prev.get("leader"):
        # Somebody now leads, or nobody does. Calling a tying score a lead
        # change handed the drafter "the lead just changed hands" next to a
        # standing line reading "level, tied at 21".
        return "lead" if now else "tied"
    if _is_late(game):
        return "late"
    return None
