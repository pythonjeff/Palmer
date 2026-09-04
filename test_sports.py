"""Scores on a schedule, not a pager.

Palmer used to poll ESPN every two minutes during a game and text on lead
changes, late scores and the final. That is gone: a followed team now surfaces
in the morning update (yesterday's result, tonight's game), the evening update
(how it went) and the Scores section of the page, all through `team_day`. The
tests here pin that shape, plus two things learned by measuring rather than
reading docs: ESPN's `site.api` scoreboard 403s from a datacenter, and team
names are ambiguous in a way show titles are not — "Cardinals" is two teams.

All offline.
"""
from datetime import date
from unittest.mock import patch

import sports


def _game(home=0, away=0, state="in", period=4, clock=200, gid="1", detail="Q4 3:20"):
    return {"id": gid, "league": "nfl", "short": "CIN @ PHI", "state": state,
            "detail": detail, "period": period, "clock": clock, "date": "2026-09-03",
            "home": {"abbrev": "PHI", "name": "Philadelphia Eagles", "score": home},
            "away": {"abbrev": "CIN", "name": "Cincinnati Bengals", "score": away}}


TEAM = {"league": "nfl", "abbrev": "PHI", "name": "Philadelphia Eagles"}
TODAY = date(2026, 9, 4)


class TestTeamDayIsTheOneRead:
    """The morning, the evening and the page all ask this one question."""

    def _day(self, boards):
        """boards: {iso day -> [games]}"""
        def _board(league, ttl=None, day=None):
            return boards.get(day.isoformat() if day else None, [])
        with patch.object(sports, "scoreboard", side_effect=_board):
            return sports.team_day(TEAM, TODAY)

    def test_yesterdays_final_and_todays_game(self):
        day = self._day({"2026-09-03": [_game(21, 17, state="post")],
                         "2026-09-04": [_game(0, 0, state="pre", detail="8:20 PM ET", gid="2")]})
        assert day["last"]["id"] == "1" and day["last"]["state"] == "post"
        assert day["today"]["id"] == "2" and day["today"]["state"] == "pre"

    def test_a_game_yesterday_that_did_not_finish_is_not_a_result(self):
        """A suspended or postponed game is not a result to report."""
        day = self._day({"2026-09-03": [_game(7, 3, state="in")]})
        assert day["last"] is None

    def test_nothing_on_either_day_is_both_none(self):
        day = self._day({})
        assert day == {"last": None, "today": None}

    def test_another_teams_game_is_not_theirs(self):
        other = _game(21, 17, state="post")
        other["home"]["abbrev"], other["away"]["abbrev"] = "DAL", "NYG"
        assert self._day({"2026-09-03": [other]})["last"] is None

    def test_the_board_is_asked_for_the_readers_day(self):
        asked = []
        with patch.object(sports, "scoreboard",
                          side_effect=lambda lg, ttl=None, day=None: asked.append(day) or []):
            sports.team_day(TEAM, TODAY)
        assert asked == [date(2026, 9, 3), TODAY]


class TestResultLineTakesTheTeamsSide:
    """The drafter is told whose side the reader is on and by how much, rather
    than left to infer it from "CIN 17, PHI 21"."""

    def test_a_win(self):
        assert sports.result_line(_game(21, 17, state="post"), TEAM) == "beat Cincinnati Bengals 21-17"

    def test_a_loss_keeps_their_score_first(self):
        assert sports.result_line(_game(14, 20, state="post"), TEAM) == "lost to Cincinnati Bengals 14-20"

    def test_a_draw(self):
        assert sports.result_line(_game(1, 1, state="post"), TEAM).startswith("drew")

    def test_in_progress_carries_the_clock(self):
        assert sports.result_line(_game(10, 7), TEAM) == "up 10-7 vs Cincinnati Bengals, Q4 3:20"
        assert sports.result_line(_game(7, 10), TEAM).startswith("down 7-10")

    def test_not_started_carries_the_time(self):
        line = sports.result_line(_game(0, 0, state="pre", detail="8:20 PM ET"), TEAM)
        assert line == "play Cincinnati Bengals, 8:20 PM ET"

    def test_the_away_team_is_read_from_its_own_side(self):
        cin = {"league": "nfl", "abbrev": "CIN", "name": "Cincinnati Bengals"}
        assert sports.result_line(_game(21, 17, state="post"), cin) == "lost to Philadelphia Eagles 17-21"


