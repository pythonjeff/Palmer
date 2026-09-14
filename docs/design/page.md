# Page

Palmer Home and its preview card. Governs `home.py`, `page.py`, `cards.py`, `artifacts.py`, `tickers.py`.

Each section below was written when the behaviour it describes was decided, usually after a production incident, and is kept verbatim. Read the one for a module before changing that module.


## Section labels are one word

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

## The page is arranged by the user, in a text, never in a form

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
sms: URIs have no form encoding). The page has no auth and accepts exactly one
POST — the one-shot setup form above — and nothing else.

## The preview image must change URL, or nobody ever refetches it

`og:image` points at `/h/{token}.png?v={fingerprint}`. The query stamp is the
whole point: link-preview scrapers — iMessage most stubbornly — cache og:images
by URL and have no reason to refetch one they have already seen. With a fixed
`/h/{token}.png`, every morning's message showed whatever card was scraped the
first time. The server was rendering today's card faithfully; nobody was asking
for it, and there was no ETag or Last-Modified to hint otherwise. The PNG route
now sends an ETag too, for caches that do revalidate.

## Windows must be shorter than the refresh opportunity, or they alias

Most users never open their page, so the only guaranteed refresh is the daily
morning send. A section whose window is 24h therefore lapses on **about half**
of them: three users were carrying Opening rows 41 hours old with no refetch
even attempted, because at the previous send the section was 20.4h old — just
under its own window — and the next chance came a day later. `STALE["opening"]`
is 20h for that reason, leaving margin for a send that drifts.
`test_home.py::TestNoSectionAliasesAgainstTheDailySend` holds every window under
a day. The refetch is nearly free anyway: `opening.py` caches by metro and week,
so a refresh inside the same week is a dict lookup.

## Empty paid sections retry sooner than full ones

The `_tried` stamp is written before the call so a failure cannot be retried in
a loop. That also meant a single empty or failed fetch left a section blank for
its entire window with nothing to show — it locked three of four users out of
Opening for a day, twice, and had to be cleared by hand both times. `_window_for`
shortens the wait to a quarter of the window (floor one hour) **only when the
section holds no data at all**. Once it holds something, a stale row beats a
blank one and the full window applies again.

## The card is cached on what it draws, not on when it was built

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

## Topics become prices via `tickers.py`

The Markets section of Palmer Home is derived from the user's `morning_topics`, so a topic only shows a price if it can be resolved to a symbol. That resolution used to be a bare uppercase-word regex, which meant it worked only when the drafting model happened to write the ticker into the topic itself — `"Nvidia stock price (NVDA)"` resolved, `"Nvidia stock"` silently did not, and the user got the topic listed under "Palmer is watching" with no price anywhere. It also matched the `US` in `"US stock market"` and spent a yfinance call on a delisted symbol every page refresh.

`resolve_topic_asset` runs cheapest-first and **never calls a model** — it is on the read path, which runs on every page view: crypto name → explicit `$SYM`/`(SYM)` → curated name map → bare uppercase token behind a `NOT_TICKERS` stopword guard. It returns `(symbol, display_label)` because Yahoo's index symbols are correct and unreadable; nobody wants `^GSPC` in their Markets section.

`resolve_company_ticker` is the escape hatch for names the map doesn't carry — Yahoo's search, not a model, per the paragraph below; the docstrings in `agent` and `tickers` still called it a Haiku pass long after it stopped being one. It runs **once when a topic is saved** (`agent._normalize_price_topic`, called from the `update_morning_briefing` dispatch), never on read.

**Resolution is Yahoo's search endpoint, not a model.** Keyless, ~0.2s, filtered to `quoteType=EQUITY` on a US exchange. It is self-updating, which is the property the alternatives lacked: it independently returns SPCX for SpaceX and XYZ for Block, the two entries the hand-written map had wrong. The filter is load-bearing rather than defensive — unfiltered, `"openai"` comes back as a tokenized crypto and a thematic ETF that merely share the name, so filtering is what makes a private company resolve to nothing instead of to somebody else's price. Strip price words from the query first: `"spacex"` returns SPCX, `"spacex stock"` returns nothing.

Two earlier versions of this got it wrong the same way, and the pattern is worth remembering: a hardcoded `PRIVATE_COMPANIES` denylist, then a Haiku lookup verified against the exchange. Both encoded a model's snapshot of who was public, and a snapshot goes stale the moment anybody lists.

**Indices stay hand-mapped.** Search returns futures for them (`"s&p 500"` → `ES=F`, `"nasdaq"` → `NQ=F`), so `INDEX_TICKERS` is correct where search is not.

Company names are gated behind a price word, indices are not. Without that gate `"SpaceX news"` resolves to SPCX and a news topic someone follows silently grows a stock ticker in their Markets section; `"nasdaq"` needs no such qualification.

**A stale model must not veto live data.** `SYSTEM_PROMPT` forbids claiming a company is private, delisted, or hasn't IPO'd from memory, and `get_price` resolves company names through `tickers.resolve_asset_name` so the tool answers rather than 404ing on `"SPACEX"`. Palmer was refusing to add SpaceX and explaining it was private, which was simply false — and the failed lookup had confirmed its prior.

`cards.MAX_PRICES` is the shared cap. Four columns fit the card's width but the sparklines start overdrawing the price text, so three is the real limit; `home._fetch_prices` imports the constant rather than keeping its own, since the card and the page render from one payload and must not disagree about how much of it survives.
