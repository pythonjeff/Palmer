"""Tests for Palmer Home — the per-user live page.

The load-bearing tests here are the cost guarantee: repeat views inside the
cooldowns must make no outbound calls at all, and the one paid input (news)
must refresh at most once per STALE window no matter how often the page is
loaded. That bound is enforced by a `headlines_tried` stamp written whether the
search succeeds, comes back empty, or raises — so every test below that touches
headlines asserts on the stamp, not just on the data.

Every test that lets a section go stale MUST patch `_fetch_headlines`. Leaving
it unpatched makes a real Tavily call and the suite quietly starts costing money
and seconds.
"""
import time
from unittest.mock import patch

import home


PROFILE = {"city": "Kirkwood, MO", "timezone": "America/Chicago",
           "commute": {"origin": "a street, town", "destination": "b street, city"},
           "morning_topics": ["Bitcoin and major stock news", "SpaceX news"]}


def _payload(**over):
    now = time.time()
    p = {"phone": "+1555", "city": "Kirkwood, MO",
         "weather": {"temp_now": 80}, "traffic": {"live_min": 17}, "prices": [],
         "headlines": [{"title": "old news"}],
         "fetched": {"weather": now, "traffic": now, "prices": now,
                     "headlines": now, "headlines_tried": now},
         "built_at": now}
    p.update(over)
    return p


