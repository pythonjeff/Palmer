"""The morning send: the line, the window, the shape, repetition.

Merged from test_morning_link.py, test_morning_schedule.py, test_briefing_shape.py, test_weather_city.py, test_repetition.py; each section keeps its
original file's notes, because those carry the incident that led to the test.
"""
import pytest
from unittest.mock import patch, MagicMock
from palmer import morning, agent, prompts, guards
from datetime import datetime
from zoneinfo import ZoneInfo
from palmer.morning import _in_send_window, _parse_morning_time, _local_today, DEFAULT_MORNING_TIME
from palmer.smstext import _normalize_hhmm, _parse_published


# ============================================================================
# from test_morning_link.py
# ============================================================================
#
# The morning update is a link plus one line.
#
# The briefing itself lives on the user's page; the text above it exists to say
# why today is worth a tap. Two properties are load-bearing and tested here:
#
# 1. The URL is LAST in the message and nothing follows it. Message apps only
#    render the rich preview when the link sits at a boundary, and that preview
#    is the reason this shape works at all.
# 2. Every failure in the page path falls back to the full text briefing. A user
#    never gets a link to an empty page, and never gets silence.

PAYLOAD = {
    "city": "Kirkwood, MO",
    "weather": {"temp_now": 71, "high": 88, "low": 64, "description": "clear", "rain_pct": 10},
    "traffic": {"live_min": 34, "delay_min": 9},
    "prices": [{"label": "BTC", "pct_24h": -4.2}],
    "headlines": [{"title": "Starship static fire slips a week", "topic": "SpaceX news"}],
}

URL = "https://palmer.example.com/h/AbC123xyz"


class TestDigest:
    def test_carries_every_section_as_plain_lines(self):
        d = morning._payload_digest(PAYLOAD)
        assert "Kirkwood, MO" in d and "clear" in d
        assert "34 min" in d and "9 min slower" in d
        assert "BTC: -4.2%" in d
        assert "Starship static fire" in d

    def test_normal_commute_is_not_dressed_up_as_a_delay(self):
        d = morning._payload_digest({"traffic": {"live_min": 20, "delay_min": 0}})
        assert "normal" in d and "slower" not in d

    def test_empty_payload_is_falsy_so_callers_can_gate_on_it(self):
        assert morning._payload_digest({}) == ""
        assert not morning._payload_digest({"weather": {}, "prices": [], "headlines": []})


class TestComposeShape:
    def _compose(self, line="Cool 71 and clear, but your drive is 9 min heavier than usual."):
        with patch("palmer.home.ensure_fresh", return_value=URL), \
             patch("palmer.home.load", return_value=PAYLOAD), \
             patch("palmer.home.home_token", return_value="tok"), \
             patch.object(morning, "generate_morning_line", return_value=line), \
             patch.object(morning, "generate_morning") as full:
            msg, carries = morning._compose_morning("+1555")
        return msg, carries, full

    def test_url_is_last_with_nothing_after_it(self):
        msg, carries, full = self._compose()
        assert carries is True
        assert msg.endswith(URL), "a trailing character kills the link preview"
        full.assert_not_called()

    def test_message_carries_exactly_one_url(self):
        msg, _, _ = self._compose()
        assert msg.count("http") == 1, "a second URL suppresses the preview"

    def test_the_line_survives_ahead_of_the_link(self):
        msg, _, _ = self._compose(line="Bitcoin is off 4.2% overnight.")
        assert msg.startswith("Bitcoin is off 4.2% overnight. ")

    def test_it_is_one_message_not_two(self):
        """The briefing used to be its own text with the link chasing it."""
        msg, _, _ = self._compose()
        assert len(msg) < 900, "must fit one send, not trigger the chunker"


class TestFallbacks:
    def _compose_with(self, **patches):
        defaults = {"ensure_fresh": URL, "load": PAYLOAD, "home_token": "tok"}
        defaults.update(patches)
        with patch("palmer.home.ensure_fresh", return_value=defaults["ensure_fresh"]), \
             patch("palmer.home.load", return_value=defaults["load"]), \
             patch("palmer.home.home_token", return_value=defaults["home_token"]), \
             patch.object(morning, "generate_morning", return_value="the long briefing"), \
             patch.object(morning, "generate_morning_line",
                          side_effect=defaults.get("line_error") or (lambda *a, **k: "a line")):
            return morning._compose_morning("+1555")

    def test_missing_app_url_falls_back_to_the_text_briefing(self):
        msg, carries = self._compose_with(ensure_fresh="/h/tok")
        assert msg == "the long briefing" and carries is False

    def test_empty_page_falls_back_rather_than_linking_to_nothing(self):
        msg, carries = self._compose_with(load={})
        assert msg == "the long briefing" and carries is False

    def test_missing_page_falls_back(self):
        msg, carries = self._compose_with(load=None)
        assert msg == "the long briefing" and carries is False

    def test_a_failed_line_draft_falls_back(self):
        msg, carries = self._compose_with(line_error=RuntimeError("sonnet down"))
        assert msg == "the long briefing" and carries is False

    def test_the_fallback_never_carries_a_link(self):
        """carries_link gates the /sms-status shorten-and-retry, which would
        happily cut a URL in half. A text briefing must keep that retry."""
        for kw in ({"ensure_fresh": "/h/tok"}, {"load": {}},
                   {"line_error": RuntimeError("x")}):
            assert self._compose_with(**kw)[1] is False


