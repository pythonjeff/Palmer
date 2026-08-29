"""Forecast accuracy audit — does NWS or Open-Meteo actually get this city right?

Palmer told a Woodland Hills user 103, 106, 107 and 111 on four consecutive
days. The actual highs at those coordinates were 98.3, 96.8, 97.8 and 99.5, and
the nearest real stations read 100-102. In the same week NWS was the single best
source available for Culver City, +1.7F against actuals where every raw model
ran 5-11F hot.

So neither source wins everywhere, a median is worse than either at one of the
two, and any rule picked from two cities and four days is fitted to anecdotes.
This module exists so the choice can be made from data instead: once a day it
records each source's forecast for each city Palmer serves, and the next day it
fills in what actually happened. `db.forecast_scores` then reports the signed
bias and mean absolute error per city per source.

Everything here is free and keyless — NWS, Open-Meteo forecast, and Open-Meteo's
reanalysis archive for the actuals. It sends nothing and is never on a user path,
so a failure is a gap in the log and nothing else.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from db import get_all_profiles, record_forecast, record_actual, pending_actuals

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"

# The models worth scoring. best_match is Open-Meteo's own pick and is what the
# fallback path would serve; ecmwf and icon are here because they bracketed the
# truth in opposite directions during the incident that prompted this.
MODELS = ("best_match", "ecmwf_ifs025", "icon_seamless")
# How far back to keep chasing a missing actual. Reanalysis lags a few days, so
# a same-day lookup returns nothing and the row waits.
BACKFILL_DAYS = 10


def _get(url: str, params: dict) -> dict | None:
    try:
        with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}", timeout=25) as r:
            return json.load(r)
    except Exception as e:
        print(f"wxaudit: {url.rsplit('/', 1)[-1]} failed: {type(e).__name__}: {e}")
        return None


def _cities() -> dict[str, tuple[float, float]]:
    """Every distinct city Palmer serves, with coordinates. One geocode each —
    weather._geocode caches, so this is usually free."""
    from weather import _geocode
    out: dict[str, tuple[float, float]] = {}
    for _phone, profile in get_all_profiles():
        city = (profile or {}).get("city")
        if not city or city in out:
            continue
        try:
            lat, lon, _resolved = _geocode(city)
            out[city] = (lat, lon)
        except Exception as e:
            print(f"wxaudit: geocode failed for {city!r}: {type(e).__name__}: {e}")
    return out


def _log_todays_forecasts(city: str, lat: float, lon: float, today: str) -> None:
    from weather import weather_snapshot, _is_us_coords
    if _is_us_coords(lat, lon):
        try:
            snap = weather_snapshot(city)
            if snap and snap.get("high") is not None and snap.get("source") == "nws":
                record_forecast(city, today, "nws", float(snap["high"]))
        except Exception as e:
            print(f"wxaudit: nws forecast failed for {city!r}: {type(e).__name__}: {e}")

    for model in MODELS:
        d = _get(FORECAST, {"latitude": lat, "longitude": lon,
                            "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
                            "timezone": "auto", "forecast_days": 1, "models": model})
        highs = ((d or {}).get("daily") or {}).get("temperature_2m_max") or []
        if highs and highs[0] is not None:
            record_forecast(city, today, model, float(highs[0]))


def _fill_actual(city: str, lat: float, lon: float, day: str) -> bool:
    d = _get(ARCHIVE, {"latitude": lat, "longitude": lon, "start_date": day, "end_date": day,
                       "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
                       "timezone": "auto"})
    highs = ((d or {}).get("daily") or {}).get("temperature_2m_max") or []
    if not highs or highs[0] is None:
        return False           # reanalysis hasn't landed yet; try again tomorrow
    record_actual(city, day, float(highs[0]))
    return True


def run_forecast_audit() -> None:
    """Log today's forecasts, then backfill any actuals that have landed.

    Called once a day by the scheduler. Never raises."""
    try:
        cities = _cities()
        if not cities:
            return
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for city, (lat, lon) in cities.items():
            _log_todays_forecasts(city, lat, lon, today)

        floor = (datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)).strftime("%Y-%m-%d")
        filled = 0
        for city, day in pending_actuals(today):
            if day < floor:
                continue       # too old to still be waiting on; leave it
            coords = cities.get(city)
            if coords and _fill_actual(city, coords[0], coords[1], day):
                filled += 1
        print(f"wxaudit: logged {len(cities)} cities, filled {filled} actuals")
    except Exception as e:
        print(f"wxaudit: run failed: {type(e).__name__}: {e}")


# --- the selector -------------------------------------------------------------
# The audit stopped being a diagnostic the moment it had enough rows to act on.
# The best source is a property of the PLACE, not of the product: measured over
# the same days, ECMWF is the most accurate forecaster for Woodland Hills
# (+3.3) and the LEAST accurate for Culver City (+12.0), while NWS is the
# reverse. Two cities 25km apart, same geocoder, same code path, inverted
# answers. Nothing about a better location fixes that; picking per location does.

LOOKBACK_DAYS = 30
# Days a source must have been scored before it may be chosen. Below this, one
# freak day decides.
MIN_SAMPLES = 5
# How much better the challenger must be before switching, in degrees of mean
# absolute error. Without a margin the choice churns between sources that are
# equally good, and a source that changes weekly is its own kind of wrong.
SWITCH_MARGIN = 2.0

_best_cache: dict[tuple[str, str], str | None] = {}


def best_source(city: str, incumbent: str = "nws", today=None) -> str | None:
    """The source proven better than `incumbent` for this city, or None.

    None means "keep doing what you were doing" and is the answer until the
    evidence is unambiguous. Three gates, all of which must pass:

      * the challenger has at least MIN_SAMPLES scored days,
      * the INCUMBENT does too — otherwise we would be switching away from
        something we have not actually measured, which is how the first version
        of this idea would have dropped NWS on a single day's reading,
      * and the challenger beats it by more than SWITCH_MARGIN.

    Cached per city per day: this is consulted on the read path, and the answer
    cannot change more than once a day anyway."""
    if not city:
        return None
    key = (city.strip().lower(), (today or datetime.now(timezone.utc).date()).isoformat())
    if key in _best_cache:
        return _best_cache[key]

    choice = None
    try:
        from db import forecast_scores
        rows = [r for r in forecast_scores(LOOKBACK_DAYS)
                if (r.get("city") or "").strip().lower() == key[0]
                and (r.get("n") or 0) >= MIN_SAMPLES and r.get("mae") is not None]
        by_source = {r["source"]: float(r["mae"]) for r in rows}
        base = by_source.get(incumbent)
        if base is not None:
            challenger = min(by_source, key=by_source.get)
            if challenger != incumbent and by_source[challenger] <= base - SWITCH_MARGIN:
                choice = challenger
                print(f"wxaudit: {city} -> {challenger} "
                      f"(mae {by_source[challenger]:.1f} vs {incumbent} {base:.1f})")
    except Exception as e:
        print(f"wxaudit: best_source failed for {city!r}: {type(e).__name__}: {e}")

    _best_cache[key] = choice
    return choice


def _clear_best_cache() -> None:
    """Tests only."""
    _best_cache.clear()


def report(days: int = 30) -> str:
    """Human-readable scoreboard. `python -c "import wxaudit; print(wxaudit.report())"`"""
    from db import forecast_scores
    rows = forecast_scores(days)
    if not rows:
        return "No scored forecasts yet — actuals lag reanalysis by a day or two."
    out = [f"{'city':26} {'source':16} {'n':>3} {'bias':>7} {'mae':>7}",
           "-" * 64]
    for r in rows:
        out.append(f"{r['city'][:26]:26} {r['source']:16} {r['n']:>3} "
                   f"{r['bias']:>+7.1f} {r['mae']:>7.1f}")
    out.append("\nbias: + means the source forecasts hotter than reality. "
               "Lowest mae per city wins.")
    return "\n".join(out)
