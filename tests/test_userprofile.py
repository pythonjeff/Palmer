"""The profile: schema, freshness, overlap, onboarding.

Merged from test_profile_schema.py, test_profile_freshness.py, test_user_covered.py, test_onboard.py; each section keeps its
original file's notes, because those carry the incident that led to the test.
"""
import pytest
from unittest.mock import patch, MagicMock
from palmer import db, userprofile, agent, artifacts, home, onboard
from scripts import migrate_profile_prune as mig
from datetime import date, timedelta
from tests.helpers import llm_reply


# ============================================================================
# from test_profile_schema.py
# ============================================================================
#
# Tests for the profile allow-list and the pruning migration.
#
# The extractor is a language model writing straight into a dict that gets dumped
# as JSON into every system prompt. Unbounded, one profile reached 624 keys — 604
# invented one-offs, ~21,700 tokens of noise per message. These pin the bound.

class TestAllowList:
    def test_known_fields_survive(self):
        out = userprofile._canonical_updates({"city": "Kirkwood", "interests": ["golf"]})
        assert out == {"city": "Kirkwood", "interests": ["golf"]}

    def test_invented_fields_are_dropped(self):
        out = userprofile._canonical_updates({
            "city": "Kirkwood", "monday_night_behavior": "watches film",
            "kendrick_fan": True, "alternatively": "x",
        })
        assert out == {"city": "Kirkwood"}

    def test_aliases_still_map_and_retire(self):
        out = userprofile._canonical_updates({"location": "Kirkwood"})
        assert out["city"] == "Kirkwood"
        assert out["location"] is None, "the alias must be cleared, not left behind"

    def test_every_field_the_code_reads_is_allowed(self):
        """A key missing from PROFILE_FIELDS would be silently discarded on write."""
        for field in ("city", "timezone", "morning_topics", "morning_time",
                      "morning_onboarded", "morning_prefs", "morning_sent_date",
                      "commute", "communication_style", "reactions",
                      "reactions_folded_count", "conversation_topics",
                      "pending_morning_suggestion", "pending_preference_notice",
                      "followup_sent_date", "intro_sent",
                      "ongoing_threads", "life_context",
                      "onboarding_ask_sent", "score_offer_sent", "followed_teams"):
            assert field in userprofile.PROFILE_FIELDS, field


class TestPrune:
    def test_splits_kept_from_dropped(self):
        kept, dropped = userprofile.prune_profile({"city": "K", "kendrick_fan": True})
        assert kept == {"city": "K"} and dropped == ["kendrick_fan"]

    def test_pure(self):
        profile = {"city": "K", "junk": 1}
        userprofile.prune_profile(profile)
        assert "junk" in profile, "prune must not mutate its input"

    def test_empty(self):
        assert userprofile.prune_profile({}) == ({}, [])


class TestNoneDeletes:
    """A stored null still costs prompt tokens. None must remove the key."""

    def test_none_removes_the_key(self, fresh_db):
        with patch.object(db, "_conn", db._conn):
            db.init_db()
            db.upsert_profile("+1555", {"city": "Kirkwood", "vibe": "dry"})
            db.upsert_profile("+1555", {"vibe": None})
            prof = db.get_profile("+1555")
        assert prof["city"] == "Kirkwood"
        assert "vibe" not in prof, "None should delete, not store a null"

    def test_deleting_an_absent_key_is_a_noop(self, fresh_db):
        db.upsert_profile("+1556", {"city": "K"})
        db.upsert_profile("+1556", {"never_set": None})
        assert db.get_profile("+1556")["city"] == "K"