class TestLinkPlaceholder:
    """The drafter knows a link is coming and reaches for a stand-in. Left in,
    "[link]" ships as literal text sitting next to the real URL."""

    @pytest.mark.parametrize("raw", [
        "Arenado to the Dodgers. [link]",
        "Arenado to the Dodgers. (url)",
        "Arenado to the Dodgers. <here>",
        "Arenado to the Dodgers. [Link]",
        "Arenado to the Dodgers. (page)",
        "Arenado to the Dodgers. [dashboard]",
    ])
    def test_placeholders_are_stripped(self, raw):
        assert morning._strip_link_placeholder(raw) == "Arenado to the Dodgers."

    def test_an_invented_url_is_stripped(self):
        """A URL Palmer made up is worse than an awkward sentence — and it
        would also break the preview by making this the second link."""
        out = morning._strip_link_placeholder("Big day https://made.up/thing")
        assert "http" not in out

    def test_a_dangling_separator_goes_with_it(self):
        assert morning._strip_link_placeholder("Rain all day -") == "Rain all day"

    def test_a_clean_line_is_untouched(self):
        line = "Cool 71 and clear, and your drive is 9 min heavier than usual."
        assert morning._strip_link_placeholder(line) == line

    def test_ordinary_brackets_survive(self):
        line = "Cards lost (again)."
        assert morning._strip_link_placeholder(line) == line


class _Block:
    def __init__(self, t): self.text = t


class _Resp:
    def __init__(self, t): self.content = [_Block(t)]


def _draft_returning(*texts, payload=None):
    """Run the drafter against a scripted sequence of model outputs."""
    calls = []

    def _create(**kw):
        calls.append(kw)
        return _Resp(texts[min(len(calls) - 1, len(texts) - 1)])

    with patch.object(morning, "get_profile", return_value={"timezone": "America/Chicago"}), \
         patch.object(morning, "_build_system", return_value="sys"), \
         patch.object(morning, "_recent_assistant_texts", return_value=[]), \
         patch.object(morning.client.messages, "create", side_effect=_create):
        return morning.generate_morning_line("+1555", payload if payload is not None else PAYLOAD), calls


class TestNamingTheLink:
    """"page has your full rundown" turns a text from a friend into a push
    notification. The prompt asks for this; the code enforces it."""

    @pytest.mark.parametrize("bad", [
        "Morning Jeff, page has your full rundown.",
        "Morning Jeff, everything's on your dashboard.",
        "Morning Jeff, click through for the details.",
        "Morning Jeff, the link has today's stuff.",
        "Morning Jeff, tap here for more.",
    ])
    def test_a_line_that_names_the_link_is_redrafted(self, bad):
        out, calls = _draft_returning(bad, "Morning Jeff, cool and clear.")
        assert out == "Morning Jeff, cool and clear."
        assert len(calls) == 2, "must redraft exactly once"

    def test_a_clean_line_is_not_redrafted(self):
        out, calls = _draft_returning("Morning Jeff, cool and clear.")
        assert out == "Morning Jeff, cool and clear."
        assert len(calls) == 1, "a clean line must not cost a second call"

    def test_the_redraft_is_told_what_it_did_wrong(self):
        _, calls = _draft_returning("Check the page.", "Cool and clear today.")
        assert "Check the page." in calls[1]["messages"][0]["content"]

    def test_it_gives_up_after_one_redraft(self):
        """Two calls, not a loop — a stubborn model must not burn the budget."""
        out, calls = _draft_returning("Check the page.", "Still on your page.")
        assert len(calls) == 2
        assert out, "a second violation still ships rather than failing the morning"

    def test_ordinary_words_are_not_false_positives(self):
        out, calls = _draft_returning("Morning Jeff, cool and clear and 81.")
        assert len(calls) == 1
        assert not morning._NAMES_THE_LINK.search("Cool and clear, pages of rain later")


class TestLineDiscipline:
    def _draft(self, text):
        return _draft_returning(text)[0]

    def test_long_output_is_trimmed_on_a_word_boundary(self):
        out = self._draft("word " * 200)
        assert len(out) <= morning.MORNING_LINE_MAX
        assert not out.endswith("wor"), "must not cut mid-word"

    def test_newlines_are_flattened_to_one_line(self):
        out = self._draft("Cool and clear today.\n\nBitcoin is down.")
        assert "\n" not in out

    def test_empty_output_raises_so_the_caller_can_fall_back(self):
        with pytest.raises(ValueError):
            self._draft("   ")

    def test_meta_commentary_raises(self):
        with pytest.raises(ValueError):
            self._draft("Not sending the weather since they asked me to skip it.")

    def test_a_normal_line_passes_through_untouched(self):
        assert self._draft("Cool 71 and clear.") == "Cool 71 and clear."

    def test_the_drafter_strips_a_placeholder_before_returning(self):
        assert self._draft("Cool 71 and clear. [link]") == "Cool 71 and clear."

    def test_the_prompt_also_warns_against_placeholders(self):
        assert "placeholder" in TestPrompt()._prompt()["messages"][0]["content"].lower()


