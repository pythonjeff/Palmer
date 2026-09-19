"""The conversation loop: prompt calibration, resilience, tool dispatch.

Merged from test_calibration.py, test_reply_resilience.py, test_commute.py, test_add_price_watch_tool.py; each section keeps its
original file's notes, because those carry the incident that led to the test.
"""
import inspect
import re
from unittest.mock import patch, MagicMock
from palmer import agent, prompts, reminders, artifacts, home, morning, page, traffic, userprofile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from palmer.timeutil import friendly_hhmm
from tests.helpers import drive_tool, tool_by_name


# ============================================================================
# from test_calibration.py
# ============================================================================
#
# Tests for register calibration — Palmer adapting HOW he sounds to who's texting.
#
# The personality lives in prose inside SYSTEM_PROMPT, so these are structural
# assertions in the style of test_rubrics.py: the sections exist, they say the
# load-bearing thing, and the profile signal actually reaches the prompt.
#
# The ordering test is not cosmetic. SOUND CHECK is nine examples in one cultural
# register, and few-shot beats instructions — SAME PALMER, DIFFERENT PEOPLE has to
# come after it to be the last word on voice.

def _render() -> str:
    """SYSTEM_PROMPT as _build_system renders it."""
    return agent.SYSTEM_PROMPT.format(clock_block="RIGHT NOW\n(clock)",
                                      profile_block="(none)")


class TestCalibrationSection:
    def test_section_exists(self):
        assert "CALIBRATION\n" in agent.SYSTEM_PROMPT

    def test_names_all_four_axes(self):
        body = agent.SYSTEM_PROMPT
        for axis in ("Irony tolerance", "Precision", "Formality and idiom", "Directness"):
            assert axis in body, f"calibration axis {axis!r} missing"

    def test_spine_is_non_negotiable(self):
        """Calibrating must never be licence to become a neutral assistant."""
        body = agent.SYSTEM_PROMPT.lower()
        assert "the spine doesn't" in body
        assert "neutral assistant" in body

    def test_does_not_announce_the_adjustment(self):
        assert "never announce that you're adjusting" in agent.SYSTEM_PROMPT.lower()


class TestRangeExamples:
    def test_block_exists(self):
        assert "SAME PALMER, DIFFERENT PEOPLE" in agent.SYSTEM_PROMPT

    def test_comes_after_sound_check(self):
        body = agent.SYSTEM_PROMPT
        assert body.index("SOUND CHECK") < body.index("SAME PALMER, DIFFERENT PEOPLE"), \
            "range examples must follow SOUND CHECK or the single-register block wins"

    def test_sound_check_is_labelled_as_one_register(self):
        """The original nine examples must not read as the only way to sound."""
        head = agent.SYSTEM_PROMPT.split("SOUND CHECK\n", 1)[1].split("them:", 1)[0]
        assert head.strip(), "SOUND CHECK has no lead-in labelling it as one register"
        assert "one register" in head.lower()

    def test_covers_distinct_registers(self):
        block = agent.SYSTEM_PROMPT.split("SAME PALMER, DIFFERENT PEOPLE", 1)[1] \
                                   .split("NEW USERS", 1)[0]
        assert "Poisson" in block, "no precise/technical register example"
        assert "second language" in block, "no formal/ESL register example"
        assert "rough day" in block, "no bad-day register example"


class TestPromptStillRenders:
    def test_format_does_not_raise(self):
        """SYSTEM_PROMPT is .format()ed — a stray brace in new prose breaks every reply."""
        _render()

    def test_placeholders_survive(self):
        out = _render()
        assert "RIGHT NOW\n(clock)" in out
        assert "(none)" in out


class TestBuildSystemWiring:
    def _build(self, profile: dict) -> str:
        with patch.object(agent, "get_profile", return_value=profile), \
             patch.object(agent, "get_user_watches", return_value=[]), \
             patch.object(agent, "get_user_price_watches", return_value=[]):
            return agent._build_system("+15550001111")

    def test_communication_style_reaches_the_prompt_as_a_directive(self):
        out = self._build({"name": "Ada",
                           "communication_style": "precise, wants the answer first"})
        assert "CALIBRATION READ" in out
        assert "precise, wants the answer first" in out
        assert "mirror it" in out

    def test_explicit_request_outranks_inference(self):
        out = self._build({"communication_style": "asked directly for less sarcasm"})
        assert "outranks" in out

    def test_absent_style_adds_no_calibration_read(self):
        assert "CALIBRATION READ" not in self._build({"name": "Ada"})

    def test_blank_style_adds_no_calibration_read(self):
        assert "CALIBRATION READ" not in self._build({"communication_style": "   "})

    def test_empty_profile_still_builds(self):
        assert "CALIBRATION READ" not in self._build({})


