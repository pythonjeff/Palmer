# Shopping and flights

Price watches and flight watches. Governs `shopping.py`, `amazon.py`, `price_alert.py`, `flights.py`, `flightwatch.py`, `hotels.py`.

Each section below was written when the behaviour it describes was decided, usually after a production incident, and is kept verbatim. Read the one for a module before changing that module.


## Price watches: a flat $2 bar, twice a day on a cron

**Materiality is a flat $2, in either direction.** `shopping.MOVE_MIN_ABS` is the whole rule: any move of more than $2 earns a text, on a $12 item and a $1,200 one alike. It is deliberately not proportional. Two earlier versions were, and both failed the same way — a flat 15% meant a $50 consumable needed a $7.65 move in one step, which groceries never make, so those watches could never fire at all; `max(5%, $2)` then held expensive items to a $10+ move, inverting the intent again. A percentage bar always encodes an assumption about what kind of product this is, and the watch list holds every kind.

Rises alert too, not just drops, which is why `_should_alert` returns `'rise'` alongside `'target'`/`'drop'`. `price_alert.draft_price_alert` gives a rise its own lead — "your price watch just hit" on a price INCREASE reads as good news and is actively misleading. `_fallback` stays direction-neutral (it states where the price *is*), so it covers every reason without branching.

Because the bar is low and fires both ways, the rate limits are what keep it civil rather than an afterthought: the twice-daily cadence, the per-watch `cooldown_hours` (default 12), `PRICE_DAILY_ALERT_MAX`, the re-baseline on every alert, and `_is_duplicate_subject`. Removing any of them turns a $2 bar into a pager.

**`run_price_watches` is on a cron trigger, and must stay one.** An APScheduler interval job's first run is scheduled at `start + interval`, and that clock restarts on every dyno boot — which means every deploy. At a twice-daily cadence it made the job a function of deploy history rather than of the clock: on a day with four deploys it never ran at all, and since a tick that finds no qualifying price change logs nothing, it failed invisibly. The two slots are 16h and 8h apart rather than evenly split, deliberately — the budget constraint is runs per day, while the hour is the part users feel, and no strict 12h split lands in waking hours for both timezones served. `test_price_watches.py::TestPriceWatchSchedule` guards the phase-independence property.

## Flight watches

Palmer told two users it could not track flights. `search_flights` worked the
whole time; what was missing was the *watch*, so instead of doing the half it
could it disclaimed the whole thing. `flightwatch.py` is that half:
`add_flight_watch` / `cancel_flight_watch`, a **once-daily cron**, alerts on a
target hit or any move over `MOVE_MIN_ABS` ($40 — fares wobble tens of dollars
daily, so the flat $2 product rule would page someone every morning).

**The daily cadence and `db.FLIGHT_WATCH_MAX` (3) are budget controls, not
preferences.** SerpAPI is the only paid input and the account is on 250
searches/month; one active watch costs ~30. Watches whose departure has passed
retire themselves rather than spending a search a day on an unbookable flight.
