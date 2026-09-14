"""The remaining places a date was computed on the wrong calendar.

Each of these answered confidently and was wrong by a day, which is worse than
failing: the output names the date, so the user reads a specific claim that
does not match reality.
"""
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

import datafeeds
import timeutil
import userprofile
import weather


class TestTheMarketDayIsNewYorks:
    def test_the_exchange_timezone_is_pinned(self):
        """Not the server's day, and deliberately not the reader's either — a
        session closes when New York says it does, whoever is asking."""
        assert datafeeds._MARKET_TZ == "America/New_York"

    def test_after_the_utc_rollover_it_is_still_the_same_trading_day(self):
        # 00:30Z on Aug 31 is 20:30 ET on Aug 30 — the afternoon's close is
        # TODAY's, and the old date.today() labelled it "yesterday".
        instant = datetime(2026, 8, 31, 0, 30, tzinfo=timezone.utc)
        with patch("timeutil.datetime") as dt:
            dt.now.side_effect = lambda tz=None: instant.astimezone(tz) if tz else instant
            assert timeutil.local_today(datafeeds._MARKET_TZ) == date(2026, 8, 30)
            assert timeutil.local_today(None) == date(2026, 8, 31)


class TestWeatherDayResolution:
    """The convention lives in timeutil now, not weather.

    It is generic date reasoning, and leaving it in the weather module was
    how the reminder path — the one place the model computes a date itself —
    ended up with no answer for "next friday" at all, while weather had a
    considered one."""

    def _delta(self, when, on):
        with patch.object(timeutil, "local_today", return_value=on):
            return timeutil.resolve_day_delta(when, when.lower())

    FRIDAY = date(2026, 8, 28)
    WEDNESDAY = date(2026, 8, 26)

    def test_tomorrow_is_one_day(self):
        assert self._delta("tomorrow", self.FRIDAY) == 1

    def test_a_bare_weekday_is_the_next_one(self):
        assert self._delta("friday", self.WEDNESDAY) == 2

    def test_next_weekday_is_a_week_past_that(self):
        """"friday" and "next friday" used to be indistinguishable, so someone
        planning a week out got this week's forecast under next week's name."""
        assert self._delta("next friday", self.WEDNESDAY) == 9

    def test_the_two_rules_compose_rather_than_stacking(self):
        """A bare weekday naming TODAY resolves a week out (an existing, tested
        decision), so a flat +7 on top would put "next friday" a fortnight away."""
        assert self._delta("friday", self.FRIDAY) == 7
        assert self._delta("next friday", self.FRIDAY) == 7

    def test_an_explicit_date_still_wins(self):
        assert self._delta("2026-08-30", self.FRIDAY) == 2

    def test_a_misspelt_weekday_still_resolves(self):
        """Substring matching is doing real work here: "thursdayish" is still a
        Thursday, and "next" still bumps it."""
        assert self._delta("next thursdayish", self.FRIDAY) == 13

    def test_unparseable_input_returns_none(self):
        """The caller then answers for TODAY and logs it. It used to silently
        become tomorrow, so an unreadable phrase was answered for a day the user
        never named — and the output states that date as fact."""
        for phrase in ("sometime soon", "when it cools off", "later"):
            assert self._delta(phrase, self.FRIDAY) is None, phrase

    def test_the_callers_default_to_today_not_tomorrow(self):
        import inspect
        src = inspect.getsource(weather)
        assert "delta = 1" not in src, "unparseable input must not mean tomorrow"

    def test_the_target_is_anchored_on_the_readers_calendar(self):
        import inspect
        src = inspect.getsource(weather._nws_report)
        # delta is computed in the user's zone, so it must be added to the
        # user's today — not to the forecast grid's, which is a different place.
        assert "_lt(tz) if valid_zone(tz)" in src


class TestTimezoneIsValidatedAndRepaired:
    def _apply(self, profile, updates, derived=None):
        with patch.object(userprofile, "upsert_profile") as up, \
             patch.object(userprofile, "get_profile", return_value=profile), \
             patch.object(userprofile, "_derive_timezone", return_value=derived), \
             patch.object(userprofile, "_eager_build_home"):
            userprofile._apply_profile_updates("+15550001111", profile, updates)
        return up.call_args[0][1] if up.call_args else {}

    def test_junk_from_the_extractor_is_dropped(self):
        """`timezone` is in EXTRACT_PROMPT's schema, so Haiku can write anything.
        An unresolvable value degrades every local_now call to UTC, silently."""
        written = self._apply({"city": "Chicago"}, {"timezone": "Pacific Time"})
        assert "timezone" not in written

    def test_a_real_zone_is_kept(self):
        written = self._apply({"city": "Chicago"}, {"timezone": "America/Denver"})
        assert written["timezone"] == "America/Denver"

    def test_a_move_re_derives_the_zone(self):
        """It used to derive only when ABSENT, so someone who moved kept the old
        zone forever and their morning arrived at the wrong hour from then on."""
        written = self._apply({"city": "Chicago", "timezone": "America/Chicago"},
                              {"city": "Los Angeles"},
                              derived="America/Los_Angeles")
        assert written["timezone"] == "America/Los_Angeles"

    def test_no_move_leaves_it_alone(self):
        written = self._apply({"city": "Chicago", "timezone": "America/Chicago"},
                              {"vibe": "cheerful"}, derived="America/Denver")
        assert "timezone" not in written

    def test_an_explicit_timezone_update_is_not_overridden_by_the_city(self):
        written = self._apply({"city": "Chicago", "timezone": "America/Chicago"},
                              {"city": "Denver", "timezone": "America/Denver"},
                              derived="America/Boise")
        assert written["timezone"] == "America/Denver"

    def test_the_forecast_correction_path_does_not_reach_this_function(self):
        """CLAUDE.md: correcting a weather city must not move the hour the
        morning arrives. That write calls upsert_profile directly."""
        import inspect
        import agent
        src = inspect.getsource(agent.get_reply)
        assert "_city_from_weather_topic" in src
        assert "_apply_profile_updates" not in src


class TestConsolidationDoesNotRunEveryTurn:
    def test_the_gate_exists(self):
        assert userprofile.CONSOLIDATE_EVERY >= 10

    def test_it_is_skipped_until_enough_new_messages(self):
        with patch.object(userprofile, "get_message_count", return_value=45), \
             patch.object(userprofile, "get_profile",
                          return_value={"consolidated_at_count": 40}), \
             patch.object(userprofile, "get_older_messages") as older:
            userprofile._consolidate_history("+15550001111")
            older.assert_not_called()

    def test_it_runs_once_the_gap_is_wide_enough(self):
        with patch.object(userprofile, "get_message_count", return_value=65), \
             patch.object(userprofile, "get_profile",
                          return_value={"consolidated_at_count": 40}), \
             patch.object(userprofile, "get_older_messages", return_value=[]) as older:
            userprofile._consolidate_history("+15550001111")
            older.assert_called_once()

    def test_the_watermark_is_bookkeeping_not_an_extraction_field(self):
        import prompts
        assert "consolidated_at_count" in userprofile.PROFILE_FIELDS
        assert "consolidated_at_count" not in prompts.EXTRACT_PROMPT


class TestCurationIsToldTheReadersDate:
    def test_curate_takes_a_date(self):
        import inspect
        sig = inspect.signature(__import__("opening")._curate)
        assert "today" in sig.parameters

    def test_the_snapshot_passes_the_local_day(self):
        import inspect
        import opening
        src = inspect.getsource(opening.opening_snapshot)
        assert "_curate(metro or _metro(city), pool, today=today)" in src