class TestTopicCleanup:
    ROUTE = ("Daily commute traffic: 33 Cedarbrook Lane, Kirkwood MO 63122 "
             "to 190 Carondelet Plaza, Clayton MO 63105")
    FORMAT = "Format: bullet points per subject, not one continuous paragraph"

    def test_directive_and_route_leave_the_topic_list(self):
        out = mig._clean_topics({"morning_topics": ["SpaceX news", self.FORMAT, self.ROUTE]})
        assert out["morning_topics"] == ["SpaceX news"]

    def test_route_is_promoted_not_discarded(self):
        out = mig._clean_topics({"morning_topics": [self.ROUTE]})
        assert out["commute"] == {
            "origin": "33 Cedarbrook Lane, Kirkwood MO 63122",
            "destination": "190 Carondelet Plaza, Clayton MO 63105",
        }

    def test_existing_commute_is_not_overwritten(self):
        existing = {"origin": "A Street, Town", "destination": "B Street, City"}
        out = mig._clean_topics({"commute": existing, "morning_topics": [self.ROUTE]})
        assert "commute" not in out
        assert out["morning_topics"] == []

    def test_clean_list_is_left_alone(self):
        assert mig._clean_topics({"morning_topics": ["SpaceX news", "LA weather"]}) == {}

    def test_no_topics(self):
        assert mig._clean_topics({}) == {}


class TestConsolidationSafety:
    def test_only_merge_targets_are_accepted(self):
        """The fold must never move a briefing time or clear a send guard."""
        from unittest.mock import MagicMock
        b = MagicMock(); b.text = '{"interests": ["golf"], "morning_time": "03:00", "city": "Nowhere"}'
        r = MagicMock(); r.content = [b]
        with patch.object(mig.client.messages, "create", return_value=r):
            out = mig._consolidate({}, {"junk": 1})
        assert out == {"interests": ["golf"]}
        assert "morning_time" not in out and "city" not in out

    def test_failure_prunes_without_merging(self):
        with patch.object(mig.client.messages, "create", side_effect=RuntimeError("boom")):
            assert mig._consolidate({}, {"junk": 1}) == {}

    def test_nothing_stray_means_no_api_call(self):
        with patch.object(mig.client.messages, "create") as create:
            assert mig._consolidate({"city": "K"}, {}) == {}
        create.assert_not_called()


class TestNameIsExtracted:
    """"My name is Jeff" returned {} from the extractor, so profile["name"]
    stayed empty while Palmer happily called the user Jeff from conversation
    history. The page reads the profile, so it showed "Your briefing" and kept
    asking for a name it had already been told twice."""

    def test_the_schema_demands_identity_explicitly(self):
        from palmer.prompts import EXTRACT_PROMPT
        assert "IDENTITY FIRST" in EXTRACT_PROMPT

    def test_it_names_the_phrasings_people_actually_use(self):
        from palmer.prompts import EXTRACT_PROMPT
        low = EXTRACT_PROMPT.lower()
        for phrase in ("my name is", "i'm jeff", "call me"):
            assert phrase in low

    def test_it_overrides_the_skip_when_already_present_instinct(self):
        """The failure was the model deciding the name was too obvious to
        bother returning, or assuming it must already be stored."""
        from palmer.prompts import EXTRACT_PROMPT
        low = EXTRACT_PROMPT.lower()
        assert "even when" in low and "already" in low

    def test_name_is_still_an_allowed_field(self):
        from palmer.userprofile import PROFILE_FIELDS
        assert "name" in PROFILE_FIELDS


class TestCityPrecisionIsExtracted:
    """A generic city mention ("LA traffic today") was overwriting a specific,
    correct one ("Culver City, CA") already on file, because EXTRACT_PROMPT had
    no rule distinguishing "where they live" from "a place they mentioned"."""

    def test_the_schema_has_a_location_precision_rule(self):
        from palmer.prompts import EXTRACT_PROMPT
        assert "LOCATION PRECISION" in EXTRACT_PROMPT

    def test_it_excludes_passing_mentions(self):
        from palmer.prompts import EXTRACT_PROMPT
        low = EXTRACT_PROMPT.lower()
        for phrase in ("in passing", "traffic into la", "leave the existing value alone"):
            assert phrase in low

    def test_consolidate_prompt_also_guards_city(self):
        from palmer.prompts import CONSOLIDATE_PROMPT
        low = CONSOLIDATE_PROMPT.lower()
        assert '"city"' in low and "leave it unchanged" in low


