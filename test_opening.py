"""Opening — the section, its cost model, and its taste gate.

The cost argument is the reason this feature is shaped the way it is, so it is
the thing most worth testing: Opening is *metro-scoped weekly* content, not
user-scoped daily content. Two users in one city must cost one fetch, and a
user with no city must cost nothing at all. If those two properties break, the
section quietly becomes the most expensive thing in the product.

Everything here is offline. A real call in this file would hit Tavily,
Ticketmaster, TMDB and Haiku in one go.
"""
from unittest.mock import patch, MagicMock

import opening
import page


LA = {"city": "Culver City", "timezone": "America/Los_Angeles"}
LA2 = {"city": "Woodland Hills, California", "timezone": "America/Los_Angeles"}
STL = {"city": "Kirkwood, MO", "timezone": "America/Chicago"}

# Geocodes, keyed by the city string the profile carries.
COORDS = {
    "Culver City": (34.02, -118.39, "Culver City, California"),
    "Woodland Hills, California": (34.17, -118.60, "Woodland Hills, California"),
    "Kirkwood, MO": (38.58, -90.40, "Kirkwood, Missouri"),
}

CURATED = {"rows": [
    {"title": "Mamele's", "subtitle": "Peruvian counter on Washington",
     "when": "opened this week", "url": "https://la.eater.com/x", "kind": "local"},
]}


def _seed_caches(local_rows, screen_rows, tz="America/Los_Angeles", coords=(34.0, -118.5)):
    """Pre-populate all three caches so a snapshot makes no outbound call.

    The keys are deliberately different shapes — candidates are cached weekly
    because Tavily costs money, curation and screens daily because Ticketmaster
    and TMDB do not. Seeding only two of the three leaves the third to fetch
    for real, which is how these tests once took 79 seconds.
    """
    from timeutil import local_today
    opening._clear_caches()
    today = local_today(tz)
    opening._candidate_cache[(*coords, opening._week_key(today))] = []
    opening._local_cache[(*coords, today.isoformat())] = list(local_rows)
    opening._screen_cache[today.isoformat()] = list(screen_rows)
    return today


from contextlib import contextmanager


@contextmanager
def _cache_hit_only():
    """Everything a seeded-cache test needs: the geocode answered locally, and
    every other outbound hop rigged to fail the test by name.

    These tests used to patch only the geocode, so a cache miss fell straight
    through to the real fetchers — live Ticketmaster, TMDB and Haiku calls from
    inside the suite, sixty seconds of network, and real concert rows showing
    up in assertions expecting seeded ones. The miss itself was a clock bug (a
    timezone-less profile reads the UTC day against a cache keyed on the LA
    day), but the silent fallthrough is what made it cost minutes to see
    instead of milliseconds: a raising mock turns the next key mismatch into
    an immediate, named failure."""
    boom = AssertionError("seeded cache missed — opening_snapshot computed a "
                          "different key than _seed_caches wrote (check the "
                          "profile timezone against the seed tz)")
    with patch("weather._geocode", side_effect=lambda c: COORDS[c]), \
         patch.object(opening, "_http_get_json", side_effect=boom), \
         patch("datafeeds._search_raw", side_effect=boom), \
         patch.object(opening, "client", MagicMock(**{"messages.create.side_effect": boom})):
        yield


def _resp(payload):
    import json
    r = MagicMock()
    r.content = [MagicMock(text=json.dumps(payload))]
    return r


def _offline(curated=None, tavily=None, http=None):
    """Patch every outbound hop opening.py can make."""
    opening._clear_caches()
    return patch.multiple(
        opening,
        _http_get_json=http or (lambda url, timeout=10: {}),
        client=MagicMock(**{"messages.create.return_value":
                            _resp(curated if curated is not None else CURATED)}),
    ), patch("datafeeds._search_raw", return_value=tavily or []), \
        patch("weather._geocode", side_effect=lambda c: COORDS[c])


def _run(profile, **kw):
    a, b, c = _offline(**kw)
    with a, b, c:
        return opening.opening_snapshot(profile)


