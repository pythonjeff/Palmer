"""Scores on a schedule, not a pager.

Palmer used to poll ESPN every two minutes during a game and text on lead
changes, late scores and the final. That is gone: a followed team now surfaces
in the morning update (yesterday's result, tonight's game), the Scores section
of the page and the paced check-in, all through `team_day`. The
tests here pin that shape, plus two things learned by measuring rather than
reading docs: ESPN's `site.api` scoreboard 403s from a datacenter, and team
names are ambiguous in a way show titles are not — "Cardinals" is two teams.

All offline.
"""
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

import db

import sports


def _game(home=0, away=0, state="in", period=4, clock=200, gid="1", detail="Q4 3:20", league="nfl"):
    return {"id": gid, "league": league, "short": "CIN @ PHI", "state": state,
            "detail": detail, "period": period, "clock": clock, "date": "2026-09-03",
            "home": {"abbrev": "PHI", "name": "Philadelphia Eagles", "score": home},
            "away": {"abbrev": "CIN", "name": "Cincinnati Bengals", "score": away}}


TEAM = {"league": "nfl", "abbrev": "PHI", "name": "Philadelphia Eagles"}
TODAY = date(2026, 9, 4)


class TestTeamDayIsTheOneRead:
    """The morning, the page and the check-in all ask this one question."""

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

    def test_the_poll_is_two_speed_and_gated_on_the_ask(self):
        """The two-speed live poll is back for the people who opted in;
        scorewatch.live_teams is what keeps everyone else out of it."""
        import scorewatch
        assert scorewatch.live_teams({"followed_teams": [TEAM]}) == []
        assert scorewatch.live_teams({"followed_teams": [dict(TEAM, live="key")]}) == [dict(TEAM, live="key")]


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


class TestLiveTextsAreOptIn:
    """Following a team puts it in the morning and on the page. Live texts
    during a game are a separate ask with a level, and the dispatch offers
    them rather than assuming."""

    def test_a_team_with_no_level_is_off(self):
        assert sports.live_mode({"abbrev": "PHI"}) == "off"
        assert sports.live_mode({"abbrev": "PHI", "live": "nonsense"}) == "off"
        assert sports.live_mode({"abbrev": "PHI", "live": "all"}) == "all"

    def test_the_dispatch_offers_when_no_level_was_given(self):
        import inspect
        import agent
        block = inspect.getsource(agent.get_reply).split('"follow_team"')[1].split("elif b.name")[0]
        assert "No live texts are set" in block and "Offer them in one clause" in block
        assert "Do not promise live texts they have not chosen" in block

    def test_the_tool_descriptions_agree(self):
        from tools_def import TOOLS
        follow = next(t for t in TOOLS if t["name"] == "follow_team")
        assert "OPT-IN" in follow["description"]
        assert follow["input_schema"]["properties"]["live"]["enum"] == ["off", "key", "all"]
        setter = next(t for t in TOOLS if t["name"] == "set_score_updates")
        assert "without unfollowing" in setter["description"]
        assert setter["input_schema"]["required"] == ["mode"]

    def test_the_offer_block_fires_once_for_a_named_team(self):
        import agent
        import userprofile
        base = {"intro_sent": True, "sports_teams": ["Cardinals fan"], "name": "Jeff", "city": "Kirkwood, MO"}
        with patch.object(agent, "get_profile", return_value=dict(base)), \
             patch.object(agent, "get_history", return_value=[]):
            assert "LIVE SCORES OFFER" in agent._build_system("+1")
        with patch.object(agent, "get_profile", return_value=dict(base, score_offer_sent=True)), \
             patch.object(agent, "get_history", return_value=[]):
            assert "LIVE SCORES OFFER" not in agent._build_system("+1")
        with patch.object(agent, "get_profile", return_value=dict(base, followed_teams=[{"abbrev": "STL"}])), \
             patch.object(agent, "get_history", return_value=[]):
            assert "LIVE SCORES OFFER" not in agent._build_system("+1")
        # Consumed after the turn under the same condition, answered or not.
        import inspect
        src = inspect.getsource(userprofile._update_profile)
        assert '"score_offer_sent": True' in src

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


# ---- the poller, for the people who asked ------------------------------------

def _told(home, away, leader, state="in"):
    return {"home_score": home, "away_score": away, "leader": leader, "state": state}