class TestRequiredContent:
    """Every user's morning text carries the same basics — weather, commute
    when they have an address on file, and 1-2 opening highlights — before
    the link. Nothing here checks the model's actual output (that would just
    be re-testing the mock); it checks that the prompt tells the model which
    of those are mandatory, based on what the payload actually has."""

    def _required_block(self, payload):
        body = _draft_returning("Cool and clear, 22 min in, and Mamele's just opened.",
                                 payload=payload)[1][0]["messages"][0]["content"]
        return body

    def test_weather_commute_and_opening_are_all_required_when_present(self):
        payload = dict(PAYLOAD, opening=[{"title": "Mamele's", "subtitle": "new deli"}])
        body = self._required_block(payload)
        assert "so every one of them must appear" in body
        assert "today's weather" in body
        assert "the commute" in body
        assert "newly open" in body

    def test_commute_is_not_required_without_an_address(self):
        payload = {k: v for k, v in PAYLOAD.items() if k != "traffic"}
        body = self._required_block(payload)
        assert "so every one of them must appear" in body
        assert "the commute" not in body

    def test_opening_is_not_required_without_data(self):
        payload = {k: v for k, v in PAYLOAD.items() if k != "headlines"}
        assert "opening" not in payload
        body = self._required_block(payload)
        assert "newly open" not in body

    def test_no_structured_data_falls_back_to_a_plain_greeting_instruction(self):
        body = self._required_block({})
        assert "plain greeting is correct" in body
        assert "so every one of them must appear" not in body


class TestPrompt:
    def _prompt(self):
        return _draft_returning("Cool 71 and clear.")[1][0]

    def test_drafts_in_palmers_voice_not_a_second_persona(self):
        """Every user-facing message goes through _build_system. A one-line
        greeting is still a user-facing message."""
        assert self._prompt()["system"] == "sys"

    def test_drafts_on_sonnet_like_every_other_user_facing_message(self):
        assert self._prompt()["model"] == morning.SONNET_MODEL

    def test_the_page_data_reaches_the_drafter(self):
        body = self._prompt()["messages"][0]["content"]
        assert "Starship static fire" in body and "34 min" in body

    def test_it_is_told_not_to_name_the_link(self):
        body = self._prompt()["messages"][0]["content"].lower()
        for word in ("link", "page", "dashboard", "click"):
            assert word in body, f"the prompt must explicitly ban {word!r}"


class TestTheirTeamInTheMorning:
    """A followed team's game rides in the morning update rather than in live
    texts during the game. The digest carries it from the team's side, and the
    REQUIRED list makes the drafter say it."""

    GAME_LAST = {"id": "1", "league": "mlb", "state": "post", "detail": "Final",
                 "home": {"abbrev": "STL", "name": "St. Louis Cardinals", "score": 5},
                 "away": {"abbrev": "CHC", "name": "Chicago Cubs", "score": 2}}
    GAME_TODAY = {"id": "2", "league": "mlb", "state": "pre", "detail": "7:15 PM CT",
                  "home": {"abbrev": "STL", "name": "St. Louis Cardinals", "score": 0},
                  "away": {"abbrev": "CHC", "name": "Chicago Cubs", "score": 0}}
    ROW = {"team": "St. Louis Cardinals", "abbrev": "STL", "league": "mlb",
           "last": GAME_LAST, "today": GAME_TODAY}

    def test_the_digest_states_result_and_next_game_from_their_side(self):
        d = morning._payload_digest(dict(PAYLOAD, scores=[self.ROW]))
        assert "Their team (St. Louis Cardinals): yesterday beat Chicago Cubs 5-2; today play Chicago Cubs, 7:15 PM CT" in d

    def test_a_row_with_only_a_result_says_only_that(self):
        lines = morning.score_lines({"scores": [dict(self.ROW, today=None)]})
        assert lines == ["Their team (St. Louis Cardinals): yesterday beat Chicago Cubs 5-2"]

    def test_no_followed_team_adds_nothing(self):
        assert morning.score_lines(PAYLOAD) == []
        assert "Their team" not in morning._payload_digest(PAYLOAD)

    def test_the_team_is_required_when_present(self):
        body = _draft_returning("Cards won 5-2 last night, back at it at 7:15.",
                                 payload=dict(PAYLOAD, scores=[self.ROW]))[1][0]["messages"][0]["content"]
        assert "their team" in body and "so every one of them must appear" in body

    def test_the_team_is_not_required_without_a_game(self):
        body = _draft_returning("Cool and clear.", payload=PAYLOAD)[1][0]["messages"][0]["content"]
        assert "their team" not in body


# ============================================================================
# from test_morning_schedule.py
# ============================================================================
#
# Tests for morning send-window logic and time parsing. Run: pytest tests/test_morning.py

def _dt(hour, minute, tz="America/Chicago"):
    return datetime(2026, 8, 3, hour, minute, tzinfo=ZoneInfo(tz))


class TestParseMorningTime:
    def test_default(self):
        assert _parse_morning_time(DEFAULT_MORNING_TIME) == (7, 0)

    def test_custom(self):
        assert _parse_morning_time("08:30") == (8, 30)
        assert _parse_morning_time("21:15") == (21, 15)

    def test_invalid_falls_back_to_default(self):
        for bad in (None, "", "notatime", "25:00", "08:99", "8", 700):
            assert _parse_morning_time(bad) == (7, 0), bad