class TestOnboardingAsk:
    """Message 1 never demands name/city (NEW USERS rule, above). From message
    2 on — intro already sent — if Palmer still doesn't know one or both, the
    dynamically-appended ONBOARDING ASK block tells him to work it in, once."""

    def _build(self, profile: dict, is_new_user: bool = False) -> str:
        with patch.object(agent, "get_profile", return_value=profile), \
             patch.object(agent, "get_user_watches", return_value=[]), \
             patch.object(agent, "get_user_price_watches", return_value=[]):
            return agent._build_system("+15550001111", is_new_user=is_new_user)

    def test_asks_for_both_when_both_are_missing(self):
        out = self._build({"intro_sent": True})
        assert "ONBOARDING ASK" in out
        assert "name and what city" in out

    def test_asks_only_for_the_one_still_missing(self):
        out = self._build({"intro_sent": True, "name": "Ada"})
        assert "ONBOARDING ASK" in out
        assert "their city" in out
        assert "name and what city" not in out

    def test_silent_on_the_very_first_message(self):
        """Message 1 is is_new_user=True — the NEW USERS rules own that reply,
        not this block, even if the profile happens to be empty."""
        assert "ONBOARDING ASK" not in self._build({}, is_new_user=True)

    def test_silent_once_name_and_city_are_both_known(self):
        out = self._build({"intro_sent": True, "name": "Ada", "city": "Chicago"})
        assert "ONBOARDING ASK" not in out

    def test_silent_once_already_asked(self):
        """Consumed once by userprofile._update_profile — see test_profile_schema.py."""
        out = self._build({"intro_sent": True, "onboarding_ask_sent": True})
        assert "ONBOARDING ASK" not in out

    def test_never_volunteers_the_link(self):
        out = self._build({"intro_sent": True})
        block = out.split("ONBOARDING ASK", 1)[1].lower()
        assert "send any link" in block or "don't mention their page" in block


class TestExtractionCapturesRegister:
    def test_prompt_asks_for_explicit_requests(self):
        body = prompts.EXTRACT_PROMPT.lower()
        assert "communication_style" in body
        assert "verbatim" in body, "explicit register asks must be recorded as stated"
        assert "joke back" in body


class TestReminderPathHonoursStyle:
    """Reminders used to carry their own mini-persona and never saw SYSTEM_PROMPT.
    They now go through _build_system like every other user-facing message."""

    def test_reminder_uses_the_shared_system_prompt(self):
        captured = {}

        def _fake_create(**kwargs):
            captured.update(kwargs)
            block = MagicMock()
            block.text = "hey - dentist at 3"
            resp = MagicMock()
            resp.content = [block]
            return resp

        with patch.object(reminders, "_build_system",
                          return_value="SYSTEM WITH CALIBRATION READ: blunt") as bs, \
             patch.object(reminders.client.messages, "create", side_effect=_fake_create):
            out = reminders._personalize_reminder(
                "+15550001111", "dentist at 3",
                {"communication_style": "blunt, asked for just the facts"},
            )

        assert out
        bs.assert_called_once()
        assert captured["system"] == "SYSTEM WITH CALIBRATION READ: blunt"
        assert captured["model"] == reminders.SONNET_MODEL, \
            "user-facing drafting belongs on Sonnet"

    def test_reminder_survives_a_drafting_failure(self):
        with patch.object(reminders, "_build_system", return_value="SYS"), \
             patch.object(reminders.client.messages, "create",
                          side_effect=RuntimeError("boom")):
            out = reminders._personalize_reminder("+15550001111", "dentist at 3", {})
        assert "dentist at 3" in out, "a failed draft must still deliver the reminder"


class TestThePromptDescribesTheProductThatExists:
    """SYSTEM_PROMPT is what Palmer paraphrases to a user when he confirms
    something. Every claim in it is a promise, and three had gone stale.

    The price rule is the sharpest case: the prompt still said "~15% drops"
    long after that bar was deleted for the second time and replaced with a
    flat $2 in either direction. So Palmer described a drop-only percentage
    watch and then sent a $2 rise alert. Nothing cross-checked the prose
    against the constant, which is why it could drift at all.
    """

    def test_the_price_bar_is_the_one_the_code_uses(self):
        from palmer import shopping
        block = agent.SYSTEM_PROMPT.split("PRICE WATCHES")[1].split("USE THE RIGHT TOOL")[0]
        assert f"${shopping.MOVE_MIN_ABS:.0f}" in block

    def test_the_prompt_never_promises_a_percentage_bar(self):
        """A percentage always encodes an assumption about the kind of product,
        and the watch list holds every kind. Two versions failed that way."""
        block = agent.SYSTEM_PROMPT.split("PRICE WATCHES")[1].split("USE THE RIGHT TOOL")[0]
        assert "%" not in block

    def test_the_price_watch_promise_includes_rises(self):
        block = agent.SYSTEM_PROMPT.split("PRICE WATCHES")[1].split("USE THE RIGHT TOOL")[0]
        assert "rise" in block.lower()

    def test_the_morning_is_described_as_basics_plus_a_link(self):
        """It stopped carrying tracked topics two versions ago; the prompt
        still promised "sports scores, news, Bitcoin price"."""
        block = agent.SYSTEM_PROMPT.split("MORNING BRIEFING")[1].split("PRICE WATCHES")[0]
        assert "page" in block
        assert "commute" in block.lower()

    def test_nothing_claims_flight_watching_is_missing(self):
        """add_flight_watch shipped, and one NEVER bullet still held up "I
        can't watch them for changes yet" as the model sentence to imitate —
        against the routing block's own "Never say you can't track flights"."""
        assert "can't watch them for changes" not in agent.SYSTEM_PROMPT


