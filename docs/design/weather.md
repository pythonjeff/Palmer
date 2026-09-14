# Weather

Forecasts. Governs `weather.py`, `wxaudit.py`.

Each section below was written when the behaviour it describes was decided, usually after a production incident, and is kept verbatim. Read the one for a module before changing that module.


## Weather: one source per user, and it is NWS wherever NWS reaches

`_weather_report` (prose) and `weather_snapshot` (page, card, morning line) both
prefer NWS for US coordinates and fall back to Open-Meteo. They did not always
agree: the snapshot used to be Open-Meteo unconditionally, so a US user's page
and their chat answer came from different forecasters and printed different
numbers for the same city on the same morning — 96 on the page against 90 in the
thread.

**The gap is not rounding, and no paid API closes it.** For one August day in
Culver City the raw models spread 15 degrees on the same point: MeteoFrance 83,
JMA 82, ICON 90, GEM 94, GFS 96, ECMWF 97, OpenWeatherMap 96. Coastal LA is
decided by how far the marine layer pushes inland and the models disagree about
it. NWS said 90 and Google (weather.com/IBM, also human-tuned) said 87 — because
both are forecaster products, where the local office corrects model output for
terrain. Open-Meteo's default `best_match` is raw GFS, which is why the page was
showing the least-corrected number in that table. Prefer the forecaster.

The split was originally justified by Open-Meteo's WMO `weather_code` mapping
"directly to which art to draw". The newspaper redesign deleted the illustrated
art (`cards.py`), so that reason had already lapsed — nothing outside `weather.py`
reads `weather_code` now, and `weather_code` is `None` on the NWS path.

Keep the Open-Meteo fallback. It is the only one of the two with coverage outside
the US, and NWS does go down. Its free tier is **non-commercial only**, which is
the one place this stack could ever start costing money; NWS is public domain with
no key and no quota, so a US-only userbase pays nothing.

Three NWS shapes are load-bearing:
- **`feels_like` and `gusts` live only on the gridpoint feed**, in degC and km/h,
  while everything else is Fahrenheit and mph off `/forecast` and
  `/forecast/hourly`. They are chips, so a gridpoint failure drops the chip and
  keeps the forecast.
- **Wind arrives as prose** ("5 to 10 mph"). `_mph` takes the top of the range —
  `cards.py` and `page.py` format it with `:.0f` and a string raises there.
- **`_nws_points` is cached** for the dyno's lifetime like `_geocode`. Grid cells
  don't move, and it saves a round trip on every refresh.

Patching `_fetch_openmeteo` no longer keeps a US location offline in tests — it
routes to NWS and makes a real call. `test_weather_source.py` patches every hop
and clears `_nws_points_cache`; `test_cards.py`'s Open-Meteo shape test reaches
that branch through Paris.

## A second weather location is additive, never a second `city`

`profile["city"]` stays the one primary location every tool, the morning send,
and the timezone derivation key off — none of that changes. `weather_locations`
(`add_weather_location` / `remove_weather_location`) is a small separate list of
places a user pins to their page *alongside* their city — a second home,
family elsewhere, somewhere they check often. Modeled on `follow_show`/
`follow_team`, not on the weather-topic path in `update_morning_briefing`'s
dispatch: `weather.resolve_weather_location` (a thin wrapper over `_geocode`)
runs once on the write path so an unresolvable place asks rather than guesses,
`weather.WEATHER_LOCATIONS_MAX` caps the list, and `home.invalidate(phone,
("weather_extra",))` expires the cache the same turn so a location just added
doesn't sit missing for up to ten minutes.

It is page-only, on purpose, in both halves of the render:

- **The page** (`page.py`) renders `payload["weather_extra"]` as its own
  "Weather" card, one row per location — the primary city keeps its unlabeled
  hero treatment, so this is the first place the word "Weather" appears at
  all, not a duplicate of anything.
- **The PNG card** (`cards.py`) does not render it, and that is not an
  oversight: the hero's chips already run to their cap of 3 and bottom out
  around y=354, and the gap above the Opening band (~y374) and the news rule
  (`H-90`) is ~26px on a fixed 1200×630 image — there is nowhere to put a
  second location without shrinking something else. Same tradeoff as Opening
  itself being capped to 3 rows on the card against 5 on the page.
- **The morning text** never mentions it either, for the same reason tracked
  topics, prices and headlines don't: the morning update is basics plus a
  link, and anything beyond that lives on the page only.

