# Agent loop

How a reply is made: the system prompt, the tool loop, dispatch, and routing. Governs `agent.py`, `prompts.py`, `tools_def.py`, `llm.py`.

Each section below was written when the behaviour it describes was decided, usually after a production incident, and is kept verbatim. Read the one for a module before changing that module.


## One voice: all user-facing text goes through `_build_system`

Anything the user reads is drafted with `agent._build_system(phone)` as the system prompt, on `SONNET_MODEL`. That is what carries SYSTEM_PROMPT, the CALIBRATION section, the user's `communication_style`, and their reaction history — so Palmer sounds like the same person everywhere.

There used to be a second tier of paths carrying their own one-line persona ("You're Palmer, a dry, sharp texting friend") that never saw any of it, which meant a user who asked for less sarcasm still got the breezy default on price alerts and reminders. Do not reintroduce that. If you add an outbound message, it goes through `_build_system`.

The deliberate exception is `traffic.py`: its output is *source data* for a draft that already carries the system prompt (morning briefings, and the `get_city_traffic` tool inside `get_reply`), so it is a plain factual summarizer on Haiku. Voicing it there would layer a second, uncalibrated Palmer under a real one.

`watches.py` was an unrecorded second exception until recently: `run_watches` sent
`_format_alert`'s bare `title\nurl` — no system prompt, no calibration, and an
unprompted URL with nothing around it. `_draft_alert` now writes the line through
`_build_system` on Sonnet, exactly as its sibling `alerts.py` always did, with the
URL appended by the caller so it stays last and alone. Three things there are
load-bearing: the draft runs **after** both dedup gates and the claim (deduping on
a paraphrase is worse than deduping on the facts, and drafting first would spend a
Sonnet call on every candidate the gates discard, at a 30-minute cadence); what is
sent is what is saved, so history and `_is_duplicate_subject` see the real message;
and every failure still delivers — `base_system()` when `_build_system` raises,
the raw headline when the draft does. `update_watch_alerted` still stores the
factual title, because that string is fed back into `_check_watch_hit`'s
already-sent block where a voiced paraphrase would degrade the match.

## A reply never dies because the turn was long

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

## A tool that raises must not take the turn with it

Nothing wrapped the dispatch chain in `get_reply`, so a raise from any of the 31
branches escaped the function: `main.py` caught it, left `reply` falsy, and
answered with `FALLBACK_SMS`. Every tool result already gathered went with it —
in a three-intent message, one failing DB write destroyed the two intents that
had already succeeded. That is the same failure `TOOL_ITERATION_CAP` was fixed
for, reached through a different door.

The chain lives in a nested `_dispatch()` and the caller wraps **one block at a
time**, not the whole loop: every `tool_use` block must come back with a matching
`tool_result` or the next `messages.create` rejects the turn, so catching around
the loop would lose the other tools' results and dead-end the turn by another
route. Nested rather than module-level because fourteen assertions across eight
test files read `inspect.getsource(get_reply)` to check what a branch says.

`_tool_error` reduces the exception to its type unless it is ours (`KeyError`,
`ValueError`, `TypeError` — our own strings, and the only ones the model can act
on). `netutil` re-raises the underlying urllib error on its last attempt, and
that message carries the request URL, which for SerpAPI carries the API key.

The six `home.invalidate` try/excepts stay. They wrap a best-effort cache expiry
that runs **after a successful write**, so folding them into the outer catch
would turn a stale cache into a tool error for an operation that actually
succeeded — Palmer would tell the user their topic wasn't added when it was.

**A failure string is a prompt.** `flights.py` set the pattern — say what failed,
say what to do next, never imply the capability is missing — and it had not
reached `datafeeds`, which backs `web_search` and `get_price` and returned eight
dead ends, three of them interpolating raw exception text into the drafting
context. `"No results found."` was the commonest failure string in the system,
since the recency window and the source floor throw most of a page away by
design. Also fixed: `shopping`'s browse and search no-result strings, `hotels`'
two (its sibling branch in `flights.py` got the treatment and it did not), and
`get_city_traffic`, which collapsed "no key", "unknown city" and "API down" into
one `None` the dispatch could not tell apart — `traffic.city_traffic` returns the
reason, and `get_city_traffic` stays a thin wrapper because `morning.py` wants
exactly a line or nothing.

## Model routing

Two Claude models, chosen in `agent.py`:
- `SONNET_MODEL = "claude-sonnet-4-6"` — conversation, drafting, all user-facing replies
- `HAIKU_MODEL = "claude-haiku-4-5-20251001"` — extraction, scoring, classification, topic-inference, thread selection

New extraction/scoring logic should go on Haiku; new user-facing drafting on Sonnet. When updating model IDs, grep for both constants — they're re-exported and imported across most modules.

## Tool routing is strict and important

The system prompt in `agent.py` hard-routes user asks to specific tools. Never mix:
- `get_weather` → NWS (US) with Open-Meteo as the fallback and the rest-of-world path. `OWM_API_KEY` is vestigial — no code has read it since the Open-Meteo switch (`4620ba6`), whatever the env table still says
- `get_price` → CoinGecko (crypto) / yfinance (stocks) only
- `get_travel_time` / `get_city_traffic` → TomTom only
- `set_commute` / `clear_commute` → the user's REGULAR drive; TomTom geocode on the write path, routed for their leave time by `home._fetch_traffic`
- `get_my_page` → the caller's own Palmer Home URL, via `home.ensure_fresh` (never a bare URL builder — that can hand out a link to a page that was never built)
- `add_price_watch` / `run_price_watches` → SerpAPI Google Shopping only (product prices, distinct from `get_price` for crypto/stocks)
- `web_search` → Tavily news mode only, never for weather or prices

