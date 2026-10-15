# Profile

What Palmer remembers about a person. Governs `userprofile.py` and `EXTRACT_PROMPT`.

Each section below was written when the behaviour it describes was decided, usually after a production incident, and is kept verbatim. Read the one for a module before changing that module.


## The profile is a bounded schema

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
(`followed_teams`, `shows`, `commute`). Listed there, Haiku fills it with prose and the
code reading it gets strings where it expects dicts.

`scripts/migrate_profile_prune.py` cleans rows that grew before the allow-list existed. It folds the stray keys into canonical fields with a Sonnet pass before dropping them, so real facts survive. Dry run by default; `--apply` writes.

## Profile facts expire; `city` outranks anything else that names a place

`PROFILE_FIELDS` bounds which keys may exist. It does nothing about keys that
are still there and no longer true, and the whole profile is dumped into every
system prompt as CURRENT fact.

That is where the "Palmer keeps getting things wrong" reports actually came
from, and it is worth being precise about what it was not: the system prompt and
tool schemas are about 17-18k tokens (measured: 36k characters of prompt, 34k of
tool JSON — the schemas are now the larger half) and a profile 1-3k, which is
still comfortable for Sonnet. The model was not overloaded. It was being told, every turn, things that
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

## The name must be extracted, not just spoken

`profile["name"]` was empty for a user who had told Palmer his name twice. Palmer still called him Jeff — it reads the name straight out of conversation history — but the page renders from the profile, so it showed "Your briefing" and kept prompting for a name it had already been given. Anything reading the profile rather than the transcript saw an anonymous user.

The cause was the extractor: `"My name is Jeff"` returned `{}`. `EXTRACT_PROMPT` asked for "life details, relationships, preferences, personality" and Haiku did not count a name as worth remembering — and where the profile already looked populated, it assumed the name must be in there. `EXTRACT_PROMPT` now opens with an IDENTITY FIRST rule that names the phrasings people use and explicitly overrides the "too obvious to return" and "surely it is already stored" instincts. `test_profile_schema.py` guards it.

The lesson generalizes: Palmer sounding like it knows something is not evidence that anything was stored. The transcript and the profile are different memories, and only one of them survives into the morning job, the page, and the card.

## `timezone` is validated on write, and re-derived when someone moves

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

## Consolidation runs on a batch, not on every turn

`_consolidate_history` fired on every turn once a user passed 40 messages,
re-summarising a near-identical 80-message window each time — one Haiku call per
turn, forever, for a profile that had barely moved. `CONSOLIDATE_EVERY` (20) and
the `consolidated_at_count` watermark gate it on how far the conversation has
actually travelled.

## Topic overlap is raised, not enforced

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