class TestEveryToolIsRouted:
    def test_the_prompt_names_every_tool_it_ships_with(self):
        """A tool the routing block never mentions is one the model resolves
        from its own description alone — and add_watch's description tells it
        to fire on "a team, a story, a market", which is exactly what the
        block assigns to update_morning_briefing and follow_team."""
        from palmer.tools_def import TOOLS
        missing = [t["name"] for t in TOOLS if t["name"] not in agent.SYSTEM_PROMPT]
        assert missing == [], f"tools with no routing guidance: {missing}"

    def test_the_three_way_track_collision_is_resolved(self):
        block = agent.SYSTEM_PROMPT.split("USE THE RIGHT TOOL")[1]
        line = next(ln for ln in block.split("\n") if ln.startswith("- add_watch vs"))
        for other in ("update_morning_briefing", "follow_team"):
            assert other in line

    def test_cancelling_is_routed_too(self):
        """"Stop tracking the Eagles" matches four tools, and guessing deletes
        something they wanted."""
        block = agent.SYSTEM_PROMPT.split("USE THE RIGHT TOOL")[1]
        for verb in ("cancel_watch", "cancel_reminders", "unfollow_team",
                     "unfollow_show", "cancel_price_watch", "cancel_flight_watch"):
            assert verb in block


class TestFailureHasAPolicyOfItsOwn:
    """It used to be a subordinate clause inside the anti-competitor NEVER
    bullet — the prompt said at length what Palmer must not say on a failure
    and almost nothing about what he should."""

    def test_there_is_a_section_for_it(self):
        assert "WHEN A TOOL COMES BACK EMPTY OR BROKEN" in agent.SYSTEM_PROMPT

    def test_it_separates_empty_from_broken_from_incapable(self):
        block = agent.SYSTEM_PROMPT.split("WHEN A TOOL COMES BACK EMPTY OR BROKEN")[1] \
                                   .split("CURATION")[0]
        assert "Empty is not broken" in block
        assert "invent" in block
        assert "somewhere else" in block

    def test_a_failed_lookup_is_not_a_fact_about_the_world(self):
        """The failed SpaceX lookup confirmed the model's own stale prior and
        Palmer told the user the company was private while SPCX was trading."""
        block = agent.SYSTEM_PROMPT.split("WHEN A TOOL COMES BACK EMPTY OR BROKEN")[1] \
                                   .split("CURATION")[0]
        assert "not a company that isn't listed" in block


class TestClarificationOutranksTheRhythmRules:
    """The rule permitting a clarifying question was stated once. The pressure
    against ending on a question was stated four times, three of them
    absolute — including one that is shape-based and unconditional, and so
    fires exactly when a clarification needs a second turn."""

    def test_the_precedence_is_stated(self):
        block = agent.SYSTEM_PROMPT.split("WHEN YOU DON'T KNOW WHAT THEY MEAN")[1] \
                                   .split("READ THE SUBTEXT")[0]
        assert "outranks" in block

    def test_asking_twice_is_allowed_when_it_is_still_unclear(self):
        block = agent.SYSTEM_PROMPT.split("WHEN YOU DON'T KNOW WHAT THEY MEAN")[1] \
                                   .split("READ THE SUBTEXT")[0]
        assert "ask again" in block

    def test_a_resolved_thing_is_named_back_not_confirmed_silently(self):
        block = agent.SYSTEM_PROMPT.split("WHEN YOU DON'T KNOW WHAT THEY MEAN")[1] \
                                   .split("READ THE SUBTEXT")[0]
        assert "resolved" in block
        assert "correct it" in block


# ============================================================================
# from test_reply_resilience.py
# ============================================================================
#
# A turn that needs one tool call too many must still answer.
#
# get_reply capped the tool loop at six and RAISED past it. main.py catches that,
# leaves `reply` as None, and answers a falsy reply with FALLBACK_SMS — so a turn
# like "add Apple, Nvidia and Tesla, then what's my commute" died outright, threw
# away every tool result it had already gathered, and told the user something went
# sideways. Same shape as the deliberation guard: machinery meant to protect the
# user producing the thing the user complained about.

def _blocks(*, text=None, tool=None):
    out = []
    if tool:
        b = MagicMock()
        b.type = "tool_use"
        b.name = tool
        b.id = "tu_1"
        b.input = {"query": "x"}
        del b.text          # so `hasattr(b, "text")` is False
        out.append(b)
    if text is not None:
        t = MagicMock()
        t.type = "text"
        t.text = text
        out.append(t)
    return out


class TestTrimToSentence:
    def test_a_truncated_draft_is_cut_back_to_a_boundary(self):
        out = agent._trim_to_sentence("Sure. 90 today, commute is 22 min. Anything el")
        assert out == "Sure. 90 today, commute is 22 min."

    def test_a_complete_draft_is_untouched(self):
        assert agent._trim_to_sentence("90 today.") == "90 today."

    def test_no_boundary_at_all_keeps_the_fragment(self):
        """A short fragment still beats nothing — there is no second draft."""
        assert agent._trim_to_sentence("no boundary here") == "no boundary here"

    def test_it_never_returns_something_uselessly_short(self):
        # Trimming "Ok. <long truncated clause>" back to "Ok." would be worse
        # than shipping the fragment.
        out = agent._trim_to_sentence("Ok. and then the thing about the fare wa")
        assert out.startswith("Ok. and then")

    def test_empty_is_safe(self):
        assert agent._trim_to_sentence("") == ""


class TestMaxTokensDoesNotShipHalfASentence:
    def test_the_reply_is_trimmed(self):
        resp = MagicMock(stop_reason="max_tokens",
                         content=_blocks(text="Sure. 90 today. Anything el"))
        with patch.object(agent.client.messages, "create", return_value=resp), \
             patch.object(agent, "_build_system", return_value="sys"), \
             patch.object(agent, "get_profile", return_value={}), \
             patch.object(agent, "get_history", return_value=[]):
            out, _ = agent.get_reply("+15550001111", "hi", history=[])
        assert out == "Sure. 90 today."