class TestCityChangeIsLogged:
    def test_city_regression_prints_old_and_new(self, fresh_db, monkeypatch, capsys):
        monkeypatch.setenv("APP_URL", "https://palmer.test")
        db.upsert_profile("+1557", {"city": "Culver City, CA"})
        profile = db.get_profile("+1557")
        with patch("palmer.home.rebuild") as rebuild:
            userprofile._apply_profile_updates("+1557", profile, {"city": "Los Angeles"})
        out = capsys.readouterr().out
        assert "Culver City, CA" in out and "Los Angeles" in out
        rebuild.assert_not_called(), "a correction to an existing city is not a day-1 build"


class TestEagerHomeBuild:
    """The first time a user's city becomes known, Palmer Home gets built right
    away rather than waiting for get_my_page or the morning job — see CLAUDE.md
    "Onboarding" / the day-1 site build. It never sends the link; that's still
    gated on an explicit ask or the first morning send, unchanged."""

    def test_first_city_triggers_a_build(self, fresh_db, monkeypatch):
        monkeypatch.setenv("APP_URL", "https://palmer.test")
        db.upsert_profile("+1558", {})
        profile = db.get_profile("+1558")
        with patch("palmer.home.home_token", return_value="tok"), \
             patch("palmer.home.load", return_value=None), \
             patch("palmer.home.rebuild") as rebuild:
            userprofile._apply_profile_updates("+1558", profile, {"city": "Chicago"})
        rebuild.assert_called_once_with("+1558", refresh_news=True)

    def test_city_correction_does_not_rebuild(self, fresh_db, monkeypatch):
        monkeypatch.setenv("APP_URL", "https://palmer.test")
        db.upsert_profile("+1559", {"city": "Culver City, CA"})
        profile = db.get_profile("+1559")
        with patch("palmer.home.rebuild") as rebuild:
            userprofile._apply_profile_updates("+1559", profile, {"city": "Los Angeles"})
        rebuild.assert_not_called()

    def test_no_app_url_skips_the_build(self, fresh_db, monkeypatch):
        monkeypatch.delenv("APP_URL", raising=False)
        db.upsert_profile("+1560", {})
        profile = db.get_profile("+1560")
        with patch("palmer.home.rebuild") as rebuild:
            userprofile._apply_profile_updates("+1560", profile, {"city": "Chicago"})
        rebuild.assert_not_called()

    def test_a_page_already_built_is_not_rebuilt_again(self, fresh_db, monkeypatch):
        monkeypatch.setenv("APP_URL", "https://palmer.test")
        db.upsert_profile("+1561", {})
        profile = db.get_profile("+1561")
        with patch("palmer.home.home_token", return_value="tok"), \
             patch("palmer.home.load", return_value={"city": "Chicago"}), \
             patch("palmer.home.rebuild") as rebuild:
            userprofile._apply_profile_updates("+1561", profile, {"city": "Chicago"})
        rebuild.assert_not_called()


class TestOnboardingAskConsumption:
    """The ONBOARDING ASK block in _build_system (agent.py) fires under this
    exact condition; _update_profile marks it consumed the first time it sees
    that same condition hold true after a turn's extraction runs."""

    def test_marks_consumed_after_a_qualifying_turn(self, fresh_db):
        db.upsert_profile("+1562", {"intro_sent": True})
        block = MagicMock(); block.text = "{}"
        resp = MagicMock(); resp.content = [block]
        with patch.object(userprofile.client.messages, "create", return_value=resp):
            userprofile._update_profile("+1562", "hey", "hey, how's it going")
        assert db.get_profile("+1562")["onboarding_ask_sent"] is True

    def test_not_marked_when_the_turn_supplies_both_fields(self, fresh_db, monkeypatch):
        monkeypatch.delenv("APP_URL", raising=False)
        db.upsert_profile("+1563", {"intro_sent": True})
        block = MagicMock(); block.text = '{"name": "Ada", "city": "Chicago"}'
        resp = MagicMock(); resp.content = [block]
        with patch.object(userprofile.client.messages, "create", return_value=resp):
            userprofile._update_profile("+1563", "I'm Ada from Chicago", "hey Ada")
        assert "onboarding_ask_sent" not in db.get_profile("+1563")

    def test_not_marked_before_intro_is_sent(self, fresh_db):
        db.upsert_profile("+1564", {})
        block = MagicMock(); block.text = "{}"
        resp = MagicMock(); resp.content = [block]
        with patch.object(userprofile.client.messages, "create", return_value=resp):
            userprofile._update_profile("+1564", "hey", "hey")
        assert "onboarding_ask_sent" not in db.get_profile("+1564")


