"""Weather: geocoding, NWS (US) and Open-Meteo (everywhere else)."""
from datetime import datetime, timedelta, date as _date

from netutil import _http_get_json_retry


_WMO_DESCRIPTIONS = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "icy fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "heavy drizzle",
    56: "light freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "moderate rain", 65: "heavy rain",
    66: "light freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "moderate snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "moderate rain showers", 82: "heavy rain showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm",
}

_NWS_USER_AGENT = "PalmerSMS/1.0 (contact: jeffreyblarson00@gmail.com)"

def _is_us_coords(lat: float, lon: float) -> bool:
    """Rough bounding boxes for NWS-covered territory: CONUS, AK, HI, PR/USVI."""
    if 24.5 <= lat <= 49.4 and -125.0 <= lon <= -66.9:      # CONUS
        return True
    if 51.2 <= lat <= 71.5 and -179.5 <= lon <= -129.9:     # Alaska
        return True
    if 18.9 <= lat <= 22.3 and -160.3 <= lon <= -154.7:     # Hawaii
        return True
    if 17.6 <= lat <= 18.6 and -67.5 <= lon <= -64.5:       # PR / USVI
        return True
    return False

# Cities don't move — cache geocode results for the dyno's lifetime so the
# flakier geocoding endpoint is hit at most once per city.
_geocode_cache: dict[str, tuple[float, float, str]] = {}

def _geocode(location: str) -> tuple[float, float, str]:
    key = location.strip().lower()
    if key in _geocode_cache:
        return _geocode_cache[key]
    data = _http_get_json_retry(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": location, "count": 1, "language": "en", "format": "json"},
        timeout=8,
    )
    results = data.get("results")
    if not results:
        raise ValueError(f"Location not found: {location}")
    r = results[0]
    name = r.get("name", location)
    admin = r.get("admin1", "")
    resolved = f"{name}, {admin}" if admin else name
    coords = (r["latitude"], r["longitude"], resolved)
    _geocode_cache[key] = coords
    return coords

def _resolve_day_delta(when: str, when_lower: str, tz: str | None = None) -> int | None:
    """Convert 'tomorrow' / weekday name / 'YYYY-MM-DD' into a day offset from
    the user's local today. Falls back to server UTC if tz is missing.
    Returns None if the input doesn't look like a future-date reference."""
    from timeutil import local_today
    today = local_today(tz)
    wd = today.weekday()
    day_offsets = {
        "tomorrow": 1,
        "monday": (0 - wd) % 7 or 7,
        "tuesday": (1 - wd) % 7 or 7,
        "wednesday": (2 - wd) % 7 or 7,
        "thursday": (3 - wd) % 7 or 7,
        "friday": (4 - wd) % 7 or 7,
        "saturday": (5 - wd) % 7 or 7,
        "sunday": (6 - wd) % 7 or 7,
        "weekend": (5 - wd) % 7 or 7,
    }
    for k, v in day_offsets.items():
        if k in when_lower:
            return v
    try:
        target = datetime.strptime(when.strip(), "%Y-%m-%d").date()
        return (target - today).days
    except Exception:
        return None

