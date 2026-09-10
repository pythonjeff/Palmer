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
morning update (last night's result, tonight's game), the Scores section of
the page, and — every week or two — the paced check-in in followup.py.
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
    """What the morning update, the page and the check-in say about one team.

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


# ---- live texts: opt-in, two levels ------------------------------------------
# A followed team gets no live texts unless the user asked. `live` on the team
# dict is the ask, and the level decides how much:
#
#   off   the default — morning update and page only
#   key   the lead changing hands, a score in the closing stretch, the final
#   all   every score, plus the final
#
# A scoring feed is a pager by construction — an NFL game has six to ten
# scoring plays, and two teams on a Sunday is twenty texts in an afternoon —
# which is why "key" is what Palmer offers first, and why even "all" has a
# per-game cap and a league it does not apply to.
LIVE_MODES = ("off", "key", "all")
MAX_ALERTS_PER_GAME = 4          # key moments; however wild the game
MAX_ALERTS_PER_GAME_ALL = 20     # every score: a backstop, not a budget
LATE_CLOCK_SECONDS = 5 * 60

# Leagues where "every score" is a text someone could want. A basket lands
# every thirty seconds, so for the NBA "all" means key moments.
EVERY_SCORE_LEAGUES = {"nfl", "ncaaf", "mlb", "nhl", "mls"}

# Two speeds for the poller. Polling every couple of minutes around the clock
# would be thousands of calls a day against an unofficial API to learn that
# nothing is happening; polling slowly during a game misses the moments.
LIVE_POLL_SECONDS = 110
IDLE_POLL_SECONDS = 15 * 60

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


def live_mode(team: dict) -> str:
    """The live-text level a followed team was set to. Absent or unknown is off."""
    mode = (team or {}).get("live") or "off"
    return mode if mode in LIVE_MODES else "off"


def alert_cap(mode: str) -> int:
    return MAX_ALERTS_PER_GAME_ALL if mode == "all" else MAX_ALERTS_PER_GAME


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


def scorer(prev: dict | None, game: dict) -> str | None:
    """Which side's score moved since they were last told: "home", "away", or None."""
    if not prev:
        return None
    if game["home"]["score"] != prev.get("home_score"):
        return "home"
    if game["away"]["score"] != prev.get("away_score"):
        return "away"
    return None


def alert_reason(prev: dict | None, game: dict, mode: str = "key") -> str | None:
    """Why this moment deserves a text, or None for the many that do not.

    `prev` is the last state this user was told about. The comparison is
    against what they were TOLD, not against the last poll — otherwise a score
    that arrives in the same tick as a lead change reads as two events.

    `mode` is the level they asked for. "key" allows the lead changing hands,
    a score in the closing stretch, and the final; "all" adds every other
    score, in the leagues where that is a text anyone could want."""
    if game.get("state") == "pre":
        return None
    if not prev:
        # First sighting is a baseline whatever state it is in. ESPN's NFL
        # board carries the whole current week, so following the Eagles on a
        # Tuesday used to open with "Final: CIN 17, PHI 21" for Sunday's game.
        return None
    if game.get("state") == "post":
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
    if mode == "all" and (game.get("league") or "") in EVERY_SCORE_LEAGUES:
        return "score"
    return None
