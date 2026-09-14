# Checkin

The one text on Palmer's own initiative. Governs `followup.py`.

Each section below was written when the behaviour it describes was decided, usually after a production incident, and is kept verbatim. Read the one for a module before changing that module.


## The check-in is the one text on Palmer's own initiative, and it is paced in days

A daily "a friend would text this" news alert (`alerts.py`, once a day from the
profile's interests) used to text people on Palmer's own judgment. It is gone,
and `test_scheduler_config.py` pins the job list so it does not come back
quietly. Live score texts exist but are not on Palmer's initiative — see the
Scores section: a user has to set a level. What remains unprompted is
`followup.py`: one text every `GAP_DAYS` (10) or more, in a 1-7pm local window,
about ONE thing.

**The subject is copied from data, never written by the model.** Three pools,
none of them fetched for this job: `ongoing_threads` from the profile, a followed
team's game yesterday or today (`sports.team_day`, the same read the page makes),
and the page's stored headlines for their topics (`home.load`, never a search).
`_candidates` turns them into `Subject` lines; Haiku picks one by echoing its text
exactly, or NONE, and `_pick_subject` matches the echo back against the list and
fails closed. That echo rule is what let the job take on news and sports without a
new way to make things up — it was written for threads, after a confabulated
thread was drafted as though it were real, and it holds for every kind.

**The gap is the rate limit, and it is measured in days, not ticks.** With teams
and news in the pool there is nearly always a candidate, so the 3-day gap the
thread-only version ran on would have become a text every three days for
everyone. `tapback.pacing_factor` stretches it; `GAP_MAX_DAYS` caps the stretch.

**`life_context` alone never triggers a check-in.** It is a paragraph about
someone's life, not a thread with a follow-up, and handing that to a model asked
to find something "worth a text today" is how one gets invented.

**The draft prompt does not ask for invented specificity.** "A statement that
just shows you remembered" is an instruction to make something up; each kind's
prompt says to use only the subject line and recent messages. A news subject
carries its URL last and alone, appended in code after the draft, never asked of
the model. A headline older than `NEWS_MAX_AGE_HOURS` is not a candidate.

**Every bail path restores `followup_sent_date`, it does not null it.**
`claim_daily_guard` overwrites the field with today, so nulling it on a bail
erased the record of the last real send — and `_should_send_followup` measures
the pacing gap against exactly that field. `followup_last_thread` (the name
predates news and teams; it holds any subject's text) keeps the next pick from
landing on the same thing twice running.

`RETIRED_FIELDS` in `userprofile.py` nulls what the retired jobs wrote
(`alert_sent_date`, `interest_genres`) on the next inbound message. Dropping a
key from `PROFILE_FIELDS` only stops new writes; the value already in a row would
otherwise stay and be dumped into every system prompt.