class TestTheCostModel:
    """Metro-scoped and weekly. This is the whole reason the feature is cheap."""

    def test_two_users_in_one_metro_share_a_single_fetch(self):
        opening._clear_caches()
        with patch.object(opening, "_http_get_json", return_value={}), \
             patch("weather._geocode", side_effect=lambda c: COORDS[c]), \
             patch("datafeeds._search_raw", return_value=[]) as tav, \
             patch.object(opening, "client") as cl:
            cl.messages.create.return_value = _resp(CURATED)
            opening.opening_snapshot(LA)
            opening.opening_snapshot(LA2)
        assert tav.call_count == 2, ("one snapshot runs two local queries; the "
                                     "second user must add none")

    def test_a_different_metro_does_not_reuse_the_first(self):
        opening._clear_caches()
        with patch.object(opening, "_http_get_json", return_value={}), \
             patch("weather._geocode", side_effect=lambda c: COORDS[c]), \
             patch("datafeeds._search_raw", return_value=[]) as tav, \
             patch.object(opening, "client") as cl:
            cl.messages.create.return_value = _resp(CURATED)
            opening.opening_snapshot(LA)
            opening.opening_snapshot(STL)
        assert tav.call_count == 4, "St. Louis is not Los Angeles"

    def test_culver_city_and_woodland_hills_land_in_one_bucket(self):
        """35 miles apart, one metro. The bucket is what makes that true without
        a city -> metro table that would need maintaining forever."""
        assert opening._bucket(34.02, -118.39) == opening._bucket(34.17, -118.60)
        assert opening._bucket(34.02, -118.39) != opening._bucket(38.58, -90.40)

    def test_a_user_with_no_city_costs_nothing(self):
        opening._clear_caches()
        with patch("weather._geocode") as geo, \
             patch("datafeeds._search_raw") as tav, \
             patch.object(opening, "_http_get_json") as http, \
             patch.object(opening, "client") as cl:
            assert opening.opening_snapshot({"timezone": "America/Chicago"}) == []
            assert opening.opening_snapshot({}) == []
        for m in (geo, tav, http, cl.messages.create):
            m.assert_not_called()


class TestDegradation:
    """Three upstreams, none of them ours. Any of them may be down."""

    def test_a_geocode_failure_returns_nothing_rather_than_raising(self):
        opening._clear_caches()
        with patch("weather._geocode", side_effect=RuntimeError("boom")):
            assert opening.opening_snapshot(LA) == []

    def test_missing_keys_drop_their_rows_and_keep_the_rest(self):
        """No Ticketmaster or TMDB key is the state on first deploy. The local
        half must still work."""
        with patch.object(opening, "TICKETMASTER_API_KEY", ""), \
             patch.object(opening, "TMDB_API_KEY", ""):
            rows = _run(LA, tavily=[{"title": "New spot opens", "url": "https://la.eater.com/x",
                                     "content": "..."}])
        assert [r["title"] for r in rows] == ["Mamele's"]

    def test_events_survive_a_dead_ticketmaster(self):
        with patch.object(opening, "TICKETMASTER_API_KEY", "k"):
            rows = _run(LA, http=lambda url, timeout=10: None)
        assert isinstance(rows, list)

    def test_a_curation_failure_yields_no_rows_rather_than_raw_junk(self):
        """If the taste gate is down, showing the unfiltered firehose is worse
        than showing nothing."""
        opening._clear_caches()
        with patch("weather._geocode", side_effect=lambda c: COORDS[c]), \
             patch("datafeeds._search_raw", return_value=[{"title": "x", "url": "https://a.com/1"}]), \
             patch.object(opening, "_http_get_json", return_value={}), \
             patch.object(opening, "client") as cl:
            cl.messages.create.side_effect = RuntimeError("haiku down")
            assert opening.opening_snapshot(LA) == []

    def test_unparseable_curation_output_is_dropped(self):
        opening._clear_caches()
        with patch("weather._geocode", side_effect=lambda c: COORDS[c]), \
             patch("datafeeds._search_raw", return_value=[{"title": "x", "url": "https://a.com/1"}]), \
             patch.object(opening, "_http_get_json", return_value={}), \
             patch.object(opening, "client") as cl:
            cl.messages.create.return_value = _resp({"rows": [{"subtitle": "no title"}]})
            assert opening.opening_snapshot(LA) == []


class TestRowShape:
    def test_rows_are_capped(self):
        many = {"rows": [{"title": f"Place {i}", "subtitle": "s", "when": "w",
                          "url": f"https://la.eater.com/{i}", "kind": "local"}
                         for i in range(12)]}
        rows = _run(LA, curated=many, tavily=[{"title": "t", "url": "https://la.eater.com/x"}])
        assert len(rows) <= opening.MAX_ROWS

    def test_a_row_carries_what_the_card_renders(self):
        rows = _run(LA, tavily=[{"title": "t", "url": "https://la.eater.com/x"}])
        r = rows[0]
        for k in ("kind", "title", "subtitle", "when", "url", "source"):
            assert k in r, f"the page reads {k}"
        # canonical_domain folds the city subdomain away, which is exactly why
        # one "eater.com" entry in trusted_sources.json covers la.eater.com,
        # sf.eater.com and the rest.
        assert r["source"] == "eater.com", "source is derived, never trusted from the model"


