# Scores and shows

Followed teams and followed shows. Governs `sports.py`, `scorewatch.py`, `shows.py`.

Each section below was written when the behaviour it describes was decided, usually after a production incident, and is kept verbatim. Read the one for a module before changing that module.


## Scores: following is the morning and the page; live texts are an ask with a level

`sports.py` reads scores. `sports.team_day(team, today)` is the read every
surface shares: yesterday's game if it finished, and today's in whatever state
it is in, both keyed on the READER's calendar day via ESPN's `dates=` parameter.
`home._fetch_scores` renders it as the one-word `Scores` section,
`morning.score_lines` puts it in the morning digest and the REQUIRED list, and
`followup._candidates` offers it to the check-in. A team with nothing on either
day produces no row. `result_line` states a game from the team's side ("beat the
Cubs 5-2") so no drafter is left to infer whose side the reader is on.

**Live texts during a game are opt-in, twice over.** A followed team dict
carries `live`: absent or `off` (the default), `key`, or `all`. `scorewatch.py`
polls only teams with a level set (`live_teams`), so following a team costs
nothing there. `key` is the original rationing — the lead changing hands, a
score in the closing stretch (`_is_late`, which means different things per
sport), and the final. `all` adds every other score in the leagues where that
is a text anyone could want (`EVERY_SCORE_LEAGUES`); for the NBA, where a basket
lands every thirty seconds, `all` means key moments. `alert_cap(mode)` is the
per-game backstop (4 for key, 20 for all) and the final is never swallowed by
it. Everything not texted still moves the stored baseline (`game_alerts`), so
the next comparison is against what the user was last TOLD.

**Palmer offers it once, and only when there is a reason.** `_build_system`
appends a LIVE SCORES OFFER block when the extractor has written `sports_teams`,
`followed_teams` is empty, and `score_offer_sent` is not set;
`userprofile._update_profile` marks it consumed the first time that condition
holds after a turn, answered or not — the same shape as the ONBOARDING ASK. The
`follow_team` result also tells the model to offer the two levels in one clause
when it was called without `live`, and `set_score_updates` changes the level of
an already-followed team without dropping it — "stop the live score texts" is
that with `off`, never `unfollow_team`. `test_sports.py` pins the default-off,
the NBA fallback, the cap, and the once-only offer.

**The obvious ESPN endpoint does not work from Heroku.**
`site.api.espn.com/.../scoreboard` — the one every guide recommends — returns
**403 from the dyno**, verified in production, so it is ESPN blocking datacenter
IPs rather than a local quirk. `site.web.api.espn.com` is the same shape,
unblocked, and returns a whole league in one call. The core API
(`sports.core.api`) also works but is reference-based: **seven** HTTP calls for
one game's score. Free and undocumented is a deliberate starting position; the
ESPN shape is confined to `sports.py` so a paid feed is a one-module swap.

**Team names are ambiguous in a way show titles are not.** `find_teams` returns
a LIST — "Cardinals" is two teams in two sports, "Rangers" likewise — and the
dispatch asks rather than picking, because guessing puts the wrong team, in the
wrong season, in someone's morning. Verified live: "text me cardinals scores"
gets *"Which Cardinals — baseball (St. Louis) or football (Arizona)?"*

`teams` on the profile is the resolved follow list. It is **not** `sports_teams`,
which is the extractor's free-text description ("Cardinals fan, emotionally
invested...") — good for Palmer's voice, useless for lookups.

## Followed shows are not discovery

`shows.py` tracks series a user named, and the distinction from the `screen`
rows beside them is the whole feature. Screens answer *what is new to anyone* —
popularity-ranked, identical for every user. A followed show answers *what is
new for the shows you watch*, and exists only because someone asked for it by
name. `follow_show` / `unfollow_show` are its controls, not `opening_remove`;
episode rows deliberately bypass `wanted_kinds`, because that setting chooses
which kinds of **discovery** you want.

TMDB gives episode-level data directly: `/tv/{id}` carries
`next_episode_to_air` and `last_episode_to_air` with air dates, season and
episode numbers and titles. One free call per show, cached by `(show id, local
day)` and therefore **shared** — two users watching Reacher cost one lookup,
the same shape as the metro cache.

Three rules came from the spec and each has a test that catches its reversal:

- **A row exists only in the week its episode lands.** Upcoming within
  `UPCOMING_DAYS`, or dropped within `JUST_DROPPED_DAYS`. A show between seasons
  produces nothing — it is not a permanent countdown.
- **The page by default, the morning text only on request.**
  `morning_prefs["episode_alerts"]` gates it, `home._refresh_identity` carries
  the flag onto the payload, and `_payload_digest` honours it without a profile
  read of its own. A weekly "new episode!!" nobody asked for is precisely the
  drumbeat this product keeps having to remove.
- **Episodes displace screens rather than lengthening the page**
  (`MAX_EPISODES` takes its slots from `MAX_SCREENS`). A show you actually watch
  is worth more than a film chosen for you, and the row count stays put.

Resolution runs on the **write** path (`find_shows`, one TMDB search when the
user follows), never on read — same terms as `_normalize_price_topic` and
`_city_from_weather_topic`. An unresolvable title asks the user to confirm; it
never guesses one and never sends them elsewhere to look it up.
