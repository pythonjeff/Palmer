# Reminders

Reminders and recurrence. Governs `reminders.py`, `timeutil.next_occurrence`, the `set_reminder` dispatch.

Each section below was written when the behaviour it describes was decided, usually after a production incident, and is kept verbatim. Read the one for a module before changing that module.


## Reminders repeat; morning topics are the other kind of repeating

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
  opposite of the daily-guard jobs (morning, followups), where releasing
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