class TestTicketmasterParsing:
    def test_an_event_payload_becomes_candidates(self):
        payload = {"_embedded": {"events": [{
            "name": "Phoebe Bridgers",
            "dates": {"start": {"localDate": "2026-08-30"}},
            "classifications": [{"genre": {"name": "Rock"}}],
            "_embedded": {"venues": [{"name": "Hollywood Bowl"}]},
            "url": "https://ticketmaster.com/e/1"}]}}
        with patch.object(opening, "TICKETMASTER_API_KEY", "k"), \
             patch.object(opening, "_http_get_json", return_value=payload):
            evs = opening._events(34.02, -118.39)
        assert evs[0]["title"] == "Phoebe Bridgers"
        assert evs[0]["venue"] == "Hollywood Bowl" and evs[0]["genre"] == "Rock"

    def test_no_key_makes_no_call(self):
        with patch.object(opening, "TICKETMASTER_API_KEY", ""), \
             patch.object(opening, "_http_get_json") as http:
            assert opening._events(1, 2) == []
        http.assert_not_called()


class TestTheCurationPromptKnowsTheDate:
    """This presented as a taste failure and was a calendar failure.

    Without today's date in the prompt, the model dates events against its
    training cutoff. Handed a concert on 2026-08-29 it called it "over a year
    away" and dropped it under the stale-content rule — rejecting all seventeen
    candidates for a week that held Todd Rundgren, The Wallflowers and Ray
    LaMontagne. The section looked like it had no taste; it had no calendar.
    """

    def _prompt_for(self, candidates):
        opening._clear_caches()
        with patch.object(opening, "client") as cl:
            cl.messages.create.return_value = _resp({"rows": []})
            opening._curate("St. Louis", candidates)
            return cl.messages.create.call_args.kwargs["messages"][0]["content"]

    def test_todays_date_is_in_the_prompt(self):
        from datetime import date
        body = self._prompt_for([{"title": "Todd Rundgren", "url": "https://t.com/1",
                                  "blurb": "Rock The Pageant 2026-08-29"}])
        assert date.today().strftime("%B") in body and str(date.today().year) in body

    def test_the_staleness_rule_points_at_that_date(self):
        """The rule has to be anchored to the stated date, not to the model's
        own sense of now — that is the whole failure."""
        body = self._prompt_for([{"title": "x", "url": "https://t.com/1", "blurb": "b"}]).lower()
        assert "already past relative to" in body

    def test_the_metro_reaches_the_prompt_not_the_suburb(self):
        """Told "Kirkwood, MO", the model correctly rejects every venue in
        St. Louis as somewhere else — which is all of them."""
        body = self._prompt_for([{"title": "x", "url": "https://t.com/1", "blurb": "b"}])
        assert "St. Louis" in body


class TestSubtitleDoesNotRepeatWhen:
    """Real output on a live page: subtitle "Rock The Pageant, Friday" sitting
    directly above a when/source line reading "Friday, August 29 ·
    ticketmaster.com" — the day appeared in both fields for every event row
    that week. The prompt must say plainly not to do that."""

    def _prompt_for(self, candidates):
        opening._clear_caches()
        with patch.object(opening, "client") as cl:
            cl.messages.create.return_value = _resp({"rows": []})
            opening._curate("St. Louis", candidates)
            return cl.messages.create.call_args.kwargs["messages"][0]["content"]

    def test_prompt_forbids_repeating_the_day_between_fields(self):
        body = self._prompt_for([{"title": "Todd Rundgren", "url": "https://t.com/1",
                                  "blurb": "Rock The Pageant 2026-08-29"}]).lower()
        assert "must never say the same thing twice" in body

    def test_prompt_tells_subtitle_to_leave_the_day_to_when(self):
        body = self._prompt_for([{"title": "x", "url": "https://t.com/1", "blurb": "b"}]).lower()
        assert "the day already goes in `when`" in body


class TestLongLeadEvents:
    """Users want to hear about a big show on sale for months out, not just
    what's happening in the next seven days — so the events pull runs twice:
    the near-term week, and a sparser long-lead window out to a year."""

    def test_a_snapshot_makes_two_ticketmaster_calls(self):
        opening._clear_caches()
        with patch.object(opening, "TICKETMASTER_API_KEY", "k"), \
             patch("weather._geocode", side_effect=lambda c: COORDS[c]), \
             patch("datafeeds._search_raw", return_value=[]), \
             patch.object(opening, "_http_get_json", return_value={}) as http, \
             patch.object(opening, "client") as cl:
            cl.messages.create.return_value = _resp({"rows": []})
            opening.opening_snapshot(LA)
        urls = [c.args[0] if c.args else c.kwargs.get("url") for c in http.call_args_list]
        tm_calls = [u for u in urls if opening.TM_BASE in u]
        assert len(tm_calls) == 2, "one near-term pull, one long-lead pull"

    def test_the_long_lead_window_reaches_a_year_out(self):
        with patch.object(opening, "TICKETMASTER_API_KEY", "k"), \
             patch.object(opening, "_http_get_json", return_value={}) as http:
            opening._events(34.02, -118.39, start_days=7,
                            end_days=opening.LONG_LEAD_DAYS, size=opening.LONG_LEAD_SIZE)
        url = http.call_args.args[0] if http.call_args.args else http.call_args.kwargs["url"]
        # Parse the endDateTime the code actually sent rather than string-
        # matching a month computed on a second clock: the old check formatted
        # `utcnow + LONG_LEAD_DAYS - 1` as "%Y-%m", which names a different
        # month than the code's `utcnow + LONG_LEAD_DAYS` whenever the horizon
        # lands on the 1st — about a dozen evenings a year, found on one.
        from datetime import datetime
        from urllib.parse import parse_qs, urlparse
        end = datetime.strptime(parse_qs(urlparse(url).query)["endDateTime"][0],
                                "%Y-%m-%dT%H:%M:%SZ")
        days_out = (end - datetime.utcnow()).total_seconds() / 86400
        assert opening.LONG_LEAD_DAYS - 1 <= days_out <= opening.LONG_LEAD_DAYS


