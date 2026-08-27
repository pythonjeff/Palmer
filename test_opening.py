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
