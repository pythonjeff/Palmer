"""Live scores, and the restraint that keeps them from being a pager.

Palmer spends most of its code rationing proactive texts. A scoring feed runs
against all of it: an NFL game has six to ten scoring plays, and two followed
teams on a Sunday is twenty texts in an afternoon. So the interesting tests here
are the ones about what does NOT get sent.

Also records two things learned by measuring rather than reading docs:
ESPN's `site.api` scoreboard 403s from a datacenter (verified from the dyno, so
it is ESPN blocking Heroku, not a sandbox), and team names are ambiguous in a
way show titles are not — "Cardinals" is two teams in two sports.

All offline.
"""
from contextlib import contextmanager
from unittest.mock import patch

import db
import sports


def _game(home=0, away=0, state="in", period=4, clock=200, gid="1"):
    return {"id": gid, "league": "nfl", "short": "CIN @ PHI", "state": state,
            "detail": "Q4 3:20", "period": period, "clock": clock,
            "home": {"abbrev": "PHI", "name": "Philadelphia Eagles", "score": home},
            "away": {"abbrev": "CIN", "name": "Cincinnati Bengals", "score": away}}


def _told(home, away, leader, state="in"):
    return {"home_score": home, "away_score": away, "leader": leader, "state": state}


@contextmanager
def _run(game, prev=None, alert_count=0, delivered=True):
    """Run one pass of the job over a single followed team, offline.

    Yields a record of what was texted and what was written back, so tests can
    assert on behaviour instead of on the source of the function."""
    import scorewatch
    rec = {"sms": [], "saved": []}
    if prev is not None:
        prev = {**prev, "alert_count": alert_count}
    team = {"league": "nfl", "abbrev": "PHI", "name": "Philadelphia Eagles"}
    profile = {"followed_teams": [team]}
    with patch.object(scorewatch, "get_all_profiles", return_value=[("+1", profile)]), \
         patch.object(sports, "scoreboard", return_value=[game]), \
         patch.object(scorewatch, "get_game_alert", return_value=prev), \
         patch.object(scorewatch, "_draft", return_value="line"), \
         patch.object(scorewatch, "record_game_alert",
                      side_effect=lambda p, g, h, a, l, st, sent: rec["saved"].append((h, a, sent))), \
         patch("sms_util.send_sms", side_effect=lambda p, t, **k: rec["sms"].append(t) or delivered):
        scorewatch.run_score_alerts()
    yield rec


class TestMostOfAGameIsSilent:
    """The default answer is no. Three moments are exceptions."""

    def test_the_first_sighting_is_a_baseline_not_news(self):
        assert sports.alert_reason(None, _game(7, 0)) is None

    def test_a_routine_score_says_nothing(self):
        """A touchdown in the second quarter of a blowout is the case that would
        make this a pager."""
        assert sports.alert_reason(_told(21, 0, "home"),
                                   _game(28, 0, period=2, clock=600)) is None

    def test_no_change_says_nothing(self):
        assert sports.alert_reason(_told(14, 10, "home"), _game(14, 10)) is None

    def test_a_game_not_started_says_nothing(self):
        assert sports.alert_reason(None, _game(0, 0, state="pre")) is None

    def test_a_final_is_announced_once(self):
        assert sports.alert_reason(_told(14, 10, "home"), _game(14, 10, state="post")) == "final"
        assert sports.alert_reason(_told(14, 10, "home", state="post"),
                                   _game(14, 10, state="post")) is None


class TestTheThreeMomentsThatEarnATex:
    def test_the_lead_changing_hands(self):
        assert sports.alert_reason(_told(14, 10, "home"), _game(14, 17)) == "lead"

    def test_a_score_inside_the_last_five_minutes(self):
        assert sports.alert_reason(_told(14, 10, "home"), _game(21, 10)) == "late"

    def test_late_needs_both_a_score_and_the_clock(self):
        """The clock alone is not an event — nothing happened."""
        assert sports.alert_reason(_told(14, 10, "home"), _game(14, 10, clock=30)) is None

    def test_early_in_the_game_the_clock_does_not_count_as_late(self):
        assert not sports._is_late(_game(period=1, clock=60))

    def test_a_tie_has_no_leader(self):
        assert sports.leader(_game(10, 10)) is None


