"""The weather city: written where the user sets it, named where it was measured.

A user in Culver City got three consecutive mornings of Los Angeles
temperatures — 98, 100, 102 — against local highs of 88, 89, 90. weather.py was
innocent throughout; it faithfully forecast whatever city it was handed. Two
independent defects stacked:

1. WRITE. He set his weather location by saying "I want the weather updates to
   be specific to Culver City California". That routes to
   update_morning_briefing, which wrote the topic string and nothing else, so
   profile["city"] kept its older, broader value — and profile["city"] is the
   sole input to every weather pull Palmer makes. EXTRACT_PROMPT could not
   cover it: LOCATION PRECISION only writes city from a statement of residence
   or an explicit correction, and a weather preference is neither.

2. READ. The drafter was handed "Weather in Los Angeles: high 102" and still
   wrote "102 in Culver City today", reconciling the number against the
   Culver City strings all over the profile in its system prompt. Nothing
   stopped it: the line prompt's only data rule was about numbers. The text
   briefing has had the city rule since the beginning (see morning.py's
   "name the city the forecast is for") — the one-line path never inherited it.

Defect 1 governs how often the data is wrong. Defect 2 governs what a wrong
value can do: a number under the wrong city name is unfalsifiable from the
message, where "102 in Los Angeles" is read as wrong in one second. Both are
tested here, because fixing only the first leaves the next unenumerated write
path free to produce the same confident lie.
"""
from unittest.mock import patch

import agent
import morning


def _dispatch_block() -> str:
    """The update_morning_briefing arm of the tool loop, as source."""
    import inspect
    src = inspect.getsource(agent.get_reply)
    return src.split('update_morning_briefing"')[1].split("elif b.name")[0]


class TestCityFromWeatherTopic:
    def test_a_weather_topic_yields_its_city(self):
        with patch("morning._infer_city_from_topics", return_value="Culver City, CA") as m:
            assert agent._city_from_weather_topic("Culver City CA weather") == "Culver City, CA"
        assert m.call_args[0][0] == ["Culver City CA weather"]

    def test_forecast_and_temperature_phrasings_also_count(self):
        with patch("morning._infer_city_from_topics", return_value="Woodland Hills, CA"):
            for t in ("Woodland Hills CA weather forecast", "Denver temperature"):
                assert agent._city_from_weather_topic(t) == "Woodland Hills, CA"

    def test_a_non_weather_topic_never_pays_for_a_lookup(self):
        """This runs on the write path, but a model call per added topic is
        still worth avoiding — and a news topic must never move the city."""
        with patch("llm.client") as client:
            for t in ("AI news", "US politics", "Bitcoin price", "SPCX stock price"):
                assert agent._city_from_weather_topic(t) is None
        client.messages.create.assert_not_called()

    def test_empty_input_is_safe(self):
        assert agent._city_from_weather_topic("") is None
        assert agent._city_from_weather_topic(None) is None

    def test_an_unresolvable_weather_topic_leaves_the_city_alone(self):
        with patch("morning._infer_city_from_topics", return_value=None):
            assert agent._city_from_weather_topic("weather") is None


class TestTheAddPathWritesTheCity:
    """Where the user sets their weather location is where it has to be saved."""

    def test_the_add_path_derives_a_city(self):
        assert "_city_from_weather_topic" in _dispatch_block()

    def test_the_derived_city_is_actually_written(self):
        block = _dispatch_block()
        assert '"city"' in block, "deriving the city and not saving it is the original bug"

    def test_a_moved_city_expires_the_cached_forecast(self):
        """The page caches weather for 10 minutes. Without expiring it, the
        correction is followed by the old city's forecast under the new name —
        the precise pairing that made this a lie rather than a stale number."""
        block = _dispatch_block()
        assert "weather" in block and "invalidate" in block

    def test_it_does_not_move_their_timezone(self):
        """city doubles as the timezone source, but timezone is only derived
        when absent. Re-deriving here would shift the hour their morning
        arrives as a side effect of correcting a forecast."""
        assert "timezone" not in _dispatch_block()


class TestTheForecastNamesWhereItWasMeasured:
    """`resolved` comes back from the same geocode that produced the numbers,
    so the name and the number cannot disagree."""

    DRIFTED = {
        # profile already corrected; the 10-minute weather stamp has not lapsed
        "city": "Culver City",
        "weather": {"resolved": "Los Angeles, California", "temp_now": 74.5,
                    "high": 101.9, "low": 71.5, "description": "clear sky"},
    }

    def test_the_digest_names_the_measured_city_not_the_profiles(self):
        d = morning._payload_digest(self.DRIFTED)
        assert "Los Angeles, California" in d
        assert "Culver City" not in d, "a Los Angeles number must not carry the Culver City name"

    def test_the_temperature_still_rides_with_it(self):
        assert "high 102" in morning._payload_digest(self.DRIFTED)

    def test_it_falls_back_to_the_profile_city_when_unresolved(self):
        d = morning._payload_digest(
            {"city": "Kirkwood, MO", "weather": {"high": 88, "low": 64, "description": "clear"}})
        assert "Kirkwood, MO" in d

    def test_neither_name_leaves_the_line_cityless(self):
        d = morning._payload_digest({"weather": {"high": 88, "low": 64, "description": "clear"}})
        assert "their city" in d


class TestTheLineIsToldNotToRenameTheCity:
    def test_the_prompt_carries_the_city_rule(self):
        """The text briefing has always had this rule; the one-line path that
        replaced it as the daily send did not inherit it."""
        calls = []

        def _create(**kw):
            calls.append(kw)
            return type("R", (), {"content": [type("B", (), {"text": "Cool 71 and clear."})()]})()

        # Patch where the function lives, not where it was defined — morning
        # binds `client` at import (CLAUDE.md, "Patching in tests follows the
        # code, not the name"). A dead target here would make a real API call.
        with patch.object(morning, "get_profile", return_value={"timezone": "America/Chicago"}), \
             patch.object(morning, "_build_system", return_value="sys"), \
             patch.object(morning, "_recent_assistant_texts", return_value=[]), \
             patch.object(morning.client.messages, "create", side_effect=_create):
            morning.generate_morning_line("+1555", TestTheForecastNamesWhereItWasMeasured.DRIFTED)
        body = calls[0]["messages"][0]["content"].lower()
        assert "name the city" in body
        assert "data wins" in body
