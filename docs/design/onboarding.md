# Onboarding

How a new user is set up. Governs `onboard.py`, the NEW USERS and ONBOARDING ASK prompt blocks, `userprofile._eager_build_home`.

Each section below was written when the behaviour it describes was decided, usually after a production incident, and is kept verbatim. Read the one for a module before changing that module.


## Setup is a page, handed out at the setup moment

`onboard.py` is the first-run path. When someone reaches the setup moment —
they asked what Palmer does, or said "set me up" before Palmer knows where
they are — and the profile has **no city**, `get_my_page`'s dispatch calls
`onboard.start` instead of `home.ensure_fresh` and the model closes its reply
with the URL, exactly as it does for a built page. `/h/{token}` renders a form
(name, city, what they follow, mornings on/off) while the token's payload says
`setup_pending`, and their real page forever after: one link that turns into
the thing. The what-I-do list in `SYSTEM_PROMPT` ends on that link rather than
on "what should I call you, and what city are you in?"

**It is not on message one.** An earlier version appended the link to every
stranger's first text. That put a feature pitch and an unasked-for URL into the
first reply for a wrong number and for someone who only asked what Bitcoin is
at, against two rules that predate it — the bare-greeting intro carries no
pitch, and Palmer never volunteers a URL. `test_onboard.py::TestWhereTheLinkGoesOut`
reads `main._handle_sms_inner` and fails if onboarding is mentioned there.

The reason it is a form is ordering. Every fact used to reach a profile through
`userprofile._update_profile`, a Haiku pass reading the chat *after* the reply
had gone out, and `update_morning_briefing`'s dispatch seeds topics from
`profile["city"]` — which on the turn a user says *"Jeff, Austin, set it up"*
is still empty, so `default_topics(None)` returned national news alone while
Palmer said the words "local news". A typed city is on the row before anything
reads it. The chat path still exists (plenty of people never tap a link from an
unknown number), so `userprofile._seed_local_topic` closes the same gap there:
it runs inside `_apply_profile_updates` on the `new_city and not old_city`
transition and adds the local topic to an already-onboarded list.

Three things are load-bearing:

- **The write is one-shot.** The token has always been the page's only
  protection (see home.py), and a form turns a read key into a write key, so
  `apply()` clears `setup_pending` before it writes anything, and a second POST
  to the same token is refused. Anyone who sees the link before the user
  submits can fill it in — the window is the minutes between the text and the
  tap, and it is the whole exposure.
- **"What do you follow" is free text, not chips.** A chip's label is a
  category, and a category is the topic shape the search answers worst ("US
  politics" never clears the relevance floor; "Philadelphia Eagles" returns
  something most days). The box collects the specific thing in the words they
  would text, and `split_follows` runs each through `agent._normalize_price_topic`
  so "nvidia stock" resolves a ticker here exactly as it would texted.
- **A parked stub is not a page.** `_eager_build_home` used to bail on any
  existing payload; it now bails only on a built one, so someone who got the
  link, never tapped it and then said their city in chat still gets the build
  instead of an address that stays a form forever.

The build runs off the request thread and `/h/{token}` serves a self-refreshing
holding page until `built_at` appears; the `.png` route 404s in both states,
because a preview scraper asks for it before anyone has typed anything.

## Onboarding asks once; the site builds ahead of it, silently

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

## A new user is set up, not interviewed

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
