# Guards

Rules the prompt states and the model breaks, enforced in code. Governs `guards.py` and `agent._finalize`.

Each section below was written when the behaviour it describes was decided, usually after a production incident, and is kept verbatim. Read the one for a module before changing that module.


## Rules the prompt states and the model breaks are enforced in code

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

## A capability denial is caught in code, like a handoff

`guards.redirects_elsewhere` only fires when a denial is *accompanied by* a
competitor. Three of the four real production violations in
`test_guards_and_flights.py` are denials first and handoffs second — strip the
brand name and nothing caught what was left. `guards.denies_capability` closes
that, wired into `agent._finalize`'s existing loop.

Two tiers, for the reason `leaks_deliberation` has two. Damning alone: the
app-inventory register ("not in my toolbox", "outside my capabilities"). A person
with a real gap says "I can't send email"; nobody says "email isn't in my
toolbox", so this tier needs no capability object and cannot collide with an
honest gap. Damning together: a **first-person** denial plus a job Palmer has a
tool for, unless the clause carries a transient marker.

First person is load-bearing twice. Every tool failure string addresses Palmer in
the second person ("never say you cannot do flights"), so anchoring on "I" keeps
the guard off Palmer's own scaffolding; and it keeps it off a third party's
limits ("the airline doesn't publish seat maps") with no separate exclusion —
the same structural reason those sentences survive the redirect guard. The
transient exemption is the sentence `SYSTEM_PROMPT` actually asks for when a tool
is down, so it is the shape being protected rather than policed.

**Redraft only, never a `send_sms` block.** A deliberation leak is blocked
outright because the drafter was announcing it had decided not to send. A
capability denial is a reply the user is waiting on, and blocking it hands them
`FALLBACK_SMS` — worse than an imperfect answer.

The corpus asserts both directions, and the second half is the one that matters:
nine categorical denials fire, while four honest gaps, three third-party limits,
four transient failures, all eight existing `MUST_SURVIVE` sentences and **every
failure string in the codebase** do not. That last check is the interlock — those
strings are what the model paraphrases, so if one read as a denial the guard
would be policing a problem we wrote.

## Palmer's own deliberation never ships

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

## Repetition is two problems with opposite remedies

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
every time. Three consecutive mornings: "Morning Alex - 103 today in Cedar
Falls", "106 in Cedar Falls today, Alex", "111 today in Cedar Falls, Alex".
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
