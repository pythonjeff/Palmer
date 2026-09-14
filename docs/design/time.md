# Time

Clocks and calendars. Governs `timeutil.py` and every daily guard.

Each section below was written when the behaviour it describes was decided, usually after a production incident, and is kept verbatim. Read the one for a module before changing that module.


## The model is told the reader's clock, not the dyno's

`timeutil.clock_block` builds the RIGHT NOW block in every system prompt, and
`SYSTEM_PROMPT` has one `{clock_block}` placeholder where it used to have
`{date}` and `{now_utc}`. Both were UTC, which is a lie for most of the day: from
17:00 Pacific onward the UTC date is already tomorrow, so "Today is Monday" was
simply false for a Los Angeles user at 5:42pm Sunday, and "remind me tomorrow at
9" filed for Tuesday. The model was not confused — it was told the wrong day and
reasoned correctly from it.

With no resolvable zone the block says so and asserts **no local date at all**.
Presenting UTC as though it were their day is the whole defect, so the honest
form is the safe one. Same rule now on the page and the card: `page._local_day`
fell back to `datetime.utcnow()` and printed it unlabelled as the reader's day,
so a zoneless user west of UTC saw tomorrow's date on their own page from 5pm.
Both omit the date instead — they render from one payload and must not disagree
about the day, which is why `cards.render_dashboard` takes `show_date`.

**The block carries the week, not just today and tomorrow.** It named exactly
those two days and emitted no ISO date at all, so anything further out — "next
Friday", "the 15th", "a week Tuesday" — was the model rebuilding a date from the
prose "Friday, September 04, 2026" and counting in its head. That is the one
computation on the reminder path nothing checks: `_normalize_due_at` catches an
unreadable string, a past time and a date over a year out, but a plausible wrong
Friday passes and then reads correctly in the confirmation, which is exactly what
makes it unfalsifiable. `_date_run` lists the next eight days with full ISO dates
so the model lifts one rather than deriving it. Eight, not seven: the weekday that
IS today then appears twice, which is the only way the repo's own convention — a
bare weekday naming today means the one a week out — has a date to point at.

**`resolve_day_delta` lives in `timeutil`, not `weather`.** It is generic date
reasoning that happened to sit in the weather module, which is why the reminder
path — the only path where the MODEL computes the date — had no answer for "next
friday" while the weather path had a considered one, and the same user could get
both in one thread. `SYSTEM_PROMPT`'s REMINDERS section states the convention
where the model does the work, and `tools_def`'s `due_at` description points at
the block rather than restating the rule, so the two cannot drift.

**`recurrence` is vetted on the write path.** A tool-use `enum` is guidance to
the model, not a constraint the API enforces, and nothing downstream checked:
`"monthly"` reached the column intact, the dispatch confirmed "It repeats
(monthly)", and `next_occurrence` then returned None so the row was never
re-armed. The user was told it repeats and got exactly one text — the outcome
`SYSTEM_PROMPT` and the tool description both name as the thing never to allow.
The dispatch refuses it now, naming the three that work, and `send_reminders`
logs the case rather than dropping a standing reminder in silence. `timeutil.valid_zone` is the gate — `profile["timezone"]`
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

## Every date is computed on the calendar that owns it

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

## A daily guard means the READER's day

`alerts.py` keyed its once-a-day guard on the UTC date while `_in_alert_window`
gates on the **local** hour 13-21. For Pacific that window is 20:00Z-04:00Z, so
the UTC day rolled over at 17:00 local — *inside* the window — and a user could
take two "daily" alerts in one local day and none the next. `morning.py` and
`followup.py` already keyed on `timeutil.local_today`; alerts now does too.

`_daily_alert_hour`'s UTC date is deliberately left alone: it is only reached when
the profile has no timezone, so there is no local day to key on, and it only needs
to stay stable within a UTC day.

The two watch caps were the ones left behind by that fix. `watches._daily_ok` and
`shopping._daily_ok` both keyed on the UTC date, and so did the matching writes in
`db.update_watch_alerted` / `update_price_watch_alerted` — read and write agreed
with each other and both disagreed with the reader, so the window rolled at 17:00
Pacific, inside the evening rather than between days, and the allowance could be
spent twice over one local evening. `run_watches` already did one profile read per
user for the pacing cap, so the local day rides along on it; `run_price_watches`
read no profiles at all and takes one batched `get_all_profiles()`, never N+1.

`agent._prompt_safe_profile` had the mirror-image version: `_stamp_volatile`
writes `field_dates` with `local_today`, and `fresh_profile_for_prompt` was called
with no `today` so it aged them against `date.today()`. The two ends of one
subtraction used different calendars — after 17:00 Pacific a fact asserted minutes
ago came back to the model as `days_old: 1`, and a volatile field was dropped a
day before its life ran out.

**`morning._recent_assistant_texts` selects prior MORNINGS**, via
`db.get_recent_messages_of_kind` and the `kind` column. It took the last four
assistant messages of any kind, which for anyone who actually texts Palmer is
four chat replies — so `guards.repeats_opening`, written for three consecutive
mornings that opened identically, was comparing today's line against ordinary
conversation and almost never against yesterday's morning. Falls back to any
assistant message for users whose history predates the column.