`home._fetch_weather_extra` fetches one `weather_snapshot` per location and
keeps whatever succeeds — a single bad geocode or a transient failure drops
that one row rather than blanking the section, the same shape `_fetch_prices`
uses for a ticker that 429s. It shares the primary slot's 600s `STALE` window
and rides the same generic per-section loop in `refresh_stale` that already
handles `prices` as a list-valued section, so no new refresh machinery was
needed — only a second entry in the fetcher tuple.

## When the forecasters disagree, Palmer says so instead of picking one

A Woodland Hills user was told 103, 106, 107 and 111 on four consecutive days
against actual highs of 98.3, 96.8, 97.8 and 99.5 — corroborated by Van Nuys and
Burbank reading 102 on the worst day. There was no bug: NWS's period forecast,
hourly forecast and raw gridpoint all said 110, and `_nws_snapshot` read them
correctly. That grid cell simply runs hot.

**Do not "fix" this by blending sources.** It was the first thing tried and it
is worse. In the same week NWS was the single best number available for coastal
Culver City (+1.7F against actuals, where every raw model ran 5-11F hot), so a
median of NWS and Open-Meteo makes one user's number ~5F worse to make the
other's better. NWS knows the marine layer the models overshoot; the models handle the
inland Valley that NWS overcooks. Neither wins everywhere.

**A single second opinion measures the wrong thing.** NWS-vs-best_match gives a
4.7F gap at Woodland Hills (GFS shares the warm error) and 6.2F at Culver City
(where NWS is right) — backwards. The spread across the *ensemble* separates
them: 16.3 against 8.7. `weather._ensemble_spread` pulls ECMWF, ICON and GFS in
one keyless call and sets `high_confident`; over `HIGH_SPREAD_HEDGE` the
snapshot carries `high_low_est`/`high_high_est` and the digest tells the drafter
**not** to state a single high. The page renders the same range, because page,
card and text come off one payload and must not disagree about how sure Palmer
is. It qualifies the claim; it never changes the number.

**The audit now chooses, not just reports.** `wxaudit.best_source(city)` returns
the forecaster a city has *earned* — and `None`, meaning carry on, until the
evidence is unambiguous. It is consulted by `weather_snapshot` on every read
(cached per city per day) and the whole system re-decides daily from a rolling
30-day window, so a source that drifts loses its place without anyone editing
code.

Three gates, all of which must pass, and the middle one is the important one:

- the challenger has at least `MIN_SAMPLES` (5) scored days;
- **the incumbent has too.** NWS has no historical-forecast endpoint, so it
  starts with almost no scored days — switching away from it on that basis
  would be exactly the anecdote-fitting this module exists to replace;
- the challenger beats it by more than `SWITCH_MARGIN` (2.0 MAE). Without a
  margin the choice churns between sources that are equally good, and a
  forecaster that changes weekly is its own kind of wrong.

**Why per-city rather than one winner:** measured over the same days, ECMWF is
the most accurate source for Woodland Hills (+3.3) and the *least* accurate for
Culver City (+12.0); NWS is the reverse. Two cities 25km apart, same geocoder,
same code path, inverted answers. That is also the answer to "would a ZIP code
help" — no. Open-Meteo's geocoder resolves `90232` and `"Culver City"` to
identical coordinates, and `63122` to **Ceyrat, France**. The disagreement is
between forecasters about one point, not about which point.

**A proven source is stated, not hedged.** `_ensemble_spread(..., proven=True)`
returns `high_confident` without making the call: the other models disagreeing
is what put this source in front, so it is no longer a reason to qualify the
number. That is what eventually retires the "somewhere between 88 and 103"
phrasing for a city — not by loosening the hedge, but by earning the right to
skip it.

`HIGH_SPREAD_HEDGE = 10.0` is **provisional** — a round number that catches the
observed bad case and clears the observed good one, set from days rather than
months. `wxaudit.py` exists to replace it with a measurement: a daily cron logs
every source's forecast per city and backfills the actual from Open-Meteo's
reanalysis archive, and `wxaudit.report()` prints signed bias and MAE per city
per source. The incident week, backfilled, already shows there is no global
winner — Culver City: GFS +4.6, ICON +5.6, ECMWF +11.8 (and NWS +1.7);
Woodland Hills: ECMWF +3.4, GFS +5.6, ICON -7.1; Kirkwood: everything within
0.6. NWS has no historical-forecast endpoint, so its rows only accumulate
forward from the day the job was added.