class TestTheLoopAnswersInsteadOfDying:
    def _run(self, final_text):
        """Every response asks for another tool, so the cap is always reached."""
        calls = []

        def _create(**kw):
            calls.append(kw)
            if "tools" not in kw:          # the final, tool-less ask
                return MagicMock(stop_reason="end_turn",
                                 content=_blocks(text=final_text))
            return MagicMock(stop_reason="tool_use", content=_blocks(tool="web_search"))

        with patch.object(agent.client.messages, "create", side_effect=_create), \
             patch.object(agent, "_build_system", return_value="sys"), \
             patch.object(agent, "get_profile", return_value={}), \
             patch.object(agent, "get_history", return_value=[]), \
             patch.object(agent, "_search", return_value="some results"):
            out, _ = agent.get_reply("+15550001111", "do lots", history=[])
        return out, calls

    def test_it_returns_an_answer_rather_than_raising(self):
        out, _ = self._run("here's what I found, in short")
        assert out == "here's what I found, in short"

    def test_the_final_ask_has_no_tools(self):
        """Tools are taken away so the model must answer from what it gathered."""
        _, calls = self._run("answer")
        assert "tools" not in calls[-1]

    def test_it_stops_at_the_cap(self):
        _, calls = self._run("answer")
        # One call per iteration, plus the final tool-less one.
        assert len(calls) == agent.TOOL_ITERATION_CAP + 1

    def test_the_gathered_work_is_carried_into_the_final_ask(self):
        _, calls = self._run("answer")
        assert len(calls[-1]["messages"]) > 1, "tool results must survive"

    def test_the_cap_is_high_enough_for_ordinary_asks(self):
        """Three tickers plus a commute is five calls before Palmer speaks."""
        assert agent.TOOL_ITERATION_CAP >= 8


class TestAToolThatRaisesDoesNotLoseTheTurn:
    """The same failure as the cap, through a different door.

    Nothing wrapped the dispatch chain, so a raise anywhere in it escaped
    get_reply, main.py answered the falsy reply with FALLBACK_SMS, and every
    tool result already gathered went with it. In a three-intent turn one
    failing DB write destroyed the two intents that had already succeeded.
    """

    def _run(self, search_side_effect, final_text="ok, here's what I have"):
        calls = []

        def _create(**kw):
            calls.append(kw)
            if len(calls) == 1:
                return MagicMock(stop_reason="tool_use", content=_blocks(tool="web_search"))
            return MagicMock(stop_reason="end_turn", content=_blocks(text=final_text))

        with patch.object(agent.client.messages, "create", side_effect=_create), \
             patch.object(agent, "_build_system", return_value="sys"), \
             patch.object(agent, "get_profile", return_value={}), \
             patch.object(agent, "get_history", return_value=[]), \
             patch.object(agent, "_search", side_effect=search_side_effect):
            out, _ = agent.get_reply("+15550001111", "what's the news", history=[])
        return out, calls

    def _tool_results(self, calls):
        """The tool_result blocks handed back on the follow-up call."""
        out = []
        for m in calls[-1]["messages"]:
            content = m.get("content")
            if isinstance(content, list):
                out += [c for c in content if isinstance(c, dict)
                        and c.get("type") == "tool_result"]
        return out

    def test_the_turn_still_answers(self):
        out, _ = self._run(RuntimeError("boom"))
        assert out == "ok, here's what I have"

    def test_the_error_comes_back_as_a_tool_result(self):
        _, calls = self._run(RuntimeError("boom"))
        results = self._tool_results(calls)
        # Every tool_use block must be answered or the next call is rejected.
        assert len(results) == 1
        assert results[0]["tool_use_id"] == "tu_1"
        assert "web_search" in results[0]["content"]

    def test_a_raise_is_still_loud_in_the_logs(self, capsys):
        self._run(RuntimeError("boom"))
        err = capsys.readouterr()
        assert "TOOL web_search raised: RuntimeError: boom" in err.out
        assert "Traceback" in err.err

    def test_keyboard_interrupt_is_not_swallowed(self):
        """`except Exception`, never `BaseException` — a ctrl-c must still stop."""
        try:
            self._run(KeyboardInterrupt())
        except KeyboardInterrupt:
            return
        assert False, "KeyboardInterrupt was swallowed by the tool guard"


class TestWhatTheModelIsToldWhenAToolRaises:
    def test_it_never_claims_a_missing_capability(self):
        from palmer import guards
        out = agent._tool_error("search_flights", RuntimeError("x"))
        assert not guards.redirects_elsewhere(out)
        assert "not a missing capability" in out

    def test_it_does_not_leak_a_query_string(self):
        """netutil re-raises the urllib error, and that URL carries the API key."""
        exc = RuntimeError(
            "HTTP Error 500: https://serpapi.com/search.json?api_key=SECRETKEY&q=x")
        out = agent._tool_error("search_shopping", exc)
        assert "SECRETKEY" not in out
        assert "api_key" not in out
        assert "RuntimeError" in out          # the type still reaches the model

    def test_an_argument_error_is_actionable(self):
        """Our own strings carry no secrets and are the only ones it can fix."""
        out = agent._tool_error("set_reminder", KeyError("due_at"))
        assert "due_at" in out
        assert "once more" in out

    def test_the_cache_invalidate_guards_are_left_alone(self):
        """They wrap a best-effort expiry AFTER a successful write.

        Folding them into the outer catch would turn a stale cache into a
        tool error for an operation that actually succeeded — Palmer would
        tell the user their topic wasn't added when it was."""
        import inspect
        src = inspect.getsource(agent.get_reply)
        assert src.count("home.invalidate after") >= 6