def _nws_report(lat: float, lon: float, resolved: str, when: str, when_lower: str,
                is_now: bool, is_today: bool, tz: str | None = None) -> str:
    """US-only weather via api.weather.gov (NWS). Raises on any failure so the
    caller can fall back to Open-Meteo. `tz` scopes 'tomorrow'/weekday parsing to
    the user's local today (still respects NWS's own local-timezone startTime
    on the primary path)."""
    headers = {"User-Agent": _NWS_USER_AGENT, "Accept": "application/geo+json"}
    points = _http_get_json_retry(
        f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}",
        params={}, timeout=8, headers=headers,
    )
    props = (points or {}).get("properties") or {}
    forecast_url = props.get("forecast")
    hourly_url = props.get("forecastHourly")
    if not forecast_url:
        raise RuntimeError("NWS points response missing forecast URL")

    forecast = _http_get_json_retry(forecast_url, params={}, timeout=10, headers=headers)
    periods = (forecast.get("properties") or {}).get("periods") or []
    if not periods:
        raise RuntimeError("NWS forecast returned no periods")

    def _period_date(iso: str | None):
        try:
            return _date.fromisoformat((iso or "")[:10])
        except Exception:
            return None

    def _pop(p: dict) -> int | None:
        v = ((p.get("probabilityOfPrecipitation") or {}).get("value"))
        return int(round(v)) if v is not None else None

    def _hour0() -> dict | None:
        if not hourly_url:
            return None
        try:
            h = _http_get_json_retry(hourly_url, params={}, timeout=8, headers=headers)
            hp = (h.get("properties") or {}).get("periods") or []
            return hp[0] if hp else None
        except Exception:
            return None

    if is_now:
        h = _hour0() or periods[0]
        temp = h.get("temperature")
        desc = (h.get("shortForecast") or periods[0].get("shortForecast") or "").strip().lower()
        wind = (h.get("windSpeed") or periods[0].get("windSpeed") or "").strip()
        pop = _pop(h)
        parts = [f"{resolved} right now: {temp}°F, {desc}."]
        if pop is not None and pop > 5:
            parts.append(f"Rain chance {pop}%.")
        if wind:
            parts.append(f"Wind {wind}.")
        return " ".join(parts)

    if is_today:
        # Use the period(s) whose local date is today. NWS periods are ordered;
        # the first daytime + first nighttime gives us high/low.
        # Determine "today" from the first period's own startTime so we honor
        # the location's local calendar rather than server UTC.
        local_today = _period_date(periods[0].get("startTime")) or _date.today()
        today_periods = [p for p in periods if _period_date(p.get("startTime")) == local_today]
        if not today_periods:
            today_periods = periods[:2]
        day = next((p for p in today_periods if p.get("isDaytime")), None)
        night = next((p for p in today_periods if not p.get("isDaytime")), None)
        primary = day or night or today_periods[0]
        desc = (primary.get("detailedForecast") or primary.get("shortForecast") or "").strip()
        if day and night:
            hilo = f" High {day.get('temperature')}°F / low {night.get('temperature')}°F."
        elif day:
            hilo = f" High {day.get('temperature')}°F."
        elif night:
            hilo = f" Low {night.get('temperature')}°F."
        else:
            hilo = ""
        tail = ""
        h = _hour0()
        if h:
            short = (h.get("shortForecast") or "").strip().lower()
            tail = f" Right now {h.get('temperature')}°F, {short}." if short else f" Right now {h.get('temperature')}°F."
        return f"{resolved} today:{hilo} {desc}{tail}".strip()

    # Future date
    delta = _resolve_day_delta(when, when_lower, tz=tz)
    if delta is None:
        delta = 1
    if delta < 0:
        raise ValueError("Past date")
    from timeutil import local_today as _lt
    local_today = _period_date(periods[0].get("startTime")) or _lt(tz)
    target = local_today + timedelta(days=delta)
    matching = [p for p in periods if _period_date(p.get("startTime")) == target]
    if not matching:
        raise ValueError(f"NWS has no forecast for {target.isoformat()}")
    day = next((p for p in matching if p.get("isDaytime")), None)
    night = next((p for p in matching if not p.get("isDaytime")), None)
    primary = day or night or matching[0]
    desc = (primary.get("detailedForecast") or primary.get("shortForecast") or "").strip()
    if day and night:
        hilo = f" High {day['temperature']}°F / low {night['temperature']}°F."
    elif day:
        hilo = f" High {day['temperature']}°F."
    elif night:
        hilo = f" Low {night['temperature']}°F."
    else:
        hilo = ""
    return f"{resolved} on {target.strftime('%A, %B %d')}:{hilo} {desc}".strip()

def _fetch_openmeteo(lat: float, lon: float) -> dict:
    """One Open-Meteo call, shared by the prose report and weather_snapshot.

    Both need the same fields; keeping the params in one place stops the two
    from drifting apart."""
    return _http_get_json_retry(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
            "daily": ("temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                      "wind_speed_10m_max,wind_gusts_10m_max,weather_code"),
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "forecast_days": 8,
            "timezone": "auto",
        },
        timeout=10,
    )


def weather_snapshot(location: str, tz: str | None = None) -> dict | None:
    """Structured weather for the visual dashboard. None on any failure.

    Deliberately always uses Open-Meteo, even for US locations where
    _weather_report prefers NWS. The two sources are good at different jobs:
    NWS writes better US narrative text ("isolated showers between 4pm and
    5pm"), which is why the prose path wants it, while Open-Meteo returns clean
    numbers plus a WMO `weather_code` that maps directly to which art to draw.
    Prose keeps NWS; the dashboard uses this. Fully additive — no existing
    branch changes, so the text briefing cannot regress."""
    try:
        lat, lon, resolved = _geocode(location)
        data = _fetch_openmeteo(lat, lon)
        curr, daily = data["current"], data["daily"]
        code = curr.get("weather_code")
        return {
            "resolved": resolved,
            "temp_now": curr.get("temperature_2m"),
            "feels_like": curr.get("apparent_temperature"),
            "humidity": curr.get("relative_humidity_2m"),
            "wind": curr.get("wind_speed_10m"),
            "weather_code": code,
            "description": _WMO_DESCRIPTIONS.get(code, "unknown conditions"),
            "high": daily["temperature_2m_max"][0],
            "low": daily["temperature_2m_min"][0],
            "rain_pct": daily["precipitation_probability_max"][0],
            "gusts": (daily.get("wind_gusts_10m_max") or [None])[0],
        }
    except Exception as e:
        print(f"weather_snapshot failed for {location!r}: {type(e).__name__}: {e}")
        return None