class TestExpiredRowsDropOut:
    """The metro cache lasts a week, but a Friday concert cached on Monday
    must not still be on the page on Saturday — the whole complaint that
    prompted this. A curated row keeps its event date so it can be dropped
    the moment it has passed, without waiting for the weekly re-curation."""

    def test_a_past_dated_row_is_dropped_on_read(self):
        # The rows, the cache keys and the reader's expiry all sit on ONE
        # calendar — the LA day the seed uses. date.today() here is the
        # machine's zone and a bare {"city": ...} profile reads the UTC day,
        # which is how this test spent its evenings fetching real concerts.
        from datetime import timedelta
        from timeutil import local_today
        today = local_today("America/Los_Angeles")
        _seed_caches([
            {"kind": "event", "title": "Already Happened", "subtitle": "", "when": "",
             "url": None, "source": "", "date": (today - timedelta(days=1)).isoformat()},
            {"kind": "local", "title": "Still Live", "subtitle": "", "when": "",
             "url": None, "source": "", "date": None},
        ], [])
        with _cache_hit_only():
            rows = opening.opening_snapshot(dict(LA))
        titles = [r["title"] for r in rows]
        assert "Still Live" in titles
        assert "Already Happened" not in titles

    def test_todays_date_survives(self):
        from timeutil import local_today
        today = local_today("America/Los_Angeles")
        _seed_caches([
            {"kind": "event", "title": "Tonight", "subtitle": "", "when": "",
             "url": None, "source": "", "date": today.isoformat()},
        ], [])
        with _cache_hit_only():
            rows = opening.opening_snapshot(dict(LA))
        assert [r["title"] for r in rows] == ["Tonight"]

    def test_curate_stores_a_valid_date_from_the_model(self):
        opening._clear_caches()
        with patch("weather._geocode", side_effect=lambda c: COORDS[c]), \
             patch("datafeeds._search_raw",
                   return_value=[{"title": "x", "url": "https://a.com/1"}]), \
             patch.object(opening, "_http_get_json", return_value={}), \
             patch.object(opening, "client") as cl:
            cl.messages.create.return_value = _resp({"rows": [
                {"title": "Phoebe Bridgers", "subtitle": "Hollywood Bowl", "when": "Friday",
                 "url": "https://t.com/1", "kind": "event", "date": "2026-09-04"}]})
            rows = opening.opening_snapshot(LA)
        assert rows[0]["date"] == "2026-09-04"

    def test_curate_drops_a_malformed_date_rather_than_raising(self):
        opening._clear_caches()
        with patch("weather._geocode", side_effect=lambda c: COORDS[c]), \
             patch("datafeeds._search_raw",
                   return_value=[{"title": "x", "url": "https://a.com/1"}]), \
             patch.object(opening, "_http_get_json", return_value={}), \
             patch.object(opening, "client") as cl:
            cl.messages.create.return_value = _resp({"rows": [
                {"title": "Bad Date", "subtitle": "", "when": "someday",
                 "url": "https://t.com/1", "kind": "event", "date": "not-a-date"}]})
            rows = opening.opening_snapshot(LA)
        assert rows and rows[0]["date"] is None


