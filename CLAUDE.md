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

**Patching in tests follows the code, not the name.** `patch("agent.client")` stopped working when functions moved out; patch the module the function actually lives in (`patch("userprofile.client")`). A dead patch target does not fail loudly — it lets the test make real API calls. Watch the suite runtime: 1058 tests in ~10s, and a jump means something is hitting the network.

### Scheduler cadence (main.py)
```
send_due_reminders       every 1 min
send_morning_messages    every 5 min   (each user has a local target time; per-day guard prevents double-sends)
run_watches              every 30 min
run_alert_checks         every 60 min
send_missing_data_asks   every 60 min  (asks users with no city so mornings can target them; DATA_ASK_DRY_RUN=1 to preview)
run_followups            every 2 hr   (cron, NOT interval — see main.py; the per-user daily claim, not the tick, is what bounds cost)
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

### A reply never dies because the turn was long
`agent.TOOL_ITERATION_CAP` bounds the tool loop, and hitting it used to **raise**.
`main.py` catches that, leaves `reply` falsy, and answers a falsy reply with
`FALLBACK_SMS` — so a turn that merely needed one call too many ("add Apple,
Nvidia and Tesla, then what's my commute" is five before Palmer speaks) died
outright, threw away every tool result already gathered, and told the user
something went sideways. The cap is 8 now, and on exhaustion `get_reply` asks
once more **with `tools` omitted**, so the model must answer from what it has.

`stop_reason == "max_tokens"` no longer ships the partial draft either — it lands
mid-word. `_trim_to_sentence` cuts back to the last complete sentence, but only
when that leaves most of the message standing: trimming "Ok. <thirty truncated
words>" back to "Ok." throws away everything the reply was for, so there the
fragment wins.

### A check-in is about something the profile actually says
`followup.py` fetches no data at all — it is pure model output conditioned on a
profile string — so every guard has to be structural.

**`_pick_thread` returns a string copied from the profile, never the model's own
words.** It used to return whatever Haiku emitted and hand it straight to the
drafter, so a confabulated thread was written up as though it were real: a
specific-sounding question about something that never happened. It now
echo-matches against `ongoing_threads` and fails closed, exactly as
`userprofile.topic_already_covered` already did, and for the stated reason — an
echo can be checked against the list, a paraphrase cannot.

**`life_context` alone no longer triggers a check-in.** It is a paragraph about
someone's life, not a thread with a follow-up, and handing that to a model asked
to find something "worth a check-in today" is how one gets invented.

**The draft prompt no longer asks for invented specificity.** "A statement that
just shows you remembered" is an instruction to make something up; it now says to
use only what the thread text and recent messages actually say, and to ask one
short question when that is not enough.

**Every bail path restores `followup_sent_date`, it does not null it.**
`claim_daily_guard` overwrites the field with today, so nulling it on a bail
erased the record of the last real send — and `_should_send_followup` measures
the 3-to-14-day pacing gap against exactly that field. The gap is the thing
standing between a check-in and a drumbeat. `followup_last_thread` then keeps the
next pick from landing on the same thread twice running.

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

  **Screens need a quality floor, not a taste gate.** Ranking TMDB by
  `vote_average` with no floor is meaningless and it showed: *"Toxic: A Fairy
  Tale for Grown-ups"* scored 6.23 from **thirteen votes** and went to every
  user on the system as a recommendation. `MIN_VOTES` (150) removes the long
  tail and **popularity** does the ranking — a film released three days ago has
  no votes yet however good it is, but popularity already reflects that people
  are looking it up.

  **Both TMDB endpoints were the wrong ones.** `/tv/on_the_air` means
  *currently airing*, not new: it returns Ted Lasso, Reacher and Silo, running
  for years, and the only thing making them look new was a filter on
  `first_air_date` — which instead surfaced obscure foreign premieres and a
  Brazilian nightly news programme that first aired in **1969**.
  `/discover/tv` with a real premiere window asks the question we meant. The
  movie window went from ±7 days to `SCREEN_WINDOW_DAYS` (30), because a film
  is new in theatres for weeks, and the narrow window was itself what forced
  the ranking down into the 13-vote tail.

  Note the floors are enforced in different places: `MIN_TV_VOTES` rides on
  TMDB's `vote_count.gte` query param, while `MIN_VOTES` is applied here
  because `now_playing` offers no server-side filter. A mocked
  `_http_get_json` therefore has to answer the two calls separately.

  **Screens skip the curation gate entirely.** TMDB is already structured and already
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

**Cache by cost, not by convenience — this is what keeps the section alive.**
All three inputs were keyed on the ISO week, which froze every row Monday to
Sunday: the same two films every day for every user on the system, and nothing
but the current weekend. But only `_local_candidates` spends anything (two
Tavily searches). Ticketmaster allows 5,000 calls a day and TMDB is free, so the
weekly key was protecting a cost that existed for one of the three.

    _candidate_cache   paid   (bucket, ISO week)    the Tavily rows
    _local_cache       free   (bucket, local day)   curation over those + events
    _screen_cache      free   (local day)           TMDB, national

Curation runs daily over *cached* candidates plus *fresh* events — one Haiku
call per metro per day, with Tavily still twice per metro per week. The day is
`timeutil.local_today(profile["timezone"])`, never `date.today()`: the dyno is
UTC and that has bitten twice already (the card masthead, and the expiry fix).

**Rotate at read time, never trim at fetch time.** `_screens` caches all six
candidates and `_rotate` serves two, offset by `today.toordinal()` — the same
deterministic trick as `morning._rotated_topics`, so a retry within a day is
stable and nothing is stored. Trimming to `MAX_SCREENS` at fetch time is exactly
what served the top two by score forever and buried the other four.

There is deliberately **no "already shown" memory**. A Saturday concert *should*
appear on Thursday, Friday and Saturday; that is relevance, not repetition.
Daily re-curation plus rotation answers the actual complaint without state.

**One local slot is reserved for something further out** (`_is_far`,
`FAR_HORIZON_DAYS`). Everything in the next seven days outranks everything
beyond it, so in a busy metro the long-lead Ticketmaster pull never won a slot
and the section read as this weekend, forever — while Kacey Musgraves in twelve
days and Journey in November sat in the candidate list unused. The slot is
reserved rather than competed for, and collapses with no gap when nothing
qualifies.

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

### Live scores: the first thing in Palmer built to interrupt
`sports.py` reads scores, `scorewatch.py` decides which moments deserve a text.
That second half is the feature. A scoring feed is a pager by construction — an
NFL game has six to ten scoring plays, and two followed teams on a Sunday is
twenty texts in an afternoon — and every other proactive path in this codebase
exists partly to ration sends. So three moments earn a text and nothing else
does:

  * the lead changes hands,
  * someone scores inside the last five minutes,
  * the game ends.

Everything else updates the stored state **silently**, which is load-bearing:
the comparison is against what the user was last TOLD, not the last poll, so a
score arriving in the same tick as a lead change is one event rather than two,
and a suppressed score does not make the next one look bigger than it was.
`MAX_ALERTS_PER_GAME` is the backstop. Simulated over a full game, five scoring
events produced three texts.

**The obvious ESPN endpoint does not work from Heroku.**
`site.api.espn.com/.../scoreboard` — the one every guide recommends — returns
**403 from the dyno**, verified in production, so it is ESPN blocking datacenter
IPs rather than a local quirk. `site.web.api.espn.com` is the same shape,
unblocked, and returns a whole league in one call. The core API
(`sports.core.api`) also works but is reference-based: **seven** HTTP calls for
one game's score. Free and undocumented is a deliberate starting position; the
ESPN shape is confined to `sports.py` so a paid feed is a one-module swap.

**"The closing stretch" is not one rule.** `_is_late` originally compared a
countdown against five minutes, which is meaningless in two of the six leagues:
baseball has innings and `clock` is always 0, and soccer's clock counts UP. Late
alerts were therefore silently dead for MLB and MLS — including the sport a real
user follows. It now asks two questions: are we in `FINAL_PERIOD` for this
league (`>=`, so extra time counts), and *if the sport has a countdown*, is it
nearly done.

**The drafter is told whose side they are on and by how much.** Leaving it to
infer "PHI" from `CIN 17, PHI 21` mostly worked and is the wrong thing to lean
on — a buddy does not deduce who you support, and the margin is what sets the
tone. It is also told, in as many words, that it can see the score and the clock
and **nothing else**: given only a final score it was writing "that one had to be
close the whole way", which it cannot know. Same failure as the weather
over-claiming, wearing personality.

**Polling is two-speed.** Checking every couple of minutes around the clock
would be thousands of calls a day to learn nothing is happening; checking slowly
during a game misses the moments. A league with something live is polled at
`LIVE_POLL_SECONDS`, an idle one at `IDLE_POLL_SECONDS`, and the board is cached
per league so two users following the same one cost a single fetch.

**Team names are ambiguous in a way show titles are not.** `find_teams` returns
a LIST — "Cardinals" is two teams in two sports, "Rangers" likewise — and the
dispatch asks rather than picking, because guessing signs someone up for alerts
about the wrong team in the wrong season. Verified live: "text me cardinals
scores" gets *"Which Cardinals — baseball (St. Louis) or football (Arizona)?"*

`teams` on the profile is the resolved follow list. It is **not** `sports_teams`,
which is the extractor's free-text description ("Cardinals fan, emotionally
invested...") — good for Palmer's voice, useless for lookups.

### Followed shows are not discovery
`shows.py` tracks series a user named, and the distinction from the `screen`
rows beside them is the whole feature. Screens answer *what is new to anyone* —
popularity-ranked, identical for every user. A followed show answers *what is
new for the shows you watch*, and exists only because someone asked for it by
name. `follow_show` / `unfollow_show` are its controls, not `opening_remove`;
episode rows deliberately bypass `wanted_kinds`, because that setting chooses
which kinds of **discovery** you want.

TMDB gives episode-level data directly: `/tv/{id}` carries
`next_episode_to_air` and `last_episode_to_air` with air dates, season and
episode numbers and titles. One free call per show, cached by `(show id, local
day)` and therefore **shared** — two users watching Reacher cost one lookup,
the same shape as the metro cache.

Three rules came from the spec and each has a test that catches its reversal:

- **A row exists only in the week its episode lands.** Upcoming within
  `UPCOMING_DAYS`, or dropped within `JUST_DROPPED_DAYS`. A show between seasons
  produces nothing — it is not a permanent countdown.
- **The page by default, the morning text only on request.**
  `morning_prefs["episode_alerts"]` gates it, `home._refresh_identity` carries
  the flag onto the payload, and `_payload_digest` honours it without a profile
  read of its own. A weekly "new episode!!" nobody asked for is precisely the
  drumbeat this product keeps having to remove.
- **Episodes displace screens rather than lengthening the page**
  (`MAX_EPISODES` takes its slots from `MAX_SCREENS`). A show you actually watch
  is worth more than a film chosen for you, and the row count stays put.

Resolution runs on the **write** path (`resolve_show`, one TMDB search when the
user follows), never on read — same terms as `_normalize_price_topic` and
`_city_from_weather_topic`. An unresolvable title asks the user to confirm; it
never guesses one and never sends them elsewhere to look it up.

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

### The page is arranged by the user, in a text, never in a form
`arrange_page` is presentation only — sort, order, visibility — and never
touches what is tracked; content stays `update_morning_briefing`'s job, and
Opening kinds stay `opening_add`/`opening_remove`. The prefs nest under
`morning_prefs` (`markets_sort`, `section_order`, `hidden_sections`) — same
trick as `opening_kinds`, so no `PROFILE_FIELDS` entry and nothing for the
extractor to write prose into.

The two halves take effect through different channels, deliberately:

- **`markets_sort` is baked into the prices list at fetch** (`_fetch_prices`),
  because the page, the card's `[:MAX_PRICES]` slice, and the og:description
  all render from that one list and must not disagree about which ticker
  leads. That is why the dispatch calls `home.invalidate(phone, ("prices",))`
  on a sort change and only then — the 5-minute stamp would otherwise serve
  the old order right after the user asked.
- **Order and visibility ride the payload** as `page_prefs`, set in `rebuild`
  and `_refresh_identity` (the `episode_alerts` pattern), so a change lands on
  the next view with no invalidate. `_page_prefs` returns **None, not `{}`,
  when nothing is set** — the same value a payload written before the field
  existed reads back, so an untouched profile settles instead of rewriting the
  row on every view (`test_home.py::TestIdentityFreshness` is the guard).

`SECTION_WORDS` and `DEFAULT_SECTION_ORDER` live in `page.py` — the module
that knows what a section is — and the dispatch imports them, so the arranger
and the renderer cannot disagree. Kind words ("movies", "concerts") are
deliberately absent from the map: they belong to `opening_remove`, and mapping
them would let "hide movies" silently hide the whole Opening section instead
of trimming a kind. An unknown word is echoed back for Palmer to ask about,
never guessed. Sections the user named come first in their order; everything
unnamed keeps its default position after them, so "put markets first" is a
one-item list and nothing vanishes. The TMDB notice follows the RENDERED page,
not the payload — a screen row in a hidden section shows no TMDB data and
gets no notice.

The "edit button" is the name-ask pattern: an `.ask` tap target that opens
Messages pre-filled with "Arrange my page: " (`quote()`, never `quote_plus()` —
sms: URIs have no form encoding). The page has no auth and nothing to POST to,
and that stays true.

### The preview image must change URL, or nobody ever refetches it
`og:image` points at `/h/{token}.png?v={fingerprint}`. The query stamp is the
whole point: link-preview scrapers — iMessage most stubbornly — cache og:images
by URL and have no reason to refetch one they have already seen. With a fixed
`/h/{token}.png`, every morning's message showed whatever card was scraped the
first time. The server was rendering today's card faithfully; nobody was asking
for it, and there was no ETag or Last-Modified to hint otherwise. The PNG route
now sends an ETag too, for caches that do revalidate.

### Windows must be shorter than the refresh opportunity, or they alias
Most users never open their page, so the only guaranteed refresh is the daily
morning send. A section whose window is 24h therefore lapses on **about half**
of them: three users were carrying Opening rows 41 hours old with no refetch
even attempted, because at the previous send the section was 20.4h old — just
under its own window — and the next chance came a day later. `STALE["opening"]`
is 20h for that reason, leaving margin for a send that drifts.
`test_home.py::TestNoSectionAliasesAgainstTheDailySend` holds every window under
a day. The refetch is nearly free anyway: `opening.py` caches by metro and week,
so a refresh inside the same week is a dict lookup.

### A new user is set up, not interviewed
"Set that up" calls `update_morning_briefing(enabled=true)` in that same turn,
and an empty topic list is seeded from `morning.default_topics(city)` — local
news plus national. It used to ask *"what topics do you want?"*, which left the
user with a briefing that was weather and nothing else and made them do setup
work before seeing whether Palmer was any good: three turns in, `morning_topics`
was `[]`, the News card was empty and Markets did not render at all.

Keep defaults **subject-shaped**. The search is Tavily in news mode behind a 24h
window, a trusted-source gate and a relevance floor, and it answers subjects
("AI news", "Philadelphia Eagles") far better than meta-queries: `"Top national
news"` returned *"Clemson Army ROTC earns top national honors"* — a literal
word match — and `"Austin, TX local news"` returned nothing at all. Expect
roughly 60% of attempted topics to return something on a given day; that is the
recency gate doing its job, not a bug.

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

