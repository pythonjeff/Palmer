"""Tests for the profile allow-list and the pruning migration.

The extractor is a language model writing straight into a dict that gets dumped
as JSON into every system prompt. Unbounded, one profile reached 624 keys — 604
invented one-offs, ~21,700 tokens of noise per message. These pin the bound.
"""
from unittest.mock import patch, MagicMock

import db
import userprofile
import migrate_profile_prune as mig


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
                      "alert_sent_date", "followup_sent_date", "intro_sent",
                      "interest_genres", "ongoing_threads", "life_context",
                      "onboarding_ask_sent"):
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

    def test_none_removes_the_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "_DB_PATH", tmp_path / "t.db", raising=False)
        with patch.object(db, "_conn", db._conn):
            db.init_db()
            db.upsert_profile("+1555", {"city": "Kirkwood", "vibe": "dry"})
            db.upsert_profile("+1555", {"vibe": None})
            prof = db.get_profile("+1555")
        assert prof["city"] == "Kirkwood"
        assert "vibe" not in prof, "None should delete, not store a null"

    def test_deleting_an_absent_key_is_a_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "_DB_PATH", tmp_path / "t2.db", raising=False)
        db.init_db()
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
        from prompts import EXTRACT_PROMPT
        assert "IDENTITY FIRST" in EXTRACT_PROMPT

    def test_it_names_the_phrasings_people_actually_use(self):
        from prompts import EXTRACT_PROMPT
        low = EXTRACT_PROMPT.lower()
        for phrase in ("my name is", "i'm jeff", "call me"):
            assert phrase in low

    def test_it_overrides_the_skip_when_already_present_instinct(self):
        """The failure was the model deciding the name was too obvious to
        bother returning, or assuming it must already be stored."""
        from prompts import EXTRACT_PROMPT
        low = EXTRACT_PROMPT.lower()
        assert "even when" in low and "already" in low

    def test_name_is_still_an_allowed_field(self):
        from userprofile import PROFILE_FIELDS
        assert "name" in PROFILE_FIELDS


class TestCityPrecisionIsExtracted:
    """A generic city mention ("LA traffic today") was overwriting a specific,
    correct one ("Culver City, CA") already on file, because EXTRACT_PROMPT had
    no rule distinguishing "where they live" from "a place they mentioned"."""

    def test_the_schema_has_a_location_precision_rule(self):
        from prompts import EXTRACT_PROMPT
        assert "LOCATION PRECISION" in EXTRACT_PROMPT

    def test_it_excludes_passing_mentions(self):
        from prompts import EXTRACT_PROMPT
        low = EXTRACT_PROMPT.lower()
        for phrase in ("in passing", "traffic into la", "leave the existing value alone"):
            assert phrase in low

    def test_consolidate_prompt_also_guards_city(self):
        from prompts import CONSOLIDATE_PROMPT
        low = CONSOLIDATE_PROMPT.lower()
        assert '"city"' in low and "leave it unchanged" in low


