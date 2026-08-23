"""Palmer Home — one stable, live page per user.

A single row per user, keyed by a permanent secret token, updated in place. The
morning job writes the full payload; a page view refreshes whatever has gone
stale, each class behind its own cooldown:

    weather   Open-Meteo    free    refresh if >10 min stale
    commute   TomTom        free    refresh if  >5 min stale
    prices    CoinGecko/yf  free    refresh if  >5 min stale
    identity  local DB      free    refresh every view
    headlines Tavily        $0.008  refresh if  >6 hr stale

That table is the entire cost story. The page feels live because most of the
live things are free; the one paid input is rate-limited by its own timestamp.

The headline rule is the only one that costs money, so it is worth stating
plainly: a refresh stamps `fetched.headlines_tried`, and the gate reads that
stamp, so a successful *or* failed refresh closes the window for another six
hours. That bounds the page to at most four news passes per user per day no
matter how hard anyone reloads — the morning job's, plus three. Headlines used
to refresh only in the morning, which made them free but left a "news ticker"
showing 13-hour-old stories by dinner. Six hours is the compromise: a bounded,
predictable bill in exchange for a page that is never badly wrong.

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
# headlines is the only paid entry — see the cost note in the module docstring
# for why it is 6h and how the window is enforced.
STALE = {"weather": 600, "traffic": 300, "prices": 300, "headlines": 6 * 3600}


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
    """The Markets section, derived from the user's morning topics.

    Resolution lives in tickers.py — a topic only shows a price if it names
    something tradeable, so "Nvidia stock" resolves and "SpaceX stock" (private)
    correctly does not."""
    from datafeeds import price_snapshot
    from tickers import resolve_topic_asset
    from cards import MAX_PRICES
    assets, seen = [], set()
    for topic in (profile.get("morning_topics") or []):
        got = resolve_topic_asset(topic)
        if got and got[0].lower() not in seen:
            seen.add(got[0].lower())
            assets.append(got)
    return [s for s in (price_snapshot(sym, label) for sym, label in assets[:MAX_PRICES]) if s]


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


def _tracking(phone: str, profile: dict | None = None) -> dict:
    """What Palmer is keeping an eye on. This is what makes it a site he
    maintains rather than a daily snapshot.

    Takes the caller's profile when it already has one — this runs on every
    page view, and re-reading the profile here would open a second connection
    for a row the caller is already holding."""
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
    if profile is None:
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
        "tracking": _tracking(phone, profile),
        "fetched": {"weather": now, "traffic": now, "prices": now,
                    "headlines": now if refresh_news
                                 else (previous.get("fetched", {}).get("headlines") or now),
                    "headlines_tried": now if refresh_news
                                 else (previous.get("fetched", {}).get("headlines_tried") or now)},
        "built_at": now,
    }
    save(token, payload)
    return payload


def _refresh_identity(payload: dict, profile: dict, phone: str) -> bool:
    """Re-read the profile-derived fields. Free — these come off a row we are
    already holding — and they are the ones a user notices going stale: they
    tell Palmer their name at noon and the page still says "Your briefing"
    until tomorrow morning, or they add a watch and the page doesn't list it."""
    fresh = {
        "city": profile.get("city") or "",
        "name": profile.get("name"),
        "timezone": profile.get("timezone"),
        "tracking": _tracking(phone, profile),
    }
    changed = any(payload.get(k) != v for k, v in fresh.items())
    payload.update(fresh)
    return changed


def _headlines_stale(fetched: dict, now: float) -> bool:
    """Whether a view may spend money on news.

    Gated on `headlines_tried`, not `headlines`, so that a refresh which comes
    back empty still closes the window — otherwise a topic with no coverage
    would re-search on every single view. Falls back to the data stamp for
    payloads written before this key existed."""
    window = STALE.get("headlines")
    if window is None:
        return False
    tried = fetched.get("headlines_tried") or fetched.get("headlines") or 0
    return (now - tried) >= window


def refresh_stale(token: str, payload: dict) -> dict:
    """Bring a payload up to date on view.

    The free sections refresh on short cooldowns; headlines refresh at most
    every 6 hours (see the module docstring for the cost bound). Anything that
    fails keeps its previous value — a stale section beats an empty one."""
    profile = get_profile(payload.get("phone") or "")
    if not profile:
        return payload
    fetched = dict(payload.get("fetched") or {})
    now = _now()
    changed = _refresh_identity(payload, profile, payload.get("phone") or "")
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

    if _headlines_stale(fetched, now):
        # Stamp the attempt before the call, so a raising fetch still closes
        # the window rather than re-searching on the next view.
        fetched["headlines_tried"] = now
        changed = True
        try:
            heads = _fetch_headlines(profile)
            if heads:
                payload["headlines"] = heads
                fetched["headlines"] = now
        except Exception as e:
            print(f"home refresh headlines failed: {e}")

    if changed:
        payload["fetched"] = fetched
        save(token, payload)
    return payload


def ensure_fresh(phone: str) -> str:
    """The user's URL, guaranteed to point at a page with real data on it.

    Every path where Palmer hands someone their link goes through this. Minting
    a token does not build a payload, so a user who has never had a morning sent
    (not onboarded, mornings off, or asking on their first day) would otherwise
    get a link straight to a 404. Never raises — callers are user-facing."""
    token = home_token(phone)
    try:
        payload = load(token)
        if payload is None:
            rebuild(phone, refresh_news=True)
        else:
            refresh_stale(token, payload)
    except Exception as e:
        print(f"home.ensure_fresh failed for {phone}: {type(e).__name__}: {e}")
    return f"{_APP_URL}/h/{token}"


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