### A daily guard means the READER's day
`alerts.py` keyed its once-a-day guard on the UTC date while `_in_alert_window`
gates on the **local** hour 13-21. For Pacific that window is 20:00Z-04:00Z, so
the UTC day rolled over at 17:00 local — *inside* the window — and a user could
take two "daily" alerts in one local day and none the next. `morning.py` and
`followup.py` already keyed on `timeutil.local_today`; alerts now does too.

`_daily_alert_hour`'s UTC date is deliberately left alone: it is only reached when
the profile has no timezone, so there is no local day to key on, and it only needs
to stay stable within a UTC day.

**`morning._recent_assistant_texts` selects prior MORNINGS**, via
`db.get_recent_messages_of_kind` and the `kind` column. It took the last four
assistant messages of any kind, which for anyone who actually texts Palmer is
four chat replies — so `guards.repeats_opening`, written for three consecutive
mornings that opened identically, was comparing today's line against ordinary
conversation and almost never against yesterday's morning. Falls back to any
assistant message for users whose history predates the column.

### Empty paid sections retry sooner than full ones
The `_tried` stamp is written before the call so a failure cannot be retried in
a loop. That also meant a single empty or failed fetch left a section blank for
its entire window with nothing to show — it locked three of four users out of
Opening for a day, twice, and had to be cleared by hand both times. `_window_for`
shortens the wait to a quarter of the window (floor one hour) **only when the
section holds no data at all**. Once it holds something, a stale row beats a
blank one and the full window applies again.