class TestCostGuarantee:
    def test_fresh_view_makes_no_outbound_calls(self):
        """Second view inside the cooldown must cost nothing."""
        with patch.object(home, "get_profile", return_value=PROFILE), \
             patch.object(home, "_fetch_weather") as w, \
             patch.object(home, "_fetch_traffic") as t, \
             patch.object(home, "_fetch_prices") as p, \
             patch.object(home, "_fetch_headlines") as h, \
             patch.object(home, "save"):
            home.refresh_stale("tok", _payload())
        for m in (w, t, p, h):
            m.assert_not_called()

    def test_headlines_do_not_refresh_inside_the_window(self):
        """The one paid input. Everything else stale, news still fresh."""
        stale = time.time() - 99999
        recent = time.time() - 60
        pl = _payload(fetched={"weather": stale, "traffic": stale,
                               "prices": stale, "headlines_tried": recent})
        with patch.object(home, "get_profile", return_value=PROFILE), \
             patch.object(home, "_fetch_weather", return_value={"temp_now": 1}), \
             patch.object(home, "_fetch_traffic", return_value={"live_min": 2}), \
             patch.object(home, "_fetch_prices", return_value=[]), \
             patch.object(home, "_fetch_headlines") as h, \
             patch.object(home, "save"):
            out = home.refresh_stale("tok", pl)
        h.assert_not_called()
        assert out["headlines"] == [{"title": "old news"}], "headlines must survive untouched"

    def test_headlines_refresh_once_the_window_has_passed(self):
        old = time.time() - (home.STALE["headlines"] + 60)
        pl = _payload(fetched={"weather": time.time(), "traffic": time.time(),
                               "prices": time.time(), "headlines_tried": old})
        with patch.object(home, "get_profile", return_value=PROFILE), \
             patch.object(home, "_fetch_headlines",
                          return_value=[{"title": "new news"}]) as h, \
             patch.object(home, "save"):
            out = home.refresh_stale("tok", pl)
        h.assert_called_once()
        assert out["headlines"] == [{"title": "new news"}]

    def test_a_second_view_right_after_a_refresh_is_free(self):
        """The actual bound: refreshing stamps the window, so hammering the
        page cannot run up a news bill."""
        old = time.time() - (home.STALE["headlines"] + 60)
        pl = _payload(fetched={"weather": time.time(), "traffic": time.time(),
                               "prices": time.time(), "headlines_tried": old})
        with patch.object(home, "get_profile", return_value=PROFILE), \
             patch.object(home, "_fetch_headlines",
                          return_value=[{"title": "new news"}]) as h, \
             patch.object(home, "save"):
            pl = home.refresh_stale("tok", pl)
            home.refresh_stale("tok", pl)
            home.refresh_stale("tok", pl)
        assert h.call_count == 1, "the window must close after the first refresh"

    def test_an_empty_news_result_still_closes_the_window(self):
        """A topic with no coverage must not re-search on every single view."""
        old = time.time() - (home.STALE["headlines"] + 60)
        pl = _payload(fetched={"weather": time.time(), "traffic": time.time(),
                               "prices": time.time(), "headlines_tried": old})
        with patch.object(home, "get_profile", return_value=PROFILE), \
             patch.object(home, "_fetch_headlines", return_value=[]) as h, \
             patch.object(home, "save"):
            out = home.refresh_stale("tok", pl)
            home.refresh_stale("tok", out)
        assert h.call_count == 1
        assert out["headlines"] == [{"title": "old news"}], "empty result must not blank the section"

    def test_a_raising_news_fetch_still_closes_the_window(self):
        old = time.time() - (home.STALE["headlines"] + 60)
        pl = _payload(fetched={"weather": time.time(), "traffic": time.time(),
                               "prices": time.time(), "headlines_tried": old})
        with patch.object(home, "get_profile", return_value=PROFILE), \
             patch.object(home, "_fetch_headlines",
                          side_effect=RuntimeError("tavily down")) as h, \
             patch.object(home, "save"):
            out = home.refresh_stale("tok", pl)
            home.refresh_stale("tok", out)
        assert h.call_count == 1
        assert out["headlines"] == [{"title": "old news"}]

    def test_a_failed_refresh_does_not_claim_fresh_data(self):
        """The page prints "Nh ago" off `headlines`, so a failed attempt must
        move `headlines_tried` without moving `headlines`."""
        old = time.time() - (home.STALE["headlines"] + 60)
        pl = _payload(fetched={"weather": time.time(), "traffic": time.time(),
                               "prices": time.time(),
                               "headlines": old, "headlines_tried": old})
        with patch.object(home, "get_profile", return_value=PROFILE), \
             patch.object(home, "_fetch_headlines", return_value=[]), \
             patch.object(home, "save"):
            out = home.refresh_stale("tok", pl)
        assert out["fetched"]["headlines"] == old, "must not backdate-lie about freshness"
        assert out["fetched"]["headlines_tried"] > old

    def test_legacy_payload_without_the_tried_stamp_falls_back(self):
        """Rows written before headlines_tried existed must still be gated."""
        recent = time.time() - 60
        pl = _payload(fetched={"weather": time.time(), "traffic": time.time(),
                               "prices": time.time(), "headlines": recent})
        with patch.object(home, "get_profile", return_value=PROFILE), \
             patch.object(home, "_fetch_headlines") as h, \
             patch.object(home, "save"):
            home.refresh_stale("tok", pl)
        h.assert_not_called()

    def test_headlines_cooldown_is_bounded_not_unlimited(self):
        w = home.STALE["headlines"]
        assert w is not None, "an unbounded news refresh would be a live cost hole"
        assert w >= 3600, "anything under an hour stops being a bound worth having"

    def test_stale_free_sections_do_refresh(self):
        stale = time.time() - 99999
        pl = _payload(fetched={"weather": stale, "traffic": stale,
                               "prices": stale, "headlines_tried": time.time()})
        with patch.object(home, "get_profile", return_value=PROFILE), \
             patch.object(home, "_fetch_weather", return_value={"temp_now": 99}) as w, \
             patch.object(home, "_fetch_traffic", return_value={"live_min": 42}) as t, \
             patch.object(home, "_fetch_prices", return_value=[{"label": "X"}]) as p, \
             patch.object(home, "_fetch_headlines", return_value=[]), \
             patch.object(home, "save"):
            out = home.refresh_stale("tok", pl)
        w.assert_called_once(); t.assert_called_once(); p.assert_called_once()
        assert out["weather"]["temp_now"] == 99 and out["traffic"]["live_min"] == 42

    def test_refresh_failure_keeps_the_old_value(self):
        stale = time.time() - 99999
        pl = _payload(fetched={"weather": stale, "traffic": stale, "prices": stale,
                               "headlines_tried": time.time()})
        with patch.object(home, "get_profile", return_value=PROFILE), \
             patch.object(home, "_fetch_weather", side_effect=RuntimeError("api down")), \
             patch.object(home, "_fetch_traffic", return_value=None), \
             patch.object(home, "_fetch_prices", return_value=[]), \
             patch.object(home, "_fetch_headlines", return_value=[]), \
             patch.object(home, "save"):
            out = home.refresh_stale("tok", pl)
        assert out["weather"] == {"temp_now": 80}, "a failed refresh must not blank the section"

    def test_unknown_phone_is_a_noop(self):
        pl = _payload()
        with patch.object(home, "get_profile", return_value={}), \
             patch.object(home, "_fetch_weather") as w:
            assert home.refresh_stale("tok", pl) is pl
        w.assert_not_called()


