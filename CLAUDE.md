# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What Palmer is

Palmer is a personal AI delivered entirely over SMS via Twilio. A FastAPI web dyno handles inbound SMS webhooks and runs all background jobs in-process via APScheduler. There is no separate worker.

## Running / commands

```bash
# Install (uses .python-version, currently 3.12)
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Local dev — hot reload
uvicorn main:app --reload

# Expose to Twilio for local dev
ngrok http 8000     # point Twilio SMS webhook at https://<ngrok>/sms

# Tests (pytest — no config file, discovers test_*.py)
pytest
pytest test_morning_schedule.py::TestSendWindow::test_before_time_no_send   # single test

# Preview a morning briefing without sending
curl 'http://localhost:8000/preview?phone=+15551234567'

# Trigger the morning job once (bypasses schedule; still respects per-user send window + per-day guard)
python send_morning.py

# Trigger reminder delivery once
python send_reminders.py

# Import main without starting the job loop (tests, shells, one-off scripts).
# Without this, importing main starts APScheduler and send_due_reminders will
# send REAL SMS on a 1-minute interval.
PALMER_NO_SCHEDULER=1 python -c "import main"
```

`.env` variables (see README for full table): `ANTHROPIC_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, `TAVILY_API_KEY`, `SERP_API_KEY`, `TMDB_API_KEY`, `TICKETMASTER_API_KEY`, `TOMTOM_API_KEY`, `GIPHY_API_KEY`, `APP_URL`, `DATABASE_URL` (optional locally — falls back to SQLite `palmer.db`).

## Architecture that spans multiple files

### Single-process is a hard requirement
`WEB_CONCURRENCY=1` must be set in production. `main.py` holds all cross-request coordination in memory: per-phone `threading.Lock` (`_phone_locks`) serializes inbound messages so conversation history never interleaves, `_in_flight` tracks concurrent turns from the same number so replies can be quote-prefixed, and `_seen_sids` deduplicates Twilio webhook retries. APScheduler also runs in-process. A second worker breaks all of the above.

### DB layer is dual-backend
`db.py` transparently switches between Postgres (when `DATABASE_URL` is set) and SQLite (`palmer.db` in repo root) using a `PH` placeholder (`%s` vs `?`) — every query in `db.py` uses `PH` and `_conn()`. Any new DB code must follow this pattern; do not hard-code `%s` or `?`.

Reminder delivery uses `FOR UPDATE SKIP LOCKED` on Postgres in `claim_due_reminders()` so multiple scheduler ticks are safe against double-sends. The SQLite branch does a plain select-then-update (fine for single-process local dev).

Schema is created lazily in `init_db()`, called at import time from `agent.py`. New columns on existing tables must be added to the `new_cols` migration list — Postgres uses `ADD COLUMN IF NOT EXISTS`, SQLite catches the duplicate-column exception.

### Module layout
`agent.py` used to be 1,721 lines holding the client, prompts, tool schemas, weather, prices, profile handling and the conversation loop. It was split; import from the real owner, not from `agent`:

```
llm.py          client, HAIKU_MODEL, SONNET_MODEL, _parse_json
netutil.py      _http_get_json, _http_get_json_retry
smstext.py      _sms_clean, shorten_message, _normalize_hhmm, _parse_published
prompts.py      SYSTEM_PROMPT, EXTRACT_PROMPT, CONSOLIDATE_PROMPT
tools_def.py    TOOLS schema
weather.py      geocoding, NWS (US) + Open-Meteo (rest of world)
sources.py      news source quality: blocklist, tiers, relevance floor, corroboration
datafeeds.py    Tavily search, crypto/stock prices, GIFs, media
tickers.py      morning topic -> tradeable symbol (Markets section)
userprofile.py  profile extract/consolidate + the two cross-send dedup gates
agent.py        _build_system, get_reply, tool dispatch, save_assistant_turn
```

Dependencies run strictly downward: `llm`/`netutil`/`sources` ← `smstext`/`weather`/`datafeeds` ← `userprofile` ← `agent`. Each module imports standalone; keep it that way.

`sources.py` imports nothing from Palmer at all — that is what lets `datafeeds` use it. The tier helpers used to live in `watches.py`, which `datafeeds` sits below, so filtering at the search call would have been a cycle.

`agent.py` exports exactly `_build_system` (used by every module that sends a user-facing message) plus `get_reply`/`save_assistant_turn` for `main.py`. It is no longer a grab-bag facade — don't add re-exports to it.

Underscore prefixes still mean "internal to Palmer", not "private to this module" — `smstext._sms_clean` is imported by six modules. Grep before renaming.

**Patching in tests follows the code, not the name.** `patch("agent.client")` stopped working when functions moved out; patch the module the function actually lives in (`patch("userprofile.client")`). A dead patch target does not fail loudly — it lets the test make real API calls. Watch the suite runtime: 643 tests in ~4s, and a jump means something is hitting the network.

### Scheduler cadence (main.py)
```
send_due_reminders       every 1 min
send_morning_messages    every 5 min   (each user has a local target time; per-day guard prevents double-sends)
run_watches              every 30 min
run_alert_checks         every 60 min
send_missing_data_asks   every 60 min  (asks users with no city so mornings can target them; DATA_ASK_DRY_RUN=1 to preview)
run_followups            every 4 hr
run_price_watches        00:00 + 16:00 UTC (cron, NOT interval — see below; SerpAPI Google Shopping + Amazon; baseline seeded at watch creation, alerts on target-hit or ANY move over $2 in either direction, then re-baselines)
```

**Materiality is a flat $2, in either direction.** `shopping.MOVE_MIN_ABS` is the whole rule: any move of more than $2 earns a text, on a $12 item and a $1,200 one alike. It is deliberately not proportional. Two earlier versions were, and both failed the same way — a flat 15% meant a $50 consumable needed a $7.65 move in one step, which groceries never make, so those watches could never fire at all; `max(5%, $2)` then held expensive items to a $10+ move, inverting the intent again. A percentage bar always encodes an assumption about what kind of product this is, and the watch list holds every kind.

Rises alert too, not just drops, which is why `_should_alert` returns `'rise'` alongside `'target'`/`'drop'`. `price_alert.draft_price_alert` gives a rise its own lead — "your price watch just hit" on a price INCREASE reads as good news and is actively misleading. `_fallback` stays direction-neutral (it states where the price *is*), so it covers every reason without branching.

Because the bar is low and fires both ways, the rate limits are what keep it civil rather than an afterthought: the twice-daily cadence, the per-watch `cooldown_hours` (default 12), `PRICE_DAILY_ALERT_MAX`, the re-baseline on every alert, and `_is_duplicate_subject`. Removing any of them turns a $2 bar into a pager.

**`run_price_watches` is on a cron trigger, and must stay one.** An APScheduler interval job's first run is scheduled at `start + interval`, and that clock restarts on every dyno boot — which means every deploy. At a twice-daily cadence it made the job a function of deploy history rather than of the clock: on a day with four deploys it never ran at all, and since a tick that finds no qualifying price change logs nothing, it failed invisibly. The two slots are 16h and 8h apart rather than evenly split, deliberately — the budget constraint is runs per day, while the hour is the part users feel, and no strict 12h split lands in waking hours for both timezones served. `test_price_watches.py::TestPriceWatchSchedule` guards the phase-independence property.

Morning briefings are sent by APScheduler at each user's chosen local time (default 7:00). No Heroku Scheduler job is required; if one runs `send_morning.py`, the per-user sent-date guard prevents double-sends.

### Reminders repeat; morning topics are the other kind of repeating
`reminders.recurrence` is NULL for a one-shot or one of `timeutil.RECURRENCES`
(`daily`/`weekdays`/`weekly`). A recurring reminder is never re-inserted — it is
**re-armed in place** by `db.rearm_reminder` after each send, so its id is stable
and `cancel_reminders` keeps working on it unchanged.

Four things here are load-bearing:

- **`timeutil.next_occurrence` advances the LOCAL wall clock, not the UTC
  instant.** A 3pm Chicago reminder is 20:00Z under CDT and 21:00Z under CST;
  adding 24h in UTC holds the instant fixed and silently walks the reminder to
  2pm local the day after a DST change, then leaves it there. Each candidate is
  rebuilt as `datetime(y, m, d, h, m, s, tzinfo=zone)` so the offset is whatever
  that date implies.
- **It skips missed periods.** The next occurrence is the first one strictly
  after *now*, not previous + one period — otherwise `due_at <= now`'s catch-up
  semantics turn an outage into one text per missed day on recovery.
- **`cancel_reminders` nulls `recurrence` as well as setting `sent = 1`.** That
  is what closes the cancel/re-arm race: claiming and cancelling both set
  `sent = 1`, so they are indistinguishable by `sent`, and `rearm_reminder`'s
  `recurrence IS NOT NULL` guard is what keeps a reminder cancelled in that
  window dead.
- **A failed send still re-arms.** The claim already consumed the occurrence, so
  bailing on a Twilio hiccup would end a standing reminder for good. This is the
  opposite of the daily-guard jobs (morning, alerts, followups), where releasing
  the claim is right because there the claim *is* the delivery record.

`save_reminder`'s duplicate guard is **same `due_at` (to the minute) AND similar
text**, where similarity is a stopword-stripped token Jaccard — no model call,
since this runs on the write path inside a live turn. The old guard was exact
text while ignoring `due_at` entirely, which was wrong in both directions: four
model rephrasings of one ask (two differing only by an em-dash vs a hyphen) all
landed at the same minute and all fired, while a legitimate second "call mom"
for next week would have been silently dropped.

`send_due_reminders` dedups **within the tick** rather than calling
`_is_duplicate_subject` like every other proactive sender. That difference is
deliberate: a reminder is explicitly requested for a named time, so suppressing
it because Palmer mentioned the topic six hours ago would defeat the request —
a missed reminder is worse than the duplicate it would prevent.

The split users get wrong, so the tool description and SYSTEM_PROMPT both spell
it out: a repeating ask for **information** ("daily Eagles camp update", a score
every morning) is `update_morning_briefing`, not a reminder. `set_reminder` with
`recurrence` is a repeating **nudge to do something**. If Palmer has to look
something up to write the message, it belongs in the morning update.

### The morning update is basics plus a link, not a full briefing
`morning._compose_morning` sends ONE message: a short Palmer-drafted text carrying the basics, then the user's Palmer Home URL. Every user gets the same shape — today's weather, the commute if they have an address on file, and 1-2 things newly open or worth catching nearby this week — so a user who never taps the link still gets those three every day. Anything beyond that (their tracked topics, prices, headlines) lives on the page only. It used to be a single one-line teaser ("here's a reason to tap"), and before that the full text briefing plus a second text carrying the link — both said less or said everything twice; this is the middle point.

Two properties are load-bearing:
- **The URL is last and alone.** Message apps only draw the rich link preview when the message carries exactly one URL at a boundary, and that preview is most of the value. Nothing may follow it — not a period, not a sign-off.
- **`carries_link` gates the status callback.** A link message is sent with `add_status_callback=False`, because the `/sms-status` shorten-and-retry would truncate the URL into garbage.

`generate_morning_line` drafts the text on Sonnet through `_build_system` like every other user-facing message. It builds a REQUIRED list from what the payload actually has (weather is basically always there once a city is known; commute only when `traffic` is populated, which only happens when the profile has an address; opening only when `opening_snapshot` returned rows) and tells the model every item on that list must appear — with real specifics, not a vague gesture at the category — plus at most one more sentence about something else on the page if it's genuinely notable. Two rules are enforced in code rather than trusted to the prompt, because the model breaks both: `_strip_link_placeholder` removes "[link]"-style stand-ins and any invented URL, and `_NAMES_THE_LINK` triggers exactly one redraft when the line says "page"/"link"/"dashboard" — that phrasing turns a text from a friend into a push notification.

Opening is no longer opt-in for this reason — `home._fetch_opening` fetches it by default for any user with a city (a user can still be excluded with `morning_prefs.opening = False`). It shipped off at first specifically so a bad metro's rows could be caught with `preview_opening.py` before anyone saw them; that review still matters, it just now happens after rollout instead of gating it.

Every failure falls back to the full text briefing (`generate_morning`, still used by `/preview?full=1`): no APP_URL, an empty page, or a failed draft. A user never gets a link to nothing.

**The forecast is named by the geocode that produced it, never by the profile.**
`_payload_digest` labels the weather line with `weather["resolved"]` — the place
`weather_snapshot` actually fetched — falling back to `payload["city"]` only when
that is absent. The two agree right up until `profile["city"]` drifts, and on that
day this is the difference between a visible error and a lie: a user in Culver City
got three consecutive mornings of Los Angeles temperatures (98, 100, 102 against
local highs of 88, 89, 90) carrying the name Culver City. `weather.py` was innocent
throughout — it forecast exactly the city it was handed.

Two independent defects stacked, and both halves of the fix matter:

- **Write.** He set his weather location by saying "I want the weather updates to be
  specific to Culver City California", which routes to `update_morning_briefing` —
  and that only ever wrote the topic string, so `profile["city"]` kept its older,
  broader value. `EXTRACT_PROMPT` could not catch it either: LOCATION PRECISION
  deliberately writes `city` only from a statement of residence or an explicit
  correction, and a weather preference is neither. `agent._city_from_weather_topic`
  now derives the city where the user actually sets it, on save, never on read —
  same terms as `_normalize_price_topic`. It leaves `timezone` alone (that is only
  derived when absent) so correcting a forecast cannot move the hour the morning
  arrives, and it expires the cached `weather` section as well as `prices`, or the
  10-minute stamp serves the old city's numbers immediately after the correction.
- **Read.** The drafter was handed `Weather in Los Angeles: high 102` and wrote
  "102 in Culver City today" anyway, reconciling the number against the Culver City
  strings throughout the profile in its system prompt. Nothing stopped it: the line
  prompt's only data rule was about numbers. The text briefing has carried the city
  rule from the start ("name the city the forecast is for, exactly as it appears in
  the data"); the one-line path that replaced it as the daily send never inherited
  it, and now does.

The write fix governs how often the city is wrong; the read fix governs what a wrong
value can do. Only the second holds against a write path nobody has enumerated yet —
"102 in Los Angeles" is read as wrong in one second, where the same number under the
right city name is unfalsifiable from the message. `test_weather_city.py` guards both.

### Onboarding asks once; the site builds ahead of it, silently
Message 1 never demands anything — `SYSTEM_PROMPT`'s NEW USERS rules cover a bare
greeting, a random question, and "what do you do" without ever requiring city or
name up front. From message 2 on, if `intro_sent` is true and the profile still has
no `name` or `city`, `_build_system` appends an ONBOARDING ASK directive telling
Palmer to work one short question in naturally — never as an opener, never a form.
`userprofile._update_profile` marks `onboarding_ask_sent` the first time it sees
that same condition hold after a turn's extraction runs, so this fires exactly
once per user, whether or not they answer. It does not repeat, and it does not
duplicate `send_missing_data_asks` (morning.py), which is a separate hourly
outbound safety net for users who already said yes to mornings but still have no
city on file.

`userprofile._apply_profile_updates` builds Palmer Home the moment a city first
lands on the profile — `_eager_build_home` calls `home.rebuild(phone,
refresh_news=True)` as soon as `new_city and not old_city`, rather than waiting
for `get_my_page` or the first morning send. That gate matters: it fires once,
on the transition from no city to a city, not on every later correction — a
correction rides the existing `home.invalidate` path in `update_morning_briefing`'s
dispatch, not another full paid rebuild, and it keeps `test_city_regression_prints_old_and_new`
free of a live network call. No `APP_URL` means nowhere to serve the page, so the
build is skipped entirely rather than spending on a link nobody can open.

**Building the page early does not mean sending it.** Nothing here calls
`ensure_fresh`, mentions the page, or drafts a link — the ONBOARDING ASK block
explicitly tells Palmer not to. `get_my_page` still only fires when the user
asks, per the existing "never send a URL unless asked" rule, and the morning job
is still the one place the link goes out unprompted. So the effect of this pair
is purely: by the time either of those paths runs, the page is already sitting
there populated with real data, instead of a user's first "send me my link"
landing on `ensure_fresh`'s cold-build path live inside that reply.

### One voice: all user-facing text goes through `_build_system`
Anything the user reads is drafted with `agent._build_system(phone)` as the system prompt, on `SONNET_MODEL`. That is what carries SYSTEM_PROMPT, the CALIBRATION section, the user's `communication_style`, and their reaction history — so Palmer sounds like the same person everywhere.

There used to be a second tier of paths carrying their own one-line persona ("You're Palmer, a dry, sharp texting friend") that never saw any of it, which meant a user who asked for less sarcasm still got the breezy default on price alerts and reminders. Do not reintroduce that. If you add an outbound message, it goes through `_build_system`.

The deliberate exception is `traffic.py`: its output is *source data* for a draft that already carries the system prompt (morning briefings, and the `get_city_traffic` tool inside `get_reply`), so it is a plain factual summarizer on Haiku. Voicing it there would layer a second, uncalibrated Palmer under a real one.

### Reactions (tapback.py)
iMessage and Google Messages degrade reactions to plain text over SMS (`Liked "..."`), so they arrive as ordinary inbound messages. `main._handle_sms_inner` short-circuits on them before anything else runs:

1. `parse_reaction()` — free regex; is this a reaction at all
2. `interpret_reaction()` — Haiku, in context and per person: what is the reaction *doing*
3. act — silence for everything except `answer` (a 👍 on "want me to add that?" is a yes)

Silence is both the default and the failure default, so a Haiku outage degrades to silence rather than to unwanted texts. **Returning `True` from the reaction branch is load-bearing** — `_handle_sms` fires `FALLBACK_SMS` on a falsy return.

Reactions then feed `communication_style`, `morning_prefs["avoid"]`, and a pacing factor that stretches followup gaps and lowers the alert cap. Each is behind a threshold so one stray tap can't reshape Palmer, and a dropped topic is announced once via `pending_preference_notice` rather than silently vanishing.

### Shared modules — don't re-copy these
- `serpapi.py` — SerpAPI key, base URL, timeout, and request transport. Both `shopping.py` and `amazon.py` use it. Each still parses its own engine's payload; only the transport is shared.
- `price_alert.py` — the one drafter for price-watch alerts, used by both price sources. It lives in its own module because `shopping.py` already imports `amazon.py`, and putting it in either would make that coupling bidirectional.

### Topics become prices via `tickers.py`
The Markets section of Palmer Home is derived from the user's `morning_topics`, so a topic only shows a price if it can be resolved to a symbol. That resolution used to be a bare uppercase-word regex, which meant it worked only when the drafting model happened to write the ticker into the topic itself — `"Nvidia stock price (NVDA)"` resolved, `"Nvidia stock"` silently did not, and the user got the topic listed under "Palmer is watching" with no price anywhere. It also matched the `US` in `"US stock market"` and spent a yfinance call on a delisted symbol every page refresh.

`resolve_topic_asset` runs cheapest-first and **never calls a model** — it is on the read path, which runs on every page view: crypto name → explicit `$SYM`/`(SYM)` → curated name map → bare uppercase token behind a `NOT_TICKERS` stopword guard. It returns `(symbol, display_label)` because Yahoo's index symbols are correct and unreadable; nobody wants `^GSPC` in their Markets section.

`resolve_company_ticker` is the Haiku escape hatch for names the map doesn't carry. It runs **once when a topic is saved** (`agent._normalize_price_topic`, called from the `update_morning_briefing` dispatch), never on read.

**Resolution is Yahoo's search endpoint, not a model.** Keyless, ~0.2s, filtered to `quoteType=EQUITY` on a US exchange. It is self-updating, which is the property the alternatives lacked: it independently returns SPCX for SpaceX and XYZ for Block, the two entries the hand-written map had wrong. The filter is load-bearing rather than defensive — unfiltered, `"openai"` comes back as a tokenized crypto and a thematic ETF that merely share the name, so filtering is what makes a private company resolve to nothing instead of to somebody else's price. Strip price words from the query first: `"spacex"` returns SPCX, `"spacex stock"` returns nothing.

Two earlier versions of this got it wrong the same way, and the pattern is worth remembering: a hardcoded `PRIVATE_COMPANIES` denylist, then a Haiku lookup verified against the exchange. Both encoded a model's snapshot of who was public, and a snapshot goes stale the moment anybody lists.

**Indices stay hand-mapped.** Search returns futures for them (`"s&p 500"` → `ES=F`, `"nasdaq"` → `NQ=F`), so `INDEX_TICKERS` is correct where search is not.

Company names are gated behind a price word, indices are not. Without that gate `"SpaceX news"` resolves to SPCX and a news topic someone follows silently grows a stock ticker in their Markets section; `"nasdaq"` needs no such qualification.

**A stale model must not veto live data.** `SYSTEM_PROMPT` forbids claiming a company is private, delisted, or hasn't IPO'd from memory, and `get_price` resolves company names through `tickers.resolve_asset_name` so the tool answers rather than 404ing on `"SPACEX"`. Palmer was refusing to add SpaceX and explaining it was private, which was simply false — and the failed lookup had confirmed its prior.

`cards.MAX_PRICES` is the shared cap. Four columns fit the card's width but the sparklines start overdrawing the price text, so three is the real limit; `home._fetch_prices` imports the constant rather than keeping its own, since the card and the page render from one payload and must not disagree about how much of it survives.

### Opening: metro-scoped weekly content
`opening.py` feeds the `Opening` card — what is newly open or worth catching near
the user this week, plus a couple of movies/shows. Three sources, **none of them
SerpAPI**: Tavily for local press, Ticketmaster Discovery for dated events, TMDB
for releases.

SerpAPI was the obvious first guess and both candidate engines failed. Do not
retry them. `google_events` returns `events_results_state: "Fully empty"` for
every query, including SerpAPI's own documented Austin example with the
`location` parameter. `google_local` works but is a proximity search with no
`opened_date` field — asking it for "new restaurants" near Culver City returns
Applebee's. Neither is an openings feed, and the account's free tier (250
searches/month, already ~40% spent on price watches) could not have carried a
per-user daily fetch regardless.

**The section is metro-scoped and weekly, and that is the entire cost model.**
"New restaurants in LA" is identical for every LA user; "movies out this week" is
identical for everyone. `_local_cache` keys on a coarse lat/lon bucket (0.5°,
~35mi, so Culver City and Woodland Hills share one fetch) plus ISO week;
`_screen_cache` keys on the week alone. Two users in a metro cost one fetch, and
adding users to a covered metro costs nothing. Same in-process pattern as
`trends.py`, safe for the same reason — `WEB_CONCURRENCY=1`.

Three things are load-bearing:

- **Suburbs are dead ends for news search.** "New restaurants opening in Culver
  City" returns nothing; the same query for Los Angeles returns the LA Times.
  Nobody writes an openings column for a suburb. `_metro` resolves city → metro
  on Haiku, once per city, cached — deliberately *not* a lookup table, which is
  the mistake `tickers.py` made twice with `PRIVATE_COMPANIES`. Ticketmaster
  sidesteps the problem entirely by taking `latlong` + radius instead of a name.
- **The local outlets had to be added to `trusted_sources.json`.** Palmer Home
  passes `trusted_only=True`, and Eater, Time Out, LAist, Thrillist and the city
  dailies were all tier 3 — so before they were added the section returned
  nothing, every time. `canonical_domain` folds subdomains, so one `eater.com`
  entry covers `la.eater.com` and the rest.
- **`_curate` is the taste gate, and it is the whole feature.** The upstreams are
  a firehose: local press runs dining-week promos and listicles beside real
  openings. Keyword filtering cannot work — the difference between "Mamele's
  opened on Washington" and "15 best brunch spots" is editorial, not lexical. The
  prompt applies a *different* test per kind, which matters: "would a paper run
  this as 'X opens'" is right for a restaurant and wrong for a concert, and an
  early version of it silently deleted every event. A sponsor's name on a real
  festival is not an ad, and an annual festival in its Nth year is still a
  festival.

  **The prompt states today's date, and that is load-bearing.** Without it the
  model dates events against its training cutoff: handed a concert on
  2026-08-29 it called it "over a year away" and dropped it as stale, rejecting
  all seventeen candidates for a St. Louis week holding Todd Rundgren, The
  Wallflowers and Ray LaMontagne. It read as a taste problem for an hour and was
  a calendar problem. The metro, not the raw city, also has to reach the prompt —
  told "Kirkwood, MO", the model correctly rejects every venue in St. Louis as
  somewhere else.

  Events are filtered to Music and Arts & Theatre at the API. Unfiltered, a
  metro's next seven days are mostly regular-season ball games, and a Tuesday
  home fixture is not something opening.

  **Screens skip the gate entirely.** TMDB is already structured and already
  ranked by `vote_average`, so there is no firehose to filter — and running them
  through the local prompt threw away every title for being "outside the metro".
  A taste gate that rejects its whole input is not a gate.

  Local and screens hold **separate allowances** (`MAX_LOCAL` 3, `MAX_SCREENS`
  2) rather than competing for one pool. They competed at first, and a good week
  locally pushed screens off the page entirely — which is not the section that
  was asked for.

**The three kinds are per-user, and users trim them by asking.** `local` (new
places, bars, food), `event` (concerts, festivals, live shows) and `screen`
(films and series out this week) are all on by default;
`morning_prefs["opening_kinds"]` records the set only once a user actually
changes it. "I want movie openings too" and "no more concerts" route to
`update_morning_briefing`'s `opening_add` / `opening_remove` — deliberately the
existing tool rather than a new one, because users already do not distinguish
"my morning", "my page" and "markets", and a fourth verb for the same mental
object would be a fifth thing to route wrong.

The dispatch does **set arithmetic** on the stored list rather than asking the
model to restate the whole set: "movies too" is additive, and a model
re-deriving the full set from a profile dump eventually drops a kind nobody
mentioned. Removing all three sets `opening = False`, so "take all that off" and
the hard switch are one state rather than two the readers must reconcile.

**Filtering happens after the caches, never inside them.** Both caches are keyed
by metro and week and shared by every user there; narrowing a fetch to one
user's taste would make the cache unshareable and turn N users back into N
fetches. Fetching a row this user does not want costs nothing, because it was
already cached for their neighbour — so `_curate` fills a deeper pool
(`CURATE_POOL`) than any single user sees, and each user filters down from it. A
kinds change expires the cached section, or they keep seeing the concerts they
just asked to stop.

`_metro` is resolved **inside** the cache-miss branch. It costs a model call and
`opening_snapshot` runs on page views; on a hit there is nothing to search, so
there is nothing to resolve it for.

It ships **on** by default — the morning text is required to carry 1-2 opening
highlights for every user, so this can no longer be opt-in. `morning_prefs["opening"]
is False` excludes a specific user, nested so it needs no `PROFILE_FIELDS` entry.
The risk here is taste, not correctness, so a bad metro is still worth checking
with `preview_opening.py` — that review now happens after rollout rather than
gating it.

TMDB's terms require the notice *"This product uses the TMDB API but is not
endorsed or certified by TMDB"* wherever their data appears; `page.py` renders it
only when a screen row is actually present. **TMDB is free for non-commercial use
only** — the same clause shape as Open-Meteo, and a question the day Palmer
charges.

### Section labels are one word
Every card label on Palmer Home is a single word — currently `Commute`,
`Markets`, `News`, `Watching`. New sections follow the rule; there is no second
tier for "just this one".

It reads as a masthead rather than prose. "Today" and "Palmer is watching" used
to sit beside "Commute" and "Markets", which made the column a mix of headings
and a sentence, and the sentence was the one that looked like a product talking
about itself.

`cards.py` uses the same words in caps so the MMS preview and the page read as
one publication — the two render from one payload and must not disagree about
what a section is called. `test_page.py::TestSectionLabelsAreOneWord` reads the
labels out of `page.py`'s markup and fails on a space in any of them, and also
checks the card image kept in step.

### The card is cached on what it draws, not on when it was built
`artifacts.render_png` keys `_png_cache` on the token plus a hash of exactly the
fields `render_dashboard` renders (`_card_inputs`), including the masthead date.
It used to key on `built_at`, and that was silently broken: `built_at` only
advances inside `home.rebuild()`, and `ensure_fresh` calls `rebuild` only when
there is **no payload at all**. So after a user's first build the key never
changed again — the card froze on that morning's weather and stayed frozen for
good, while the page beside it refreshed normally. One user's `built_at` read
four days older than their fetch stamps.

The caller passes the bare token and the key is derived inside `render_png`.
That is deliberate: a caller composing its own cache key is exactly how this
happened, and there is no reason for `main.py` to know what the card draws.

The masthead date is the **reader's** day, not the dyno's: `render_png` passes
`when=_card_now(payload)` and the fingerprint uses the same value. `cards.py`
defaulted to `datetime.now()`, which is UTC in production, so from 5pm Pacific
the card printed tomorrow's date beside a page printing today's — `page.py` has
always used the user's zone.

`opening` renders in the left column between the weather chips (~y354) and the
news rule (`H-90`) — the one band of the card that was empty. `CARD_OPENING_ROWS`
is 3 against the page's 5, because that is what fits above the news rule.

**Local card renders now match production.** macOS ships no `Menlo-Bold.ttc`, so
a bold mono lookup fell through to Pillow's builtin bitmap face, which does not
scale — the 118pt hero temperature drew at roughly 8px. Production was never
affected (the slug has DejaVu), but the card's design is reviewed by rendering it
locally, and a local render that does not look like the real one is worse than
no render at all.

### One list drives the morning and the page
`morning_topics` is the single source for both the morning update and Palmer Home. A topic that resolves to a ticker becomes a live Markets row; everything else becomes a followed subject. So "add Apple stock to markets", "put Nvidia on my site" and "add Bitcoin to my morning" are all the same operation — `update_morning_briefing` — and its description and the `USE THE RIGHT TOOL` block say so explicitly, because users do not know they are one list.

Three things make that flow feel live rather than broken:
- **`home.invalidate(phone, ("prices",))` runs on every topic change.** The page caches prices for 5 minutes, so without it a ticker the user just added does not appear for up to five minutes, which reads as "it didn't work". It expires the stamp rather than refetching inline — the user is waiting on a text reply, and seconds of network for data nobody is looking at yet would go straight onto that reply.
- **A failed price fetch keeps its previous row.** CoinGecko 429s under load and yfinance times out. Without the fallback in `_fetch_prices`, a blip silently deletes a ticker the user is tracking, which looks exactly like Palmer forgetting — far worse than a stale number under a visible "N min ago" stamp.
- **Asking a price is not tracking one.** "what's Apple at" is `get_price`; "add Apple", "and Nvidia", "spacex too" are `update_morning_briefing`. Mid-list continuations were routing to `get_price`, so Palmer quoted a price at someone who was plainly still adding things.

A topic that resolves to a ticker is also **excluded from the paid news search** — Markets already answers it, and "Apple stock price" is a poor news query. The text briefing always did this; `home._fetch_headlines` now matches.

### The name must be extracted, not just spoken
`profile["name"]` was empty for a user who had told Palmer his name twice. Palmer still called him Jeff — it reads the name straight out of conversation history — but the page renders from the profile, so it showed "Your briefing" and kept prompting for a name it had already been given. Anything reading the profile rather than the transcript saw an anonymous user.

The cause was the extractor: `"My name is Jeff"` returned `{}`. `EXTRACT_PROMPT` asked for "life details, relationships, preferences, personality" and Haiku did not count a name as worth remembering — and where the profile already looked populated, it assumed the name must be in there. `EXTRACT_PROMPT` now opens with an IDENTITY FIRST rule that names the phrasings people use and explicitly overrides the "too obvious to return" and "surely it is already stored" instincts. `test_profile_schema.py` guards it.

The lesson generalizes: Palmer sounding like it knows something is not evidence that anything was stored. The transcript and the profile are different memories, and only one of them survives into the morning job, the page, and the card.

### Profile facts expire; `city` outranks anything else that names a place
`PROFILE_FIELDS` bounds which keys may exist. It does nothing about keys that
are still there and no longer true, and the whole profile is dumped into every
system prompt as CURRENT fact.

That is where the "Palmer keeps getting things wrong" reports actually came
from, and it is worth being precise about what it was not: the system prompt and
tool schemas are about 13k tokens and a profile 1-3k, which is comfortable for
Sonnet. The model was not overloaded. It was being told, every turn, things that
had stopped being true — one profile read `city: "Culver City"` three lines
above `life_context: "Based in LA"`, both accurate when written, and the model
reconciled them by putting an LA temperature under the Culver City name. That is
the same incident the weather fixes chased through the data path; this is the
half that was still in the prompt.

`userprofile.VOLATILE_FIELDS` names the facts that rot and how long each stays
true. Every write stamps `field_dates`; `fresh_profile_for_prompt` (read side,
via `agent._prompt_safe_profile`) drops anything past its life and renders what
survives as `{"value": ..., "as_of": ..., "days_old": N}` once it is a few days
old. **Storage is untouched** — a fact that went quiet was not wrong, and the
consolidator may reassert it tomorrow. Durable facts (name, city, job,
relationships, communication_style) are deliberately not in the list; dating
them would invite the model to doubt things it should not.

`_build_system` also states outright that **`city` is the location and nothing
else in the profile outranks it**, because `city` is the only field any tool
reads. Without that line the model is free to average two true statements into
a false one.

Two extraction rules follow from what was found in real profiles.
`follow_up` held `"confirm_morning_briefing_delivery_is_consistent_daily"` for
one user and `"Maintain single-message format"` for another — notes about
Palmer's own operation, read back every turn as facts about a person.
`EXTRACT_PROMPT` now says these fields are about the user's life and that
recording Palmer's performance in them is not an option.

### Empty paid sections retry sooner than full ones
The `_tried` stamp is written before the call so a failure cannot be retried in
a loop. That also meant a single empty or failed fetch left a section blank for
its entire window with nothing to show — it locked three of four users out of
Opening for a day, twice, and had to be cleared by hand both times. `_window_for`
shortens the wait to a quarter of the window (floor one hour) **only when the
section holds no data at all**. Once it holds something, a stale row beats a
blank one and the full window applies again.

### The profile is a bounded schema
`userprofile.PROFILE_FIELDS` is the complete set of keys a profile may hold, and `_canonical_updates` drops anything outside it. This is not tidiness — the whole profile is dumped as JSON into **every** system prompt, and the per-turn extractor is a language model that will invent a new key every turn if nothing stops it. One profile reached 624 keys, 604 of them one-offs (`monday_night_behavior`, `kendrick_fan`, `tv_taste_update`, `alternatively`): ~21,700 tokens of noise per message, roughly double SYSTEM_PROMPT and the tool schemas combined, burying the 20 keys that mattered.

Adding a field means adding it to `PROFILE_FIELDS` **and** to the schema list in `prompts.EXTRACT_PROMPT`. A key missing from the allow-list is silently discarded on write, so `test_profile_schema.py` asserts that every field the code reads is allowed.

`upsert_profile(phone, {"key": None})` **deletes** the key. Callers already used None to mean "clear this" (releasing a send guard, retiring an alias) and every reader goes through `.get()`, so absent and null are equivalent to them — but a stored null still costs prompt tokens.

`migrate_profile_prune.py` cleans rows that grew before the allow-list existed. It folds the stray keys into canonical fields with a Sonnet pass before dropping them, so real facts survive. Dry run by default; `--apply` writes.

### DB access patterns
- `get_all_profiles()` returns every `(phone, profile)` in ONE query. The scheduler jobs use it. Do not write `for phone in get_all_phones(): get_profile(phone)` — `_conn()` opens a fresh connection per call, so that is N+1 per tick.
- `upsert_profile()` does its read and write on one connection, and takes a row lock on Postgres. It used to be two connections with an unsynchronised gap, so concurrent writers could drop each other's fields.
- Pass profiles down rather than re-reading them. The reaction path once cost five `get_profile` calls for a single inbound tapback.

### Model routing
Two Claude models, chosen in `agent.py`:
- `SONNET_MODEL = "claude-sonnet-4-6"` — conversation, drafting, all user-facing replies
- `HAIKU_MODEL = "claude-haiku-4-5-20251001"` — extraction, scoring, classification, topic-inference, thread selection

New extraction/scoring logic should go on Haiku; new user-facing drafting on Sonnet. When updating model IDs, grep for both constants — they're re-exported and imported across most modules.

### Tool routing is strict and important
The system prompt in `agent.py` hard-routes user asks to specific tools. Never mix:
- `get_weather` → NWS (US) with Open-Meteo as the fallback and the rest-of-world path. `OWM_API_KEY` is vestigial — no code has read it since the Open-Meteo switch (`4620ba6`), whatever the env table still says
- `get_price` → CoinGecko (crypto) / yfinance (stocks) only
- `get_travel_time` / `get_city_traffic` → TomTom only
- `get_my_page` → the caller's own Palmer Home URL, via `home.ensure_fresh` (never `home_url` — that can hand out a link to a page that was never built)
- `add_price_watch` / `run_price_watches` → SerpAPI Google Shopping only (product prices, distinct from `get_price` for crypto/stocks)
- `web_search` → Tavily news mode only, never for weather or prices

If you add a new tool, follow the same discipline: one data source per tool, and update the `USE THE RIGHT TOOL` block in `SYSTEM_PROMPT` so Claude routes correctly.

### Source quality is one gate, applied at the search call
Every news fact and every news link Palmer sends — watch alerts, the morning briefing, Palmer Home, and the conversation `web_search` — comes out of `datafeeds._search_raw` or `datafeeds._search`. Both apply `sources.py` before returning, so quality is decided in one place rather than by each caller.

It used to be per-caller, and the callers disagreed: `watches.py` ranked by tier, `home._fetch_headlines` sorted but then took `results[0]` regardless, and the conversation search did nothing at all and dropped the URL besides. Filtering at the search call is what makes a change here reach every surface at once.

`sources.py` deliberately imports nothing from Palmer. The helpers were in `watches.py`, which imports `datafeeds` — so putting the filter where the search happens required moving them below it.

Four gates, cheapest first:

1. **Blocklist** (`is_blocked`) — dropped outright, never ranked. Two structural kinds: press-release wires (`prnewswire`, `globenewswire`, `einpresswire`…), where the "article" is a paid placement wearing a news layout, and republishing aggregators (`msn.com`, `biztoc`, `newsbreak`, `news.google.com`…), whose copy is a worse link than the original that is almost always sitting next to it in the same result set. **Keep this structural.** Do not add an outlet because its reporting is weak — that judgment ages badly in a JSON file the way the `PRIVATE_COMPANIES` denylist did in `tickers.py`. Demote by leaving it off the allowlist instead.
2. **Relevance floor** (`meets_score`) — tier 3 must clear `min_score`; trusted sources get `TRUSTED_SCORE_RELIEF` (0.15) of slack. This is not politeness. The floor runs *before* the tier sort, and Tavily's score measures query-text match, which is precisely what an SEO content farm is built to win — so a flat floor cut the Reuters piece at 0.45 and kept the farm at 0.90, and the tier sort never got the chance to undo it. Ranking was already in place and still could not save the good source, because the good source was gone before ranking ran.
3. **Tier ordering** (`source_tier`, applied by `rank`) — 1 = premier newsroom, wire, or official (`.gov`/`.edu` at runtime), 2 = mainstream and reputable specialist, 3 = everything else. Sorts by `(tier, -score)` so a wire report beats a higher-scoring blog. `rank(trusted_only=True)` drops tier 3 entirely; **only Palmer Home passes it**, because the page is a short curated list read top to bottom with the source name showing, so one bad row taints the card — and unlike a conversation reply, nobody asked a question that has to be answered. Conversation and the morning briefing keep tier 3 as a last resort: an obscure-but-real source beats "nothing found".
4. **Corroboration** (`corroborated`) — a watch or daily alert will not fire unless ≥ 2 distinct canonical domains agree, OR ≥ 1 tier-1 source confirms. Single unknown-domain hits are how rumor and spam leak through.

Suffix matching is on a dot boundary in both directions, so `notreuters.com` and `reuters.com.evil.example` are both tier 3. Without that a domain launders itself into tier 1 and nothing looks wrong until it is.

`_search_raw` pulls **10** candidates, not 5. Tavily bills per search, not per result, and the recency window throws most of a page away — a 5-result pull that loses three to the 12-hour cutoff leaves the tier sort nothing to choose between, which is how a lone content farm ends up as the best available source.

`trusted_sources.json` is meant to be hand-edited with no code change, which makes its shape a runtime dependency: bare lowercase hosts, no scheme or path or `www.`, no duplicates, and **no domain in both `domains` and `blocked`** (blocking runs first, so such a domain is silently blocked while reading as trusted). `test_sources.py::TestSourceListIntegrity` enforces all of it, and requires every blocked entry to carry a `why` — that field is the guardrail keeping the list structural.

Watches then add their own gates on top of the shared ones: a strict criticality rubric, 12-hour recency, `_url_reachable` (HEAD, 405 counts as alive) so a dead top link falls through to the next result, per-watch cooldown (default 4h), a `DAILY_ALERT_MAX` cap, and a dedup check against recent alert summaries. When editing this pipeline, keep all gates — removing any one produced noisy or bad alerts historically.

**The morning briefing and `web_search` label each story with its domain** (`[reuters.com] headline`). The drafting model was previously handed a flat list with no provenance, so it could not tell a wire report from a content farm and had no way to attribute anything it repeated.

### Weather: one source per user, and it is NWS wherever NWS reaches
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

### When the forecasters disagree, Palmer says so instead of picking one
A Woodland Hills user was told 103, 106, 107 and 111 on four consecutive days
against actual highs of 98.3, 96.8, 97.8 and 99.5 — corroborated by Van Nuys and
Burbank reading 102 on the worst day. There was no bug: NWS's period forecast,
hourly forecast and raw gridpoint all said 110, and `_nws_snapshot` read them
correctly. That grid cell simply runs hot.

**Do not "fix" this by blending sources.** It was the first thing tried and it
is worse. In the same week NWS was the single best number available for coastal
Culver City (+1.7F against actuals, where every raw model ran 5-11F hot), so a
median of NWS and Open-Meteo makes Danny's number ~5F worse to make Drew's
better. NWS knows the marine layer the models overshoot; the models handle the
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

### Landmarks vs. addresses in the traffic pipeline
TomTom's geocoder is a mapping API, not a search engine, and mis-ranks landmark names (e.g. "White House", "Fenway", "LAX"). `traffic.py` and the `get_travel_time` tool run landmark destinations through Sonnet to resolve them to street addresses *before* geocoding. Preserve this indirection when touching routing code.

### SMS send pipeline
All outbound SMS goes through `sms_util.send_sms` / `ensure_sms`. It cleans text (`_sms_clean` strips markdown and non-SMS glyphs), splits on paragraph breaks over 1500 chars, and falls back through progressively shorter candidates (original → `shorten_message` → hard truncate → `FALLBACK_SMS`) so a user is never left with silence. Never call Twilio's `messages.create` directly from feature code; go through this module.

### Twilio safety
Every `/sms` and `/sms-status` request is validated with Twilio's HMAC-SHA1 `RequestValidator`. Requests failing validation return 403. All DB queries are parameterized and scoped by phone number.

### `/sms-status` retry
Twilio delivery failures with error codes `30019` or `21617` (content-size issues) trigger an automatic shorten-and-retry via the `/sms-status` webhook. Other delivery failures are logged and dropped — do not add blanket retry-on-any-failure without thinking about loops.

## Voice / prompt rules (see `SYSTEM_PROMPT` in `agent.py`)

Palmer has a specific voice: dry, observational, plain-text SMS (no markdown, no bullets except the one numbered onboarding list). If you touch the system prompt or write new drafting prompts (Haiku personalizations, morning drafts, followups), keep to the same rules — no "Great question", no summarizing user words back, no ending every message with a question, and **never redirect the user to competing apps** (Google Maps, Waze, ChatGPT, etc.).