class TestSendWindow:
    def test_before_time_no_send(self):
        assert not _in_send_window(_dt(8, 29), "08:30")

    def test_exactly_at_time_sends(self):
        assert _in_send_window(_dt(8, 30), "08:30")

    def test_within_catchup_sends(self):
        assert _in_send_window(_dt(9, 45), "08:30")
        assert _in_send_window(_dt(10, 29), "08:30")

    def test_after_catchup_no_send(self):
        assert not _in_send_window(_dt(10, 30), "08:30")
        assert not _in_send_window(_dt(21, 0), "08:30")

    def test_none_uses_default(self):
        assert _in_send_window(_dt(7, 0), None)
        assert not _in_send_window(_dt(6, 30), None)

    def test_custom_time(self):
        assert _in_send_window(_dt(7, 0), "07:00")
        assert not _in_send_window(_dt(6, 55), "07:00")
        assert not _in_send_window(_dt(9, 5), "07:00")

    def test_other_timezone(self):
        assert _in_send_window(_dt(8, 35, tz="America/New_York"), "08:30")

    def test_invalid_pref_falls_back_to_default(self):
        assert _in_send_window(_dt(7, 0), "garbage")
        assert not _in_send_window(_dt(6, 30), "garbage")


class TestLocalToday:
    def test_valid_tz(self):
        assert _local_today("America/Chicago") is not None

    def test_missing_or_bad_tz_falls_back(self):
        """Fallback is the UTC date, not the runner's local date — comparing to
        date.today() made this fail every evening west of UTC."""
        from datetime import datetime, timezone
        utc_today = datetime.now(timezone.utc).date()
        assert _local_today(None) == utc_today
        assert _local_today("Not/AZone") == utc_today


class TestNormalizeHhmm:
    def test_valid(self):
        assert _normalize_hhmm("07:00") == "07:00"
        assert _normalize_hhmm("7:05") == "07:05"
        assert _normalize_hhmm("23:59") == "23:59"
        assert _normalize_hhmm(" 08:30 ") == "08:30"

    def test_invalid(self):
        for bad in ("24:00", "12:60", "7am", "730", "", None, "7:5"):
            assert _normalize_hhmm(bad) is None, bad


class TestParsePublished:
    def test_rfc2822(self):
        dt = _parse_published("Mon, 03 Aug 2026 10:00:00 GMT")
        assert dt is not None and dt.year == 2026

    def test_iso(self):
        dt = _parse_published("2026-08-03T10:00:00Z")
        assert dt is not None and dt.hour == 10

    def test_garbage(self):
        assert _parse_published("unknown") is None
        assert _parse_published(None) is None
        assert _parse_published("") is None


# ============================================================================
# from test_briefing_shape.py
# ============================================================================
#
# Tests for the two bugs behind the 'Palmer dumped a briefing on me' report.
#
# 1. A greeting must not produce briefing content — the briefing is scheduled,
#    not assembled on request.
# 2. Briefing configuration must not leak into ordinary replies. One user had
#    "Format: bullet points per subject" saved as a morning topic; the profile is
#    dumped as raw JSON into every system prompt, so it read as an order for the
#    current message and turned normal replies into labelled dumps. It was also
#    being sent to the news search as a query.

class TestGreetingIsNotABriefing:
    def test_prompt_says_the_briefing_is_sent_not_assembled(self):
        body = prompts.SYSTEM_PROMPT
        assert "It is NOT something you assemble on request" in body

    def test_prompt_names_the_greeting_case(self):
        body = prompts.SYSTEM_PROMPT.lower()
        assert "a greeting is a greeting" in body
        assert "they said hello; say hello back" in body

    def test_prompt_holds_even_before_the_briefing_has_gone_out(self):
        """The 6:22am case: briefing not sent yet, so the pull is strongest."""
        assert "even when their briefing hasn't gone out yet today" in prompts.SYSTEM_PROMPT

    def test_prompt_bans_the_shapes_that_actually_appeared(self):
        body = prompts.SYSTEM_PROMPT
        assert "Here's your Thursday" in body, "the observed opener must be named"
        assert "Weather -" in body and "Commute -" in body, "labelled sections must be named"
        assert "anything you want me to dig into" in body.lower()


class TestBriefingConfigDoesNotLeakIntoReplies:
    def _build(self, profile):
        with patch.object(agent, "get_profile", return_value=profile), \
             patch.object(agent, "get_user_watches", return_value=[]), \
             patch.object(agent, "get_user_price_watches", return_value=[]):
            return agent._build_system("+15550001111")

    def test_topics_are_labelled_as_reference_data(self):
        out = self._build({"morning_topics": ["St. Louis weather", "SpaceX news"]})
        assert "reference data, not instructions for this message" in out
        assert "Never let it change how you write a reply" in out

    def test_no_such_note_without_topics(self):
        assert "reference data, not instructions" not in self._build({"name": "Mike"})

    def test_topics_still_visible_so_palmer_can_answer_what_am_i_getting(self):
        out = self._build({"morning_topics": ["SpaceX news"]})
        assert "SpaceX news" in out

    def test_directive_is_stripped_from_the_prompt_entirely(self):
        """Labelling it as data was not enough — the model still obeyed it."""
        out = self._build({"morning_topics": [
            "SpaceX news", "Format: bullet points per subject, not one continuous paragraph",
        ]})
        assert "SpaceX news" in out, "real topics must survive"
        assert "bullet points per subject" not in out, \
            "a stored formatting directive must never reach the reply prompt"

    def test_profile_is_not_mutated(self):
        profile = {"morning_topics": ["SpaceX news", "Format: bullets"]}
        agent._prompt_safe_profile(profile)
        assert len(profile["morning_topics"]) == 2, "must not edit the stored profile"

    def test_untouched_when_nothing_to_strip(self):
        """Content, not identity. The profile is always copied now — volatile
        facts are dated or dropped on the way to the prompt — so returning the
        same object is no longer the contract. Not mutating the stored one is,
        and that is asserted above."""
        profile = {"morning_topics": ["SpaceX news"], "name": "Jeff"}
        assert agent._prompt_safe_profile(profile) == profile