class TestIdentityFreshness:
    """The profile-derived fields are free to refresh and are the ones a user
    notices going stale — they tell Palmer their name at noon and the page is
    still anonymous until tomorrow morning."""

    def _refresh(self, payload, profile):
        with patch.object(home, "get_profile", return_value=profile), \
             patch.object(home, "_tracking", return_value={"topics": []}), \
             patch.object(home, "_fetch_headlines", return_value=[]), \
             patch.object(home, "save") as save:
            return home.refresh_stale("tok", payload), save

    def test_a_name_added_since_the_build_shows_up(self):
        out, _ = self._refresh(_payload(name=None), dict(PROFILE, name="Jeff"))
        assert out["name"] == "Jeff"

    def test_a_city_change_shows_up(self):
        out, _ = self._refresh(_payload(), dict(PROFILE, city="Denver, CO"))
        assert out["city"] == "Denver, CO"

    def test_watches_added_since_the_build_show_up(self):
        pl = _payload(tracking={"topics": ["old"]})
        with patch.object(home, "get_profile", return_value=PROFILE), \
             patch.object(home, "_tracking", return_value={"topics": ["new"]}), \
             patch.object(home, "_fetch_headlines", return_value=[]), \
             patch.object(home, "save"):
            out = home.refresh_stale("tok", pl)
        assert out["tracking"] == {"topics": ["new"]}

    def test_an_unchanged_profile_does_not_rewrite_the_row(self):
        """Every view calls this, so it must settle rather than write forever."""
        pl = _payload(name="Jeff", city="Kirkwood, MO",
                      timezone="America/Chicago", tracking={"topics": []})
        _, save = self._refresh(pl, dict(PROFILE, name="Jeff"))
        save.assert_not_called()

    def test_tracking_reuses_the_profile_the_caller_already_read(self):
        """_conn() opens a connection per call; re-reading here would make
        every page view an N+1."""
        with patch.object(home, "get_profile", return_value=PROFILE) as gp, \
             patch.object(home, "_fetch_headlines", return_value=[]), \
             patch.object(home, "save"):
            home.refresh_stale("tok", _payload())
        assert gp.call_count == 1


class TestPricesSurviveAFailure:
    """A rate-limited fetch must not delete a ticker the user is tracking.
    CoinGecko 429s under load and yfinance times out; a row vanishing looks
    exactly like Palmer forgetting, which is worse than a stale number sitting
    under a visible "N min ago" stamp."""

    PROF = {"morning_topics": ["Bitcoin price", "Nvidia stock"]}
    PREV = [{"label": "Bitcoin", "price": 77000.0}, {"label": "NVDA", "price": 214.0}]

    def test_a_failed_symbol_keeps_its_last_row(self):
        with patch("datafeeds.price_snapshot", side_effect=[None, {"label": "NVDA", "price": 215.0}]):
            out = home._fetch_prices(self.PROF, self.PREV)
        assert [p["label"] for p in out] == ["Bitcoin", "NVDA"]
        assert out[0]["price"] == 77000.0, "kept the stale row"
        assert out[1]["price"] == 215.0, "took the fresh one"

    def test_a_fresh_fetch_wins_over_the_stale_row(self):
        with patch("datafeeds.price_snapshot", return_value={"label": "Bitcoin", "price": 80000.0}):
            out = home._fetch_prices({"morning_topics": ["Bitcoin price"]}, self.PREV)
        assert out[0]["price"] == 80000.0

    def test_with_no_history_a_failure_simply_drops_out(self):
        """First ever build has nothing to fall back to; better an absent row
        than a fabricated one."""
        with patch("datafeeds.price_snapshot", return_value=None):
            assert home._fetch_prices(self.PROF, None) == []

    def test_a_removed_ticker_does_not_come_back_from_the_stale_set(self):
        """The stale rows are a fallback, never a source of symbols."""
        with patch("datafeeds.price_snapshot", return_value={"label": "NVDA", "price": 215.0}):
            out = home._fetch_prices({"morning_topics": ["Nvidia stock"]}, self.PREV)
        assert [p["label"] for p in out] == ["NVDA"]


class TestInvalidate:
    """"add apple stock" has to show up now, not in five minutes."""

    def test_it_expires_the_named_section(self):
        pl = _payload()
        with patch.object(home, "get_profile", return_value={"home_token": "tok"}), \
             patch.object(home, "load", return_value=pl), \
             patch.object(home, "save") as save:
            home.invalidate("+1555", ("prices",))
        assert save.call_args[0][1]["fetched"]["prices"] == 0

    def test_it_leaves_other_sections_alone(self):
        """Expiring headlines would spend money on a topic change."""
        pl = _payload()
        before = pl["fetched"]["headlines_tried"]
        with patch.object(home, "get_profile", return_value={"home_token": "tok"}), \
             patch.object(home, "load", return_value=pl), \
             patch.object(home, "save") as save:
            home.invalidate("+1555", ("prices",))
        saved = save.call_args[0][1]["fetched"]
        assert saved["headlines_tried"] == before
        assert saved["weather"] != 0

    def test_the_expired_section_actually_refetches(self):
        pl = _payload()
        pl["fetched"]["prices"] = 0
        with patch.object(home, "get_profile", return_value=PROFILE), \
             patch.object(home, "_fetch_prices", return_value=[{"label": "AAPL"}]) as fp, \
             patch.object(home, "_fetch_headlines", return_value=[]), \
             patch.object(home, "save"):
            out = home.refresh_stale("tok", pl)
        fp.assert_called_once()
        assert out["prices"] == [{"label": "AAPL"}]

    def test_no_page_yet_is_a_noop(self):
        with patch.object(home, "get_profile", return_value={}), \
             patch.object(home, "save") as save:
            home.invalidate("+1555")
        save.assert_not_called()

    def test_it_never_raises(self):
        with patch.object(home, "get_profile", side_effect=RuntimeError("db down")):
            home.invalidate("+1555")


