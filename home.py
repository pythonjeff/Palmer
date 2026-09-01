"""Palmer Home — one stable, live page per user.

A single row per user, keyed by a permanent secret token, updated in place. The
morning job writes the full payload; a page view refreshes whatever has gone
stale, each class behind its own cooldown:

    weather   Open-Meteo    free    refresh if >10 min stale
    commute   TomTom        free    refresh if  >5 min stale
    prices    CoinGecko/yf  free    refresh if  >5 min stale
    identity  local DB      free    refresh every view
    headlines Tavily        $0.008  refresh if  >6 hr stale
    opening   Tavily+TM+TMDB $0.008 refresh if >24 hr stale, shared per metro

That table is the entire cost story. The page feels live because most of the
live things are free; the paid inputs are rate-limited by their own timestamps.

`opening` is the second paid input and it is metered differently, because its
content is weekly and regional rather than personal: the fetch is cached by
metro and ISO week inside opening.py, so every user in one city shares a single
call and adding users in a city already covered costs nothing. Its Ticketmaster
and TMDB halves are free; only the local-press search spends.

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
# A section's window must be SHORTER than the interval at which refresh
# opportunities occur, or it aliases. Most users never open their page, so the
# only guaranteed opportunity is the daily morning send — and a 24h window
# sampled once every 24h lapses on only about half of them. Three users were
# carrying Opening rows 41 hours old with no refetch even attempted in between:
# at the previous send the section was 20.4h old, just under its own window, so
# it was correctly skipped, and the next chance was a day later. 20h leaves
# four hours of margin against a send that drifts. The refetch is nearly free
# anyway — opening.py caches by metro and week, so a "refresh" inside the same
# week is a dict lookup.
STALE = {"weather": 600, "weather_extra": 600, "traffic": 300, "prices": 300,
         "headlines": 6 * 3600, "opening": 20 * 3600}

# Score a story must clear to reach the page from OUTSIDE the trusted list.
# Higher than the trusted floor (0.5) on purpose: an unvetted source has to earn
# its place on match strength, since it is not earning it on provenance.
UNTRUSTED_MIN_SCORE = 0.60


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


def _fetch_weather_extra(profile: dict) -> list[dict]:
    """Secondary locations pinned to the page via add_weather_location, shown
    alongside the primary city. Same fetcher and same STALE window as the
    primary slot — one failed location just drops that row rather than
    costing the whole section, the same shape as _fetch_prices keeping a
    stale row over a vanished one. Page-only: the PNG card has no room for
    it (see weather.WEATHER_LOCATIONS_MAX) and the morning text is basics
    plus a link, not a full briefing."""
    from weather import weather_snapshot
    out = []
    for loc in (profile.get("weather_locations") or []):
        snap = weather_snapshot(loc, profile.get("timezone"))
        if snap:
            out.append(snap)
    return out


def _fetch_traffic(profile: dict) -> dict | None:
    from traffic import traffic_snapshot
    c = profile.get("commute") or {}
    if not (c.get("origin") and c.get("destination")):
        return None
    return traffic_snapshot(c["origin"], c["destination"])


# How many tickers the page's Markets section carries. Deliberately larger than
# cards.MAX_PRICES: the card lays its rows out in columns across a fixed 1200px
# and the sparklines start overdrawing the price text past three, while the page
# is a vertical scrolling list with no such limit. The card slices this list
# down to what it has room for, so the two never disagree — the card is a
# summary of the payload, not a different payload.
MAX_PRICES = 6


def _fetch_prices(profile: dict, previous: list[dict] | None = None) -> list[dict]:
    """The Markets section, derived from the user's morning topics.

    Resolution lives in tickers.py — a topic only shows a price if it names
    something tradeable, so "Nvidia stock" resolves and a private company
    correctly does not.

    A symbol whose fetch fails keeps its last known row instead of vanishing.
    CoinGecko rate-limits (429) and yfinance times out, and without this a
    transient blip silently deletes a ticker the user is tracking — which looks
    exactly like Palmer forgetting, and is far worse than a slightly stale
    number sitting under a visible "N min ago" stamp."""
    from datafeeds import price_snapshot
    from tickers import resolve_topic_asset
    stale = {(p.get("label") or "").lower(): p for p in (previous or [])}
    assets, seen = [], set()
    for topic in (profile.get("morning_topics") or []):
        got = resolve_topic_asset(topic)
        if got and got[0].lower() not in seen:
            seen.add(got[0].lower())
            assets.append(got)
    out = []
    for symbol, label in assets[:MAX_PRICES]:
        snapshot = price_snapshot(symbol, label) or stale.get(label.lower())
        if snapshot:
            out.append(snapshot)
    # The user's chosen order, applied to the PAYLOAD rather than at render:
    # the page, the card (which slices [:cards.MAX_PRICES]) and the og
    # description all render from this one list and must not disagree about
    # which ticker leads. Set by arrange_page; absent means the order they
    # added topics, which is what the loop above already produced.
    sort = (profile.get("morning_prefs") or {}).get("markets_sort")
    if sort == "movers":
        out.sort(key=lambda p: abs(p.get("pct_24h") or 0.0), reverse=True)
    elif sort == "alpha":
        out.sort(key=lambda p: (p.get("label") or "").lower())
    return out


def _fetch_headlines(profile: dict) -> list[dict]:
    """The one paid path. Morning job only — never on view.

    Solid sourcing only, or no story at all. Unlike the drafted text briefing
    (morning._topic_digest hands the model three ranked stories and a domain
    tag, so a shaky citation gets folded into a paraphrase a reader never
    inspects), the page links straight to the source — the reader taps it. A
    topic whose only coverage is an unranked domain is dropped for the day
    rather than shown as a "Today" story with a weak citation. This is the
    same tier gate the rest of the product holds itself to; the page never had
    it wired in, so it was one search-result-ranking step behind that bar.
    Palmer Home is the only caller that passes ``trusted_only`` — conversation
    and the morning briefing keep tier 3 as a last resort, because an
    obscure-but-real source beats "nothing found" when someone asked.
    """
    from datafeeds import _search_raw
    from morning import _is_directive, _rotated_topics, _WEATHER_KEYWORDS, _TRAFFIC_KEYWORDS
    from sources import canonical_domain
    from timeutil import local_today
    from tickers import resolve_topic_asset
    auto = _WEATHER_KEYWORDS + _TRAFFIC_KEYWORDS
    # A topic that resolves to a ticker is already answered by the Markets
    # section, so searching news for it buys a second, paid answer to the same
    # question — and "Apple stock price" is a poor news query anyway. The text
    # briefing has always skipped these (_gather_morning_data continues past
    # them); this is the page catching up.
    topics = [t for t in (profile.get("morning_topics") or [])
              if t and not any(w in t.lower() for w in auto)
              and not _is_directive(t) and not resolve_topic_asset(t)]
    out = []
    seen_urls: set[str] = set()
    for topic in _rotated_topics(topics, local_today(profile.get("timezone"))):
        try:
            # trusted_only: the page is the one surface where an untrusted row is
            # worse than no row. It is a short list the user reads top to bottom
            # with the source name showing, so a single content farm in it taints
            # the whole card — and unlike a conversation reply, nobody asked a
            # question that has to be answered. A dropped topic just doesn't
            # appear today.
            results = _search_raw(topic, days=1, max_age_hours=24, min_score=0.5,
                                  trusted_only=True)
            if not results:
                # Trusted-only cannot cover local or specialist beats, and it was
                # not failing on junk — it was dropping the BEST source there is.
                # "Philadelphia Eagles news" lost philadelphiaeagles.com at 0.75
                # and nbcsportsphiladelphia.com at 0.61; "St. Louis area news"
                # lost fox2now, ksdk and stlamerican, every real newsroom in the
                # market. The allowlist is 100-odd domains and there are
                # thousands of local outlets, so it will never cover them.
                #
                # Conversation and the morning briefing have always fallen back
                # to tier 3 on the principle that an obscure-but-real source
                # beats "nothing found"; the page was the only surface that did
                # not, and it paid for that with empty News cards. It falls back
                # too now, at a HIGHER score bar: unvetted means the match itself
                # has to carry the weight the source is not carrying. 0.60 keeps
                # the local newsrooms above and cuts the content mill below them
                # (vocal.media at 0.52), with room on both sides.
                results = _search_raw(topic, days=1, max_age_hours=24,
                                      min_score=UNTRUSTED_MIN_SCORE, trusted_only=False)
            if not results:
                continue
            # Two topics covering the same beat return the same article, and the
            # page rendered it twice — one user had "Kirkwood, MO news" and
            # "St. Louis area news" and got a duplicate row. Take the best
            # result this topic has that no earlier topic already used.
            top = next((r for r in results if r.get("url") not in seen_urls), None)
            if top is None:
                continue
            seen_urls.add(top.get("url"))
            out.append({"title": (top.get("title") or "")[:110],
                        "url": top.get("url"),
                        "source": canonical_domain(top.get("url", "")),
                        "topic": topic})
        except Exception as e:
            print(f"home headlines failed for {topic!r}: {e}")
    return out


def _fetch_opening(profile: dict) -> list[dict]:
    """The second paid path, and the cheapest one per user.

    opening.py caches by metro and week, so this is a real call for the first
    user in a city each week and a dict lookup for everyone after them. A user
    with no city returns [] without spending anything — see opening_snapshot.
    """
    # On by default: the morning update is required to carry 1-2 opening
    # highlights for every user, which means this can no longer be an opt-in.
    # It shipped off at first specifically so a bad metro could be caught with
    # preview_opening.py before anyone saw it — that review still applies, it
    # just now happens after the fact rather than gating the rollout. A user
    # can still be opted out explicitly (`morning_prefs.opening = False`) if
    # the section is wrong for them. Nested under morning_prefs so it needs no
    # PROFILE_FIELDS entry; a key outside that allow-list is silently dropped
    # on write.
    if (profile.get("morning_prefs") or {}).get("opening") is False:
        return []
    from opening import opening_snapshot
    try:
        return opening_snapshot(profile)
    except Exception as e:
        print(f"home opening fetch failed: {type(e).__name__}: {e}")
        return []


def _opening_stale(fetched: dict, now: float, has_data: bool = True) -> bool:
    """Whether a view may spend on the Opening section. Same two-stamp rule as
    _headlines_stale: the attempt closes the window, not just the success."""
    window = _window_for("opening", has_data)
    if window is None:
        return False
    tried = fetched.get("opening_tried") or fetched.get("opening") or 0
    return (now - tried) >= window


def _tracking(phone: str, profile: dict | None = None) -> dict:
    """What Palmer is keeping an eye on. This is what makes it a site he
    maintains rather than a daily snapshot.

    Takes the caller's profile when it already has one — this runs on every
    page view, and re-reading the profile here would open a second connection
    for a row the caller is already holding."""
    from db import get_user_watches, get_user_price_watches
    try:
        watches = [{"description": w.get("description"), "cooldown_hours": w.get("cooldown_hours"),
                    "url": w.get("last_alert_url"), "source": w.get("last_alert_domain")}
                   for w in (get_user_watches(phone) or [])]
    except Exception:
        watches = []
    try:
        prices = [{"product": w.get("product_name"),
                   "target": float(w["target_price"]) if w.get("target_price") is not None else None,
                   "last_seen": float(w["last_seen_price"]) if w.get("last_seen_price") is not None else None,
                   "url": w.get("last_seen_url"),
                   "merchant": w.get("last_seen_merchant")}
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


def _page_prefs(profile: dict) -> dict | None:
    """The arrangement page.render honours — carried on the payload (the
    episode_alerts pattern) so a page view never needs a profile read of its
    own. markets_sort is deliberately NOT here: it is baked into the prices
    list at fetch, so the card inherits it too.

    None, not an empty dict, when nothing is set: a payload written before
    this field existed reads back None as well, so an untouched profile
    settles in _refresh_identity instead of rewriting the row on every view."""
    prefs = profile.get("morning_prefs") or {}
    order = list(prefs.get("section_order") or [])
    hidden = list(prefs.get("hidden_sections") or [])
    if not order and not hidden:
        return None
    return {"section_order": order, "hidden_sections": hidden}


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
        "weather_extra": _fetch_weather_extra(profile),
        "traffic": _fetch_traffic(profile),
        "prices": _fetch_prices(profile, previous.get("prices")),
        "headlines": _fetch_headlines(profile) if refresh_news
                     else (previous.get("headlines") or []),
        "opening": _fetch_opening(profile) if refresh_news
                   else (previous.get("opening") or []),
        "tracking": _tracking(phone, profile),
        "episode_alerts": bool((profile.get("morning_prefs") or {}).get("episode_alerts")),
        "page_prefs": _page_prefs(profile),
        "fetched": {"weather": now, "weather_extra": now, "traffic": now, "prices": now,
                    "headlines": now if refresh_news
                                 else (previous.get("fetched", {}).get("headlines") or now),
                    "headlines_tried": now if refresh_news
                                 else (previous.get("fetched", {}).get("headlines_tried") or now),
                    "opening": now if refresh_news
                               else (previous.get("fetched", {}).get("opening") or now),
                    "opening_tried": now if refresh_news
                               else (previous.get("fetched", {}).get("opening_tried") or now)},
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
        # Whether followed shows may reach the morning TEXT. Carried on the
        # payload so _payload_digest can honour it without a profile read of
        # its own — being on the page is passive, being texted is not.
        "episode_alerts": bool((profile.get("morning_prefs") or {}).get("episode_alerts")),
        # The page arrangement, so "put markets first" takes effect on the
        # next view with no invalidate — this runs on every page load.
        "page_prefs": _page_prefs(profile),
    }
    changed = any(payload.get(k) != v for k, v in fresh.items())
    payload.update(fresh)
    return changed


# A paid section that has NO data yet does not wait its full window before
# trying again. The `_tried` stamp is set before the call so a failure cannot be
# retried in a loop — right — but that also meant one empty or failed fetch left
# a section blank for the whole window with nothing to show meanwhile. It locked
# three of four users out of Opening for a day, twice, and had to be cleared by
# hand. Once a section holds data, a stale row beats a blank one and the full
# window applies again; it is only the blank case that retries sooner, and at a
# quarter of the window that is bounded rather than a loop.
EMPTY_RETRY_DIVISOR = 4
EMPTY_RETRY_FLOOR = 3600


def _window_for(section: str, has_data: bool) -> float | None:
    window = STALE.get(section)
    if window is None or has_data:
        return window
    return max(window / EMPTY_RETRY_DIVISOR, EMPTY_RETRY_FLOOR)


def _headlines_stale(fetched: dict, now: float, has_data: bool = True) -> bool:
    """Whether a view may spend money on news.

    Gated on `headlines_tried`, not `headlines`, so that a refresh which comes
    back empty still closes the window — otherwise a topic with no coverage
    would re-search on every single view. Falls back to the data stamp for
    payloads written before this key existed."""
    window = _window_for("headlines", has_data)
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
                             ("weather_extra", _fetch_weather_extra),
                             ("traffic", _fetch_traffic),
                             ("prices", lambda p: _fetch_prices(p, payload.get("prices")))):
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

    if _headlines_stale(fetched, now, bool(payload.get("headlines"))):
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

    if _opening_stale(fetched, now, bool(payload.get("opening"))):
        # Same tried-before-call stamp as headlines: a failed or empty fetch
        # still closes the day's window, so a reload loop cannot re-spend.
        fetched["opening_tried"] = now
        changed = True
        try:
            rows = _fetch_opening(profile)
            if rows:
                payload["opening"] = rows
                fetched["opening"] = now
        except Exception as e:
            print(f"home refresh opening failed: {e}")

    if changed:
        payload["fetched"] = fetched
        save(token, payload)
    return payload


def invalidate(phone: str, sections: tuple[str, ...] = ("prices",)) -> None:
    """Force the named sections to refetch on the next page view.

    Called when something the user just said changes what the page should hold —
    adding a ticker, dropping one. Without it Markets keeps serving the cached
    row set until the 5-minute cooldown lapses, so "add apple stock" appears to
    do nothing for up to five minutes, which reads as broken.

    Expiring a stamp rather than rebuilding here is deliberate: the user is
    waiting on a text reply, and refetching prices inline would put seconds of
    network on that reply for data nobody is looking at yet. Never raises."""
    try:
        token = get_profile(phone).get("home_token")
        if not token:
            return          # no page minted yet; ensure_fresh will build it fresh
        payload = load(token)
        if not payload:
            return
        fetched = dict(payload.get("fetched") or {})
        for section in sections:
            fetched[section] = 0
            # Paid sections gate on their `_tried` stamp, not the data stamp
            # (see _headlines_stale / _opening_stale), so clearing only the
            # data stamp expires nothing — the section reads as recently
            # attempted and the refetch never happens. Clear both.
            if f"{section}_tried" in fetched:
                fetched[f"{section}_tried"] = 0
        payload["fetched"] = fetched
        save(token, payload)
    except Exception as e:
        print(f"home.invalidate failed for {phone}: {type(e).__name__}: {e}")


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