class TestTheClosingStretchMeansDifferentThingsPerSport:
    """`_is_late` compared a countdown against five minutes. Baseball has no
    clock at all (`clock` is always 0) and soccer's counts UP, so late alerts
    were silently dead for two of six leagues — including the one a real user
    follows."""

    def _late(self, league, period, clock):
        return sports._is_late({"league": league, "period": period, "clock": clock})

    def test_football_needs_both_the_period_and_the_clock(self):
        assert self._late("nfl", 4, 200)
        assert not self._late("nfl", 4, 720), "twelve minutes left is not the closing stretch"
        assert not self._late("nfl", 2, 200), "a low clock in Q2 is just halftime approaching"

    def test_overtime_counts(self):
        assert self._late("nfl", 5, 120)
        assert self._late("mlb", 11, 0), "extra innings"

    def test_baseball_has_no_clock_to_read(self):
        assert self._late("mlb", 9, 0)
        assert not self._late("mlb", 4, 0)

    def test_soccer_needs_a_floor_because_its_clock_counts_up(self):
        assert self._late("mls", 2, 5100), "85th minute"
        assert not self._late("mls", 2, 3000), "the 50th minute is not the closing stretch"


    def test_hockey_ends_in_the_third(self):
        assert self._late("nhl", 3, 180)
        assert not self._late("nhl", 2, 180)

    def test_an_unknown_league_assumes_four_periods(self):
        assert not self._late("quidditch", 1, 10)


class TestAmbiguousTeamNames:
    """"Cardinals" is two teams in two sports and "Rangers" is two. Guessing
    signs someone up for alerts about the wrong team in the wrong season."""

    TEAMS = {
        "nfl": [{"league": "nfl", "abbrev": "ARI", "name": "Arizona Cardinals",
                 "_match": {"arizona cardinals", "cardinals", "arizona", "ari"}},
                {"league": "nfl", "abbrev": "PHI", "name": "Philadelphia Eagles",
                 "_match": {"philadelphia eagles", "eagles", "philadelphia", "phi"}}],
        "mlb": [{"league": "mlb", "abbrev": "STL", "name": "St. Louis Cardinals",
                 "_match": {"st. louis cardinals", "cardinals", "st. louis", "stl"}}],
    }

    def _find(self, q):
        with patch.object(sports, "LEAGUES", {"nfl": "x", "mlb": "y"}), \
             patch.object(sports, "_teams", side_effect=lambda lg: self.TEAMS[lg]):
            return sports.find_teams(q)

    def test_an_ambiguous_name_returns_every_match(self):
        assert len(self._find("cardinals")) == 2

    def test_an_unambiguous_name_returns_one(self):
        assert [t["abbrev"] for t in self._find("eagles")] == ["PHI"]

    def test_a_fuller_name_disambiguates(self):
        assert [t["abbrev"] for t in self._find("st. louis cardinals")] == ["STL"]

    def test_nonsense_matches_nothing(self):
        assert self._find("asdfqwer") == []

    def test_the_dispatch_asks_rather_than_picking(self):
        import inspect
        import agent
        block = inspect.getsource(agent.get_reply).split('"follow_team"')[1].split("elif b.name")[0]
        assert "Do NOT pick one yourself" in block
        assert "matches more than one team" in block


class TestUnfollowSurvivesTheWrongKey:
    """Observed live: asked to "stop the eagles score texts", the model passed
    name=Eagles, carrying the key over from follow_team. A dispatch reading only
    text_match would have unfollowed nothing while reporting success."""

    def test_both_keys_are_accepted(self):
        import inspect
        import agent
        src = inspect.getsource(agent.get_reply)
        for tool in ('"unfollow_team"', '"unfollow_show"'):
            block = src.split(tool)[1].split("elif b.name")[0]
            assert 'b.input.get("name")' in block, f"{tool} ignores the key the model sends"