# ============================================================================
# from test_commute.py
# ============================================================================
#
# The commute is routed for the leave time, not for the moment of the fetch.
#
# The morning job runs at the user's morning time and the page refreshes on tap,
# so a user who leaves at 8:30 used to be told the 7:00 number. Now a commute is
# saved by a tool (set_commute) that geocodes on the write path and may carry a
# leave time; the fetch routes for that departure when it is still ahead
# (TomTom departAt) and live otherwise, and every surface says which one it got.
# The page never renders the addresses — it is a tokenized URL and those are
# someone's home and office.

CHI = ZoneInfo("America/Chicago")
ROUTE = {"routes": [{"summary": {"travelTimeInSeconds": 1020,
                                 "noTrafficTravelTimeInSeconds": 960,
                                 "trafficDelayInSeconds": 60,
                                 "lengthInMeters": 22000}}]}
ORIGIN = "33 Cedarbrook Lane, Kirkwood MO 63122"
DEST = "1 Market St, St. Louis MO 63102"


class TestFriendlyTime:
    def test_morning_afternoon_and_the_two_twelves(self):
        assert friendly_hhmm("08:30") == "8:30am"
        assert friendly_hhmm("17:05") == "5:05pm"
        assert friendly_hhmm("12:00") == "12:00pm"
        assert friendly_hhmm("00:05") == "12:05am"

    def test_never_raises_on_a_render_path(self):
        assert friendly_hhmm("") == ""
        assert friendly_hhmm(None) == ""
        assert friendly_hhmm("noon") == "noon"
        assert friendly_hhmm("25:00") == "25:00"


# What TomTom actually returns for a departAt route: the trip is a third
# slower than free-flow and trafficDelayInSeconds is 0, because that field
# counts live incidents and a prediction has none.
PREDICTED_ROUTE = {"routes": [{"summary": {"travelTimeInSeconds": 1740,
                                           "noTrafficTravelTimeInSeconds": 1320,
                                           "trafficDelayInSeconds": 0,
                                           "lengthInMeters": 22000}}]}


def _snapshot(route=ROUTE, **kw):
    """traffic_snapshot against a canned TomTom reply; returns (result, url)."""
    seen = {}

    def _get(url, **_):
        seen["url"] = url
        return route

    with patch.object(traffic, "_http_get_json", side_effect=_get):
        out = traffic.traffic_snapshot(ORIGIN, DEST, **kw)
    return out, seen.get("url", "")


class TestSnapshotDeparture:
    def setup_method(self):
        traffic._addr_geo_cache.clear()

    def test_a_future_departure_is_routed_for_and_labelled(self):
        depart = datetime.now(CHI) + timedelta(hours=2)
        with patch.object(traffic, "_geocode_address", return_value=(38.5, -90.4)):
            out, url = _snapshot(depart_at=depart, tz_name="America/Chicago")
        assert "departAt=" in url
        assert out["predicted"] is True
        assert out["depart_at"] == depart.strftime("%H:%M")
        assert out["arrive_at"] == (depart + timedelta(seconds=1020)).strftime("%H:%M")

    def test_the_stamp_is_percent_encoded(self):
        """An offset east of UTC carries a "+", which decodes to a space in a
        query string and TomTom 400s on it."""
        depart = datetime.now(ZoneInfo("Europe/Paris")) + timedelta(hours=2)
        with patch.object(traffic, "_geocode_address", return_value=(48.8, 2.3)):
            _, url = _snapshot(depart_at=depart, tz_name="Europe/Paris")
        stamp = url.split("departAt=")[1]
        assert "+" not in stamp and "%2B" in stamp and "%3A" in stamp

    def test_a_predicted_delay_is_live_minus_free_flow(self):
        depart = datetime.now(CHI) + timedelta(hours=2)
        with patch.object(traffic, "_geocode_address", return_value=(38.5, -90.4)):
            out, _ = _snapshot(route=PREDICTED_ROUTE, depart_at=depart, tz_name="America/Chicago")
        assert out["live_min"] == 29 and out["free_min"] == 22
        assert out["delay_min"] == 7, "a 29-vs-22 prediction must not read as 'normal'"
        # A live route keeps TomTom's incident delay, unchanged from before.
        with patch.object(traffic, "_geocode_address", return_value=(38.5, -90.4)):
            out, _ = _snapshot(tz_name="America/Chicago")
        assert out["delay_min"] == 1

    def test_no_zone_means_no_arrival_clock(self):
        with patch.object(traffic, "_geocode_address", return_value=(38.5, -90.4)):
            out, _ = _snapshot(tz_name=None)
            bad, _ = _snapshot(tz_name="Pacific Time")
            ok, _ = _snapshot(tz_name="America/Chicago")
        assert out["arrive_at"] is None and bad["arrive_at"] is None
        assert ok["arrive_at"] and out["live_min"] == ok["live_min"]

    def test_a_past_departure_routes_live(self):
        depart = datetime.now(CHI) - timedelta(minutes=10)
        with patch.object(traffic, "_geocode_address", return_value=(38.5, -90.4)):
            out, url = _snapshot(depart_at=depart, tz_name="America/Chicago")
        assert "departAt" not in url
        assert out["predicted"] is False and "depart_at" not in out
        assert re.fullmatch(r"\d\d:\d\d", out["arrive_at"])

    def test_a_naive_departure_is_ignored_rather_than_read_as_utc(self):
        depart = datetime.now() + timedelta(hours=2)
        with patch.object(traffic, "_geocode_address", return_value=(38.5, -90.4)):
            out, url = _snapshot(depart_at=depart)
        assert "departAt" not in url and out["predicted"] is False

    def test_no_departure_is_the_old_behaviour_plus_a_label(self):
        with patch.object(traffic, "_geocode_address", return_value=(38.5, -90.4)):
            out, url = _snapshot()
        assert "departAt" not in url
        assert out["live_min"] == 17 and out["free_min"] == 16 and out["delay_min"] == 1
        assert round(out["ratio"], 2) == 1.06 and out["miles"] == 13.7
        assert out["predicted"] is False

    def test_stored_coordinates_skip_the_geocoder(self):
        with patch.object(traffic, "_geocode_address", side_effect=AssertionError("geocoded")):
            out, url = _snapshot(origin_ll=[38.5, -90.4], dest_ll=[38.6, -90.2])
        assert "38.5,-90.4:38.6,-90.2" in url and out["live_min"] == 17

    def test_a_legacy_string_commute_still_geocodes(self):
        with patch.object(traffic, "_geocode_address", return_value=(38.5, -90.4)) as geo:
            out, _ = _snapshot()
        assert geo.call_count == 2 and out is not None

    def test_the_result_never_carries_the_addresses(self):
        """The page renders whatever this returns, and the page has no auth."""
        with patch.object(traffic, "_geocode_address", return_value=(38.5, -90.4)):
            out, _ = _snapshot(depart_at=datetime.now(CHI) + timedelta(hours=1))
        assert "origin" not in out and "destination" not in out
        assert not any(ORIGIN in str(v) or DEST in str(v) for v in out.values())