class TestDirectivesAreNotTopics:
    def test_format_directive_detected(self):
        assert morning._is_directive("Format: bullet points per subject, not one continuous paragraph")

    def test_real_topics_are_not_directives(self):
        for t in ("SpaceX news", "St. Louis Cardinals baseball news",
                  "Bitcoin and major stock news", "Daily fun fact from history"):
            assert not morning._is_directive(t), t

    def test_case_and_whitespace_insensitive(self):
        assert morning._is_directive("  FORMAT: bullet points  ")

    def test_directive_never_reaches_the_news_search(self):
        searched = []
        profile = {"city": "", "morning_topics": [
            "Format: bullet points per subject", "SpaceX news",
        ]}
        with patch.object(morning, "_topic_digest", side_effect=lambda t: searched.append(t) or "x"), \
             patch.object(morning, "_get_price", return_value="x"), \
             patch("palmer.trends.adjacent_story", return_value=None):
            morning._gather_morning_data(profile)
        assert "SpaceX news" in searched
        assert not any("Format:" in s for s in searched), \
            "a formatting preference was sent to Tavily as a news query"


class TestTopicCoverage:
    def test_cap_covers_a_realistic_subscription(self):
        """8 real topics used to silently become 3."""
        assert morning.MAX_TOPICS >= 6

    def test_all_topics_up_to_the_cap_are_pulled(self):
        searched = []
        profile = {"city": "", "morning_topics": [f"topic {i}" for i in range(8)]}
        with patch.object(morning, "_topic_digest", side_effect=lambda t: searched.append(t) or "x"), \
             patch.object(morning, "_get_price", return_value="x"), \
             patch("palmer.trends.adjacent_story", return_value=None):
            morning._gather_morning_data(profile)
        assert len(searched) == morning.MAX_TOPICS


class TestBriefingShapeRules:
    def test_prompt_forbids_subject_labels(self):
        import inspect
        src = inspect.getsource(morning.generate_morning)
        assert "Never label a line with its subject" in src
        assert "Cardinals - lost 5-4" in src, "the observed bad shape should be the example"


class TestCommuteRoute:
    """Palmer promised 'live drive time from Cedarbrook to Carondelet Plaza' but
    morning.py only ever called get_city_traffic(city), which gives area-wide
    conditions. The route tool existed; it just wasn't wired in."""

    TOPIC = ("Daily commute traffic: 33 Cedarbrook Lane, Kirkwood MO 63122 "
             "to 190 Carondelet Plaza, Clayton MO 63105")

    def test_parses_the_shape_users_actually_save(self):
        assert morning._commute_route({"morning_topics": [self.TOPIC]}) == (
            "33 Cedarbrook Lane, Kirkwood MO 63122", "190 Carondelet Plaza, Clayton MO 63105")

    def test_structured_field_wins_over_topic_text(self):
        route = morning._commute_route({
            "commute": {"origin": "1 Main St, Springfield", "destination": "2 Oak Ave, Shelbyville"},
            "morning_topics": [self.TOPIC],
        })
        assert route == ("1 Main St, Springfield", "2 Oak Ave, Shelbyville")

    def test_no_route_when_none_saved(self):
        assert morning._commute_route({"morning_topics": ["SpaceX news"]}) is None

    def test_plain_traffic_topic_is_not_a_route(self):
        assert morning._commute_route({"morning_topics": ["St. Louis traffic"]}) is None

    def test_uses_route_not_city_when_available(self):
        with patch.object(morning, "get_travel_time", return_value="22 minutes, 13 miles. 3 over normal.") as route, \
             patch.object(morning, "get_city_traffic", return_value="Roads are clear.") as city, \
             patch.object(morning, "_weather_report", return_value="warm"), \
             patch.object(morning, "_topic_digest", return_value="x"), \
             patch("palmer.trends.adjacent_story", return_value=None):
            out = morning._gather_morning_data({"city": "Kirkwood", "morning_topics": [self.TOPIC]})
        route.assert_called_once()
        city.assert_not_called()
        assert any("22 minutes" in s for s in out)

    def test_falls_back_to_city_without_a_route(self):
        with patch.object(morning, "get_travel_time") as route, \
             patch.object(morning, "get_city_traffic", return_value="Roads are clear.") as city, \
             patch.object(morning, "_weather_report", return_value="warm"), \
             patch.object(morning, "_topic_digest", return_value="x"), \
             patch("palmer.trends.adjacent_story", return_value=None):
            morning._gather_morning_data({"city": "Kirkwood", "morning_topics": ["SpaceX news"]})
        route.assert_not_called()
        city.assert_called_once()

    def test_routing_failure_falls_back_instead_of_leaking_the_error(self):
        """get_travel_time returns its errors as strings — those must never
        reach a briefing as if they were traffic."""
        for failure in ("Couldn't find that starting address: '33 Cedarbrook'.",
                        "Routing failed for 'a' → 'b'.",
                        "Traffic API is not configured."):
            with patch.object(morning, "get_travel_time", return_value=failure), \
                 patch.object(morning, "get_city_traffic", return_value="Roads are clear.") as city, \
                 patch.object(morning, "_weather_report", return_value="warm"), \
                 patch.object(morning, "_topic_digest", return_value="x"), \
                 patch("palmer.trends.adjacent_story", return_value=None):
                out = morning._gather_morning_data({"city": "Kirkwood", "morning_topics": [self.TOPIC]})
            city.assert_called_once()
            assert not any("Couldn't find" in s or "Routing failed" in s for s in out)

    def test_route_line_ok(self):
        assert morning._route_line_ok("17 minutes, 13.7 miles. Basically free-flow.")
        assert not morning._route_line_ok("")
        assert not morning._route_line_ok(None)
        assert not morning._route_line_ok("Need both an origin and destination address to route.")

    def test_commute_is_a_tool_not_an_extracted_field(self):
        """set_commute stores coordinates and a leave time the extractor knows
        nothing about, so the field left EXTRACT_PROMPT the way followed_teams
        and shows did: a Haiku write of {origin, destination} would replace the
        tool's dict and drop both."""
        from palmer import prompts
        from palmer.tools_def import TOOLS
        assert '"commute"' not in prompts.EXTRACT_PROMPT
        assert "set_commute" in prompts.SYSTEM_PROMPT
        assert any(t["name"] == "set_commute" for t in TOOLS)