### Every date is computed on the calendar that owns it
Three different calendars are in play and each answer belongs to exactly one.

**The exchange's.** `datafeeds._MARKET_TZ` is `America/New_York`. The stock day
label used `date.today()`, so from 19:00 ET the UTC date had rolled and that
afternoon's close was reported as *"yesterday"*. Deliberately not the reader's
zone either — a session closes when New York says it does, whoever is asking.

**The reader's.** `weather._nws_report` anchors its target on `local_today(tz)`.
The delta was computed in the user's zone and then added to the FORECAST
LOCATION's day, so asking from Los Angeles at 10pm about New York landed a day
off. With no zone on file there is no reader's day, so it falls back to the
grid's own first period as before. `opening._curate` likewise takes the reader's
date for the `{today}` its prompt uses to drop past events — a UTC date there
tells the curator to drop tonight's show.

**Nobody's, when the input cannot be read.** `_resolve_day_delta` returning None
used to mean *tomorrow*, so an unreadable phrase was answered confidently for a
day the user never named. It means today now, and it logs. Both report paths
print the resolved date, so a wrong guess is visible rather than silent.

`"next friday"` is the Friday after this coming one. The two rules have to
compose rather than stack: a bare weekday naming TODAY resolves a week out
(`test_timeutil.TestResolveDayDeltaHonorsTz` fixes that decision), so a flat +7
on top would put "next friday" a fortnight away.