class TestScreensBypassTheLocalGate:
    """Screens were being run through the local-openings curation prompt, whose
    rules reject anything "outside the metro" — so every movie was thrown away
    and the section was local-only without anyone noticing. TMDB is already
    structured and ranked; there is no firehose there to filter."""

    # popularity ranks and vote_count gates. Ranking by vote_average with no
    # floor put a 13-vote film in front of every user on the system.
    TMDB = {"results": [
        {"id": 1, "title": "Colony", "release_date": None, "overview": "A virus spreads.",
         "vote_average": 8.1, "vote_count": 630, "popularity": 900.0},
        {"id": 2, "title": "Second Film", "release_date": None, "overview": "Something else.",
         "vote_average": 7.4, "vote_count": 400, "popularity": 500.0},
        {"id": 3, "title": "Third Film", "release_date": None, "overview": "More.",
         "vote_average": 7.0, "vote_count": 200, "popularity": 100.0},
    ]}

    def _screens(self):
        from datetime import date
        today = date.today().isoformat()
        payload = {"results": [dict(r, release_date=today) for r in self.TMDB["results"]]}
        with patch.object(opening, "TMDB_API_KEY", "k"), \
             patch.object(opening, "_http_get_json", return_value=payload):
            return opening._screens()

    def test_titles_in_the_window_survive(self):
        assert [r["title"] for r in self._screens()][:1] == ["Colony"], "ranked by popularity"

    def test_a_thinly_rated_title_never_reaches_anyone(self):
        """"Toxic: A Fairy Tale for Grown-ups" scored 6.23 from THIRTEEN votes
        and was shown to every user as a recommendation. A rating from thirteen
        people is not comparable to one from six hundred."""
        from datetime import date
        movie = {"results": [{"id": 9, "title": "Thinly Rated", "overview": "x",
                              "release_date": date.today().isoformat(),
                              "vote_average": 9.9, "vote_count": 13,
                              "popularity": 5000.0}]}
        # The TV floor is enforced by TMDB via vote_count.gte in the query, not
        # by our code, so the two calls have to be answered separately.
        def _get(url, timeout=10):
            return {"results": []} if "discover/tv" in url else movie

        with patch.object(opening, "TMDB_API_KEY", "k"), \
             patch.object(opening, "_http_get_json", side_effect=_get):
            assert opening._screens() == []

    def test_popularity_outranks_a_high_score_from_few_voters(self):
        """A film released three days ago has no votes yet however good it is;
        popularity already reflects that people are looking it up."""
        from datetime import date
        today = date.today().isoformat()
        payload = {"results": [
            {"id": 1, "title": "Quietly Adored", "release_date": today, "overview": "x",
             "vote_average": 9.5, "vote_count": 200, "popularity": 20.0},
            {"id": 2, "title": "What People Are Watching", "release_date": today,
             "overview": "x", "vote_average": 7.0, "vote_count": 900, "popularity": 900.0}]}
        with patch.object(opening, "TMDB_API_KEY", "k"), \
             patch.object(opening, "_http_get_json", return_value=payload):
            assert opening._screens()[0]["title"] == "What People Are Watching"

    def test_tv_comes_from_a_premiere_window_not_whatever_is_airing(self):
        """/tv/on_the_air means CURRENTLY AIRING, not new — it returned Ted
        Lasso, Reacher and Silo, and filtering it for "new" surfaced a
        Brazilian news programme that first aired in 1969."""
        seen = {}

        def _get(url, timeout=10):
            seen["tv"] = url if "/tv" in url or "discover" in url else seen.get("tv")
            return {"results": []}

        with patch.object(opening, "TMDB_API_KEY", "k"), \
             patch.object(opening, "_http_get_json", side_effect=_get):
            opening._screens()
        assert "discover/tv" in seen["tv"] and "first_air_date.gte" in seen["tv"]
        assert "on_the_air" not in seen["tv"]

    def test_the_window_is_weeks_not_days(self):
        """The old +/-7 day window is what forced the ranking into the 13-vote
        tail: almost nothing good was left inside it."""
        assert opening.SCREEN_WINDOW_DAYS >= 21

    def test_screens_cost_no_model_call(self):
        opening._clear_caches()
        from datetime import date
        payload = {"results": [dict(r, release_date=date.today().isoformat())
                               for r in self.TMDB["results"]]}
        with patch("weather._geocode", side_effect=lambda c: COORDS[c]), \
             patch("datafeeds._search_raw", return_value=[]), \
             patch.object(opening, "TMDB_API_KEY", "k"), \
             patch.object(opening, "TICKETMASTER_API_KEY", ""), \
             patch.object(opening, "_http_get_json", return_value=payload), \
             patch.object(opening, "client") as cl:
            cl.messages.create.return_value = _resp({"rows": []})
            rows = opening.opening_snapshot(LA)
        screens = [r for r in rows if r["kind"] == "screen"]
        assert screens, "a movie released today must reach the page"
        # metro lookup + local curation only — nothing for screens
        assert cl.messages.create.call_count <= 2

    def test_screens_are_capped_so_they_cannot_crowd_out_the_local_rows(self):
        opening._clear_caches()
        from datetime import date
        payload = {"results": [dict(r, release_date=date.today().isoformat())
                               for r in self.TMDB["results"]]}
        with patch("weather._geocode", side_effect=lambda c: COORDS[c]), \
             patch("datafeeds._search_raw", return_value=[]), \
             patch.object(opening, "TMDB_API_KEY", "k"), \
             patch.object(opening, "TICKETMASTER_API_KEY", ""), \
             patch.object(opening, "_http_get_json", return_value=payload), \
             patch.object(opening, "client") as cl:
            cl.messages.create.return_value = _resp({"rows": []})
            rows = opening.opening_snapshot(LA)
        assert len([r for r in rows if r["kind"] == "screen"]) <= opening.MAX_SCREENS


