# Opening

What is newly open nearby. Governs `opening.py`.

Each section below was written when the behaviour it describes was decided, usually after a production incident, and is kept verbatim. Read the one for a module before changing that module.


## Opening: metro-scoped weekly content

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
with `scripts/preview_opening.py` — that review now happens after rollout rather than
gating it.

TMDB's terms require the notice *"This product uses the TMDB API but is not
endorsed or certified by TMDB"* wherever their data appears; `page.py` renders it
only when a screen row is actually present. **TMDB is free for non-commercial use
only** — the same clause shape as Open-Meteo, and a question the day Palmer
charges.