### `timezone` is validated on write, and re-derived when someone moves
It is the field every `local_now`/`local_today` call depends on, and an
unresolvable value degrades all of them to UTC silently and permanently. Two
paths could put one there and `_apply_profile_updates` now handles both: the
Haiku extractor (it is named in `EXTRACT_PROMPT`'s schema, so it can write
`"Pacific Time"`) is validated through `timeutil.valid_zone`, and a city change
re-derives the zone instead of only filling it when absent — someone who moved
from Chicago to Los Angeles kept `America/Chicago` forever, with no tool and no
repair job, and their morning arrived two hours early from then on.

This does not violate the rule that correcting a forecast must not move the hour
the morning arrives: the weather-topic city write in `update_morning_briefing`'s
dispatch calls `upsert_profile` **directly** and never reaches this function.

### Consolidation runs on a batch, not on every turn
`_consolidate_history` fired on every turn once a user passed 40 messages,
re-summarising a near-identical 80-message window each time — one Haiku call per
turn, forever, for a profile that had barely moved. `CONSOLIDATE_EVERY` (20) and
the `consolidated_at_count` watermark gate it on how far the conversation has
actually travelled.

### The profile is a bounded schema
`userprofile.PROFILE_FIELDS` is the complete set of keys a profile may hold, and `_canonical_updates` drops anything outside it. This is not tidiness — the whole profile is dumped as JSON into **every** system prompt, and the per-turn extractor is a language model that will invent a new key every turn if nothing stops it. One profile reached 624 keys, 604 of them one-offs (`monday_night_behavior`, `kendrick_fan`, `tv_taste_update`, `alternatively`): ~21,700 tokens of noise per message, roughly double SYSTEM_PROMPT and the tool schemas combined, burying the 20 keys that mattered.