class TestAddressCache:
    def setup_method(self):
        traffic._addr_geo_cache.clear()

    def test_second_lookup_costs_nothing(self):
        reply = {"results": [{"position": {"lat": 1.0, "lon": 2.0}}]}
        with patch.object(traffic, "_http_get_json", return_value=reply) as http:
            assert traffic._geocode_address("1 Main St") == (1.0, 2.0)
            assert traffic._geocode_address(" 1 main st ") == (1.0, 2.0)
        assert http.call_count == 1

    def test_a_miss_is_not_cached(self):
        with patch.object(traffic, "_http_get_json", return_value={"results": []}) as http:
            assert traffic._geocode_address("nowhere") is None
            assert traffic._geocode_address("nowhere") is None
        assert http.call_count == 2


class TestDepartureRule:
    def _at(self, hhmm):
        h, m = hhmm.split(":")
        return datetime(2026, 9, 2, int(h), int(m), tzinfo=CHI)

    def test_a_leave_time_still_ahead_is_routed_for(self):
        out = home._commute_depart_at({"leave_time": "08:30"}, "America/Chicago", now=self._at("07:00"))
        assert out == self._at("08:30") and out.utcoffset() == timedelta(hours=-5)

    def test_inside_the_lead_or_already_past_routes_live(self):
        assert home._commute_depart_at({"leave_time": "08:30"}, "America/Chicago", now=self._at("08:28")) is None
        assert home._commute_depart_at({"leave_time": "08:30"}, "America/Chicago", now=self._at("09:00")) is None

    def test_no_or_bad_leave_time_routes_live(self):
        assert home._commute_depart_at({}, "America/Chicago", now=self._at("07:00")) is None
        assert home._commute_depart_at({"leave_time": "soonish"}, "America/Chicago", now=self._at("07:00")) is None

    def test_no_zone_routes_live_rather_than_predicting_for_utc(self):
        # Without a resolvable zone "08:30" has no clock to sit on; predicting
        # for 08:30Z and labelling it 8:30am would be confidently wrong.
        assert home._commute_depart_at({"leave_time": "08:30"}, None) is None
        assert home._commute_depart_at({"leave_time": "08:30"}, "Pacific Time") is None

    def test_fetch_forwards_the_stored_route_and_departure(self):
        profile = {"timezone": "America/Chicago",
                   "commute": {"origin": ORIGIN, "destination": DEST, "leave_time": "08:30",
                               "origin_ll": [38.5, -90.4], "dest_ll": [38.6, -90.2]}}
        depart = self._at("08:30")
        with patch.object(home, "_commute_depart_at", return_value=depart), \
             patch("palmer.traffic.traffic_snapshot", return_value={"live_min": 3}) as snap:
            assert home._fetch_traffic(profile) == {"live_min": 3}
        kw = snap.call_args.kwargs
        assert kw["depart_at"] == depart and kw["tz_name"] == "America/Chicago"
        assert kw["origin_ll"] == [38.5, -90.4] and kw["dest_ll"] == [38.6, -90.2]

    def test_a_legacy_commute_passes_no_coordinates(self):
        profile = {"commute": {"origin": ORIGIN, "destination": DEST}}
        with patch("palmer.traffic.traffic_snapshot", return_value=None) as snap:
            home._fetch_traffic(profile)
        kw = snap.call_args.kwargs
        assert kw["origin_ll"] is None and kw["dest_ll"] is None and kw["depart_at"] is None