class TestTheCapIsTheBackstop:
    def test_a_wild_game_still_stops(self):
        assert sports.MAX_ALERTS_PER_GAME <= 4

    def test_following_is_capped(self):
        assert sports.FOLLOW_MAX <= 4

    def test_the_job_honours_the_cap(self):
        import inspect
        import scorewatch
        src = inspect.getsource(scorewatch.run_score_alerts)
        assert "MAX_ALERTS_PER_GAME" in src

    def test_a_suppressed_alert_still_updates_what_they_know(self):
        """Otherwise the next comparison is against a score they were never
        told, and the moment after a suppression reads as a bigger event than
        it was.

        Driven through the job rather than grepping its source — two earlier
        versions of this test pinned the shape of the calls and broke on a
        refactor that preserved the behaviour exactly."""
        routine = _game(28, 0, period=2, clock=600)      # earns no text
        with _run(routine, prev=_told(21, 0, "home")) as rec:
            assert rec["sms"] == [], "a routine score should stay quiet"
            assert rec["saved"][-1][:2] == (28, 0), "but the baseline must move"
            assert rec["saved"][-1][2] is False, "a silent update is not an alert"


class TestPollingIsTwoSpeed:
    def test_an_idle_league_is_checked_far_less_often(self):
        assert sports.IDLE_POLL_SECONDS >= 5 * sports.LIVE_POLL_SECONDS

    def test_a_live_league_is_checked_often_enough_to_catch_a_lead_change(self):
        assert sports.LIVE_POLL_SECONDS <= 120

    def test_the_board_is_cached_and_shared(self):
        """Two users following the same league cost one fetch."""
        sports._clear_cache()
        calls = []
        with patch.object(sports, "_get", side_effect=lambda u: calls.append(u) or {"events": []}):
            sports.scoreboard("nfl")
            sports.scoreboard("nfl")
        assert len(calls) == 1

    def test_an_unknown_league_makes_no_call(self):
        with patch.object(sports, "_get") as g:
            assert sports.scoreboard("quidditch") == []
        g.assert_not_called()


class TestParsingAndSafety:
    def test_a_game_parses_into_the_shape_the_job_expects(self):
        ev = {"id": 7, "shortName": "CIN @ PHI", "competitions": [{
            "status": {"period": 4, "clock": 120.0,
                       "type": {"state": "in", "shortDetail": "Q4 2:00"}},
            "competitors": [
                {"homeAway": "home", "score": "21", "team": {"abbreviation": "PHI",
                                                             "displayName": "Philadelphia Eagles"}},
                {"homeAway": "away", "score": "17", "team": {"abbreviation": "CIN",
                                                             "displayName": "Cincinnati Bengals"}}]}]}
        g = sports._parse_game(ev, "nfl")
        assert g["home"]["score"] == 21 and g["away"]["score"] == 17
        assert g["state"] == "in" and g["id"] == "7"

    def test_a_malformed_event_is_dropped_not_raised(self):
        assert sports._parse_game({"id": 1, "competitions": [{}]}, "nfl") is None

    def test_a_missing_score_reads_as_zero_rather_than_crashing(self):
        ev = {"id": 1, "competitions": [{"status": {"type": {"state": "pre"}}, "competitors": [
            {"homeAway": "home", "score": None, "team": {"abbreviation": "A"}},
            {"homeAway": "away", "score": "", "team": {"abbreviation": "B"}}]}]}
        assert sports._parse_game(ev, "nfl")["home"]["score"] == 0

    def test_a_dead_upstream_returns_no_games(self):
        sports._clear_cache()
        with patch.object(sports, "_get", return_value=None):
            assert sports.scoreboard("nfl") == []

    def test_the_job_never_raises(self):
        import scorewatch
        with patch("scorewatch.get_all_profiles", side_effect=RuntimeError("db down")):
            scorewatch.run_score_alerts()

    def test_describe_reads_like_a_person_said_it(self):
        assert sports.describe(_game(21, 17)) == "CIN 17, PHI 21 - Q4 3:20"
        assert "at" in sports.describe(_game(0, 0, state="pre"))


class TestStoredState:
    def _fresh(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "_DB_PATH", tmp_path / "g.db")
        db.init_db()

    def test_a_sent_alert_counts_and_a_silent_update_does_not(self, tmp_path, monkeypatch):
        self._fresh(tmp_path, monkeypatch)
        db.record_game_alert("+1", "9", 7, 0, "home", "in", sent=True)
        db.record_game_alert("+1", "9", 14, 0, "home", "in", sent=False)
        row = db.get_game_alert("+1", "9")
        assert row["alert_count"] == 1, "only texts count against the cap"
        assert row["home_score"] == 14, "but the baseline still moves"

    def test_state_is_per_user(self, tmp_path, monkeypatch):
        self._fresh(tmp_path, monkeypatch)
        db.record_game_alert("+1", "9", 7, 0, "home", "in", sent=True)
        assert db.get_game_alert("+2", "9") is None