If you add a new tool, follow the same discipline: one data source per tool, and update the `USE THE RIGHT TOOL` block in `SYSTEM_PROMPT` so Claude routes correctly.

**A tool the prompt never names is one the model routes from its own description
alone.** Eight of the thirty-one were in that state. `add_watch` was the costly
one: its description tells the model to fire on "a team, a story, a market",
which is exactly what the routing block assigns to `update_morning_briefing` and
`follow_team`, so "track the Cardinals" satisfied all three and nothing
arbitrated. They are three different promises — breaking news, the daily list,
live scores — and the block says so, with the daily as the safe default because
it is the one that costs nothing when the story is quiet. Both traffic tools were
absent outright while the prompt advertised "traffic, drive times" among Palmer's
capabilities. So was every undo verb, though "stop tracking the Eagles" matches
four of them and guessing there deletes something the user wanted.
`test_calibration.py::TestEveryToolIsRouted` fails on any tool name missing from
`SYSTEM_PROMPT`, so a tool added later cannot ship unrouted.

**The prompt's factual claims are tested against the code they describe.** It
still said price watches alert on "~15% drops" long after that bar was deleted
for the second time and replaced with a flat `$2` in either direction — so Palmer
described a drop-only percentage watch to users and then sent a rise alert. It
described the morning update as carrying "sports scores, news, Bitcoin price",
which moved to the page two versions ago. And a NEVER bullet held up "here are
the fares now — I can't watch them for changes yet" as the sentence to imitate,
four lines from the routing block's own "Never say you can't track flights".
`TestThePromptDescribesTheProductThatExists` pins the first against
`shopping.MOVE_MIN_ABS` and forbids a percentage in that section at all.

## Ask which one, where guessing wrong costs them the turn

`sports.find_teams` returns a LIST because "Cardinals" is two teams in two
sports, and the dispatch asks rather than picking. Nothing else did. Every other
resolution took the top hit and then confirmed it to the user as though they had
named it, which is the failure that stays silent longest — the user has no reason
to doubt a confirmation.

- **Places.** `weather._geocode` asked for `count=1`, so Springfield, Portland,
  Columbus and Cambridge each resolved to whichever the geocoder ranked first.
  The count is 5 on the same call at no extra cost; `_geocode` still returns the
  top hit and caches exactly as before, and the runners-up are kept for
  `weather.ambiguous_location`, used on the **write** paths only. Note
  `resolve_weather_location`'s docstring has always claimed "None means the model
  should ask rather than guess" — that was only ever true when *nothing* matched.
- **Shows.** The show resolver took `results[0]` too, and `sports.py`'s own comment
  says naming a show is not ambiguous the way naming a team is. True of Reacher,
  false of The Office, Shameless, Skins and Ghosts. `shows.find_shows` returns
  matches with year and origin country; only a genuinely shared title is a
  question, since "Reacher" also matching "Reacher: Behind the Scenes" is the
  search working, not two things the user might have meant.
- **Products.** `add_price_watch` echoed the words the user typed while
  `add_amazon_watch` echoed the listing it resolved. The Google Shopping match is
  picked by a model with no confidence floor, so "AirPods" can baseline on Gen 2,
  Gen 4 or Pro — and echoing their own phrasing back made a wrong pick invisible
  until an alert arrived about the wrong product.

**Reminders were the one table-backed thing the model could not see.**
`_build_system` lists active watches and price watches; the thing the user
explicitly asked to happen at a named time was absent, so "what have I got on"
had nothing to answer from and "cancel my 4pm one" was a guess against twenty
messages of history — against a tool that deletes. `db.get_pending_reminders`
feeds them in on the reader's clock, with the hazard stated: `cancel_reminders`
with no `text_match` takes all of them, and `text_match` is a substring, so
"call" takes "call mom" and "call the vet" together. `cancel_reminders_named`
returns the texts that went, because a bare count is the half the user cannot
check.

This is deliberately not "ask about everything". `SYSTEM_PROMPT`'s cost test
still governs — ask when guessing wrong wastes their turn, act when either
reading gets them something useful — and the rules that say act immediately (a
reminder, an Amazon link, turning the morning on) still win. What changed is that
the clarification rule now **outranks the rhythm rules** it was previously
outvoted by. It was stated once; the pressure against ending on a question was
stated four times, and one of those is shape-based and unconditional ("if your
last reply ended with a question, this one ends on a take or silence") so it
fired exactly when a clarification needed a second turn.

## Shared modules — don't re-copy these

- `serpapi.py` — SerpAPI key, base URL, timeout, and request transport. Both `shopping.py` and `amazon.py` use it. Each still parses its own engine's payload; only the transport is shared.
- `price_alert.py` — the one drafter for price-watch alerts, used by both price sources. It lives in its own module because `shopping.py` already imports `amazon.py`, and putting it in either would make that coupling bidirectional.