class TestDigest:
    def test_predicted_names_the_departure_and_arrival(self):
        d = morning._payload_digest({"traffic": {"live_min": 34, "delay_min": 9, "predicted": True,
                                                 "depart_at": "08:30", "arrive_at": "09:04"}})
        assert "at 8:30am" in d and "predicted for that departure" in d
        assert "34 min" in d and "9 min slower than normal" in d
        assert "arriving about 9:04am" in d

    def test_live_says_so_and_invents_no_leave_time(self):
        d = morning._payload_digest({"traffic": {"live_min": 20, "delay_min": 0, "predicted": False,
                                                 "arrive_at": "07:22"}})
        assert d.startswith("Commute right now: 20 min, normal")
        assert "arriving about 7:22am" in d and "leave" not in d

    def test_the_drafter_is_told_which_moment_the_number_is_for(self):
        src = inspect.getsource(morning.generate_morning_line)
        assert "for THAT departure" in src and "don't invent a time" in src


class TestPage:
    BASE = dict(city="Kirkwood, MO", weather={"temp_now": 71}, prices=[], headlines=[],
                fetched={}, tracking={})

    def _html(self, traffic_dict):
        return page.render(dict(self.BASE, traffic=traffic_dict), token="tok",
                           image_url="https://x/y.png", page_url="https://x/h/tok")

    def test_predicted_card_shows_leave_and_arrive(self):
        html = self._html({"live_min": 34, "delay_min": 9, "ratio": 1.36, "predicted": True,
                           "depart_at": "08:30", "arrive_at": "09:04"})
        assert "leaves 8:30am" in html and "arrives ~9:04am" in html
        assert "34 min commute at 8:30am" in html  # og:description

    def test_live_card_says_right_now(self):
        html = self._html({"live_min": 17, "delay_min": 0, "ratio": 1.0, "predicted": False,
                           "arrive_at": "07:19"})
        assert "right now" in html and "leaves" not in html
        assert "17 min commute" in html and "commute at" not in html

    def test_addresses_never_reach_the_page(self):
        """Structural: the payload's traffic dict is whatever traffic_snapshot
        returned, and that never carries the addresses. Belt and braces here."""
        html = self._html({"live_min": 34, "delay_min": 9, "ratio": 1.36, "predicted": True,
                           "depart_at": "08:30", "arrive_at": "09:04"})
        assert ORIGIN not in html and DEST not in html and "Cedarbrook" not in html


class TestCard:
    def _payload(self, traffic_dict):
        return {"city": "Kirkwood, MO", "timezone": "America/Chicago",
                "weather": {"temp_now": 71, "high": 88, "low": 64, "description": "clear"},
                "traffic": traffic_dict, "prices": [], "headlines": [], "opening": []}

    def test_renders_with_the_departure_line_and_changes_the_fingerprint(self):
        predicted = self._payload({"live_min": 34, "delay_min": 9, "ratio": 1.36, "predicted": True,
                                   "depart_at": "08:30", "arrive_at": "09:04"})
        live = self._payload({"live_min": 34, "delay_min": 9, "ratio": 1.36, "predicted": False,
                              "arrive_at": "09:04"})
        png = artifacts.render_png("tok-commute-test", predicted)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert artifacts._card_fingerprint(predicted) != artifacts._card_fingerprint(live)


class TestToolsAndRouting:
    def test_both_tools_exist_and_set_takes_an_optional_leave_time(self):
        s = tool_by_name("set_commute")
        assert s is not None and tool_by_name("clear_commute") is not None
        assert set(s["input_schema"]["required"]) == {"origin", "destination"}
        assert "leave_time" in s["input_schema"]["properties"]

    def test_set_carries_the_landmark_warning_get_travel_time_has(self):
        s, g = tool_by_name("set_commute")["description"], tool_by_name("get_travel_time")["description"]
        assert "landmark" in s and "street address" in s
        assert "Fenway Park" in s and "Fenway Park" in g

    def test_travel_time_no_longer_disclaims_storing_addresses(self):
        g = tool_by_name("get_travel_time")["description"]
        assert "don't store" not in g and "set_commute" in g

    def test_system_prompt_routes_the_regular_drive(self):
        block = prompts.SYSTEM_PROMPT.split("USE THE RIGHT TOOL")[1]
        assert "set_commute" in block and "clear_commute" in block
        assert "every day" in block

    def test_commute_is_not_in_the_extractor_schema(self):
        assert '"commute"' not in prompts.EXTRACT_PROMPT


def _drive_commute(tool_name, tool_input, geocodes=((38.5, -90.4), (38.6, -90.2)), key="k"):
    _, result, (upsert, _key, _geo, invalidate) = drive_tool(
        tool_name, tool_input, message="my commute",
        patches=[patch.object(agent, "upsert_profile"),
                 patch.object(traffic, "TOMTOM_API_KEY", key),
                 patch.object(traffic, "_geocode_address", side_effect=list(geocodes)),
                 patch("palmer.home.invalidate")])
    saved = upsert.call_args[0][1] if upsert.called else None
    return result, saved, invalidate