Adding a field means adding it to `PROFILE_FIELDS` **and** to the schema list in `prompts.EXTRACT_PROMPT`. A key missing from the allow-list is silently discarded on write, so `test_profile_schema.py` asserts that every field the code reads is allowed.

`upsert_profile(phone, {"key": None})` **deletes** the key. Callers already used None to mean "clear this" (releasing a send guard, retiring an alias) and every reader goes through `.get()`, so absent and null are equivalent to them — but a stored null still costs prompt tokens.

**A new field must not collide with `_PROFILE_ALIASES`.** The alias table maps
the names the *extractor* invents onto canonical ones, and `_normalize_profile`
applies it on every inbound message. `teams` shipped as a real field while
`teams -> sports_teams` was still in that table, so `follow_team` stored a
follow list, Palmer confirmed it, and the user's next message migrated it into
`sports_teams` and wrote `teams: None` — the follow gone before any alert could
fire. It then put dicts in a field holding prose, and `_all_interests` does
`.lower()` on those items from a call site *outside* the `try` in `alerts.py`,
so one follower would have aborted `run_alert_checks` for themselves and every
user after them in the loop, with the daily guard already claimed. The field is
`followed_teams`, and `test_profile_schema.py` now asserts no alias key is ever
a real field.

A field written by tool dispatch also stays **out of `EXTRACT_PROMPT`**
(`followed_teams`, `shows`). Listed there, Haiku fills it with prose and the
code reading it gets strings where it expects dicts.

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
3. **Tier ordering** (`source_tier`, applied by `rank`) — 1 = premier newsroom, wire, or official (`.gov`/`.edu` at runtime), 2 = mainstream and reputable specialist, 3 = everything else. Sorts by `(tier, -score)` so a wire report beats a higher-scoring blog. `rank(trusted_only=True)` drops tier 3 entirely. Palmer Home asks for it **first**, then falls back — see below. Conversation and the morning briefing go straight to tier 3 as a last resort: an obscure-but-real source beats "nothing found".

**The page falls back too, and the allowlist is why it has to.** Trusted-only was not filtering junk off the page; it was dropping the best source that exists. `"Philadelphia Eagles news"` lost `philadelphiaeagles.com` at score 0.75 and `nbcsportsphiladelphia.com` at 0.61; `"St. Louis area news"` lost `fox2now`, `ksdk` and `stlamerican` — every real newsroom in the market — and returned an empty card instead. The allowlist is ~100 domains and there are thousands of local outlets and team sites; it will never cover them, and adding them one at a time is the maintenance trap the blocklist notes warn about.

