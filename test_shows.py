"""Followed shows: episode-level tracking for series someone actually watches.

Different in kind from the `screen` rows beside them, and the distinction is the
feature. Those are discovery — what is new to anyone, ranked by popularity,
identical for every user. These exist only because someone named the show.

Three rules came from the spec and each has a test that would catch its
reversal:

  * a show earns a row in the WEEK its episode lands, and is silent between
    seasons — not a permanent countdown;
  * it lives on the page and reaches the morning TEXT only if asked, because a
    weekly "new episode!!" nobody requested is the drumbeat this product keeps
    having to remove;
  * it displaces generic film discovery rather than adding to the row count — a
    show you watch is worth more than a film picked for you.

All offline.
"""
from datetime import date, timedelta
from unittest.mock import patch

import morning
import opening
import shows


TODAY = date(2026, 8, 29)


def _tv(next_days=None, last_days=None, name="Furious"):
    """A TMDB /tv/{id} payload with episodes at offsets from TODAY."""
    out = {"name": name, "next_episode_to_air": None, "last_episode_to_air": None}
    if next_days is not None:
        out["next_episode_to_air"] = {
            "air_date": (TODAY + timedelta(days=next_days)).isoformat(),
            "season_number": 1, "episode_number": 8, "name": "Hart Island"}
    if last_days is not None:
        out["last_episode_to_air"] = {
            "air_date": (TODAY + timedelta(days=last_days)).isoformat(),
            "season_number": 1, "episode_number": 7, "name": "Previously"}
    return out


class TestARowOnlyInItsWeek:
    def setup_method(self):
        shows._clear_cache()

    def _ep(self, payload):
        with patch.object(shows, "_key", return_value="k"), \
             patch.object(shows, "_get", return_value=payload):
            return shows.next_episode(1, TODAY)

    def test_an_episode_landing_this_week_earns_a_row(self):
        ep = self._ep(_tv(next_days=2))
        assert ep and ep["state"] == "upcoming" and ep["episode"] == 8

    def test_one_that_just_dropped_still_counts_as_news(self):
        ep = self._ep(_tv(last_days=-1))
        assert ep and ep["state"] == "dropped"

    def test_a_show_between_seasons_produces_nothing(self):
        """Not a permanent countdown — the row exists for the week an episode
        is in play."""
        assert self._ep(_tv()) is None

    def test_an_episode_a_month_out_is_not_this_week(self):
        assert self._ep(_tv(next_days=30)) is None

    def test_last_week_is_history_not_news(self):
        assert self._ep(_tv(last_days=-9)) is None

    def test_upcoming_wins_over_an_older_drop(self):
        ep = self._ep(_tv(next_days=3, last_days=-1))
        assert ep["state"] == "upcoming"

    def test_two_users_watching_the_same_show_cost_one_lookup(self):
        """Cached by (show, day) and therefore shared, the same way the metro
        cache works for events."""
        calls = []

        def _get(path, **kw):
            calls.append(path)
            return _tv(next_days=2)

        with patch.object(shows, "_key", return_value="k"), \
             patch.object(shows, "_get", side_effect=_get):
            shows.next_episode(1, TODAY)
            shows.next_episode(1, TODAY)
        assert len(calls) == 1

    def test_no_key_means_no_call(self):
        with patch.object(shows, "_key", return_value=""), \
             patch.object(shows, "_get") as g:
            assert shows.next_episode(1, TODAY) is None
        g.assert_not_called()