@contextmanager
def _run(game, prev=None, alert_count=0, delivered=True, live="key"):
    """Run one pass of the job over a single followed team, offline.

    Yields a record of what was texted and what was written back, so tests can
    assert on behaviour instead of on the source of the function."""
    import scorewatch
    rec = {"sms": [], "saved": []}
    if prev is not None:
        prev = {**prev, "alert_count": alert_count}
    team = {"league": "nfl", "abbrev": "PHI", "name": "Philadelphia Eagles"}
    if live:
        team["live"] = live
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


class TestFollowingAloneSendsNothingLive:
    def test_a_team_with_no_level_is_not_polled_or_texted(self):
        with patch.object(sports, "scoreboard") as board:
            with _run(_game(14, 17), prev=_told(14, 10, "home"), live=None) as rec:
                assert rec["sms"] == []
        board.assert_not_called()

    def test_off_is_the_same_as_absent(self):
        with _run(_game(14, 17), prev=_told(14, 10, "home"), live="off") as rec:
            assert rec["sms"] == []


class TestMostOfAGameIsSilent:
    """The default answer is no. Three moments are exceptions at the key level."""

    def test_the_first_sighting_is_a_baseline_not_news(self):
        assert sports.alert_reason(None, _game(7, 0)) is None
        assert sports.alert_reason(None, _game(7, 0), "all") is None

    def test_a_routine_score_says_nothing(self):
        """A touchdown in the second quarter of a blowout is the case that would
        make this a pager."""
        assert sports.alert_reason(_told(21, 0, "home"),
                                   _game(28, 0, period=2, clock=600)) is None

    def test_no_change_says_nothing(self):
        assert sports.alert_reason(_told(14, 10, "home"), _game(14, 10)) is None
        assert sports.alert_reason(_told(14, 10, "home"), _game(14, 10), "all") is None

    def test_a_game_not_started_says_nothing(self):
        assert sports.alert_reason(None, _game(0, 0, state="pre")) is None

    def test_a_final_is_announced_once(self):
        assert sports.alert_reason(_told(14, 10, "home"), _game(14, 10, state="post")) == "final"
        assert sports.alert_reason(_told(14, 10, "home", state="post"),
                                   _game(14, 10, state="post")) is None


class TestTheThreeMomentsThatEarnATextAtKey:
    def test_the_lead_changing_hands(self):
        assert sports.alert_reason(_told(14, 10, "home"), _game(14, 17)) == "lead"

    def test_a_score_inside_the_last_five_minutes(self):
        assert sports.alert_reason(_told(14, 10, "home"), _game(21, 10)) == "late"

    def test_late_needs_both_a_score_and_the_clock(self):
        assert sports.alert_reason(_told(14, 10, "home"), _game(14, 10, clock=30)) is None

    def test_an_equalising_score_reads_as_tied(self):
        assert sports.alert_reason(_told(21, 14, "home"), _game(21, 21)) == "tied"

    def test_a_go_ahead_score_after_a_tie_is_still_a_lead_change(self):
        assert sports.alert_reason(_told(21, 21, None), _game(28, 21)) == "lead"


class TestEveryScoreIsTheSecondLevel:
    def test_a_routine_score_earns_a_text_at_all(self):
        routine = _game(28, 0, period=2, clock=600)
        assert sports.alert_reason(_told(21, 0, "home"), routine, "all") == "score"

    def test_a_lead_change_is_still_a_lead_change(self):
        assert sports.alert_reason(_told(14, 10, "home"), _game(14, 17), "all") == "lead"

    def test_the_nba_falls_back_to_key_moments(self):
        """A basket lands every thirty seconds; nobody wants that text."""
        routine = _game(58, 40, period=2, clock=600, league="nba")
        assert sports.alert_reason(_told(56, 40, "home"), routine, "all") is None
        assert sports.alert_reason(_told(56, 57, "away"), _game(58, 57, period=2, clock=600, league="nba"), "all") == "lead"

    def test_the_cap_is_a_backstop_not_a_budget(self):
        assert sports.alert_cap("all") > sports.alert_cap("key")
        assert sports.alert_cap("key") == sports.MAX_ALERTS_PER_GAME <= 4

    def test_the_drafter_knows_who_scored(self):
        assert sports.scorer(_told(14, 10, "home"), _game(21, 10)) == "home"
        assert sports.scorer(_told(14, 10, "home"), _game(14, 17)) == "away"
        assert sports.scorer(None, _game(14, 17)) is None
        import inspect
        import scorewatch
        assert "just scored" in inspect.getsource(scorewatch._draft)

    def test_the_job_texts_every_score_only_for_all(self):
        routine = _game(28, 0, period=2, clock=600)
        with _run(routine, prev=_told(21, 0, "home"), live="key") as rec:
            assert rec["sms"] == []
        with _run(routine, prev=_told(21, 0, "home"), live="all") as rec:
            assert rec["sms"] == ["line"]