So `home._fetch_headlines` tries trusted, and on nothing falls back to `trusted_only=False` at `UNTRUSTED_MIN_SCORE` (0.60) rather than the usual 0.5. **The higher bar is the point**: an unvetted source has to earn its place on match strength because it is not earning it on provenance. Measured across every real user topic: 65% returned something under trusted-only, 82% with the fallback, and the rows it recovers are `fox2now.com`, `philadelphiaeagles.com` and `fintechfutures.com` — not content farms. The one mill it did let through at 0.5 (`vocal.media`, 0.52) is cut by the 0.60 bar and blocklisted structurally as a user-generated platform.

**Where the losses actually are, when a topic returns nothing.** Every topic gets 10 raw results and essentially none are lost to the recency window — the filtering is all score and tier. Three distinct failure modes, and only the first is fixable in code: an authoritative local/specialist source that is not on the allowlist (fixed by the fallback); a vague query whose matches are all weak (`"US politics"` tops out at 0.36 — the floor is right, the topic is too broad); and an ambiguous topic (`"Kirkwood, MO news"` returns an IndyCar driver named Kirkwood). The last two are topic-quality problems, not gate problems.
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

### A second weather location is additive, never a second `city`
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

### Landmarks vs. addresses in the traffic pipeline
TomTom's geocoder is a mapping API, not a search engine, and mis-ranks landmark names (e.g. "White House", "Fenway", "LAX"). `traffic.py` and the `get_travel_time` tool run landmark destinations through Sonnet to resolve them to street addresses *before* geocoding. Preserve this indirection when touching routing code.

### The model is told the reader's clock, not the dyno's
`timeutil.clock_block` builds the RIGHT NOW block in every system prompt, and
`SYSTEM_PROMPT` has one `{clock_block}` placeholder where it used to have
`{date}` and `{now_utc}`. Both were UTC, which is a lie for most of the day: from
17:00 Pacific onward the UTC date is already tomorrow, so "Today is Monday" was
simply false for a Los Angeles user at 5:42pm Sunday, and "remind me tomorrow at
9" filed for Tuesday. The model was not confused — it was told the wrong day and
reasoned correctly from it.

With no resolvable zone the block says so and asserts **no local date at all**.
Presenting UTC as though it were their day is the whole defect, so the honest
form is the safe one. `timeutil.valid_zone` is the gate — `profile["timezone"]`
is named in `EXTRACT_PROMPT`, so Haiku can write anything there, and an
unresolvable value silently degrades every `local_now`/`local_today` call.

**`due_at` is vetted on the write path, and that is not optional.**
`claim_due_reminders` decides due-ness with a **lexicographic** `due_at <= now`
on a TEXT column, so the comparison equals a chronological one only while every
writer stores `YYYY-MM-DDTHH:MM:SS+00:00`. Nothing enforced that: a model
reasoning in local time emits `-05:00`, the string compare read that hour as
UTC, and the reminder fired five hours early. `agent._normalize_due_at` corrects
the offset, refuses a past or unreadable time with something the model can act
on **inside the same turn**, and returns the LOCAL time so the dispatch echoes it
instead of making the model convert a second time for the half the user reads.
A naive string is read as the user's local clock, not UTC — a model that drops
the offset was thinking in their day.

`db.normalize_due_at_rows` repairs rows written before this, from `init_db`,
idempotently. Widening the SQL claim window and re-filtering in Python is not an
alternative: on Postgres the claim is one `UPDATE ... RETURNING`, so a wider
predicate marks not-yet-due reminders as sent.

### A URL survives this codebase byte for byte, or is not sent
Three independent mechanical defects produced every "bad link", none of them in
the code that chooses a link:

- the markdown scrub was `[text](anything) -> text`, **deleting** the target, so
  a reply reading `[your page](https://...)` arrived as "your page";
- `encode('ascii', 'ignore')` **drops** bytes rather than failing, so a
  non-ASCII path became a shorter URL that still looks like one — a dead link
  that looks alive is worse than a visibly broken one;
- three paths truncated at a fixed offset (`shorten_message`'s slice,
  `send_sms`'s `body[:320]`, `_split_for_sms`'s hard chunker), each landing
  mid-URL on exactly the messages most likely to carry one.

`smstext.URL_RE` is the one definition. `_protect_urls`/`_restore_urls` hold
links out of every transform and put them back percent-encoded;
`truncate_preserving_urls` never cuts inside one and returns a link alone when it
cannot fit beside prose.

**`shorten_message` never shows the model a URL.** Asking it to preserve a
placeholder was tried and is the worse bet — a dropped marker loses the link
silently. Prose is shortened alone and links are re-appended last, the shape that
lets an app draw a preview.

**`send_sms` decides the status-callback question, not the caller.**
`/sms-status` answers a content-size failure by rerunning the original body
through `shorten_message`, so a message carrying a URL must opt out. `morning.py`
did this by hand and said why; chat replies, watch alerts and price alerts all
carried URLs and did not. Deciding it centrally means a new sender inherits the
rule instead of remembering it.

