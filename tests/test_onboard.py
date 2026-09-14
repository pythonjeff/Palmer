"""Setup form: the link on message one, the one-shot write, and what it seeds.

The regressions these hold are the ones the flow exists to fix — a city that
lands too late to seed local news — plus the two rules that shape it: the link
goes out at the setup moment through get_my_page, never on message one, and
the box is free text so topics arrive subject-shaped.
"""
from unittest.mock import patch

import pytest

from palmer import db
from palmer import onboard


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "onboard.db", raising=False)
    monkeypatch.setenv("APP_URL", "https://palmer.test")
    db.init_db()
    return db


class _Form(dict):
    """Starlette's FormData interface, as much of it as onboard.apply uses."""


class TestStart:
    def test_mints_a_token_and_parks_a_stub(self, store):
        with patch("palmer.home.save") as save, patch("palmer.home.load", return_value=None):
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
        with patch("palmer.home.save") as save, patch("palmer.home.load", return_value={"built_at": 1}):
            onboard.start("+15550001111")
        save.assert_not_called()

    def test_never_raises(self, store):
        with patch("palmer.home.home_token", side_effect=RuntimeError("db down")):
            assert onboard.start("+15550001111") is None


class TestApply:
    def _apply(self, phone, form, city_topics=("Austin local news",
                                               "National and international news")):
        payload = {"phone": phone, "setup_pending": True}
        with patch("palmer.morning.default_topics", return_value=list(city_topics)), \
             patch("palmer.home.save") as save, \
             patch.object(onboard, "_build_async") as build, \
             patch("palmer.agent._normalize_price_topic", side_effect=lambda t: t), \
             patch("palmer.userprofile._eager_build_home"):
            ok = onboard.apply("tok", payload, form)
        return ok, save, build

    def test_writes_name_city_and_topics(self, store):
        form = _Form({"name": "Jeff", "city": "Austin, TX", "mornings": "on",
                      "follows": "Eagles, AI"})
        ok, _, build = self._apply("+15550002222", form)
        assert ok is True
        p = db.get_profile("+15550002222")
        assert p["name"] == "Jeff"
        assert p["city"] == "Austin, TX"
        assert p["morning_enabled"] is True
        assert p["morning_onboarded"] is True
        assert p["setup_done"] is True
        assert "Eagles" in p["morning_topics"]
        assert "AI" in p["morning_topics"]
        build.assert_called_once()

    def test_local_news_is_seeded_because_the_city_is_known_first(self, store):
        """The whole point of the form. update_morning_briefing's dispatch seeds
        from `profile["city"]`, which is still empty during the reply that turns
        mornings on — so the chat path silently seeds national news alone."""
        form = _Form({"name": "Jeff", "city": "Austin, TX"})
        self._apply("+15550002223", form)
        topics = db.get_profile("+15550002223")["morning_topics"]
        assert "Austin local news" in topics

    def test_timezone_is_derived_from_the_typed_city(self, store):
        """Without it every local_today() call degrades to UTC and the morning
        job has no local clock to aim at."""
        form = _Form({"name": "Jeff", "city": "Austin, TX"})
        with patch("palmer.userprofile._derive_timezone", return_value="America/Chicago"):
            self._apply("+15550002224", form)
        assert db.get_profile("+15550002224")["timezone"] == "America/Chicago"

    def test_second_submit_is_refused(self, store):
        """The token is the page's only protection and a form turns a read key
        into a write key, so the write is one-shot."""
        form = _Form({"name": "Jeff", "city": "Austin, TX"})
        self._apply("+15550002225", form)
        again = _Form({"name": "Mallory", "city": "Nowhere"})
        ok, _, _ = self._apply_already_done("+15550002225", again)
        assert ok is False
        assert db.get_profile("+15550002225")["name"] == "Jeff"

    def _apply_already_done(self, phone, form):
        payload = {"phone": phone, "setup_pending": False}
        with patch.object(onboard, "_build_async") as build:
            ok = onboard.apply("tok", payload, form)
        return ok, None, build

    def test_mornings_unchecked_stays_off(self, store):
        form = _Form({"name": "Jeff", "city": "Austin, TX"})
        self._apply("+15550002226", form)
        assert db.get_profile("+15550002226")["morning_enabled"] is False

    def test_closes_the_window_before_writing(self, store):
        form = _Form({"name": "Jeff", "city": "Austin, TX"})
        _, save, _ = self._apply("+15550002227", form)
        assert save.call_args_list[0][0][1] == {"phone": "+15550002227",
                                               "setup_pending": False}


