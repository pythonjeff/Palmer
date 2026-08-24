"""Tests for the profile allow-list and the pruning migration.

The extractor is a language model writing straight into a dict that gets dumped
as JSON into every system prompt. Unbounded, one profile reached 624 keys — 604
invented one-offs, ~21,700 tokens of noise per message. These pin the bound.
"""
from unittest.mock import patch

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
                      "interest_genres", "ongoing_threads", "life_context"):
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