### History records what was sent, and unprompted means silent-on-failure
`send_sms` returns False on a Twilio failure **and** on a `leaks_deliberation`
block. `alerts.py` and `watches.py` ignored it and saved unconditionally, so
history held messages the user never received — and `_build_system` feeds history
back to the model, which then refers to them. `shopping.py` and `flightwatch.py`
never saved at all, making their alerts invisible both to the model and to their
own `_is_duplicate_subject` check, which reads assistant messages.

The asymmetry on a failed **watch** send is deliberate: the claim stays spent,
because it is a rate limit rather than a delivery record, and retrying every tick
against a body the guard blocks identically is worse than burning one cooldown.
That is the inverse of the reminder rule, for the same reason the reminder rule
gives.

**`ensure_sms` is for replies only.** Its contract — never leave them with
silence — is right for a message the user is waiting on and wrong for anything
unprompted: a failed price check used to text "something went sideways on my
end, try again" to someone who had asked for nothing. Proactive senders use
`send_sms` and accept False. `test_phantom_history.py` asserts none of them
reaches for `ensure_sms` again.

`messages.kind` records which job sent an assistant message (`morning`,
`followup`, `alert`, `watch`, `price`, `flight`, `reminder`, `reply`, `city_ask`).
NULL means written before the column existed; readers must tolerate it.

### SMS send pipeline
All outbound SMS goes through `sms_util.send_sms` / `ensure_sms`. It cleans text (`_sms_clean` strips markdown and non-SMS glyphs), splits on paragraph breaks over 1500 chars, and falls back through progressively shorter candidates (original → `shorten_message` → hard truncate → `FALLBACK_SMS`) so a user is never left with silence. Never call Twilio's `messages.create` directly from feature code; go through this module.

### Twilio safety
Every `/sms` and `/sms-status` request is validated with Twilio's HMAC-SHA1 `RequestValidator`. Requests failing validation return 403. All DB queries are parameterized and scoped by phone number.

### `/sms-status` retry
Twilio delivery failures with error codes `30019` or `21617` (content-size issues) trigger an automatic shorten-and-retry via the `/sms-status` webhook. Other delivery failures are logged and dropped — do not add blanket retry-on-any-failure without thinking about loops.

### Rules the prompt states and the model breaks are enforced in code
`SYSTEM_PROMPT` has forbidden sending users to competing products since the
beginning — *"Palmer is the product — don't send people elsewhere... Do NOT
suggest 'just Google it' — ever."* Palmer did it anyway, five times across two
users, once while quoting the rule back: *"I'd point you to Google Flights but I
know that's not helpful coming from me."*

**A prompt rule was not enough, and that is the general lesson.** `guards.py`
plus `agent._finalize` check the drafted reply and redraft exactly once — the
same remedy as `morning._NAMES_THE_LINK`, for the same reason. When both drafts
hand off, the better-formed one ships and the event is logged loudly rather than
replaced with a canned line that would cost Palmer's voice every time.

**The guard matches the shape of a handoff, never the brand name.** Palmer
legitimately says "Google Cloud", "ChatGPT has hundreds of millions of users",
and sends URLs carrying `utm_source=google`. Precision beats recall here: a
pattern for *"Brand's app has..."* was written and **removed** because it caught
"Anthropic's site lists the new model IDs" and "the team's site has the full
injury report" — pointing at a primary source, which is the opposite of a
handoff. `test_guards_and_flights.py` holds a corpus of the four real production
violations and eight legitimate sentences, and both directions must pass.

**The trigger was usually a bare failure string.** `flights.py` returned
"Flight search is unavailable right now", which the model reasonably paraphrased
into "I can't do flights" and then into a competitor. Failure strings handed to a
drafting model now follow the `weather.py` pattern: say what failed, say what to
do next, and never imply the capability is missing. `price_alert` also used to
drop the **entire** system prompt when `_build_system` raised, taking every NEVER
rule with it; it falls back to `agent.base_system()` instead — note
`SYSTEM_PROMPT` is a template and passing it raw ships literal `{profile_block}`.

### Flight watches
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

### Topic overlap is raised, not enforced
Adding a topic runs `userprofile.topic_already_covered` — a Haiku check beside
the existing substring one, because containment cannot see that "Kirkwood, MO
news" and "St. Louis area news" are the same beat. It **adds the topic anyway**
and tells Palmer to mention the overlap and ask. Semantic overlap has false
positives — "NFL headlines" reads as a duplicate of "Philadelphia Eagles news"
and is not, and one user legitimately tracks both — so silently dropping what
someone asked for is the worse failure.