class TestPerUserKinds:
    """Three kinds, all on by default, each addable and removable by asking.

    The filtering happens after the caches, never inside them. Narrowing the
    fetch to one user's taste would make the metro cache unshareable and turn
    N users back into N fetches — which is the one property this feature cannot
    lose.
    """

    POOL = [{"kind": "local", "title": "Mamele's", "subtitle": "", "when": "", "url": None, "source": ""},
            {"kind": "event", "title": "Muse", "subtitle": "", "when": "", "url": None, "source": ""},
            {"kind": "event", "title": "Wallflowers", "subtitle": "", "when": "", "url": None, "source": ""},
            {"kind": "local", "title": "Bar Etoile", "subtitle": "", "when": "", "url": None, "source": ""}]
    SCREENS = [{"kind": "screen", "title": "Colony", "subtitle": "", "when": "", "url": None, "source": ""},
               {"kind": "screen", "title": "All That", "subtitle": "", "when": "", "url": None, "source": ""}]

    def _snapshot(self, prefs):
        _seed_caches(self.POOL, self.SCREENS)
        # dict(LA), not a bare city: the profile's timezone is what makes
        # opening_snapshot read the same LA day the seed keyed on. Without it
        # the reader computes the UTC day, and from 5pm Pacific to midnight
        # that is tomorrow — a cache miss, and (before _cache_hit_only) a live
        # fetch whose real rows failed the kind assertions.
        profile = dict(LA)
        if prefs is not None:
            profile["morning_prefs"] = prefs
        with _cache_hit_only():
            return opening.opening_snapshot(profile)

    def test_a_cache_hit_costs_no_model_call(self):
        """opening_snapshot runs on page views. The metro lookup used to sit
        above the cache check, so a hit still paid a Haiku call for a metro
        nothing was going to be searched for."""
        opening._clear_caches()
        _seed_caches(self.POOL, self.SCREENS)
        with patch("weather._geocode", side_effect=lambda c: COORDS[c]), \
             patch.object(opening, "client") as cl, \
             patch("datafeeds._search_raw") as tav, \
             patch.object(opening, "_http_get_json") as http:
            rows = opening.opening_snapshot(dict(LA))
        assert rows, "the cached rows must still come back"
        cl.messages.create.assert_not_called()
        tav.assert_not_called()
        http.assert_not_called()

    def test_default_is_everything(self):
        kinds = {r["kind"] for r in self._snapshot(None)}
        assert kinds == {"local", "event", "screen"}

    def test_removing_movies_drops_only_screens(self):
        rows = self._snapshot({"opening_kinds": ["local", "event"]})
        assert {r["kind"] for r in rows} == {"local", "event"}

    def test_removing_a_kind_gives_its_slots_to_the_others(self):
        """Otherwise trimming the section just makes it shorter, which is not
        what someone asking for fewer movies wants."""
        with_screens = self._snapshot(None)
        without = self._snapshot({"opening_kinds": ["local", "event"]})
        assert len(without) >= len(with_screens) - 1

    def test_movies_only(self):
        rows = self._snapshot({"opening_kinds": ["screen"]})
        assert rows and all(r["kind"] == "screen" for r in rows)

    def test_removing_every_kind_yields_nothing(self):
        assert self._snapshot({"opening_kinds": []}) == []

    def test_two_users_with_different_tastes_still_share_one_fetch(self):
        """The cost model. Filtering is per user; fetching is per metro."""
        opening._clear_caches()
        with patch.object(opening, "_http_get_json", return_value={}), \
             patch("weather._geocode", side_effect=lambda c: COORDS[c]), \
             patch("datafeeds._search_raw",
                   return_value=[{"title": "gig", "url": "https://t.com/1"}]) as tav, \
             patch.object(opening, "client") as cl:
            cl.messages.create.return_value = _resp(
                {"rows": [{"title": "Muse", "url": "https://t.com/1", "kind": "event"}]})
            a = opening.opening_snapshot({"city": "Culver City"})
            b = opening.opening_snapshot({"city": "Woodland Hills, California",
                                          "morning_prefs": {"opening_kinds": ["screen"]}})
        assert tav.call_count == 2, "two searches for the metro, not four for two users"
        assert [r["kind"] for r in a] == ["event"]
        assert b == [], "different taste, same cache, correctly filtered to nothing"