class TestScoreboardByDay:
    def test_a_day_is_asked_for_with_espns_dates_parameter(self):
        sports._clear_cache()
        urls = []
        with patch.object(sports, "_get", side_effect=lambda u: urls.append(u) or {"events": []}):
            sports.scoreboard("nfl", day=date(2026, 9, 3))
        assert urls[0].endswith("?dates=20260903")

    def test_without_a_day_the_url_is_bare(self):
        sports._clear_cache()
        urls = []
        with patch.object(sports, "_get", side_effect=lambda u: urls.append(u) or {"events": []}):
            sports.scoreboard("nfl")
        assert "dates=" not in urls[0]

    def test_two_days_are_two_cache_entries(self):
        sports._clear_cache()
        calls = []
        with patch.object(sports, "_get", side_effect=lambda u: calls.append(u) or {"events": []}):
            sports.scoreboard("nfl", day=date(2026, 9, 3))
            sports.scoreboard("nfl", day=date(2026, 9, 4))
            sports.scoreboard("nfl", day=date(2026, 9, 3))
        assert len(calls) == 2, "the same day twice must be one fetch"

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

    def test_there_is_one_polling_speed_and_it_is_not_a_pager(self):
        """The two-speed live poll went with the alerts. Nothing in this
        module is tuned to catch a moment inside a game any more."""
        assert not hasattr(sports, "LIVE_POLL_SECONDS")
        assert not hasattr(sports, "alert_reason")
        assert not hasattr(sports, "MAX_ALERTS_PER_GAME")


class TestAmbiguousTeamNames:
    """"Cardinals" is two teams in two sports and "Rangers" is two. Guessing
    signs someone up for updates about the wrong team in the wrong season."""

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


class TestFollowingIsNotSigningUpForLiveTexts:
    def test_the_dispatch_says_so_in_as_many_words(self):
        """The model confirms from the tool result, so the tool result has to
        say what they actually get — and that it is not live."""
        import inspect
        import agent
        block = inspect.getsource(agent.get_reply).split('"follow_team"')[1].split("elif b.name")[0]
        assert "NO live score texts" in block
        assert "morning" in block and "evening" in block

    def test_the_tool_description_agrees(self):
        from tools_def import TOOLS
        d = next(t for t in TOOLS if t["name"] == "follow_team")["description"]
        assert "NO live texts" in d
        assert "lead changes" not in d

    def test_a_follow_expires_the_page_section(self):
        """Otherwise the Scores card stays empty for up to ten minutes after
        they followed, which reads as it not having worked."""
        import inspect
        import agent
        src = inspect.getsource(agent.get_reply)
        for tool in ('"follow_team"', '"unfollow_team"'):
            block = src.split(tool)[1].split("elif b.name")[0]
            assert '("scores",)' in block, f"{tool} leaves the cached Scores rows in place"

    def test_following_is_capped(self):
        assert sports.FOLLOW_MAX <= 4


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


class TestParsingAndSafety:
    def test_a_game_parses_into_the_shape_the_job_expects(self):
        ev = {"id": 7, "shortName": "CIN @ PHI", "date": "2026-09-04T00:20Z", "competitions": [{
            "status": {"period": 4, "clock": 120.0,
                       "type": {"state": "in", "shortDetail": "Q4 2:00"}},
            "competitors": [
                {"homeAway": "home", "score": "21", "team": {"abbreviation": "PHI",
                                                             "displayName": "Philadelphia Eagles"}},
                {"homeAway": "away", "score": "17", "team": {"abbreviation": "CIN",
                                                             "displayName": "Cincinnati Bengals"}}]}]}
        g = sports._parse_game(ev, "nfl")
        assert g["home"]["score"] == 21 and g["away"]["score"] == 17
        assert g["state"] == "in" and g["id"] == "7" and g["date"] == "2026-09-04"

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

    def test_describe_reads_like_a_person_said_it(self):
        assert sports.describe(_game(21, 17)) == "CIN 17, PHI 21 - Q4 3:20"
        assert "at" in sports.describe(_game(0, 0, state="pre"))

    def test_a_tie_has_no_leader(self):
        assert sports.leader(_game(10, 10)) is None


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

    def test_a_blip_keeps_the_last_board_rather_than_going_dark(self):
        """An empty board reads as "no game today" to the morning and the page."""
        sports._clear_cache()
        live = {"events": [{"id": "1", "competitions": [{
            "status": {"type": {"state": "in"}},
            "competitors": [{"homeAway": "home", "score": "7", "team": {"abbreviation": "PHI"}},
                            {"homeAway": "away", "score": "0", "team": {"abbreviation": "CIN"}}]}]}]}
        with patch.object(sports, "_get", return_value=live):
            assert len(sports.scoreboard("nfl")) == 1
        with patch.object(sports, "_get", return_value=None):
            assert len(sports.scoreboard("nfl", ttl=0)) == 1, "went dark on one bad request"


class TestTheStoredAlertStateIsGone:
    def test_db_no_longer_carries_game_alert_state(self):
        import db
        assert not hasattr(db, "record_game_alert")
        assert not hasattr(db, "get_game_alert")
