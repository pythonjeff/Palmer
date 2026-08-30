"""The job that decides a game moment is worth interrupting someone for.

Palmer spends most of its code rationing proactive texts — `_is_duplicate_subject`,
`DAILY_ALERT_MAX`, the reaction pacing factor, the repetition guards. A live
scoring feed runs against all of it: an NFL game has six to ten scoring plays,
and two followed teams on a Sunday is twenty texts in an afternoon.

So this is the first thing in Palmer built to interrupt, and it is built to
interrupt rarely. `sports.alert_reason` allows three moments — the lead changing
hands, a score inside the last five minutes, and the final — and
`MAX_ALERTS_PER_GAME` is the backstop when a game is genuinely wild. Everything
else updates the stored state silently so the next comparison is honest.

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


def followed_teams(profile: dict) -> list[dict]:
    return [t for t in ((profile or {}).get("followed_teams") or []) if t.get("abbrev")]


def _draft(phone: str, game: dict, team: dict, reason: str) -> str:
    """The alert, in Palmer's voice. Falls back to the plain line."""
    plain = _plain(game, team, reason)
    try:
        from agent import _build_system
        from llm import client, SONNET_MODEL
        from smstext import _sms_clean
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
        side = sports.side_of(game, team["abbrev"])
        other = "away" if side == "home" else "home"
        mine, theirs = game[side]["score"], game[other]["score"]
        standing = ("ahead by" if mine > theirs else
                    "behind by" if mine < theirs else "level, tied at")
        margin = abs(mine - theirs) or mine
        resp = client.messages.create(
            model=SONNET_MODEL, max_tokens=90, system=_build_system(phone),
            messages=[{"role": "user", "content":
                       f"""Their team is {team['name']}, playing {game[other]['name']}. {cue.capitalize()}.

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
    """One pass over every followed team. Never raises."""
    from sms_util import send_sms
    try:
        profiles = [(p, prof) for p, prof in get_all_profiles() if followed_teams(prof)]
    except Exception as e:
        print(f"scorewatch: could not load profiles: {type(e).__name__}: {e}")
        return
    if not profiles:
        return

    wanted = {t["league"] for _p, prof in profiles for t in followed_teams(prof)}
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
        for team in followed_teams(profile):
            try:
                game = next((g for g in boards.get(team["league"], [])
                             if team["abbrev"] in (g["home"]["abbrev"], g["away"]["abbrev"])),
                            None)
                if not game or game["state"] == "pre":
                    continue
                def remember(texted: bool) -> None:
                    """Move the baseline to what they now know.

                    Runs on every path, texted or not: the next comparison is
                    against what the user was last told, so a moment we chose
                    to stay quiet about must still count as known."""
                    record_game_alert(phone, game["id"], game["home"]["score"],
                                      game["away"]["score"], sports.leader(game),
                                      game["state"], sent=texted)

                prev = get_game_alert(phone, game["id"])
                reason = sports.alert_reason(prev, game)
                # The cap never swallows the final. A game wild enough to spend
                # four alerts is exactly the one whose result they want, and
                # ending on a mid-game score with no result reads as Palmer
                # losing interest.
                capped = (reason != "final"
                          and (prev or {}).get("alert_count", 0) >= sports.MAX_ALERTS_PER_GAME)
                if not reason or capped:
                    remember(texted=False)
                    continue
                # Only a text that actually went out counts against the cap or
                # consumes the moment; a Twilio failure leaves it to retry.
                delivered = bool(send_sms(phone, _draft(phone, game, team, reason)))
                sent += delivered
                remember(texted=delivered)
            except Exception as e:
                print(f"scorewatch: {team.get('abbrev')} for {phone} failed: "
                      f"{type(e).__name__}: {e}")
    print(f"scorewatch: {len(profiles)} follower(s), leagues live={sorted(_live_leagues)}, "
          f"sent {sent}")