# ============================================================================
# from test_weather_city.py
# ============================================================================
#
# The weather city: written where the user sets it, named where it was measured.
#
# A user in Culver City got three consecutive mornings of Los Angeles
# temperatures — 98, 100, 102 — against local highs of 88, 89, 90. weather.py was
# innocent throughout; it faithfully forecast whatever city it was handed. Two
# independent defects stacked:
#
# 1. WRITE. He set his weather location by saying "I want the weather updates to
#    be specific to Culver City California". That routes to
#    update_morning_briefing, which wrote the topic string and nothing else, so
#    profile["city"] kept its older, broader value — and profile["city"] is the
#    sole input to every weather pull Palmer makes. EXTRACT_PROMPT could not
#    cover it: LOCATION PRECISION only writes city from a statement of residence
#    or an explicit correction, and a weather preference is neither.
#
# 2. READ. The drafter was handed "Weather in Los Angeles: high 102" and still
#    wrote "102 in Culver City today", reconciling the number against the
#    Culver City strings all over the profile in its system prompt. Nothing
#    stopped it: the line prompt's only data rule was about numbers. The text
#    briefing has had the city rule since the beginning (see morning.py's
#    "name the city the forecast is for") — the one-line path never inherited it.
#
# Defect 1 governs how often the data is wrong. Defect 2 governs what a wrong
# value can do: a number under the wrong city name is unfalsifiable from the
# message, where "102 in Los Angeles" is read as wrong in one second. Both are
# tested here, because fixing only the first leaves the next unenumerated write
# path free to produce the same confident lie.

def _dispatch_block() -> str:
    """The update_morning_briefing arm of the tool loop, as source."""
    import inspect
    src = inspect.getsource(agent.get_reply)
    return src.split('update_morning_briefing"')[1].split("elif b.name")[0]


class TestCityFromWeatherTopic:
    def test_a_weather_topic_yields_its_city(self):
        with patch("palmer.morning._infer_city_from_topics", return_value="Culver City, CA") as m:
            assert agent._city_from_weather_topic("Culver City CA weather") == "Culver City, CA"
        assert m.call_args[0][0] == ["Culver City CA weather"]

    def test_forecast_and_temperature_phrasings_also_count(self):
        with patch("palmer.morning._infer_city_from_topics", return_value="Woodland Hills, CA"):
            for t in ("Woodland Hills CA weather forecast", "Denver temperature"):
                assert agent._city_from_weather_topic(t) == "Woodland Hills, CA"

    def test_a_non_weather_topic_never_pays_for_a_lookup(self):
        """This runs on the write path, but a model call per added topic is
        still worth avoiding — and a news topic must never move the city."""
        with patch("palmer.llm.client") as client:
            for t in ("AI news", "US politics", "Bitcoin price", "SPCX stock price"):
                assert agent._city_from_weather_topic(t) is None
        client.messages.create.assert_not_called()

    def test_empty_input_is_safe(self):
        assert agent._city_from_weather_topic("") is None
        assert agent._city_from_weather_topic(None) is None

    def test_an_unresolvable_weather_topic_leaves_the_city_alone(self):
        with patch("palmer.morning._infer_city_from_topics", return_value=None):
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