class TestSetDispatch:
    def test_geocodes_on_the_write_path_and_expires_the_card(self):
        result, saved, invalidate = _drive_commute("set_commute",
                                           {"origin": ORIGIN, "destination": DEST, "leave_time": "8:30"})
        assert saved["commute"] == {"origin": ORIGIN, "destination": DEST, "leave_time": "08:30",
                                    "origin_ll": [38.5, -90.4], "dest_ll": [38.6, -90.2]}
        invalidate.assert_called_once_with("+1555", ("traffic",))
        assert "08:30" in result and "without reading the addresses back" in result

    def test_no_leave_time_saves_and_says_the_number_is_live(self):
        result, saved, _ = _drive_commute("set_commute", {"origin": ORIGIN, "destination": DEST})
        assert "leave_time" not in saved["commute"] and saved["commute"]["origin_ll"]
        assert "live" in result and "optional" in result

    def test_an_unresolvable_address_asks_rather_than_guesses(self):
        result, saved, invalidate = _drive_commute("set_commute", {"origin": ORIGIN, "destination": DEST},
                                           geocodes=((38.5, -90.4), None))
        assert saved is None and "do not guess" in result and DEST in result
        invalidate.assert_not_called()

    def test_a_bad_leave_time_saves_nothing(self):
        result, saved, _ = _drive_commute("set_commute",
                                  {"origin": ORIGIN, "destination": DEST, "leave_time": "half eight"})
        assert saved is None and "Nothing saved" in result and "HH:MM" in result

    def test_a_missing_key_is_not_reported_as_a_bad_address(self):
        result, saved, _ = _drive_commute("set_commute", {"origin": ORIGIN, "destination": DEST}, key="")
        assert saved is None and "Nothing saved" in result and "Couldn't find" not in result

    def test_clear_deletes_the_key_and_expires_the_card(self):
        result, saved, invalidate = _drive_commute("clear_commute", {})
        assert saved == {"commute": None}
        invalidate.assert_called_once_with("+1555", ("traffic",))

    def test_source_shape(self):
        src = inspect.getsource(agent.get_reply)
        block = src.split('"set_commute"')[1].split('elif b.name == "clear_commute"')[0]
        assert "_geocode_address" in block and "_normalize_hhmm" in block
        assert "do not guess" in block and "do not name a maps app" in block


class TestExtractorGuard:
    def _apply(self, stored, incoming):
        profile = {"commute": stored} if stored is not None else {}
        with patch.object(userprofile, "upsert_profile") as up, \
             patch.object(userprofile, "get_profile", return_value=profile), \
             patch.object(userprofile, "_derive_timezone", return_value=None):
            userprofile._apply_profile_updates("+1555", profile, {"commute": incoming, "job": "x"})
        return up.call_args[0][1] if up.called else {}

    def test_a_tool_written_commute_survives_an_extractor_write(self):
        written = self._apply({"origin": "a", "destination": "b", "origin_ll": [1, 2]},
                              {"origin": "c", "destination": "d"})
        assert "commute" not in written and written.get("job") == "x"

    def test_a_legacy_commute_may_still_be_replaced(self):
        written = self._apply({"origin": "a", "destination": "b"}, {"origin": "c", "destination": "d"})
        assert written.get("commute") == {"origin": "c", "destination": "d"}

    def test_an_absent_commute_is_written(self):
        written = self._apply(None, {"origin": "c", "destination": "d"})
        assert written.get("commute") == {"origin": "c", "destination": "d"}


# ============================================================================
# from test_add_price_watch_tool.py
# ============================================================================
#
# add_price_watch's dispatch used to insert a row with baseline_price=NULL and
# defer baseline-setting to the next scheduler tick (12h later), unlike
# add_amazon_watch which seeds it immediately. If that first scheduler-side
# match ever failed, the baseline stayed NULL forever and run_price_watches
# could never reach the alert comparison — a real drop would just get silently
# recorded as the (late) baseline with no alert. Seed it at creation time,
# same as Amazon, so a bad match is visible immediately instead of silent.

def _drive_price_watch(tool_input, check_price_result):
    _, result, (save, set_baseline, _check) = drive_tool(
        "add_price_watch", tool_input, message="track this for me",
        profile={"timezone": "America/Chicago"},
        patches=[patch.object(agent, "save_price_watch", return_value=42),
                 patch.object(agent, "set_price_watch_baseline"),
                 patch("palmer.shopping.check_price", return_value=check_price_result)])
    return result, save, set_baseline


class TestBaselineSeededAtCreation:
    def test_a_successful_match_seeds_the_baseline_immediately(self):
        current = {"price": 29.99, "url": "https://example.com/p", "merchant": "Target"}
        result, save, set_baseline = _drive_price_watch({"product_name": "Premier Protein Chocolate 30-pack"}, current)
        save.assert_called_once()
        set_baseline.assert_called_once_with(42, 29.99, "https://example.com/p", "Target")
        assert "29.99" in result

    def test_a_failed_match_does_not_seed_a_baseline_but_still_creates_the_watch(self):
        result, save, set_baseline = _drive_price_watch({"product_name": "some obscure item"}, None)
        save.assert_called_once()
        set_baseline.assert_not_called()
        assert "couldn't pin down a confident match" in result.lower()


class TestTheWatchNamesWhatItMatched:
    """add_amazon_watch echoes the resolved listing; this path echoed the
    user's own words back. The match is picked by a model with no confidence
    floor, so "AirPods" can baseline on Gen 2, Gen 4 or Pro — and a wrong pick
    stayed invisible until an alert arrived about the wrong product."""

    def test_the_matched_title_is_in_the_tool_result(self):
        import inspect
        from palmer import agent
        block = inspect.getsource(agent.get_reply).split('"add_price_watch"')[1] \
                                                  .split("elif b.name")[0]
        assert 'current.get("title")' in block
        assert "It matched:" in block

    def test_the_model_is_told_to_say_it_out_loud(self):
        """A resolved thing named only in the tool result is still invisible
        to the person who can correct it."""
        import inspect
        from palmer import agent
        block = inspect.getsource(agent.get_reply).split('"add_price_watch"')[1] \
                                                  .split("elif b.name")[0]
        assert "correct you" in block