class TestNoFieldIsAlsoAnAlias:
    """`teams` shipped as a real PROFILE_FIELD while `_PROFILE_ALIASES` still
    mapped it to `sports_teams`. `_normalize_profile` runs on every inbound
    message, so `follow_team` stored a follow list, Palmer confirmed it, and the
    next message migrated it into `sports_teams` and wrote `teams: None` —
    the follow silently gone, its games out of the morning, and the interest collector then
    raising AttributeError on a dict from a call site outside a try.
    """

    def test_an_alias_key_is_never_a_real_field(self):
        from palmer import userprofile as up
        clashes = {k: v for k, v in up._PROFILE_ALIASES.items() if k in up.PROFILE_FIELDS}
        assert not clashes, f"these fields are silently rewritten on write: {clashes}"

    def test_the_structured_follow_lists_survive_a_normalise(self):
        from palmer import userprofile as up
        for field in ("followed_teams", "shows"):
            row = [{"league": "nfl", "abbrev": "PHI", "name": "Philadelphia Eagles"}]
            out = up._canonical_updates({field: row})
            assert out.get(field) == row, f"{field} did not survive canonicalisation"

    def test_the_tool_written_lists_are_not_in_the_extractor_schema(self):
        """They hold structured dicts written by tool dispatch. In the schema,
        Haiku fills them with prose and downstream code gets strings."""
        from palmer import prompts
        for field in ("followed_teams", "shows", "weather_locations"):
            assert f'"{field}"' not in prompts.EXTRACT_PROMPT


# ============================================================================
# from test_profile_freshness.py
# ============================================================================
#
# Stale facts, and the three places they leaked into what Palmer says.
#
# The whole profile is dumped into every system prompt as CURRENT fact and
# nothing in it ever expired. One profile read `city: "Culver City"` three lines
# above `life_context: "Based in LA"` — both true when written, and together the
# exact contradiction behind a week of Los Angeles temperatures arriving under
# the name Culver City. Another still carried `stressed_about: "active fire
# emergency in LA area"` long after the fire, and two carried a `follow_up` full
# of notes about Palmer's own delivery rather than anything about the person.
#
# None of that was the model being confused by volume — the system prompt and
# tools are about 13k tokens and the profile 1-3k, which is comfortable. It was
# the model being told, every turn, things that had stopped being true.
#
# Three mechanisms here: facts age and then disappear, `city` is declared
# authoritative over anything else that names a place, and the card reads the
# user's clock rather than the dyno's.

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
        old = {"name": "Sam", "city": "Springfield", "job": "teacher",
               "field_dates": {"name": "2020-01-01"}}
        out = userprofile.fresh_profile_for_prompt(old)
        assert out["name"] == "Sam" and out["city"] == "Springfield"
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

    def test_a_missing_timezone_asserts_no_day_at_all(self):
        """It used to fall back to a naive datetime.now() — UTC on the dyno —
        and print that unlabelled as the reader's day, which is the exact
        thing clock_block was rewritten to stop doing. page.py omits the date
        in this case, so the card has to as well or the two disagree."""
        assert artifacts._card_now({}) is None
        assert artifacts._card_now({"timezone": "Not/AZone"}) is None

    def test_the_card_still_renders_without_a_day(self):
        from palmer import cards
        png = cards.render_dashboard(city="Austin", weather=None, traffic=None,
                                     prices=None, headlines=None, show_date=False)
        assert png[:4] == b"\x89PNG"

    def test_the_page_omits_it_too(self):
        from palmer import page
        assert page._local_day(None) == ""
        assert page._local_day("Not/AZone") == ""
        assert page._local_day("America/Chicago") != ""

    def test_the_renderer_is_told_the_time(self):
        import inspect
        src = inspect.getsource(artifacts.render_png)
        assert "_card_now(payload)" in src
        assert "show_date=" in src

    def test_the_cache_key_uses_the_readers_day(self):
        """Otherwise the cached card outlives the reader's midnight."""
        import inspect
        assert "_card_now(payload)" in inspect.getsource(artifacts._card_inputs)