class TestHowARowReads:
    def setup_method(self):
        shows._clear_cache()

    def _rows(self, payload):
        # Cleared per call: the (show, day) cache is doing its job, so calling
        # this twice in one test would otherwise return the first answer.
        shows._clear_cache()
        with patch.object(shows, "_key", return_value="k"), \
             patch.object(shows, "_get", return_value=payload):
            return shows.episode_rows({"shows": [{"id": 1, "name": "Furious"}]}, TODAY)

    def test_the_show_is_the_title_and_the_episode_is_the_detail(self):
        r = self._rows(_tv(next_days=2))[0]
        assert r["title"] == "Furious"
        assert r["subtitle"] == "S1E8 - Hart Island"
        assert r["kind"] == "episode"

    def test_when_reads_like_a_person_said_it(self):
        assert self._rows(_tv(next_days=0))[0]["when"] == "out today"
        assert self._rows(_tv(next_days=1))[0]["when"] == "tomorrow"
        assert self._rows(_tv(last_days=-1))[0]["when"] == "out now"

    def test_a_few_days_out_names_the_day(self):
        r = self._rows(_tv(next_days=3))[0]
        assert r["when"] == (TODAY + timedelta(days=3)).strftime("%A")

    def test_it_carries_a_date_so_it_expires_like_any_other_row(self):
        assert self._rows(_tv(next_days=2))[0]["date"]

    def test_a_broken_lookup_drops_that_show_only(self):
        prof = {"shows": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]}
        with patch.object(shows, "_key", return_value="k"), \
             patch.object(shows, "next_episode",
                          side_effect=[RuntimeError("tmdb down"), None]):
            assert shows.episode_rows(prof, TODAY) == []

    def test_no_followed_shows_is_not_an_error(self):
        assert shows.episode_rows({}, TODAY) == []


class TestEpisodesLeadAndDisplace:
    ROW = {"kind": "episode", "title": "Furious", "subtitle": "S1E8", "when": "Monday",
           "url": None, "source": "themoviedb.org", "date": None}

    def _snapshot(self, episodes):
        from timeutil import local_today
        opening._clear_caches()
        today = local_today("America/Chicago")
        opening._candidate_cache[(38.5, -90.5, opening._week_key(today))] = []
        opening._local_cache[(38.5, -90.5, today.isoformat())] = [
            {"kind": "event", "title": f"Gig {i}", "subtitle": "", "when": "",
             "url": None, "source": "", "date": None} for i in range(3)]
        opening._screen_cache[today.isoformat()] = [
            {"kind": "screen", "title": f"Film {i}", "subtitle": "", "when": "",
             "url": None, "source": ""} for i in range(4)]
        with patch("weather._geocode", return_value=(38.5, -90.5, "Kirkwood")), \
             patch("shows.episode_rows", return_value=episodes):
            return opening.opening_snapshot({"city": "Kirkwood, MO",
                                             "timezone": "America/Chicago"})

    def test_a_followed_show_leads(self):
        """The only row on the page anyone asked for by name."""
        rows = self._snapshot([self.ROW])
        assert rows[0]["kind"] == "episode"

    def test_it_displaces_a_film_rather_than_lengthening_the_page(self):
        with_ep = self._snapshot([self.ROW])
        without = self._snapshot([])
        assert len(with_ep) == len(without)
        assert sum(r["kind"] == "screen" for r in with_ep) \
            == sum(r["kind"] == "screen" for r in without) - 1

    def test_two_shows_take_both_film_slots(self):
        rows = self._snapshot([self.ROW, dict(self.ROW, title="Reacher")])
        assert sum(r["kind"] == "screen" for r in rows) == 0

    def test_it_is_capped(self):
        rows = self._snapshot([dict(self.ROW, title=f"Show {i}") for i in range(5)])
        assert sum(r["kind"] == "episode" for r in rows) == opening.MAX_EPISODES

    def test_it_ignores_the_discovery_kinds_setting(self):
        """`opening_kinds` chooses which kinds of DISCOVERY you want. A show you
        named is not discovery — unfollowing is its control."""
        from timeutil import local_today
        opening._clear_caches()
        today = local_today("America/Chicago")
        opening._candidate_cache[(38.5, -90.5, opening._week_key(today))] = []
        opening._local_cache[(38.5, -90.5, today.isoformat())] = []
        opening._screen_cache[today.isoformat()] = []
        with patch("weather._geocode", return_value=(38.5, -90.5, "Kirkwood")), \
             patch("shows.episode_rows", return_value=[self.ROW]):
            rows = opening.opening_snapshot({
                "city": "Kirkwood, MO", "timezone": "America/Chicago",
                "morning_prefs": {"opening_kinds": ["local"]}})
        assert [r["kind"] for r in rows] == ["episode"]

    def test_a_broken_shows_module_never_costs_the_section(self):
        from timeutil import local_today
        opening._clear_caches()
        today = local_today("America/Chicago")
        opening._candidate_cache[(38.5, -90.5, opening._week_key(today))] = []
        opening._local_cache[(38.5, -90.5, today.isoformat())] = [
            {"kind": "event", "title": "Gig", "subtitle": "", "when": "",
             "url": None, "source": "", "date": None}]
        opening._screen_cache[today.isoformat()] = []
        with patch("weather._geocode", return_value=(38.5, -90.5, "Kirkwood")), \
             patch("shows.episode_rows", side_effect=RuntimeError("boom")):
            rows = opening.opening_snapshot({"city": "Kirkwood, MO",
                                             "timezone": "America/Chicago"})
        assert [r["title"] for r in rows] == ["Gig"]


