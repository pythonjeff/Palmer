"""The commute is routed for the leave time, not for the moment of the fetch.

The morning job runs at the user's morning time and the page refreshes on tap,
so a user who leaves at 8:30 used to be told the 7:00 number. Now a commute is
saved by a tool (set_commute) that geocodes on the write path and may carry a
leave time; the fetch routes for that departure when it is still ahead
(TomTom departAt) and live otherwise, and every surface says which one it got.
The page never renders the addresses — it is a tokenized URL and those are
someone's home and office.
"""
import inspect
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import agent
import artifacts
import home
import morning
import page
import prompts
import traffic
import userprofile
from timeutil import friendly_hhmm
from tools_def import TOOLS

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


def _snapshot(**kw):
    """traffic_snapshot against a canned TomTom reply; returns (result, url)."""
    seen = {}

    def _get(url, **_):
        seen["url"] = url
        return ROUTE

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

    def test_fetch_forwards_the_stored_route_and_departure(self):
        profile = {"timezone": "America/Chicago",
                   "commute": {"origin": ORIGIN, "destination": DEST, "leave_time": "08:30",
                               "origin_ll": [38.5, -90.4], "dest_ll": [38.6, -90.2]}}
        depart = self._at("08:30")
        with patch.object(home, "_commute_depart_at", return_value=depart), \
             patch("traffic.traffic_snapshot", return_value={"live_min": 3}) as snap:
            assert home._fetch_traffic(profile) == {"live_min": 3}
        kw = snap.call_args.kwargs
        assert kw["depart_at"] == depart and kw["tz_name"] == "America/Chicago"
        assert kw["origin_ll"] == [38.5, -90.4] and kw["dest_ll"] == [38.6, -90.2]

    def test_a_legacy_commute_passes_no_coordinates(self):
        profile = {"commute": {"origin": ORIGIN, "destination": DEST}}
        with patch("traffic.traffic_snapshot", return_value=None) as snap:
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


def _tool(name):
    return next((t for t in TOOLS if t["name"] == name), None)


class TestToolsAndRouting:
    def test_both_tools_exist_and_set_takes_an_optional_leave_time(self):
        s = _tool("set_commute")
        assert s is not None and _tool("clear_commute") is not None
        assert set(s["input_schema"]["required"]) == {"origin", "destination"}
        assert "leave_time" in s["input_schema"]["properties"]

    def test_set_carries_the_landmark_warning_get_travel_time_has(self):
        s, g = _tool("set_commute")["description"], _tool("get_travel_time")["description"]
        assert "landmark" in s and "street address" in s
        assert "Fenway Park" in s and "Fenway Park" in g

    def test_travel_time_no_longer_disclaims_storing_addresses(self):
        g = _tool("get_travel_time")["description"]
        assert "don't store" not in g and "set_commute" in g

    def test_system_prompt_routes_the_regular_drive(self):
        block = prompts.SYSTEM_PROMPT.split("USE THE RIGHT TOOL")[1]
        assert "set_commute" in block and "clear_commute" in block
        assert "every day" in block

    def test_commute_is_not_in_the_extractor_schema(self):
        assert '"commute"' not in prompts.EXTRACT_PROMPT


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, content, stop_reason):
        self.content, self.stop_reason = content, stop_reason


def _drive(tool_name, tool_input, geocodes=((38.5, -90.4), (38.6, -90.2)), key="k"):
    calls = []
    responses = [
        _Resp([_Block(type="tool_use", name=tool_name, id="t1", input=tool_input)], "tool_use"),
        _Resp([_Block(type="text", text="done")], "end_turn"),
    ]

    def _create(**kw):
        calls.append(kw)
        return responses[len(calls) - 1]

    with patch.object(agent, "_build_system", return_value="sys"), \
         patch.object(agent, "get_history", return_value=[]), \
         patch.object(agent, "get_profile", return_value={}), \
         patch.object(agent, "upsert_profile") as upsert, \
         patch.object(traffic, "TOMTOM_API_KEY", key), \
         patch.object(traffic, "_geocode_address", side_effect=list(geocodes)), \
         patch("home.invalidate") as invalidate, \
         patch.object(agent.client.messages, "create", side_effect=_create):
        agent.get_reply("+1555", "my commute")
    result = calls[1]["messages"][-1]["content"][0]["content"]
    saved = upsert.call_args[0][1] if upsert.called else None
    return result, saved, invalidate


class TestSetDispatch:
    def test_geocodes_on_the_write_path_and_expires_the_card(self):
        result, saved, invalidate = _drive("set_commute",
                                           {"origin": ORIGIN, "destination": DEST, "leave_time": "8:30"})
        assert saved["commute"] == {"origin": ORIGIN, "destination": DEST, "leave_time": "08:30",
                                    "origin_ll": [38.5, -90.4], "dest_ll": [38.6, -90.2]}
        invalidate.assert_called_once_with("+1555", ("traffic",))
        assert "08:30" in result and "without reading the addresses back" in result

    def test_no_leave_time_saves_and_says_the_number_is_live(self):
        result, saved, _ = _drive("set_commute", {"origin": ORIGIN, "destination": DEST})
        assert "leave_time" not in saved["commute"] and saved["commute"]["origin_ll"]
        assert "live" in result and "optional" in result

    def test_an_unresolvable_address_asks_rather_than_guesses(self):
        result, saved, invalidate = _drive("set_commute", {"origin": ORIGIN, "destination": DEST},
                                           geocodes=((38.5, -90.4), None))
        assert saved is None and "do not guess" in result and DEST in result
        invalidate.assert_not_called()

    def test_a_bad_leave_time_saves_nothing(self):
        result, saved, _ = _drive("set_commute",
                                  {"origin": ORIGIN, "destination": DEST, "leave_time": "half eight"})
        assert saved is None and "Nothing saved" in result and "HH:MM" in result

    def test_a_missing_key_is_not_reported_as_a_bad_address(self):
        result, saved, _ = _drive("set_commute", {"origin": ORIGIN, "destination": DEST}, key="")
        assert saved is None and "Nothing saved" in result and "Couldn't find" not in result

    def test_clear_deletes_the_key_and_expires_the_card(self):
        result, saved, invalidate = _drive("clear_commute", {})
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