Ask the model to **echo the duplicated subject**, not its index. Asking for a
number was tried: Haiku answers "2" while naming the third item in the prose
after it. An echo can be matched back against the list; an index cannot.

`home._fetch_headlines` also dedupes by URL across topics, so two overlapping
topics cannot render the same article twice on the page.

### Repetition is two problems with opposite remedies
Measured across every message sent: 39 near-duplicate pairs for one user, 11 for
another. They are not one bug.

**Suppression** — an *unprompted* message repeating one already sent. One user
got the identical followup twice, verbatim; another got "Here you go - <link>"
three times word for word. `_is_duplicate_subject` should have caught the first
and did not: its window is 6h, the followup job runs every 4h, and the subject
stayed live for days. It now runs a free lexical pass first
(`guards.near_duplicate`, stopword-stripped Jaccard, `VERBATIM_WINDOW_HOURS` 72)
before spending a Haiku call. Cheap enough to look back three days, which is the
point — the semantic check never could.

**Variation** — a *scheduled* message the user asked for, said the same way
every time. Three consecutive mornings: "Morning Drew - 103 today in Woodland
Hills", "106 in Woodland Hills today, Drew", "111 today in Woodland Hills, Drew".
Suppressing these would be wrong — they asked for a daily briefing — so only the
phrasing may move.

**Token overlap cannot see variation, and this is the trap.** Those three score
**0.23** against each other, because the numbers and trailing clauses differ
every day; nothing lexical separates them from a genuinely fresh morning. What
repeats is the *shape of the opening*, so `guards.opening_shape` flattens
numbers to `#` and compares the first **three** meaningful words. Three, not
five: by the fourth the trailing clause has diverged and every day looks unique
again. `generate_morning_line` redrafts once on a match, and the correction
insists every number stay identical — it is the phrasing that moves, never the
facts.

`URL`s are stripped before either comparison. Without that, every message ending
in the user's page link reads as near-identical to every other one.

**Reminders stay exempt from all of it**, for the reason already documented: a
reminder is explicitly requested for a named time, and a missed one is worse
than the duplicate it would prevent.

### Palmer's own deliberation never ships
A user received *"Both of these fall into the crime/dark content category they
explicitly asked to avoid. Skipping."* — the drafter narrating its filtering
decision, in the third person, to the person it was about.

`morning.py` had a guard for this. It lived there, so alerts, followups, watches
and reminders never ran it, and it matched fixed phrases the model simply wrote
around — a rule looking for "they asked" misses "they EXPLICITLY asked".
`guards.leaks_deliberation` replaces it with two signals, either damning alone:
third-person reference to the reader near an intent verb, or an announcement of
a send decision. It is checked in `sms_util.send_sms`, the one function every
outbound message passes through.

**Blocking the send is the right outcome for an unprompted message.** Every one
of these was a drafter saying it had decided *not* to send something; doing that
silently is what it was trying to do. `send_sms` returns False and logs, and no
fallback goes out in its place.

**On a reply it is the wrong trade, so `agent._finalize` redrafts first.** The
user is waiting on an answer, and a block there means `main.py`'s falsy-send path
hands them `FALLBACK_SMS` instead — the guard turns a good reply into "something
went sideways on my end, try again". Same shape as `redirects_elsewhere`: check,
redraft once, ship the better-formed of the two. The `send_sms` block stays
behind it as the backstop, and proactive senders never reach `_finalize` at all.

**Neither signal is safe alone, and that was the actual defect.** The first
version fired on EITHER third-person reference or a send decision, and both have
common legitimate forms: *"they said the deal closes Friday"* is news Palmer
exists to send, and *"got it, not sending those anymore"* is Palmer agreeing to
stop. So the guard is two tiers now. Damning alone: calling the reader **"the
user"** (nobody texting a friend does), and **internal machinery** vocabulary —
threshold, criteria, filtered out, suppressing — which are words about Palmer's
own plumbing. Damning only **together**: a send decision plus a third-person
claim about the reader's preferences. `"said"` is deliberately not one of those
intent verbs. `test_repetition.py` holds both directions, including the four
real replies the loose version blocked.

## Voice / prompt rules (see `SYSTEM_PROMPT` in `agent.py`)

Palmer has a specific voice: dry, observational, plain-text SMS (no markdown, no bullets except the one numbered onboarding list). If you touch the system prompt or write new drafting prompts (Haiku personalizations, morning drafts, followups), keep to the same rules — no "Great question", no summarizing user words back, no ending every message with a question, and **never redirect the user to competing apps** (Google Maps, Waze, ChatGPT, etc.).