class TestTheExtractorStopsRecordingItself:
    def test_the_prompt_forbids_meta_facts(self):
        """follow_up held "confirm_morning_briefing_delivery_is_consistent_daily"
        for one user and "Maintain single-message format" for another — notes
        about the product, read back as facts about a person."""
        from palmer import prompts
        assert "never about Palmer's own operation" in prompts.EXTRACT_PROMPT
        assert "return nothing for these fields" in prompts.EXTRACT_PROMPT


# ============================================================================
# from test_user_covered.py
# ============================================================================
#
# Tests for _user_already_covered — suppresses proactive sends when the user
# already brought up the same story themselves in their recent messages.

class TestUserAlreadyCovered:
    def test_no_recent_user_messages_returns_false_without_haiku(self):
        with patch("palmer.db.get_recent_user_messages", return_value=[]), \
             patch("palmer.userprofile.client") as mock_client:
            assert userprofile._user_already_covered("+15550000000", "Iran launched strikes") is False
            mock_client.messages.create.assert_not_called()

    def test_yes_reply_suppresses(self):
        with patch("palmer.db.get_recent_user_messages", return_value=[
            "did you see the Iran thing?",
            "wild what's happening over there",
        ]), patch("palmer.userprofile.client") as mock_client:
            mock_client.messages.create.return_value = llm_reply("YES")
            assert userprofile._user_already_covered(
                "+15550000000",
                "Iran launched missiles at a US base in Iraq."
            ) is True

    def test_no_reply_allows_send(self):
        with patch("palmer.db.get_recent_user_messages", return_value=[
            "grocery list: milk, bread",
            "what's the weather tomorrow?",
        ]), patch("palmer.userprofile.client") as mock_client:
            mock_client.messages.create.return_value = llm_reply("NO")
            assert userprofile._user_already_covered(
                "+15550000000",
                "Cardinals move into first place with 4-2 win."
            ) is False

    def test_haiku_failure_fails_open(self):
        """Fail-open semantics — a broken Haiku call must NOT silently suppress
        real alerts. Better to send a possibly-duplicate than to drop a real one."""
        with patch("palmer.db.get_recent_user_messages", return_value=["did you see that"]), \
             patch("palmer.userprofile.client") as mock_client:
            mock_client.messages.create.side_effect = RuntimeError("boom")
            assert userprofile._user_already_covered("+15550000000", "some alert") is False

    def test_prompt_shape_includes_recent_and_candidate(self):
        captured = []

        def _create(**kwargs):
            captured.append(kwargs["messages"][0]["content"])
            return llm_reply("NO")

        with patch("palmer.db.get_recent_user_messages",
                   return_value=["did you see the Iran thing", "crazy"]), \
             patch("palmer.userprofile.client") as mock_client:
            mock_client.messages.create.side_effect = _create
            userprofile._user_already_covered("+15550000000", "Iran launched missiles.")
        prompt = captured[0]
        assert "Iran launched missiles" in prompt
        assert "Iran thing" in prompt
        # Prompt should distinguish specific-event awareness from general-topic mention
        low = prompt.lower()
        assert "did you see" in low or "have you heard" in low
        assert "general topic" in low or "background chatter" in low

    def test_window_hours_default_is_12(self):
        """The 12h default is intentional — user mentioning a story in the morning
        should suppress an afternoon alert on the same story."""
        # Check the signature default without invoking Haiku
        import inspect
        sig = inspect.signature(userprofile._user_already_covered)
        assert sig.parameters["window_hours"].default == 12


# ============================================================================
# from test_onboard.py
# ============================================================================
#
# Setup form: the link on message one, the one-shot write, and what it seeds.
#
# The regressions these hold are the ones the flow exists to fix — a city that
# lands too late to seed local news — plus the two rules that shape it: the link
# goes out at the setup moment through get_my_page, never on message one, and
# the box is free text so topics arrive subject-shaped.

@pytest.fixture
def store(fresh_db, monkeypatch):
    monkeypatch.setenv("APP_URL", "https://palmer.test")
    return fresh_db


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
