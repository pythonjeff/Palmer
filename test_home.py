"""Tests for Palmer Home — the per-user live page.

The load-bearing test here is the cost guarantee: a page view must never trigger
the paid news search, and repeat views inside the cooldown must make no outbound
calls at all. Everything else on the page is a free API, which is the only
reason "live" is affordable.
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
         "fetched": {"weather": now, "traffic": now, "prices": now, "headlines": now},
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

    def test_headlines_never_refresh_on_view(self):
        """The one paid input. Stale by a week must still not refetch."""
        stale = time.time() - 7 * 86400
        pl = _payload(fetched={"weather": stale, "traffic": stale,
                               "prices": stale, "headlines": stale})
        with patch.object(home, "get_profile", return_value=PROFILE), \
             patch.object(home, "_fetch_weather", return_value={"temp_now": 1}), \
             patch.object(home, "_fetch_traffic", return_value={"live_min": 2}), \
             patch.object(home, "_fetch_prices", return_value=[]), \
             patch.object(home, "_fetch_headlines") as h, \
             patch.object(home, "save"):
            out = home.refresh_stale("tok", pl)
        h.assert_not_called()
        assert out["headlines"] == [{"title": "old news"}], "headlines must survive untouched"

    def test_headlines_have_no_cooldown_configured(self):
        assert home.STALE["headlines"] is None

    def test_stale_free_sections_do_refresh(self):
        stale = time.time() - 99999
        pl = _payload(fetched={"weather": stale, "traffic": stale,
                               "prices": stale, "headlines": stale})
        with patch.object(home, "get_profile", return_value=PROFILE), \
             patch.object(home, "_fetch_weather", return_value={"temp_now": 99}) as w, \
             patch.object(home, "_fetch_traffic", return_value={"live_min": 42}) as t, \
             patch.object(home, "_fetch_prices", return_value=[{"label": "X"}]) as p, \
             patch.object(home, "save"):
            out = home.refresh_stale("tok", pl)
        w.assert_called_once(); t.assert_called_once(); p.assert_called_once()
        assert out["weather"]["temp_now"] == 99 and out["traffic"]["live_min"] == 42

    def test_refresh_failure_keeps_the_old_value(self):
        stale = time.time() - 99999
        pl = _payload(fetched={"weather": stale, "traffic": stale, "prices": stale,
                               "headlines": stale})
        with patch.object(home, "get_profile", return_value=PROFILE), \
             patch.object(home, "_fetch_weather", side_effect=RuntimeError("api down")), \
             patch.object(home, "_fetch_traffic", return_value=None), \
             patch.object(home, "_fetch_prices", return_value=[]), \
             patch.object(home, "save"):
            out = home.refresh_stale("tok", pl)
        assert out["weather"] == {"temp_now": 80}, "a failed refresh must not blank the section"

    def test_unknown_phone_is_a_noop(self):
        pl = _payload()
        with patch.object(home, "get_profile", return_value={}), \
             patch.object(home, "_fetch_weather") as w:
            assert home.refresh_stale("tok", pl) is pl
        w.assert_not_called()


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
