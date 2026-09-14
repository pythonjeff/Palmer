# Commute

Traffic and the commute. Governs `traffic.py`, `home._fetch_traffic`.

Each section below was written when the behaviour it describes was decided, usually after a production incident, and is kept verbatim. Read the one for a module before changing that module.


## The commute is routed for the leave time, not for the moment of the fetch

The commute is one of the three basics the morning text is required to carry, and
it was the weakest of them: `profile["commute"]` was written only by the Haiku
extractor (no tool — `get_travel_time` even said "we don't store addresses"), and
`home._fetch_traffic` routed for *now*. The morning job runs at `morning_time`
(default 07:00), so a user who leaves at 8:30 was told the 7:00 number, and the
page showed whatever traffic was doing when they happened to tap.

`set_commute(origin, destination, leave_time?)` / `clear_commute` are the controls
now, modeled on `add_weather_location`: both addresses are geocoded **on the write
path** and stored as `origin_ll`/`dest_ll`, so the read path — every page view —
geocodes nothing; an unresolvable address asks, never guesses; `leave_time` goes
through `_normalize_hhmm`; and the dispatch expires the `traffic` section so the
card is right on the next view. A legacy string-only commute still geocodes at
fetch time (now behind `traffic._addr_geo_cache`, successes only).

**The rule** (`home._commute_depart_at`, `COMMUTE_PREDICT_MIN_LEAD`): today at
`leave_time` in the user's zone, if it is still ≥ 5 minutes ahead, is passed to
TomTom as `departAt`, which routes on historical speed profiles for that
departure and comes back `predicted: True`. Otherwise the fetch is live, exactly
as before — including every view after the leave time has passed, when the card
shows evening traffic under the Commute label and its sub-line says "right now".
A Saturday `departAt` predicts Saturday traffic; that is honest and left alone.

**Prediction vs. live is labelled on every surface.** The digest says
`Commute at 8:30am (their usual leave time — predicted for that departure)` or
`Commute right now`; the page card carries `leaves 8:30am · arrives ~9:04am` or
`right now`; the og:description and the PNG card follow. A forecast presented as
current traffic is the same over-claim as stating a high the forecasters disagree
on, so the drafter is told in as many words which moment the number is for.
Times are stored as 24-hour `HH:MM` (the `morning_time` shape) and rendered
through one `timeutil.friendly_hhmm`, so the three surfaces cannot disagree.

**Addresses never render on the page.** It is an unauthenticated tokenized URL and
the addresses are someone's home and office. The guarantee is structural —
`traffic_snapshot` never puts `origin`/`destination` in its result, and
`test_commute.py` asserts the key set — rather than a rule in `page.py`.

**`commute` left `EXTRACT_PROMPT`**, the way `followed_teams` and `shows` are kept
out: a Haiku write of `{origin, destination}` would replace the tool's dict and
silently drop the coordinates and the leave time. It is still a real field, so
`_apply_profile_updates` also drops an extractor `commute` when the stored one is
tool-written. `departAt` must be percent-encoded: an offset east of UTC carries a
`+`, which decodes to a space in a query string and TomTom 400s on it.

## Landmarks vs. addresses in the traffic pipeline

TomTom's geocoder is a mapping API, not a search engine, and mis-ranks landmark names (e.g. "White House", "Fenway", "LAX"). `traffic.py` and the `get_travel_time` tool run landmark destinations through Sonnet to resolve them to street addresses *before* geocoding. Preserve this indirection when touching routing code.
