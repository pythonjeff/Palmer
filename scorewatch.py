"""Live game texts for people who asked for them, at the level they asked for.

This is the one job in Palmer built to interrupt, and it is opt-in twice over:
a followed team gets no live texts unless its `live` level is set, and the
level decides how much. `sports.live_mode`:

  off   the default — the team rides the morning update and the page only
  key   the lead changing hands, a score in the closing stretch, the final
  all   every score, plus the final; a lead change still reads as one

Even "all" is not a firehose: a basket lands every thirty seconds, so for the
NBA it falls back to key moments (`sports.EVERY_SCORE_LEAGUES`), and
`sports.alert_cap` is the backstop per game. The final is never swallowed by
the cap — a game wild enough to spend the budget is exactly the one whose
result they want.

Everything not texted still updates the stored state, silently: the next
comparison is against what the user was last TOLD, so a score arriving in the
same tick as a lead change is one event rather than two, and a suppressed
score does not make the next one look bigger than it was.

Two-speed polling. Checking every couple of minutes around the clock would be
thousands of calls a day against an unofficial API to learn that nothing is
happening; checking slowly during a game misses the moments entirely. A league
with something live is polled at `LIVE_POLL_SECONDS`, an idle one at
`IDLE_POLL_SECONDS`.
"""
from __future__ import annotations

import sports
from db import get_all_profiles, get_game_alert, record_game_alert

# Leagues seen with a live game last tick, so the next tick knows how hard to
# look. Process-local and safe for the same reason every other cache here is:
# WEB_CONCURRENCY=1.
_live_leagues: set[str] = set()


def live_teams(profile: dict) -> list[dict]:
    """Followed teams the user asked live texts for. Following alone is not asking."""
    return [t for t in ((profile or {}).get("followed_teams") or [])
            if t.get("abbrev") and sports.live_mode(t) != "off"]


def _draft(phone: str, game: dict, team: dict, reason: str, prev: dict | None = None) -> str:
    """The alert, in Palmer's voice. Falls back to the plain line."""
    plain = _plain(game, team, reason)
    try:
        from agent import _build_system
        from llm import client, SONNET_MODEL
        from smstext import _sms_clean
        side = sports.side_of(game, team["abbrev"])
        other = "away" if side == "home" else "home"
        mine, theirs = game[side]["score"], game[other]["score"]
        if reason == "score":
            who = sports.scorer(prev, game)
            cue = (f"{team['name']} just scored" if who == side
                   else f"{game[other]['name']} just scored")
        else:
            cue = {
                "lead": "the lead just changed hands",
                "late": "someone scored in the closing stretch",
                "tied": "the game is level again",
                "final": "the game just ended",
            }[reason]
        # Say outright whose side they are on and by how much, rather than
        # leaving the model to work it out from "CIN 17, PHI 21". It managed
        # that most of the time, but a buddy does not deduce who you support,
        # and the margin is what sets the tone — a one-point game and a
        # twenty-point game are not the same text.
        standing = ("ahead by" if mine > theirs else
                    "behind by" if mine < theirs else "level, tied at")
        margin = abs(mine - theirs) or mine
        resp = client.messages.create(
            # include_recent, like every other Sonnet drafter. Without it this
            # path — the one most likely to send several texts in one hour —
            # had no way to see it had just said something similar.
            model=SONNET_MODEL, max_tokens=90,
            system=_build_system(phone, include_recent=True),
            messages=[{"role": "user", "content":
                       f"""Their team is {team['name']}, playing {game[other]['name']}. {cue[0].upper() + cue[1:]}.

{team['name']} {standing} {margin}. Score: {sports.describe(game)}

Write ONE short text telling them, the way you would shout it across a room —
this is the fun kind of interruption, not a bulletin. Lead with what happened.
Use the real numbers.

You are watching the same feed they are, which means you know the score and the
clock and NOTHING ELSE. Do not narrate how the game has gone, who played well,
or whether it was close throughout — you did not see it. React to the number in
front of you.

No preamble, no question at the end, no emoji, plain ASCII, under 140
characters."""}],
        )
        line = _sms_clean(resp.content[0].text.strip())
        return line or plain
    except Exception as e:
        print(f"scorewatch: draft failed: {type(e).__name__}: {e}")
        return plain


def _plain(game: dict, team: dict, reason: str) -> str:
    line = sports.describe(game)
    if reason == "final":
        return f"Final: {line}"
    return line


def run_score_alerts() -> None:
    """One pass over every team someone asked live texts for. Never raises."""
    from sms_util import send_sms
    try:
        profiles = [(p, prof) for p, prof in get_all_profiles() if live_teams(prof)]
    except Exception as e:
        print(f"scorewatch: could not load profiles: {type(e).__name__}: {e}")
        return
    if not profiles:
        return

    wanted = {t["league"] for _p, prof in profiles for t in live_teams(prof)}
    boards: dict[str, list[dict]] = {}
    for league in wanted:
        ttl = sports.LIVE_POLL_SECONDS if league in _live_leagues else sports.IDLE_POLL_SECONDS
        boards[league] = sports.scoreboard(league, ttl=ttl)
    _live_leagues.clear()
    for league, games in boards.items():
        if any(g["state"] == "in" for g in games):
            _live_leagues.add(league)

    sent = 0
    for phone, profile in profiles:
        for team in live_teams(profile):
            try:
                game = sports._game_for(team, boards.get(team["league"], []))
                if not game or game["state"] == "pre":
                    continue
                mode = sports.live_mode(team)

                def remember(texted: bool) -> None:
                    """Move the baseline to what they now know.

                    Runs on every path, texted or not: the next comparison is
                    against what the user was last told, so a moment we chose
                    to stay quiet about must still count as known."""
                    record_game_alert(phone, game["id"], game["home"]["score"],
                                      game["away"]["score"], sports.leader(game),
                                      game["state"], sent=texted)

                prev = get_game_alert(phone, game["id"])
                reason = sports.alert_reason(prev, game, mode)
                # The cap never swallows the final. A game wild enough to spend
                # the budget is exactly the one whose result they want, and
                # ending on a mid-game score with no result reads as Palmer
                # losing interest.
                capped = (reason != "final"
                          and (prev or {}).get("alert_count", 0) >= sports.alert_cap(mode))
                if not reason or capped:
                    remember(texted=False)
                    continue
                # Only a text that actually went out counts against the cap or
                # consumes the moment; a Twilio failure leaves it to retry.
                delivered = bool(send_sms(phone, _draft(phone, game, team, reason, prev)))
                sent += delivered
                remember(texted=delivered)
            except Exception as e:
                print(f"scorewatch: {team.get('abbrev')} for {phone} failed: "
                      f"{type(e).__name__}: {e}")
    print(f"scorewatch: {len(profiles)} follower(s), leagues live={sorted(_live_leagues)}, "
          f"sent {sent}")
