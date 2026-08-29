"""Followed TV shows, and when their next episode lands.

Categorically different from the `screen` rows in Opening, and the distinction
is the whole feature. Those answer "what is new to anyone" — discovery, ranked
by popularity, the same for every user. This answers "what is new for the shows
YOU watch", and only exists because someone asked for it by name.

TMDB gives it directly: `/tv/{id}` carries `next_episode_to_air` and
`last_episode_to_air` with air dates, season and episode numbers, and titles.
One free call per show.

A followed show earns a row in the week its episode lands and goes quiet
otherwise. It never reaches the morning TEXT unless the user asks for that
separately — see `morning_prefs["episode_alerts"]`. Being on the page is
passive; being texted is not, and a weekly drumbeat of "new episode!" is
exactly the repetition this product keeps having to fix.
"""
from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from datetime import date, timedelta

TMDB_BASE = "https://api.themoviedb.org/3"

# Per user. Small on purpose: this is the list of shows someone actually
# watches, not a catalogue, and every one of them competes for a row.
FOLLOW_MAX = 6
# How far ahead a drop counts as "this week".
UPCOMING_DAYS = 7
# How long after an episode lands it still reads as news rather than history.
JUST_DROPPED_DAYS = 2

# Keyed by (show id, local date) and therefore shared by every user following
# the same show — two people watching Reacher cost one lookup, the same way the
# metro cache works for events.
_episode_cache: dict[tuple[int, str], dict | None] = {}
_cache_lock = threading.Lock()


def _key() -> str:
    import os
    return os.environ.get("TMDB_API_KEY", "")


def _get(path: str, **params) -> dict | None:
    params["api_key"] = _key()
    try:
        url = f"{TMDB_BASE}{path}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.load(r)
    except Exception as e:
        print(f"shows: {path} failed: {type(e).__name__}: {e}")
        return None


def _clear_cache() -> None:
    """Tests only."""
    with _cache_lock:
        _episode_cache.clear()


def resolve_show(name: str) -> dict | None:
    """{"id", "name"} for a show title, or None.

    Runs once when a user follows something — the write path — never on read,
    the same terms as `tickers.resolve_company_ticker` and
    `agent._city_from_weather_topic`."""
    if not name or not _key():
        return None
    data = _get("/search/tv", query=name)
    results = (data or {}).get("results") or []
    if not results:
        return None
    top = results[0]
    return {"id": top.get("id"), "name": top.get("name")}


def next_episode(show_id: int, today: date | None = None) -> dict | None:
    """What this show is doing this week, or None if nothing is.

    Returns the episode that is about to land or has just landed, whichever is
    current. A show between seasons returns None and simply does not appear —
    the row exists for the week an episode is in play, not permanently."""
    if not show_id or not _key():
        return None
    today = today or date.today()
    ckey = (int(show_id), today.isoformat())
    with _cache_lock:
        if ckey in _episode_cache:
            return _episode_cache[ckey]

    out = None
    data = _get(f"/tv/{show_id}")
    if data:
        for slot, kind in (("next_episode_to_air", "upcoming"),
                           ("last_episode_to_air", "dropped")):
            ep = data.get(slot) or {}
            when = _parse(ep.get("air_date"))
            if not when:
                continue
            delta = (when - today).days
            fits = (0 <= delta <= UPCOMING_DAYS) if kind == "upcoming" \
                else (-JUST_DROPPED_DAYS <= delta <= 0)
            if fits:
                out = {"show": data.get("name"), "show_id": show_id,
                       "season": ep.get("season_number"), "episode": ep.get("episode_number"),
                       "title": ep.get("name"), "air_date": ep.get("air_date"),
                       "days": delta, "state": kind}
                break
    with _cache_lock:
        _episode_cache[ckey] = out
    return out


def _parse(iso: str | None) -> date | None:
    try:
        return date.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def _when_label(ep: dict, today: date) -> str:
    """How the drop reads on the page. Short and human, like every other row."""
    d = ep.get("days")
    if d is None:
        return ""
    if d < 0:
        return "out now"
    if d == 0:
        return "out today"
    if d == 1:
        return "tomorrow"
    aired = _parse(ep.get("air_date"))
    return aired.strftime("%A") if aired else "this week"


def followed(profile: dict) -> list[dict]:
    return [s for s in ((profile or {}).get("shows") or []) if s.get("id")]


def episode_rows(profile: dict, today: date | None = None) -> list[dict]:
    """Opening rows for this user's followed shows. Never raises."""
    today = today or date.today()
    rows = []
    for show in followed(profile):
        try:
            ep = next_episode(show["id"], today)
        except Exception as e:
            print(f"shows: episode lookup failed for {show.get('name')!r}: "
                  f"{type(e).__name__}: {e}")
            continue
        if not ep:
            continue          # between seasons; no row this week
        label = f"S{ep['season']}E{ep['episode']}" if ep.get("season") else ""
        name = (ep.get("title") or "").strip()
        rows.append({
            "kind": "episode",
            "title": ep.get("show") or show.get("name"),
            # The episode is the news, so it goes in the subtitle where the page
            # renders it under the show name: "S4E6 - Plum Out of Luck".
            "subtitle": " - ".join(x for x in (label, name) if x)[:90],
            "when": _when_label(ep, today),
            "url": f"https://www.themoviedb.org/tv/{ep['show_id']}",
            "source": "themoviedb.org",
            "date": ep.get("air_date"),
        })
    # Soonest first: something out today outranks something on Sunday.
    rows.sort(key=lambda r: (r.get("date") or ""))
    return rows