class TestTopics:
    def _topics(self, city, follows, base=("National and international news",)):
        with patch("palmer.morning.default_topics", return_value=list(base)), \
             patch("palmer.agent._normalize_price_topic", side_effect=lambda t: t):
            return onboard._topics_for(city, follows)

    def test_free_text_splits_on_commas_not_and(self):
        """"Simon and Garfunkel" is one thing; a comma is the separator."""
        assert self._topics("Austin", "Eagles, Simon and Garfunkel; AI\nNvidia stock") == [
            "National and international news", "Eagles", "Simon and Garfunkel", "AI",
            "Nvidia stock"]

    def test_blank_and_duplicates_dropped(self):
        assert self._topics("Austin", " , eagles, Eagles, . ") == [
            "National and international news", "eagles"]

    def test_capped(self):
        many = ", ".join(f"t{i}" for i in range(20))
        assert len(self._topics("Austin", many)) == onboard.TOPIC_MAX

    def test_each_entry_gets_the_texted_topic_normalization(self):
        """"nvidia stock" typed on the form resolves a ticker exactly as it
        would texted — same pass, so Markets renders either way."""
        with patch("palmer.morning.default_topics", return_value=[]), \
             patch("palmer.agent._normalize_price_topic",
                   side_effect=lambda t: f"{t} (NVDA)" if "nvidia" in t.lower() else t):
            assert onboard._topics_for("Austin", "nvidia stock, AI") == [
                "nvidia stock (NVDA)", "AI"]

    def test_normalizer_failure_keeps_the_raw_topic(self):
        with patch("palmer.morning.default_topics", return_value=[]), \
             patch("palmer.agent._normalize_price_topic", side_effect=RuntimeError("yahoo down")):
            assert onboard._topics_for("Austin", "Eagles") == ["Eagles"]


class TestForm:
    def test_posts_to_itself_with_a_free_text_follows_box(self):
        out = onboard.render_setup("tok", action="https://palmer.test/h/tok")
        assert 'method=post action="https://palmer.test/h/tok"' in out
        assert 'name=follows' in out
        assert 'type=checkbox name=interests' not in out

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


class TestWhereTheLinkGoesOut:
    """The setup page is handed out by get_my_page at the setup moment, never
    appended to a stranger's first message — a wrong number or someone asking
    what Bitcoin is at should get an answer, not a form."""

    def test_message_one_appends_nothing(self):
        import inspect
        from palmer import main
        src = inspect.getsource(main._handle_sms_inner)
        assert "onboard" not in src and "setup" not in src

    def test_get_my_page_serves_the_setup_page_when_there_is_no_city(self):
        import inspect
        from palmer import agent
        src = inspect.getsource(agent.get_reply)
        branch = src[src.index('b.name == "get_my_page"'):]
        branch = branch[:branch.index("elif b.name")]
        assert "onboard import start" in branch
        assert 'profile.get("city")' in branch
        assert "Do NOT ask for their name or city" in branch

    def test_prompt_routes_setup_through_get_my_page(self):
        from palmer.prompts import SYSTEM_PROMPT
        assert "call get_my_page — it returns their setup page" in SYSTEM_PROMPT
        assert "what should I call you, and what city are you in" not in SYSTEM_PROMPT
        assert "THE SETUP LINK" not in SYSTEM_PROMPT


class TestCityLandingInChat:
    """The other half of the seed. The dispatch seeds from profile["city"],
    which is empty on the turn a user says "Jeff, Austin, set it up" — the
    extractor runs after the reply — so that path seeded national news alone
    while Palmer said "local news". The topic is added where the city lands."""

    def _land(self, store, phone, before):
        from palmer import userprofile
        db.upsert_profile(phone, before)
        with patch("palmer.morning.default_topics",
                   side_effect=lambda c: ["Austin local news", "National and international news"]
                   if c else ["National and international news"]), \
             patch("palmer.userprofile._derive_timezone", return_value="America/Chicago"), \
             patch("palmer.userprofile._eager_build_home"):
            userprofile._apply_profile_updates(phone, db.get_profile(phone), {"city": "Austin, TX"})
        return db.get_profile(phone)["morning_topics"]

    def test_local_topic_added_when_city_first_lands(self, store):
        topics = self._land(store, "+15550003331",
                            {"morning_onboarded": True,
                             "morning_topics": ["National and international news"]})
        assert topics == ["Austin local news", "National and international news"]

    def test_not_seeded_for_someone_who_never_turned_mornings_on(self, store):
        assert self._land(store, "+15550003332", {"morning_topics": []}) == []

    def test_eager_build_replaces_a_parked_setup_form(self, store):
        """Got the link, never tapped it, then said their city in chat: the
        stub must not block the build or their address stays a form."""
        from palmer import userprofile
        with patch("palmer.home.home_token", return_value="tok"), \
             patch("palmer.home.load", return_value={"phone": "+1", "setup_pending": True}), \
             patch("palmer.home.rebuild") as rebuild:
            userprofile._eager_build_home("+15550003333")
        rebuild.assert_called_once()