class TestTheKindsDispatch:
    def _apply(self, profile, tool_input):
        import agent
        updates = {}
        note = agent._apply_opening_kinds(profile, tool_input, updates)
        return updates.get("morning_prefs", {}), note

    def test_adding_is_additive_not_a_replacement(self):
        """"I want movies too" must not silently drop what they already had."""
        prefs, _ = self._apply({"morning_prefs": {"opening_kinds": ["local"]}},
                               {"opening_add": ["movies"]})
        assert prefs["opening_kinds"] == ["local", "screen"]

    def test_removing_from_the_default_starts_from_all_three(self):
        prefs, _ = self._apply({}, {"opening_remove": ["movies"]})
        assert prefs["opening_kinds"] == ["local", "event"]

    def test_removing_everything_switches_the_section_off(self):
        prefs, note = self._apply({}, {"opening_remove": ["restaurants", "events", "movies"]})
        assert prefs["opening_kinds"] == [] and prefs["opening"] is False
        assert "off" in note

    def test_no_opening_args_writes_nothing(self):
        prefs, note = self._apply({}, {"add": ["Tesla stock"]})
        assert prefs == {} and note == ""

    def test_an_unknown_word_is_ignored_rather_than_stored(self):
        prefs, _ = self._apply({}, {"opening_add": ["sports"], "opening_remove": ["movies"]})
        assert prefs["opening_kinds"] == ["local", "event"]

    def test_the_tool_exposes_both_directions(self):
        from tools_def import TOOLS
        props = next(t for t in TOOLS if t["name"] == "update_morning_briefing")["input_schema"]["properties"]
        for k in ("opening_add", "opening_remove"):
            assert k in props
            assert set(props[k]["items"]["enum"]) == {"restaurants", "events", "movies"}

    def test_the_prompt_routes_these_away_from_topics(self):
        import prompts
        block = prompts.SYSTEM_PROMPT
        assert "opening_add" in block and "opening_remove" in block

    def test_a_kinds_change_expires_the_cached_rows(self):
        """Otherwise they keep seeing the concerts they just asked to stop."""
        import inspect, agent
        src = inspect.getsource(agent.get_reply)
        block = src.split('update_morning_briefing"')[1].split("elif b.name")[0]
        assert "morning_prefs" in block and "opening" in block


class TestExpiryUsesTheReadersDate:
    """The dyno runs UTC. From 5pm Pacific onward date.today() is already
    tomorrow, so a show happening THAT EVENING would drop off the page hours
    before it started — the same failure the card masthead had."""

    def test_the_reader_timezone_is_what_expires_a_row(self):
        import inspect
        src = inspect.getsource(opening.opening_snapshot)
        # The date is derived once from the reader's zone and reused for
        # expiry, the cache keys and rotation — so assert the derivation and
        # that expiry uses it, not one exact call site.
        assert 'local_today(profile.get("timezone"))' in src
        assert "_not_expired(r, today)" in src

    def test_tonights_show_survives_after_utc_rolls_over(self):
        from datetime import date
        row = {"date": "2026-08-29", "kind": "event", "title": "Tonight"}
        assert opening._not_expired(row, date(2026, 8, 29)), "still on tonight"
        assert not opening._not_expired(row, date(2026, 8, 30)), "over by tomorrow"

    def test_an_undated_row_never_expires(self):
        from datetime import date
        assert opening._not_expired({"kind": "local", "title": "A bar"}, date(2099, 1, 1))

    def test_a_malformed_date_fails_open(self):
        from datetime import date
        assert opening._not_expired({"date": "next friday"}, date(2026, 8, 29))


class TestTheSectionMovesDayToDay:
    """It was frozen Monday to Sunday: both caches keyed on the ISO week, so a
    user saw the same movie every day and nothing but the current weekend."""

    POOL6 = [{"kind": "event", "title": f"Act {i}", "subtitle": "", "when": "",
              "url": None, "source": "", "date": None} for i in range(6)]
    SCREENS6 = [{"kind": "screen", "title": f"Film {i}", "subtitle": "", "when": "",
                 "url": None, "source": ""} for i in range(6)]

    def test_the_paid_cache_is_weekly_and_the_free_ones_are_daily(self):
        """Only the Tavily search costs money. Ticketmaster is 5,000/day free
        and TMDB is free, so keying those weekly bought nothing and froze the
        section."""
        from timeutil import local_today
        _seed_caches(self.POOL6, self.SCREENS6)
        today = local_today("America/Los_Angeles")
        assert any(opening._week_key(today) in str(k) for k in opening._candidate_cache)
        assert any(today.isoformat() in str(k) for k in opening._local_cache)
        assert today.isoformat() in opening._screen_cache

    def test_screens_rotate_across_days(self):
        from datetime import date
        a = opening._rotate(self.SCREENS6, date(2026, 8, 29), 2)
        b = opening._rotate(self.SCREENS6, date(2026, 8, 30), 2)
        assert a != b, "the same two titles every day is the complaint"

    def test_rotation_is_stable_within_a_day(self):
        """A retry inside one day must not reshuffle the page under someone."""
        from datetime import date
        d = date(2026, 8, 29)
        assert opening._rotate(self.SCREENS6, d, 2) == opening._rotate(self.SCREENS6, d, 2)

    def test_every_candidate_gets_a_turn(self):
        from datetime import date, timedelta
        seen = set()
        for i in range(6):
            seen.update(r["title"] for r in
                        opening._rotate(self.SCREENS6, date(2026, 8, 29) + timedelta(days=i), 2))
        assert len(seen) == 6, "four of six were never shown before rotation"

    def test_a_short_list_is_served_whole(self):
        from datetime import date
        one = [self.SCREENS6[0]]
        assert opening._rotate(one, date(2026, 8, 29), 2) == one