class TestAFailedFetchIsNotAnAnswer:
    """Caching a failure is worse than not caching: the empty result is served
    for the whole TTL, and for `_teams` that TTL is the life of the dyno."""

    def test_a_blip_does_not_erase_every_team_until_the_next_deploy(self):
        sports._clear_cache()
        with patch.object(sports, "_get", return_value=None):
            assert sports._teams("nfl") == []
        assert "nfl" not in sports._team_cache, "a failure was cached forever"
        with patch.object(sports, "_get", return_value={"sports": [{"leagues": [{"teams": [
                {"team": {"abbreviation": "PHI", "displayName": "Philadelphia Eagles"}}]}]}]}):
            assert [t["abbrev"] for t in sports._teams("nfl")] == ["PHI"]

    def test_a_blip_mid_game_keeps_the_last_board_rather_than_going_dark(self):
        """`scorewatch` recomputes which leagues are live from the board it gets
        back, so an empty one demotes a live league to the 15-minute poll."""
        sports._clear_cache()
        live = {"events": [{"id": "1", "competitions": [{
            "status": {"type": {"state": "in"}},
            "competitors": [{"homeAway": "home", "score": "7", "team": {"abbreviation": "PHI"}},
                            {"homeAway": "away", "score": "0", "team": {"abbreviation": "CIN"}}]}]}]}
        with patch.object(sports, "_get", return_value=live):
            assert len(sports.scoreboard("nfl")) == 1
        with patch.object(sports, "_get", return_value=None):
            assert len(sports.scoreboard("nfl", ttl=0)) == 1, "went dark on one bad request"


class TestFollowingATeamMidWeek:
    def test_a_game_that_ended_before_they_followed_is_not_news(self):
        """ESPN's NFL board carries the whole current week, so a Tuesday follow
        used to open with a final score from Sunday."""
        assert sports.alert_reason(None, _game(21, 17, state="post")) is None

    def test_but_a_game_they_were_watching_still_gets_its_final(self):
        assert sports.alert_reason(_told(21, 17, "home"), _game(21, 17, state="post")) == "final"


class TestATieIsNotALeadChange:
    def test_an_equalising_score_reads_as_tied(self):
        assert sports.alert_reason(_told(21, 14, "home"), _game(21, 21)) == "tied"

    def test_a_go_ahead_score_after_a_tie_is_still_a_lead_change(self):
        assert sports.alert_reason(_told(21, 21, None), _game(28, 21)) == "lead"

    def test_the_drafter_has_a_cue_for_it(self):
        import inspect
        import scorewatch
        assert '"tied"' in inspect.getsource(scorewatch._draft)


class TestTheCapNeverSwallowsTheResult:
    def test_a_mid_game_score_is_suppressed_once_the_cap_is_hit(self):
        with _run(_game(21, 24), prev=_told(21, 17, "home"),
                  alert_count=sports.MAX_ALERTS_PER_GAME) as rec:
            assert rec["sms"] == []

    def test_but_the_result_still_arrives(self):
        """A game wild enough to spend four alerts is exactly the one whose
        result they want; ending on a mid-game score reads as Palmer losing
        interest."""
        with _run(_game(21, 24, state="post"), prev=_told(21, 17, "home"),
                  alert_count=sports.MAX_ALERTS_PER_GAME) as rec:
            assert rec["sms"], "the final was swallowed by the cap"

    def test_an_undelivered_text_does_not_consume_the_moment(self):
        """Twilio failing must not burn an alert slot — otherwise the moment is
        counted against the cap without the user ever seeing it."""
        with _run(_game(14, 17), prev=_told(14, 10, "home"), delivered=False) as rec:
            assert rec["sms"], "it should have tried"
            assert rec["saved"][-1][2] is False, "a text that never arrived is not an alert"

    def test_a_delivered_text_does_count(self):
        with _run(_game(14, 17), prev=_told(14, 10, "home")) as rec:
            assert rec["saved"][-1][2] is True