class TestTheClosingStretchMeansDifferentThingsPerSport:
    def _late(self, league, period, clock):
        return sports._is_late({"league": league, "period": period, "clock": clock})

    def test_football_needs_both_the_period_and_the_clock(self):
        assert self._late("nfl", 4, 200)
        assert not self._late("nfl", 4, 720)
        assert not self._late("nfl", 2, 200)

    def test_overtime_counts(self):
        assert self._late("nfl", 5, 120)
        assert self._late("mlb", 11, 0)

    def test_baseball_has_no_clock_to_read(self):
        assert self._late("mlb", 9, 0)
        assert not self._late("mlb", 4, 0)

    def test_soccer_needs_a_floor_because_its_clock_counts_up(self):
        assert self._late("mls", 2, 5100)
        assert not self._late("mls", 2, 3000)

    def test_hockey_ends_in_the_third(self):
        assert self._late("nhl", 3, 180)
        assert not self._late("nhl", 2, 180)


class TestTheCapNeverSwallowsTheResult:
    def test_a_suppressed_alert_still_updates_what_they_know(self):
        routine = _game(28, 0, period=2, clock=600)
        with _run(routine, prev=_told(21, 0, "home")) as rec:
            assert rec["sms"] == []
            assert rec["saved"][-1][:2] == (28, 0)
            assert rec["saved"][-1][2] is False

    def test_a_mid_game_score_is_suppressed_once_the_cap_is_hit(self):
        with _run(_game(21, 24), prev=_told(21, 17, "home"),
                  alert_count=sports.MAX_ALERTS_PER_GAME) as rec:
            assert rec["sms"] == []

    def test_but_the_result_still_arrives(self):
        with _run(_game(21, 24, state="post"), prev=_told(21, 17, "home"),
                  alert_count=sports.MAX_ALERTS_PER_GAME) as rec:
            assert rec["sms"], "the final was swallowed by the cap"

    def test_an_undelivered_text_does_not_consume_the_moment(self):
        with _run(_game(14, 17), prev=_told(14, 10, "home"), delivered=False) as rec:
            assert rec["sms"]
            assert rec["saved"][-1][2] is False

    def test_a_delivered_text_does_count(self):
        with _run(_game(14, 17), prev=_told(14, 10, "home")) as rec:
            assert rec["saved"][-1][2] is True

    def test_a_game_that_ended_before_they_followed_is_not_news(self):
        assert sports.alert_reason(None, _game(21, 17, state="post")) is None


class TestPollingIsTwoSpeed:
    def test_an_idle_league_is_checked_far_less_often(self):
        assert sports.IDLE_POLL_SECONDS >= 5 * sports.LIVE_POLL_SECONDS

    def test_a_live_league_is_checked_often_enough_to_catch_a_lead_change(self):
        assert sports.LIVE_POLL_SECONDS <= 120

    def test_the_job_never_raises(self):
        import scorewatch
        with patch("scorewatch.get_all_profiles", side_effect=RuntimeError("db down")):
            scorewatch.run_score_alerts()


class TestStoredState:
    def _fresh(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "_DB_PATH", tmp_path / "g.db")
        db.init_db()

    def test_a_sent_alert_counts_and_a_silent_update_does_not(self, tmp_path, monkeypatch):
        self._fresh(tmp_path, monkeypatch)
        db.record_game_alert("+1", "9", 7, 0, "home", "in", sent=True)
        db.record_game_alert("+1", "9", 14, 0, "home", "in", sent=False)
        row = db.get_game_alert("+1", "9")
        assert row["alert_count"] == 1
        assert row["home_score"] == 14

    def test_state_is_per_user(self, tmp_path, monkeypatch):
        self._fresh(tmp_path, monkeypatch)
        db.record_game_alert("+1", "9", 7, 0, "home", "in", sent=True)
        assert db.get_game_alert("+2", "9") is None


class TestSetScoreUpdatesDispatch:
    def _block(self):
        import inspect
        import agent
        return inspect.getsource(agent.get_reply).split('"set_score_updates"')[1].split("elif b.name")[0]

    def test_it_changes_the_level_without_dropping_the_team(self):
        block = self._block()
        assert 'upsert_profile(phone_number, {"followed_teams": current})' in block
        assert "They still follow the team" in block

    def test_it_accepts_the_key_the_model_carries_over(self):
        assert 'b.input.get("name")' in self._block()
