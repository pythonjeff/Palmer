# Morning

The morning send. Governs `morning.py`.

Each section below was written when the behaviour it describes was decided, usually after a production incident, and is kept verbatim. Read the one for a module before changing that module.


## The morning update is basics plus a link, not a full briefing

`morning._compose_morning` sends ONE message: a short Palmer-drafted text carrying the basics, then the user's Palmer Home URL. Every user gets the same shape — today's weather, the commute if they have an address on file, and 1-2 things newly open or worth catching nearby this week — so a user who never taps the link still gets those three every day. Anything beyond that (their tracked topics, prices, headlines) lives on the page only. It used to be a single one-line teaser ("here's a reason to tap"), and before that the full text briefing plus a second text carrying the link — both said less or said everything twice; this is the middle point.

Two properties are load-bearing:
- **The URL is last and alone.** Message apps only draw the rich link preview when the message carries exactly one URL at a boundary, and that preview is most of the value. Nothing may follow it — not a period, not a sign-off.
- **`carries_link` gates the status callback.** A link message is sent with `add_status_callback=False`, because the `/sms-status` shorten-and-retry would truncate the URL into garbage.

`generate_morning_line` drafts the text on Sonnet through `_build_system` like every other user-facing message. It builds a REQUIRED list from what the payload actually has (weather is basically always there once a city is known; commute only when `traffic` is populated, which only happens when the profile has an address; opening only when `opening_snapshot` returned rows) and tells the model every item on that list must appear — with real specifics, not a vague gesture at the category — plus at most one more sentence about something else on the page if it's genuinely notable. Two rules are enforced in code rather than trusted to the prompt, because the model breaks both: `_strip_link_placeholder` removes "[link]"-style stand-ins and any invented URL, and `_NAMES_THE_LINK` triggers exactly one redraft when the line says "page"/"link"/"dashboard" — that phrasing turns a text from a friend into a push notification.

Opening is no longer opt-in for this reason — `home._fetch_opening` fetches it by default for any user with a city (a user can still be excluded with `morning_prefs.opening = False`). It shipped off at first specifically so a bad metro's rows could be caught with `scripts/preview_opening.py` before anyone saw them; that review still matters, it just now happens after rollout instead of gating it.

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

## One list drives the morning and the page

`morning_topics` is the single source for both the morning update and Palmer Home. A topic that resolves to a ticker becomes a live Markets row; everything else becomes a followed subject. So "add Apple stock to markets", "put Nvidia on my site" and "add Bitcoin to my morning" are all the same operation — `update_morning_briefing` — and its description and the `USE THE RIGHT TOOL` block say so explicitly, because users do not know they are one list.

Three things make that flow feel live rather than broken:
- **`home.invalidate(phone, ("prices",))` runs on every topic change.** The page caches prices for 5 minutes, so without it a ticker the user just added does not appear for up to five minutes, which reads as "it didn't work". It expires the stamp rather than refetching inline — the user is waiting on a text reply, and seconds of network for data nobody is looking at yet would go straight onto that reply.
- **A failed price fetch keeps its previous row.** CoinGecko 429s under load and yfinance times out. Without the fallback in `_fetch_prices`, a blip silently deletes a ticker the user is tracking, which looks exactly like Palmer forgetting — far worse than a stale number under a visible "N min ago" stamp.
- **Asking a price is not tracking one.** "what's Apple at" is `get_price`; "add Apple", "and Nvidia", "spacex too" are `update_morning_briefing`. Mid-list continuations were routing to `get_price`, so Palmer quoted a price at someone who was plainly still adding things.

A topic that resolves to a ticker is also **excluded from the paid news search** — Markets already answers it, and "Apple stock price" is a poor news query. The text briefing always did this; `home._fetch_headlines` now matches.
