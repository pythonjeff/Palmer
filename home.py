"""Palmer Home — one stable, live page per user.

A single row per user, keyed by a permanent secret token, updated in place. The
morning job writes the full payload (including the paid news search); a page
view refreshes only the free data classes, each behind its own cooldown:

    weather   Open-Meteo    free    refresh if >10 min stale
    commute   TomTom        free    refresh if  >5 min stale
    prices    CoinGecko/yf  free    refresh if  >5 min stale
    headlines Tavily        $0.008  never on view — morning job only

That table is the entire cost story. The page feels live because the live things
happen to be free; the one paid input rides the schedule that already runs. A
user hammering refresh cannot run up a bill.

There is no login. The token is the whole protection, so the page must only ever
show what the user already received in a briefing — never the SMS transcript,
never the raw profile. `rotate()` is the answer to a forwarded screenshot and
exists from day one.
"""
from __future__ import annotations

import os
import time

from db import get_profile, upsert_profile, save_artifact, get_artifact
from artifacts import new_token

_APP_URL = os.environ.get("APP_URL", "").rstrip("/")

KIND = "home"
TTL_HOURS = 24 * 400          # effectively permanent; refreshed on every write

# seconds before a section is worth refetching on view. None = never on view.
STALE = {"weather": 600, "traffic": 300, "prices": 300, "headlines": None}


def home_token(phone: str) -> str:
    """The user's permanent token, minted on first use."""
    profile = get_profile(phone)
    token = profile.get("home_token")
    if not token:
        token = new_token()
        upsert_profile(phone, {"home_token": token})
    return token


def home_url(phone: str) -> str:
    return f"{_APP_URL}/h/{home_token(phone)}"


def rotate(phone: str) -> str:
    """Mint a new token, orphaning the old URL. The answer to a leaked link."""
    old = get_profile(phone).get("home_token")
    token = new_token()
    upsert_profile(phone, {"home_token": token})
    if old:
        # Expire rather than delete so an in-flight fetch fails closed.
        try:
            save_artifact(old, KIND, b"{}", ttl_hours=0)
        except Exception as e:
            print(f"home.rotate: could not expire old token for {phone}: {e}")
    payload = load(token) or {}
    if not payload:
        rebuild(phone, refresh_news=False)
    return f"{_APP_URL}/h/{token}"


def _now() -> float:
    return time.time()


def _fetch_weather(profile: dict) -> dict | None:
    from weather import weather_snapshot
    city = profile.get("city")
    return weather_snapshot(city, profile.get("timezone")) if city else None


def _fetch_traffic(profile: dict) -> dict | None:
    from traffic import traffic_snapshot
    c = profile.get("commute") or {}
    if not (c.get("origin") and c.get("destination")):
        return None
    return traffic_snapshot(c["origin"], c["destination"])


def _fetch_prices(profile: dict) -> list[dict]:
    from datafeeds import price_snapshot
    from morning import _price_asset_for_topic
    assets, seen = [], set()
    for topic in (profile.get("morning_topics") or []):
        a = _price_asset_for_topic(topic)
        if a and a.lower() not in seen:
            seen.add(a.lower())
            assets.append(a)
    return [s for s in (price_snapshot(a) for a in assets[:3]) if s]


def _fetch_headlines(profile: dict) -> list[dict]:
    """The one paid path. Morning job only — never on view."""
    from datafeeds import _search_raw
    from morning import _is_directive, _rotated_topics, _WEATHER_KEYWORDS, _TRAFFIC_KEYWORDS
    from watches import _source_tier, _canonical_domain
    from timeutil import local_today
    auto = _WEATHER_KEYWORDS + _TRAFFIC_KEYWORDS
    topics = [t for t in (profile.get("morning_topics") or [])
              if t and not any(w in t.lower() for w in auto) and not _is_directive(t)]
    out = []
    for topic in _rotated_topics(topics, local_today(profile.get("timezone"))):
        try:
            results = _search_raw(topic, days=1, max_age_hours=24, min_score=0.5)
            if not results:
                continue
            results.sort(key=lambda r: (_source_tier(r.get("url", "")), -(r.get("score") or 0)))
            top = results[0]
            out.append({"title": (top.get("title") or "")[:110],
                        "url": top.get("url"),
                        "source": _canonical_domain(top.get("url", "")),
                        "topic": topic})
        except Exception as e:
            print(f"home headlines failed for {topic!r}: {e}")
    return out


def _tracking(phone: str) -> dict:
    """What Palmer is keeping an eye on. This is what makes it a site he
    maintains rather than a daily snapshot."""
    from db import get_user_watches, get_user_price_watches
    try:
        watches = [{"description": w.get("description"), "cooldown_hours": w.get("cooldown_hours")}
                   for w in (get_user_watches(phone) or [])]
    except Exception:
        watches = []
    try:
        prices = [{"product": w.get("product_name"),
                   "target": float(w["target_price"]) if w.get("target_price") is not None else None,
                   "last_seen": float(w["last_seen_price"]) if w.get("last_seen_price") is not None else None,
                   "source": w.get("source")}
                  for w in (get_user_price_watches(phone) or [])]
    except Exception:
        prices = []
    profile = get_profile(phone)
    return {
        "watches": watches,
        "price_watches": prices,
        "topics": [t for t in (profile.get("morning_topics") or []) if t],
        "morning_time": profile.get("morning_time"),
    }


def rebuild(phone: str, refresh_news: bool = True) -> dict:
    """Full build — what the morning job calls. Includes the paid news pass."""
    profile = get_profile(phone)
    token = home_token(phone)
    previous = load(token) or {}
    now = _now()
    payload = {
        "phone": phone,
        "city": profile.get("city") or "",
        "name": profile.get("name"),
        "timezone": profile.get("timezone"),
        "weather": _fetch_weather(profile),
        "traffic": _fetch_traffic(profile),
        "prices": _fetch_prices(profile),
        "headlines": _fetch_headlines(profile) if refresh_news
                     else (previous.get("headlines") or []),
        "tracking": _tracking(phone),
        "fetched": {"weather": now, "traffic": now, "prices": now,
                    "headlines": now if refresh_news
                                 else (previous.get("fetched", {}).get("headlines") or now)},
        "built_at": now,
    }
    save(token, payload)
    return payload


def refresh_stale(token: str, payload: dict) -> dict:
    """Refresh only the free sections that have gone stale. Never touches
    headlines — that is the paid path and it belongs to the morning job."""
    profile = get_profile(payload.get("phone") or "")
    if not profile:
        return payload
    fetched = dict(payload.get("fetched") or {})
    now = _now()
    changed = False
    for section, fetcher in (("weather", _fetch_weather),
                             ("traffic", _fetch_traffic),
                             ("prices", _fetch_prices)):
        window = STALE.get(section)
        if window is None:
            continue
        if now - (fetched.get(section) or 0) < window:
            continue
        try:
            payload[section] = fetcher(profile)
            fetched[section] = now
            changed = True
        except Exception as e:
            print(f"home refresh {section} failed: {e}")
    if changed:
        payload["fetched"] = fetched
        save(token, payload)
    return payload


def save(token: str, payload: dict) -> None:
    import json
    save_artifact(token, KIND, json.dumps(payload).encode(), ttl_hours=TTL_HOURS)


def load(token: str) -> dict | None:
    import json
    got = get_artifact(token)
    if not got:
        return None
    kind, body = got
    if kind != KIND:
        return None
    try:
        return json.loads(body.decode())
    except Exception:
        return None