class TestThePageByDefaultNeverTheText:
    EP = {"kind": "episode", "title": "Furious", "subtitle": "S1E8 - Hart Island",
          "when": "Monday"}
    GIG = {"kind": "event", "title": "Todd Rundgren", "subtitle": "The Pageant",
           "when": "Tonight"}

    def _digest(self, alerts):
        return morning._payload_digest({
            "city": "Kirkwood", "weather": {"high": 90, "low": 70, "description": "sunny",
                                            "high_confident": True},
            "opening": [self.EP, self.GIG], "episode_alerts": alerts})

    def test_a_followed_show_stays_off_the_morning_text_by_default(self):
        d = self._digest(False)
        assert "Furious" not in d and "Todd Rundgren" in d

    def test_it_appears_once_they_ask(self):
        assert "Furious" in self._digest(True)

    def test_the_flag_rides_the_payload_not_a_profile_read(self):
        """_payload_digest has no phone and must not grow one."""
        import inspect
        src = inspect.getsource(morning._payload_digest)
        assert "episode_alerts" in src and "get_profile" not in src

    def test_home_puts_the_flag_on_the_payload(self):
        import inspect
        import home
        assert "episode_alerts" in inspect.getsource(home._refresh_identity)


class TestFollowing:
    def test_the_field_is_on_the_schema(self):
        """A key outside PROFILE_FIELDS is silently dropped on write."""
        from userprofile import PROFILE_FIELDS
        assert "shows" in PROFILE_FIELDS

    def test_resolution_happens_on_the_write_path(self):
        import inspect
        import agent
        src = inspect.getsource(agent.get_reply)
        block = src.split('"follow_show"')[1].split("elif b.name")[0]
        assert "resolve_show" in block

    def test_an_unresolvable_title_asks_rather_than_guessing(self):
        import inspect
        import agent
        src = inspect.getsource(agent.get_reply)
        block = src.split('"follow_show"')[1].split("elif b.name")[0]
        assert "confirm the title" in block
        assert "do not send them elsewhere" in block

    def test_following_is_capped(self):
        import inspect
        import agent
        src = inspect.getsource(agent.get_reply)
        block = src.split('"follow_show"')[1].split("elif b.name")[0]
        assert "FOLLOW_MAX" in block

    def test_following_expires_the_cached_section(self):
        """Otherwise the show they just added is missing for up to a day."""
        import inspect
        import agent
        src = inspect.getsource(agent.get_reply)
        block = src.split('"follow_show"')[1].split("elif b.name")[0]
        assert "invalidate" in block

    def test_resolve_returns_none_rather_than_a_wrong_show(self):
        with patch.object(shows, "_key", return_value="k"), \
             patch.object(shows, "_get", return_value={"results": []}):
            assert shows.resolve_show("asdfqwer") is None