# ============================================================================
# from test_repetition.py
# ============================================================================
#
# Repetition, and the two opposite remedies it needs.
#
# Measured across every message Palmer has sent: 39 near-duplicate pairs for one
# user, 11 for another. They were not one problem.
#
# SUPPRESSION — an unprompted message repeating one already sent. One user got the
# identical followup twice, verbatim, because `_is_duplicate_subject` looked back
# six hours while the followup job runs every four and the subject stayed live for
# days. Another got "Here you go - <link>" three times, word for word.
#
# VARIATION — a scheduled message the user DID ask for, said the same way every
# time: "Morning Alex - 103 today in Cedar Falls", "106 in Cedar Falls
# today, Alex", "111 today in Cedar Falls, Alex". Suppressing those would be
# wrong; they asked for a daily briefing. Only the phrasing may not repeat. And
# token overlap cannot see it — those score 0.23 against each other.
#
# LEAKAGE — a third thing found on the way: Palmer narrating its own filtering to
# the reader, in the third person, about her. morning.py had a guard; the four
# other senders never ran it.
#
# The corpora here are real messages. All offline.

REPEAT_MORNINGS = [
    "Morning Alex - 103 today in Cedar Falls, stay inside if you can.",
    "106 in Cedar Falls today, Alex - hottest it's been all week.",
    "111 today in Cedar Falls, Alex - and there's actually a chance of thunderstorms.",
    "110 in Cedar Falls today, so outdoor plans need a rethink.",
]
FRESH_LINES = [
    "Courtney Barnett plays the Hollywood Palladium Friday if you want a reason to get out.",
    "Bitcoin's up 3.1% overnight - quiet week so that move's worth a look.",
    "85 and sunny in Kirkwood, commute's clean at 17 minutes.",
]


class TestSuppressingVerbatimRepeats:
    def test_the_identical_followup_is_caught(self):
        line = "yo how'd practice look today? hurts moving like they said?"
        assert guards.near_duplicate(line, [line]) == line

    def test_the_repeated_link_reply_is_caught(self):
        a = "Here you go -\n\nhttps://palmer-app.example/h/abc"
        b = "Here you go -\n\nhttps://palmer-app.example/h/abc"
        assert guards.near_duplicate(a, [b])

    def test_the_url_is_not_what_makes_them_similar(self):
        """Two different sentences carrying the same page link are not repeats —
        otherwise every message that ends in their URL reads as identical."""
        a = "Muse plays the Hollywood Bowl Sunday. https://x.example/h/abc"
        b = "Bitcoin's up 3% overnight. https://x.example/h/abc"
        assert guards.near_duplicate(a, [b]) is None

    def test_genuinely_different_messages_pass(self):
        for i, line in enumerate(FRESH_LINES):
            others = FRESH_LINES[:i] + FRESH_LINES[i + 1:]
            assert guards.near_duplicate(line, others) is None, line

    def test_the_lexical_check_runs_before_any_model_call(self):
        """The point is that a verbatim repeat costs nothing to catch, so it can
        look back three days where the semantic check cannot afford to."""
        import inspect
        from palmer import userprofile
        src = inspect.getsource(userprofile._is_duplicate_subject)
        assert src.index("near_duplicate") < src.index("client.messages.create")
        assert userprofile.VERBATIM_WINDOW_HOURS > 24

    def test_no_history_is_not_a_duplicate(self):
        assert guards.near_duplicate("anything", []) is None
        assert guards.near_duplicate("", ["anything"]) is None


class TestVaryingAScheduledMessage:
    """The content is supposed to recur. The opening is not."""

    def test_the_repeated_mornings_share_an_opening_shape(self):
        shapes = {guards.opening_shape(m) for m in REPEAT_MORNINGS[1:]}
        assert len(shapes) == 1, f"these three open identically: {shapes}"

    def test_a_repeat_is_detected_against_recent_sends(self):
        assert guards.repeats_opening(REPEAT_MORNINGS[3], REPEAT_MORNINGS[:3])

    def test_numbers_do_not_disguise_a_repeat(self):
        """103 / 106 / 111 made every day look unique to a token comparison."""
        assert guards.similarity(REPEAT_MORNINGS[1], REPEAT_MORNINGS[2]) < 0.4
        assert guards.repeats_opening(REPEAT_MORNINGS[1], [REPEAT_MORNINGS[2]])

    def test_a_genuinely_different_opening_is_left_alone(self):
        for line in FRESH_LINES:
            assert guards.repeats_opening(line, REPEAT_MORNINGS) is None, line

    def test_leading_with_something_else_clears_it(self):
        """The redraft asks for the same facts starting somewhere else."""
        recast = "Courtney Barnett's at the Hollywood Palladium Friday, and it's 110 in Cedar Falls."
        assert guards.repeats_opening(recast, REPEAT_MORNINGS) is None

    def test_the_morning_line_redrafts_on_a_repeat(self):
        import inspect
        from palmer import morning
        src = inspect.getsource(morning.generate_morning_line)
        assert "repeats_opening" in src
        # Split across source lines in the literal, so match a fragment.
        assert "number identical" in src, "a recast must not move the facts"