def _openmeteo_report(lat: float, lon: float, resolved: str, when: str, when_lower: str,
                     is_now: bool, is_today: bool, tz: str | None = None) -> str:
    """Fallback weather via Open-Meteo. Used worldwide and when NWS fails.
    `tz` scopes 'tomorrow'/weekday parsing to the user's local today."""
    data = _fetch_openmeteo(lat, lon)

    curr = data["current"]
    daily = data["daily"]

    def _gust_str(gust_max, wind_max) -> str:
        if gust_max is not None and gust_max > (wind_max or 0) + 5:
            return f", gusts to {gust_max:.0f} mph"
        return ""

    if is_now:
        temp = curr["temperature_2m"]
        feels = curr["apparent_temperature"]
        humidity = curr["relative_humidity_2m"]
        wind_now = curr["wind_speed_10m"]
        desc = _WMO_DESCRIPTIONS.get(curr["weather_code"], "unknown conditions")
        rain_pct = daily["precipitation_probability_max"][0]
        rain_str = f" Rain chance {rain_pct}%." if rain_pct and rain_pct > 5 else ""
        return (
            f"{resolved} right now: {temp:.0f}°F (feels {feels:.0f}°F), {desc}."
            f"{rain_str} Humidity {humidity}%. Wind {wind_now:.0f} mph."
        )

    if is_today:
        hi = daily["temperature_2m_max"][0]
        lo = daily["temperature_2m_min"][0]
        rain_pct = daily["precipitation_probability_max"][0]
        wind_max = daily["wind_speed_10m_max"][0]
        gust_max = (daily.get("wind_gusts_10m_max") or [None])[0]
        desc_daily = _WMO_DESCRIPTIONS.get(daily["weather_code"][0], "unknown conditions")
        rain_str = f" Rain chance {rain_pct}%." if rain_pct and rain_pct > 5 else ""
        tail = (
            f" Right now {curr['temperature_2m']:.0f}°F, "
            f"{_WMO_DESCRIPTIONS.get(curr['weather_code'], 'unknown conditions')}."
        )
        return (
            f"{resolved} today: high {hi:.0f}°F / low {lo:.0f}°F, {desc_daily}."
            f"{rain_str} Wind up to {wind_max:.0f} mph{_gust_str(gust_max, wind_max)}."
            f"{tail}"
        )

    # Future date
    delta = _resolve_day_delta(when, when_lower, tz=tz)
    if delta is None:
        delta = 1
    if delta < 0 or delta >= len(daily["time"]):
        return f"No forecast available for that date in {resolved} — forecast covers 8 days out."
    from timeutil import local_today as _lt
    target_date = _lt(tz) + timedelta(days=delta)
    hi = daily["temperature_2m_max"][delta]
    lo = daily["temperature_2m_min"][delta]
    rain_pct = daily["precipitation_probability_max"][delta]
    wind_max = daily["wind_speed_10m_max"][delta]
    gust_max = (daily.get("wind_gusts_10m_max") or [None] * (delta + 1))[delta]
    desc_daily = _WMO_DESCRIPTIONS.get(daily["weather_code"][delta], "unknown conditions")
    return (
        f"{resolved} on {target_date.strftime('%A, %B %d')}: "
        f"High {hi:.0f}°F / low {lo:.0f}°F, {desc_daily}. "
        f"Rain chance {rain_pct}%. Wind up to {wind_max:.0f} mph{_gust_str(gust_max, wind_max)}."
    )

def _weather_report(location: str, when: str = "today", tz: str | None = None) -> str:
    """Core weather lookup. Raises on failure — callers decide how to degrade.
    Prefers NWS (api.weather.gov) for US coordinates; falls back to Open-Meteo
    on any NWS failure and uses Open-Meteo directly outside the US. `tz` is
    the user's IANA timezone (e.g. 'America/Los_Angeles'); when provided,
    'tomorrow' and weekday names are resolved against the user's local today
    rather than server UTC, fixing the late-night west-coast off-by-one."""
    when_lower = (when or "today").lower().strip()
    is_now = any(w in when_lower for w in ("now", "current"))
    is_today = any(w in when_lower for w in ("today", "tonight"))

    lat, lon, resolved = _geocode(location)

    if _is_us_coords(lat, lon):
        try:
            return _nws_report(lat, lon, resolved, when, when_lower, is_now, is_today, tz=tz)
        except Exception as e:
            print(f"NWS lookup failed for {location!r} ({lat:.3f},{lon:.3f}): {e}; falling back to Open-Meteo")

    return _openmeteo_report(lat, lon, resolved, when, when_lower, is_now, is_today, tz=tz)

def _get_weather(location: str, when: str = "today", tz: str | None = None) -> str:
    """Tool-facing wrapper: never raises, returns a fallback hint string on failure."""
    try:
        return _weather_report(location, when, tz=tz)
    except ValueError as e:
        print(f"Weather geocode failed for {location!r}: {e}")
        return (
            f"Couldn't find a location matching '{location}'. Ask the user to confirm "
            "the city (state or country if it's ambiguous) — do not guess a forecast and "
            "do not redirect them to another app or website."
        )
    except Exception as e:
        print(f"Weather lookup failed for {location!r}: {e}")
        return (
            f"Weather service temporarily unavailable for {location}. Tell the user plainly "
            "you can't pull it right now and offer to try again in a bit. Do not guess numbers, "
            "do not use web_search for weather, and do not redirect them to another app or site."
        )
