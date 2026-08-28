"""Stale facts, and the three places they leaked into what Palmer says.

The whole profile is dumped into every system prompt as CURRENT fact and
nothing in it ever expired. One profile read `city: "Culver City"` three lines
above `life_context: "Based in LA"` — both true when written, and together the
exact contradiction behind a week of Los Angeles temperatures arriving under
the name Culver City. Another still carried `stressed_about: "active fire
emergency in LA area"` long after the fire, and two carried a `follow_up` full
of notes about Palmer's own delivery rather than anything about the person.

None of that was the model being confused by volume — the system prompt and
tools are about 13k tokens and the profile 1-3k, which is comfortable. It was
the model being told, every turn, things that had stopped being true.

Three mechanisms here: facts age and then disappear, `city` is declared
authoritative over anything else that names a place, and the card reads the
user's clock rather than the dyno's.
"""
from datetime import date, timedelta
from unittest.mock import patch

import agent
import artifacts
import home
import userprofile


def _aged(field, days, value="something"):
    return {"city": "Culver City", "timezone": "America/Los_Angeles", field: value,
            "field_dates": {field: (date.today() - timedelta(days=days)).isoformat()}}


class TestVolatileFactsAge:
    def test_a_fresh_fact_is_shown_plainly(self):
        out = userprofile.fresh_profile_for_prompt(_aged("stressed_about", 0))
        assert out["stressed_about"] == "something"

    def test_a_few_days_old_carries_its_date(self):
        """The model should be able to tell a live worry from a week-old one."""
        out = userprofile.fresh_profile_for_prompt(_aged("stressed_about", 6))
        assert out["stressed_about"]["value"] == "something"
        assert out["stressed_about"]["days_old"] == 6

    def test_past_its_life_it_disappears(self):
        life = userprofile.VOLATILE_FIELDS["stressed_about"]
        out = userprofile.fresh_profile_for_prompt(_aged("stressed_about", life + 1))
        assert "stressed_about" not in out

    def test_durable_facts_never_expire(self):
        """Name, city, job and relationships do not rot, and dating them would
        invite the model to doubt things it should not."""
        old = {"name": "Danny", "city": "Culver City", "job": "producer",
               "field_dates": {"name": "2020-01-01"}}
        out = userprofile.fresh_profile_for_prompt(old)
        assert out["name"] == "Danny" and out["city"] == "Culver City"
        for f in ("name", "city", "job"):
            assert f not in userprofile.VOLATILE_FIELDS

    def test_an_unstamped_fact_is_left_alone(self):
        """Profiles written before stamping existed must not vanish wholesale."""
        out = userprofile.fresh_profile_for_prompt({"stressed_about": "x", "city": "Y"})
        assert out["stressed_about"] == "x"

    def test_storage_is_untouched(self):
        """A fact that went quiet was not wrong — the consolidator may reassert
        it tomorrow, so nothing is deleted from the row."""
        p = _aged("stressed_about", 999)
        userprofile.fresh_profile_for_prompt(p)
        assert p["stressed_about"] == "something"

    def test_writing_a_volatile_field_stamps_it(self):
        updates = {"stressed_about": "a deadline"}
        userprofile._stamp_volatile({"timezone": "America/Chicago"}, updates)
        assert updates["field_dates"]["stressed_about"] == date.today().isoformat()

    def test_writing_a_durable_field_stamps_nothing(self):
        updates = {"name": "Jeff"}
        userprofile._stamp_volatile({}, updates)
        assert "field_dates" not in updates

    def test_clearing_a_field_does_not_restamp_it(self):
        updates = {"stressed_about": None}
        userprofile._stamp_volatile({}, updates)
        assert "field_dates" not in updates


class TestCityIsAuthoritative:
    """`city` is the only location any tool reads. Everything else that names a
    place is background and may be months out of date."""

    def test_the_prompt_names_the_city_and_ranks_it(self):
        profile = {"city": "Culver City", "life_context": "Based in LA."}
        with patch.object(agent, "get_profile", return_value=profile):
            sys = agent._build_system("+1555")
        assert "Their location is Culver City, full stop" in sys
        assert "never pair a number with a place it did not come from" in sys

    def test_no_city_means_no_claim(self):
        with patch.object(agent, "get_profile", return_value={"name": "Jeff"}):
            sys = agent._build_system("+1555")
        assert "full stop" not in sys

    def test_stale_context_is_filtered_before_the_model_sees_it(self):
        life = userprofile.VOLATILE_FIELDS["life_context"]
        profile = {"city": "Culver City", "timezone": "America/Los_Angeles",
                   "life_context": "Based in LA.",
                   "field_dates": {"life_context": (date.today() - timedelta(days=life + 1)).isoformat()}}
        with patch.object(agent, "get_profile", return_value=profile):
            sys = agent._build_system("+1555")
        assert "Based in LA" not in sys


class TestEmptySectionsDoNotLockForTheFullWindow:
    """The `_tried` stamp is written before the call so a failure cannot loop.
    That also meant one empty fetch left a section blank for its whole window —
    it locked three of four users out of Opening for a day, twice."""

    def test_a_section_holding_data_waits_the_full_window(self):
        assert home._window_for("opening", True) == home.STALE["opening"]

    def test_an_empty_section_retries_sooner(self):
        assert home._window_for("opening", False) < home.STALE["opening"]
        assert home._window_for("headlines", False) < home.STALE["headlines"]

    def test_the_retry_is_bounded_not_a_loop(self):
        for section in ("opening", "headlines"):
            assert home._window_for(section, False) >= home.EMPTY_RETRY_FLOOR

    def test_a_never_on_view_section_stays_that_way(self):
        with patch.dict(home.STALE, {"opening": None}):
            assert home._window_for("opening", False) is None

    def test_the_gates_read_whether_data_exists(self):
        import inspect
        src = inspect.getsource(home.refresh_stale)
        assert 'payload.get("opening")' in src and 'payload.get("headlines")' in src


class TestTheCardUsesTheReadersClock:
    """page.py has always rendered the user's local day; cards.py defaulted to
    datetime.now(), which is UTC on the dyno. From 5pm Pacific the card printed
    tomorrow's date beside a page printing today's."""

    def test_the_masthead_follows_the_profile_timezone(self):
        la = artifacts._card_now({"timezone": "America/Los_Angeles"})
        chi = artifacts._card_now({"timezone": "America/Chicago"})
        assert la.utcoffset() != chi.utcoffset()

    def test_a_missing_timezone_still_renders(self):
        assert artifacts._card_now({}) is not None
        assert artifacts._card_now({"timezone": "Not/AZone"}) is not None

    def test_the_renderer_is_told_the_time(self):
        import inspect
        assert "when=_card_now(payload)" in inspect.getsource(artifacts.render_png)

    def test_the_cache_key_uses_the_readers_day(self):
        """Otherwise the cached card outlives the reader's midnight."""
        import inspect
        assert "_card_now(payload)" in inspect.getsource(artifacts._card_inputs)


class TestTheExtractorStopsRecordingItself:
    def test_the_prompt_forbids_meta_facts(self):
        """follow_up held "confirm_morning_briefing_delivery_is_consistent_daily"
        for one user and "Maintain single-message format" for another — notes
        about the product, read back as facts about a person."""
        import prompts
        assert "never about Palmer's own operation" in prompts.EXTRACT_PROMPT
        assert "return nothing for these fields" in prompts.EXTRACT_PROMPT