class TestSomethingFurtherOutGetsASlot:
    """Everything in the next seven days outranked everything beyond, so with a
    busy metro the long-lead pull never won a slot and the section read as this
    weekend, forever."""

    def _rows(self, today):
        near = [{"kind": "event", "title": f"Soon {i}", "date": today.isoformat(),
                 "subtitle": "", "when": "", "url": None, "source": ""} for i in range(4)]
        from datetime import timedelta
        far = [{"kind": "event", "title": "Kacey Musgraves",
                "date": (today + timedelta(days=14)).isoformat(),
                "subtitle": "", "when": "", "url": None, "source": ""}]
        return near + far

    def test_a_far_row_survives_a_full_near_term_window(self):
        from timeutil import local_today
        today = local_today("America/Los_Angeles")
        _seed_caches(self._rows(today), [])
        with _cache_hit_only():
            rows = opening.opening_snapshot(dict(LA))
        assert "Kacey Musgraves" in [r["title"] for r in rows]

    def test_near_term_still_leads(self):
        from timeutil import local_today
        today = local_today("America/Los_Angeles")
        _seed_caches(self._rows(today), [])
        with _cache_hit_only():
            rows = opening.opening_snapshot(dict(LA))
        assert rows[0]["title"].startswith("Soon"), "advance notice must not lead"

    def test_no_far_candidate_means_no_slot_wasted(self):
        from timeutil import local_today
        today = local_today("America/Los_Angeles")
        near = [r for r in self._rows(today) if r["title"].startswith("Soon")]
        _seed_caches(near, [])
        with _cache_hit_only():
            rows = opening.opening_snapshot(dict(LA))
        # With no screens, local gets the whole MAX_ROWS allowance — the point
        # is that reserving a slot for a far row leaves no gap when there is
        # no far row to put in it.
        assert [r["title"] for r in rows] == ["Soon 0", "Soon 1", "Soon 2", "Soon 3"]

    def test_the_horizon_is_what_counts_as_far(self):
        from datetime import date, timedelta
        today = date(2026, 8, 29)
        soon = {"date": (today + timedelta(days=3)).isoformat()}
        far = {"date": (today + timedelta(days=30)).isoformat()}
        assert not opening._is_far(soon, today) and opening._is_far(far, today)
        assert not opening._is_far({"date": None}, today), "places are never 'far'"


class TestThePageCard:
    def _render(self, rows):
        payload = {"city": "Culver City", "weather": {}, "prices": [], "headlines": [],
                   "opening": rows, "tracking": {"watches": [], "price_watches": [], "topics": []},
                   "fetched": {}}
        return page.render(payload, token="t", image_url="i", page_url="p")

    def test_rows_render_with_their_source(self):
        html = self._render([{"kind": "local", "title": "Mamele's",
                              "subtitle": "Peruvian counter", "when": "opened this week",
                              "url": "https://la.eater.com/x", "source": "la.eater.com"}])
        assert ">Opening" in html and "Mamele&#x27;s" in html
        assert 'href="https://la.eater.com/x"' in html
        assert "la.eater.com" in html

    def test_the_page_cap_matches_what_opening_produces(self):
        """A second cap in page.py silently truncated the last row when
        MAX_LOCAL/MAX_SCREENS were raised — the payload held five, the page drew
        four, and nothing failed."""
        assert page.OPENING_ROW_CAP == opening.MAX_ROWS

    def test_every_row_the_payload_holds_reaches_the_page(self):
        rows = [{"kind": "event", "title": f"Act {i}", "source": "t.com"}
                for i in range(opening.MAX_ROWS)]
        html = self._render(rows)
        for r in rows:
            assert r["title"] in html, f"{r['title']} was dropped by the page cap"

    def test_the_section_is_absent_when_there_is_nothing(self):
        assert ">Opening" not in self._render([])

    def test_tmdb_attribution_appears_only_with_a_screen_row(self):
        """Required by TMDB's terms when their data is shown — and not a notice
        to put in front of a user whose rows are all local."""
        local_only = self._render([{"kind": "local", "title": "A", "source": "eater.com"}])
        assert "TMDB" not in local_only
        with_screen = self._render([{"kind": "screen", "title": "Wicked",
                                     "source": "themoviedb.org"}])
        assert "not endorsed or certified by TMDB" in with_screen

    def test_untrusted_text_is_escaped(self):
        """Event and article titles are third-party input."""
        html = self._render([{"kind": "local", "title": "<script>alert(1)</script>",
                              "source": "x.com"}])
        assert "<script>alert(1)</script>" not in html


class TestTheMorningLineCanSeeIt:
    def test_the_digest_carries_opening_rows(self):
        import morning
        d = morning._payload_digest({"opening": [
            {"title": "Mamele's", "subtitle": "Peruvian counter", "when": "opened this week"}]})
        assert "Mamele's" in d and "Opening near them" in d
