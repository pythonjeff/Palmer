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
        assert sports._leader(_game(10, 10)) is None


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
        it was."""
        import inspect
        import scorewatch
        src = inspect.getsource(scorewatch.run_score_alerts)
        assert src.count("sent=False") >= 2


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
