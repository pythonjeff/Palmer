"""Flight price watches — the thing Palmer said it couldn't do.

A user asked Palmer to track LAX→MXP for a September trip. Palmer has
`search_flights` and it works, but it had no way to *watch* a route, so instead
of delivering the half it could it disclaimed the whole capability and named a
competitor. This is that missing half.

Deliberately thinner than the product price watch. It runs **once a day**, and
that cadence is the whole cost control: SerpAPI is the only paid input and the
account is on a 250-searches/month plan, so one active watch costs ~30 a month
and `db.FLIGHT_WATCH_MAX` caps a user at three. There is no per-watch cooldown
because a daily check cannot fire more than daily.

Alerts reuse `price_alert.draft_price_alert`, so a flight alert sounds like the
same Palmer as a product alert and carries the user's calibrated register.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from db import (get_active_flight_watches, update_flight_watch_price,
                get_profile)

# A fare has to move by more than this to be worth a text. Flights are volatile
# by tens of dollars daily, so the flat $2 rule that governs product watches
# would page someone every morning. $40 is roughly the point where a traveller
# would actually rebook.
MOVE_MIN_ABS = 40.0


def _route(watch: dict) -> str:
    # "LAX to MXP", not an arrow. _sms_clean folds to ASCII, and an unmapped
    # glyph was DELETED rather than replaced — price_alert._fallback shipped
    # "LAX  MXP 2026-09-10 is $842.00 on flight search." with a hole in it.
    # _UNICODE_MAP now covers the arrow too, but correct copy should not depend
    # on the fold: a route read aloud in a text is "LAX to MXP".
    trip = f"{watch['origin']} to {watch['destination']}"
    return f"{trip} {watch['outbound_date']}" + (
        f" / {watch['return_date']}" if watch.get("return_date") else "")


def _cheapest(watch: dict) -> float | None:
    """Lowest current fare for the route, or None if the search gave nothing."""
    from flights import _serpapi_flights_search
    try:
        results = _serpapi_flights_search(
            watch["origin"], watch["destination"],
            watch["outbound_date"], watch.get("return_date"))
    except Exception as e:
        print(f"flightwatch: search failed for {_route(watch)}: {type(e).__name__}: {e}")
        return None
    prices = [r.get("price") for r in (results or []) if r.get("price")]
    return float(min(prices)) if prices else None


def _expired(watch: dict) -> bool:
    """A watch for a departure already in the past is dead weight — it would
    keep spending a search a day on a flight nobody can book."""
    try:
        return date.fromisoformat(watch["outbound_date"]) < date.today()
    except (TypeError, ValueError):
        return False


def _should_alert(watch: dict, current: float) -> str | None:
    target = watch.get("target_price")
    if target and current <= float(target):
        return "target"
    baseline = watch.get("baseline_price")
    if baseline is None:
        return None                      # first sighting is the baseline, not news
    delta = current - float(baseline)
    if delta <= -MOVE_MIN_ABS:
        return "drop"
    if delta >= MOVE_MIN_ABS:
        return "rise"
    return None


def run_flight_watches() -> None:
    """Check every active flight watch once. Never raises."""
    from sms_util import send_sms
    from price_alert import draft_price_alert
    from userprofile import _is_duplicate_subject
    from db import cancel_flight_watches

    try:
        watches = get_active_flight_watches()
    except Exception as e:
        print(f"flightwatch: could not load watches: {type(e).__name__}: {e}")
        return

    checked = alerted = 0
    for w in watches:
        try:
            if _expired(w):
                cancel_flight_watches(w["phone"], w["origin"])
                print(f"flightwatch: retired past-departure watch {_route(w)}")
                continue
            current = _cheapest(w)
            if current is None:
                continue
            checked += 1
            reason = _should_alert(w, current)
            if reason is None:
                update_flight_watch_price(w["id"], current,
                                          baseline=w.get("baseline_price") is None)
                continue

            line = draft_price_alert(
                _route(w),
                {"price": current, "merchant": "flights"},
                w, reason, source_label="flight search")
            # Same cross-job gate every other proactive sender uses, so a fare
            # alert doesn't land on top of a morning update about the same trip.
            if _is_duplicate_subject(w["phone"], line):
                print(f"flightwatch: suppressed duplicate subject for {_route(w)}")
                update_flight_watch_price(w["id"], current, alerted=True)
                continue
            if send_sms(w["phone"], line):
                alerted += 1
                update_flight_watch_price(w["id"], current, alerted=True)
        except Exception as e:
            print(f"flightwatch: watch {w.get('id')} failed: {type(e).__name__}: {e}")
    print(f"flightwatch: checked {checked} watches, sent {alerted} alerts")