class TestCityChangeIsLogged:
    def test_city_regression_prints_old_and_new(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(db, "_DB_PATH", tmp_path / "t3.db", raising=False)
        monkeypatch.setenv("APP_URL", "https://palmer.test")
        db.init_db()
        db.upsert_profile("+1557", {"city": "Culver City, CA"})
        profile = db.get_profile("+1557")
        with patch("home.rebuild") as rebuild:
            userprofile._apply_profile_updates("+1557", profile, {"city": "Los Angeles"})
        out = capsys.readouterr().out
        assert "Culver City, CA" in out and "Los Angeles" in out
        rebuild.assert_not_called(), "a correction to an existing city is not a day-1 build"


class TestEagerHomeBuild:
    """The first time a user's city becomes known, Palmer Home gets built right
    away rather than waiting for get_my_page or the morning job — see CLAUDE.md
    "Onboarding" / the day-1 site build. It never sends the link; that's still
    gated on an explicit ask or the first morning send, unchanged."""

    def test_first_city_triggers_a_build(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "_DB_PATH", tmp_path / "t6.db", raising=False)
        monkeypatch.setenv("APP_URL", "https://palmer.test")
        db.init_db()
        db.upsert_profile("+1558", {})
        profile = db.get_profile("+1558")
        with patch("home.home_token", return_value="tok"), \
             patch("home.load", return_value=None), \
             patch("home.rebuild") as rebuild:
            userprofile._apply_profile_updates("+1558", profile, {"city": "Chicago"})
        rebuild.assert_called_once_with("+1558", refresh_news=True)

    def test_city_correction_does_not_rebuild(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "_DB_PATH", tmp_path / "t7.db", raising=False)
        monkeypatch.setenv("APP_URL", "https://palmer.test")
        db.init_db()
        db.upsert_profile("+1559", {"city": "Culver City, CA"})
        profile = db.get_profile("+1559")
        with patch("home.rebuild") as rebuild:
            userprofile._apply_profile_updates("+1559", profile, {"city": "Los Angeles"})
        rebuild.assert_not_called()

    def test_no_app_url_skips_the_build(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "_DB_PATH", tmp_path / "t8.db", raising=False)
        monkeypatch.delenv("APP_URL", raising=False)
        db.init_db()
        db.upsert_profile("+1560", {})
        profile = db.get_profile("+1560")
        with patch("home.rebuild") as rebuild:
            userprofile._apply_profile_updates("+1560", profile, {"city": "Chicago"})
        rebuild.assert_not_called()

    def test_a_page_already_built_is_not_rebuilt_again(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "_DB_PATH", tmp_path / "t9.db", raising=False)
        monkeypatch.setenv("APP_URL", "https://palmer.test")
        db.init_db()
        db.upsert_profile("+1561", {})
        profile = db.get_profile("+1561")
        with patch("home.home_token", return_value="tok"), \
             patch("home.load", return_value={"city": "Chicago"}), \
             patch("home.rebuild") as rebuild:
            userprofile._apply_profile_updates("+1561", profile, {"city": "Chicago"})
        rebuild.assert_not_called()


class TestOnboardingAskConsumption:
    """The ONBOARDING ASK block in _build_system (agent.py) fires under this
    exact condition; _update_profile marks it consumed the first time it sees
    that same condition hold true after a turn's extraction runs."""

    def test_marks_consumed_after_a_qualifying_turn(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "_DB_PATH", tmp_path / "t10.db", raising=False)
        db.init_db()
        db.upsert_profile("+1562", {"intro_sent": True})
        block = MagicMock(); block.text = "{}"
        resp = MagicMock(); resp.content = [block]
        with patch.object(userprofile.client.messages, "create", return_value=resp):
            userprofile._update_profile("+1562", "hey", "hey, how's it going")
        assert db.get_profile("+1562")["onboarding_ask_sent"] is True

    def test_not_marked_when_the_turn_supplies_both_fields(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "_DB_PATH", tmp_path / "t11.db", raising=False)
        monkeypatch.delenv("APP_URL", raising=False)
        db.init_db()
        db.upsert_profile("+1563", {"intro_sent": True})
        block = MagicMock(); block.text = '{"name": "Ada", "city": "Chicago"}'
        resp = MagicMock(); resp.content = [block]
        with patch.object(userprofile.client.messages, "create", return_value=resp):
            userprofile._update_profile("+1563", "I'm Ada from Chicago", "hey Ada")
        assert "onboarding_ask_sent" not in db.get_profile("+1563")

    def test_not_marked_before_intro_is_sent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "_DB_PATH", tmp_path / "t12.db", raising=False)
        db.init_db()
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
    the follow silently gone, score alerts dead, and `_all_interests` then
    raising AttributeError on a dict from a call site outside a try.
    """

    def test_an_alias_key_is_never_a_real_field(self):
        import userprofile as up
        clashes = {k: v for k, v in up._PROFILE_ALIASES.items() if k in up.PROFILE_FIELDS}
        assert not clashes, f"these fields are silently rewritten on write: {clashes}"

    def test_the_structured_follow_lists_survive_a_normalise(self):
        import userprofile as up
        for field in ("followed_teams", "shows"):
            row = [{"league": "nfl", "abbrev": "PHI", "name": "Philadelphia Eagles"}]
            out = up._canonical_updates({field: row})
            assert out.get(field) == row, f"{field} did not survive canonicalisation"

    def test_the_tool_written_lists_are_not_in_the_extractor_schema(self):
        """They hold structured dicts written by tool dispatch. In the schema,
        Haiku fills them with prose and downstream code gets strings."""
        import prompts
        for field in ("followed_teams", "shows", "weather_locations"):
            assert f'"{field}"' not in prompts.EXTRACT_PROMPT
