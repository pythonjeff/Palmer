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


class TestScreensBypassTheLocalGate:
    """Screens were being run through the local-openings curation prompt, whose
    rules reject anything "outside the metro" — so every movie was thrown away
    and the section was local-only without anyone noticing. TMDB is already
    structured and ranked; there is no firehose there to filter."""

    TMDB = {"results": [
        {"id": 1, "title": "Colony", "release_date": None, "overview": "A virus spreads.",
         "vote_average": 8.1},
        {"id": 2, "title": "Second Film", "release_date": None, "overview": "Something else.",
         "vote_average": 7.4},
        {"id": 3, "title": "Third Film", "release_date": None, "overview": "More.",
         "vote_average": 7.0},
    ]}

    def _screens(self):
        from datetime import date
        today = date.today().isoformat()
        payload = {"results": [dict(r, release_date=today) for r in self.TMDB["results"]]}
        with patch.object(opening, "TMDB_API_KEY", "k"), \
             patch.object(opening, "_http_get_json", return_value=payload):
            return opening._screens()

    def test_titles_in_the_window_survive(self):
        assert [r["title"] for r in self._screens()][:1] == ["Colony"], "ranked by score"

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
