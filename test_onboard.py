"""Setup form: the link on message one, the one-shot write, and what it seeds.

The regressions these hold are the ones the flow exists to fix — a city that
lands too late to seed local news, and a link that goes out twice because
`is_new_user` is computed before the per-phone lock.
"""
from unittest.mock import patch

import pytest

import db
import onboard


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "onboard.db", raising=False)
    monkeypatch.setenv("APP_URL", "https://palmer.test")
    db.init_db()
    return db


class _Form(dict):
    """Starlette's FormData interface, as much of it as onboard.apply uses."""
    def __init__(self, data, multi=None):
        super().__init__(data)
        self._multi = multi or {}

    def getlist(self, key):
        return self._multi.get(key, [])


class TestStart:
    def test_mints_a_token_and_parks_a_stub(self, store):
        with patch("home.save") as save, patch("home.load", return_value=None):
            url = onboard.start("+15550001111")
        assert url.startswith("https://palmer.test/h/")
        token, payload = save.call_args[0]
        assert payload == {"phone": "+15550001111", "setup_pending": True}
        assert token in url

    def test_no_app_url_means_no_link(self, store, monkeypatch):
        monkeypatch.delenv("APP_URL", raising=False)
        assert onboard.start("+15550001111") is None

    def test_does_not_clobber_an_existing_page(self, store):
        """A user who already has a payload is not a new user; never overwrite
        a live page with a setup stub."""
        with patch("home.save") as save, patch("home.load", return_value={"built_at": 1}):
            onboard.start("+15550001111")
        save.assert_not_called()

    def test_never_raises(self, store):
        with patch("home.home_token", side_effect=RuntimeError("db down")):
            assert onboard.start("+15550001111") is None


class TestApply:
    def _apply(self, phone, form, city_topics=("Austin local news",
                                               "National and international news")):
        payload = {"phone": phone, "setup_pending": True}
        with patch("morning.default_topics", return_value=list(city_topics)), \
             patch("home.save") as save, \
             patch.object(onboard, "_build_async") as build, \
             patch("userprofile._eager_build_home"):
            ok = onboard.apply("tok", payload, form)
        return ok, save, build

    def test_writes_name_city_and_topics(self, store):
        form = _Form({"name": "Jeff", "city": "Austin, TX", "mornings": "on"},
                     {"interests": ["tech", "sports"]})
        ok, _, build = self._apply("+15550002222", form)
        assert ok is True
        p = db.get_profile("+15550002222")
        assert p["name"] == "Jeff"
        assert p["city"] == "Austin, TX"
        assert p["morning_enabled"] is True
        assert p["morning_onboarded"] is True
        assert p["setup_done"] is True
        assert "AI and technology news" in p["morning_topics"]
        assert "Sports news" in p["morning_topics"]
        build.assert_called_once()

    def test_local_news_is_seeded_because_the_city_is_known_first(self, store):
        """The whole point of the form. update_morning_briefing's dispatch seeds
        from `profile["city"]`, which is still empty during the reply that turns
        mornings on — so the chat path silently seeds national news alone."""
        form = _Form({"name": "Jeff", "city": "Austin, TX"}, {"interests": []})
        self._apply("+15550002223", form)
        topics = db.get_profile("+15550002223")["morning_topics"]
        assert "Austin local news" in topics

    def test_timezone_is_derived_from_the_typed_city(self, store):
        """Without it every local_today() call degrades to UTC and the morning
        job has no local clock to aim at."""
        form = _Form({"name": "Jeff", "city": "Austin, TX"}, {"interests": []})
        with patch("userprofile._derive_timezone", return_value="America/Chicago"):
            self._apply("+15550002224", form)
        assert db.get_profile("+15550002224")["timezone"] == "America/Chicago"

    def test_second_submit_is_refused(self, store):
        """The token is the page's only protection and a form turns a read key
        into a write key, so the write is one-shot."""
        form = _Form({"name": "Jeff", "city": "Austin, TX"}, {"interests": []})
        self._apply("+15550002225", form)
        again = _Form({"name": "Mallory", "city": "Nowhere"}, {"interests": []})
        ok, _, _ = self._apply_already_done("+15550002225", again)
        assert ok is False
        assert db.get_profile("+15550002225")["name"] == "Jeff"

    def _apply_already_done(self, phone, form):
        payload = {"phone": phone, "setup_pending": False}
        with patch.object(onboard, "_build_async") as build:
            ok = onboard.apply("tok", payload, form)
        return ok, None, build

    def test_mornings_unchecked_stays_off(self, store):
        form = _Form({"name": "Jeff", "city": "Austin, TX"}, {"interests": []})
        self._apply("+15550002226", form)
        assert db.get_profile("+15550002226")["morning_enabled"] is False

    def test_closes_the_window_before_writing(self, store):
        form = _Form({"name": "Jeff", "city": "Austin, TX"}, {"interests": []})
        _, save, _ = self._apply("+15550002227", form)
        assert save.call_args_list[0][0][1] == {"phone": "+15550002227",
                                               "setup_pending": False}


class TestTopics:
    def test_unknown_chip_keys_are_dropped(self):
        with patch("morning.default_topics", return_value=["National and international news"]):
            topics = onboard._topics_for("Austin", ["tech", "'; DROP TABLE--", "nope"])
        assert topics == ["National and international news", "AI and technology news"]

    def test_capped(self):
        with patch("morning.default_topics", return_value=["a", "b"]):
            topics = onboard._topics_for("Austin", [k for k, _, _ in onboard.INTERESTS])
        assert len(topics) == onboard.TOPIC_MAX

    def test_labels_and_queries_are_deliberately_different(self):
        """A topic is a search query, not a tag: `_search_raw` matches on query
        text, and "Top national news" once returned a Clemson ROTC story on a
        literal word match. Labels stay human, topics stay subject-shaped."""
        for key, label, topic in onboard.INTERESTS:
            assert topic.strip() and topic != label
            assert topic.lower().endswith("news")


class TestForm:
    def test_renders_every_interest_and_posts_to_itself(self):
        out = onboard.render_setup("tok", action="https://palmer.test/h/tok")
        assert 'method=post action="https://palmer.test/h/tok"' in out
        import html as _html
        for key, label, _ in onboard.INTERESTS:
            assert f'value="{key}"' in out
            assert _html.escape(label) in out

    def test_no_javascript_and_no_external_requests(self):
        """Same constraint as page.py — it opens on a phone over a cell
        connection and nowhere else."""
        out = onboard.render_setup("tok", action="/h/tok")
        assert "<script" not in out.lower()
        assert "http://" not in out
        assert out.count("https://") == out.count('action="https://')

    def test_action_is_escaped(self):
        out = onboard.render_setup("tok", action='/h/tok" onload="x')
        assert 'onload="x' not in out


class TestNeedsSetup:
    def test_pending_payload(self):
        assert onboard.needs_setup({"setup_pending": True}) is True

    def test_a_real_page_is_not_a_form(self):
        assert onboard.needs_setup({"built_at": 1.0}) is False

    def test_missing_payload(self):
        assert onboard.needs_setup(None) is False