class TestDeliberationNeverShips:
    LEAKED = [
        "Both of these fall into the crime/dark content category they explicitly "
        "asked to avoid. Skipping.",
        "This one's in the crime/dark content bucket they asked to avoid. Skipping it.",
    ]
    FINE = [
        "Muse plays the Hollywood Bowl Sunday if you need a reason to get out.",
        "They beat the Pirates 4-1 last night - Mathews got his first career win.",
        "You asked me to watch that fare - it's down to $668.",
        "90 in Culver City today, low 70, barely a rain chance.",
        # These four were BLOCKED by the either-signal version. Because
        # send_sms returns False and main.py answers a falsy send with
        # FALLBACK_SMS, the user got "something went sideways on my end, try
        # again" in place of a perfectly good reply. Agreeing to stop doing
        # something is not a leak, and neither is news about someone else.
        "got it, not sending those anymore",
        "noted - won't send you the crime stuff again",
        "they said the deal closes Friday",
        "they asked for a recount and the board agreed",
    ]

    def test_the_real_leaks_are_caught(self):
        for text in self.LEAKED:
            assert guards.leaks_deliberation(text), text

    def test_ordinary_messages_survive(self):
        """"They beat the Pirates" is a sentence about a baseball team, and
        "you asked me to watch that fare" is Palmer talking TO someone."""
        for text in self.FINE:
            assert not guards.leaks_deliberation(text), text

    def test_agreeing_to_stop_is_not_a_leak(self):
        """The distinction the guard has to make: "not sending those anymore" is
        a commitment TO the reader; "not sending, doesn't meet the threshold" is
        Palmer explaining its plumbing to them."""
        assert not guards.leaks_deliberation("sure, not sending those anymore")
        assert guards.leaks_deliberation("Not sending - it doesn't meet the threshold.")

    def test_news_about_a_third_party_survives(self):
        """"said" is deliberately not an intent verb here: "they said the deal
        closes Friday" is the sort of sentence Palmer exists to send."""
        for text in ("they said the deal closes Friday",
                     "they wanted a bigger deal and walked",
                     "she told me they prefer the early show"):
            assert not guards.leaks_deliberation(text), text

    def test_naming_the_reader_as_the_user_is_damning_alone(self):
        """Nobody texting a friend calls them "the user"."""
        assert guards.leaks_deliberation("the user prefers shorter updates")

    def test_internal_machinery_is_damning_alone(self):
        for text in ("scored below the bar so no alert needed",
                     "this one was filtered out",
                     "suppressing that one"):
            assert guards.leaks_deliberation(text), text

    def test_paraphrase_does_not_escape_it(self):
        """The guard it replaces matched fixed phrases, so the model wrote
        around them — "they EXPLICITLY asked" missed a rule looking for
        "they asked"."""
        for text in ("the user specified no sports so leaving that out",
                     "This one doesn't meet the threshold, not sending.",
                     "They said they prefer lighter stuff, so I'll skip it."):
            assert guards.leaks_deliberation(text), text

    def test_it_is_blocked_at_the_one_place_everything_passes_through(self):
        """morning.py had a guard for this and four other senders never ran it."""
        import inspect
        from palmer import sms_util
        assert "leaks_deliberation" in inspect.getsource(sms_util.send_sms)

    def test_blocking_returns_false_rather_than_sending_something_else(self):
        from palmer import sms_util
        with patch.object(sms_util, "_twilio") as tw:
            sent = sms_util.send_sms("+1555", self.LEAKED[0])
        assert sent is False
        tw.messages.create.assert_not_called(), "nothing may go out, not even a fallback"


class TestADeliberationLeakInAReplyIsRedraftedNotDropped:
    """send_sms blocks a leak outright, and for an unprompted message that is
    exactly right — every real violation was a drafter announcing it had decided
    NOT to send something, so doing that silently is what it was trying to do.

    On a reply it is the wrong trade. The user is waiting on an answer, and a
    block there means main.py's falsy-send path hands them FALLBACK_SMS. So
    _finalize redrafts first, and the send_sms block stays behind it."""

    def _finalize(self, first, *retries):
        from palmer import agent
        calls = []

        def _create(**kw):
            calls.append(kw)
            text = retries[min(len(calls) - 1, len(retries) - 1)] if retries else first
            return MagicMock(content=[MagicMock(text=text)])

        with patch.object(agent.client.messages, "create", side_effect=_create):
            out, _ = agent._finalize(first, "sys", [{"role": "user", "content": "hi"}], None)
        return out, calls

    def test_a_leak_is_redrafted_once(self):
        out, calls = self._finalize(
            "This one doesn't meet the threshold, not sending.",
            "nothing worth flagging today")
        assert "threshold" not in out
        assert len(calls) == 1

    def test_a_clean_reply_still_costs_nothing(self):
        out, calls = self._finalize("sure, not sending those anymore")
        assert calls == []
        assert out == "sure, not sending those anymore"

    def test_a_failed_redraft_keeps_the_original_rather_than_going_silent(self):
        from palmer import agent
        leak = "This one doesn't meet the threshold, not sending."
        with patch.object(agent.client.messages, "create", side_effect=RuntimeError("down")):
            out, _ = agent._finalize(leak, "sys", [], None)
        assert out == leak, "send_sms is the backstop; _finalize must not blank it"

    def test_the_correction_tells_it_what_to_do_instead(self):
        assert "TO them" in guards.DELIBERATION_CORRECTION

    def test_the_send_sms_block_is_still_there(self):
        """The chokepoint stays — proactive senders never reach _finalize."""
        import inspect
        from palmer import sms_util
        assert "leaks_deliberation" in inspect.getsource(sms_util.send_sms)