class TestEnsureFresh:
    """The one entry point for every path where Palmer hands over the link."""

    def test_it_builds_a_page_that_does_not_exist_yet(self):
        """Minting a token does not build a payload, so a user who has never
        had a morning sent would otherwise get a link straight to a 404."""
        with patch.object(home, "home_token", return_value="tok"), \
             patch.object(home, "load", return_value=None), \
             patch.object(home, "rebuild") as rb, \
             patch.object(home, "_APP_URL", "https://x.test"):
            url = home.ensure_fresh("+1555")
        rb.assert_called_once_with("+1555", refresh_news=True)
        assert url == "https://x.test/h/tok"

    def test_it_only_refreshes_an_existing_page(self):
        with patch.object(home, "home_token", return_value="tok"), \
             patch.object(home, "load", return_value=_payload()), \
             patch.object(home, "rebuild") as rb, \
             patch.object(home, "refresh_stale") as rs, \
             patch.object(home, "_APP_URL", "https://x.test"):
            home.ensure_fresh("+1555")
        rb.assert_not_called()
        rs.assert_called_once()

    def test_it_still_returns_a_url_when_the_refresh_blows_up(self):
        """Callers are user-facing. A dead weather API must cost freshness,
        not the link."""
        with patch.object(home, "home_token", return_value="tok"), \
             patch.object(home, "load", side_effect=RuntimeError("db down")), \
             patch.object(home, "_APP_URL", "https://x.test"):
            assert home.ensure_fresh("+1555") == "https://x.test/h/tok"

    def test_a_missing_app_url_is_detectable_by_the_caller(self):
        """Callers gate on startswith('http') to fall back to text."""
        with patch.object(home, "home_token", return_value="tok"), \
             patch.object(home, "load", return_value=_payload()), \
             patch.object(home, "refresh_stale"), \
             patch.object(home, "_APP_URL", ""):
            assert not home.ensure_fresh("+1555").startswith("http")


class TestToken:
    def test_minted_once_then_stable(self):
        store = {}
        with patch.object(home, "get_profile", side_effect=lambda p: dict(store)), \
             patch.object(home, "upsert_profile", side_effect=lambda p, u: store.update(u)):
            first = home.home_token("+1555")
            second = home.home_token("+1555")
        assert first == second and len(first) >= 20

    def test_rotation_expires_the_old_token(self):
        store = {"home_token": "OLD"}
        expired = []
        with patch.object(home, "get_profile", side_effect=lambda p: dict(store)), \
             patch.object(home, "upsert_profile", side_effect=lambda p, u: store.update(u)), \
             patch.object(home, "save_artifact",
                          side_effect=lambda t, k, b, ttl_hours: expired.append((t, ttl_hours))), \
             patch.object(home, "load", return_value={"phone": "+1555"}):
            url = home.rotate("+1555")
        assert store["home_token"] != "OLD"
        assert ("OLD", 0) in expired, "the leaked link must stop working immediately"
        assert store["home_token"] in url


class TestRebuild:
    def test_refresh_news_false_reuses_previous_headlines(self):
        """Rebuilding without the paid pass must not silently drop the news."""
        prev = {"headlines": [{"title": "kept"}], "fetched": {"headlines": 123}}
        with patch.object(home, "get_profile", return_value=PROFILE), \
             patch.object(home, "home_token", return_value="tok"), \
             patch.object(home, "load", return_value=prev), \
             patch.object(home, "_fetch_weather", return_value=None), \
             patch.object(home, "_fetch_traffic", return_value=None), \
             patch.object(home, "_fetch_prices", return_value=[]), \
             patch.object(home, "_fetch_headlines") as h, \
             patch.object(home, "_tracking", return_value={}), \
             patch.object(home, "save"):
            out = home.rebuild("+1555", refresh_news=False)
        h.assert_not_called()
        assert out["headlines"] == [{"title": "kept"}]
        assert out["fetched"]["headlines"] == 123, "freshness stamp must not be faked forward"

    def test_tracking_survives_a_db_failure(self):
        with patch("db.get_user_watches", side_effect=RuntimeError("db down")), \
             patch("db.get_user_price_watches", side_effect=RuntimeError("db down")), \
             patch.object(home, "get_profile", return_value=PROFILE):
            t = home._tracking("+1555")
        assert t["watches"] == [] and t["price_watches"] == []
        assert t["topics"], "topics come from the profile and should survive"
