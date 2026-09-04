"""The evening update is a diff against the morning, and nothing else.

It replaced three unprompted senders (live scores, a daily news alert, a
profile check-in), and the property that makes it different from all of them
is structural rather than a matter of taste: it only ever says what CHANGED
since the morning update, and on a day when nothing did, it says nothing.

All offline.
"""
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import db
import evening


PHONE = "+15550001111"
TODAY = date(2026, 9, 4)
URL = "https://palmer.example.com/h/AbC123xyz"


def _game(home, away, state, gid="g1", detail=""):
    return {"id": gid, "league": "mlb", "state": state, "detail": detail,
            "home": {"abbrev": "STL", "name": "St. Louis Cardinals", "score": home},
            "away": {"abbrev": "CHC", "name": "Chicago Cubs", "score": away}}


def _row(game):
    return {"team": "St. Louis Cardinals", "abbrev": "STL", "league": "mlb",
            "last": None, "today": game}


MORNING = {
    "prices": [{"label": "NVDA", "price": 180.0}, {"label": "BTC", "price": 60000.0}],
    "headlines": [{"title": "old story", "url": "https://a.example/old", "topic": "AI news",
                   "source": "reuters.com"}],
    "scores": [_row(_game(0, 0, "pre", detail="7:15 PM CT"))],
}


def _with_open(payload: dict, opened_from: dict = MORNING, day: date = TODAY) -> dict:
    return dict(payload, day_open=evening._open_snapshot(opened_from, day))


class TestTheSnapshotIsWhatTheyWereTold:
    def test_it_captures_prices_headline_urls_and_game_state(self):
        snap = evening._open_snapshot(MORNING, TODAY)
        assert snap["date"] == "2026-09-04"
        assert snap["prices"] == {"NVDA": 180.0, "BTC": 60000.0}
        assert snap["headline_urls"] == ["https://a.example/old"]
        assert snap["scores"] == {"g1": {"state": "pre", "home": 0, "away": 0}}

    def test_record_day_open_writes_it_onto_the_page(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "_DB_PATH", tmp_path / "e.db")
        db.init_db()
        import home
        db.upsert_profile(PHONE, {"home_token": "tok"})
        home.save("tok", dict(MORNING, phone=PHONE))
        evening.record_day_open(PHONE, "2026-09-04")
        assert home.load("tok")["day_open"]["prices"]["NVDA"] == 180.0

    def test_record_day_open_never_raises(self):
        with patch("home.home_token", side_effect=RuntimeError("db down")):
            evening.record_day_open(PHONE, TODAY)


class TestNothingChangedMeansNothing:
    def test_an_identical_page_is_an_empty_diff(self):
        assert evening.day_changes(_with_open(MORNING), TODAY) == []

    def test_a_market_move_under_the_bar_is_not_a_change(self):
        later = dict(MORNING, prices=[{"label": "NVDA", "price": 180.9},
                                      {"label": "BTC", "price": 60000.0}])
        assert evening.day_changes(_with_open(later), TODAY) == []

    def test_a_game_still_not_started_is_not_a_change(self):
        """The morning already said they play tonight."""
        assert evening.day_changes(_with_open(MORNING), TODAY) == []

    def test_a_game_that_was_already_final_this_morning_is_not_news_again(self):
        final = dict(MORNING, scores=[_row(_game(5, 2, "post"))])
        assert evening.day_changes(_with_open(final, opened_from=final), TODAY) == []

    def test_compose_returns_none_rather_than_a_message(self):
        with patch("home.ensure_fresh", return_value=URL), \
             patch("home.load", return_value=_with_open(MORNING)), \
             patch("home.home_token", return_value="tok"), \
             patch.object(evening, "get_profile", return_value={"timezone": "America/Chicago"}), \
             patch("timeutil.local_today", return_value=TODAY), \
             patch.object(evening, "generate_evening_line") as draft:
            assert evening._compose_evening(PHONE) == (None, False)
        draft.assert_not_called()


class TestWhatCountsAsAChange:
    def test_the_game_ending(self):
        later = dict(MORNING, scores=[_row(_game(5, 2, "post"))])
        lines = evening.day_changes(_with_open(later), TODAY)
        assert lines == ["St. Louis Cardinals beat Chicago Cubs 5-2"]

    def test_the_game_in_progress(self):
        later = dict(MORNING, scores=[_row(_game(3, 1, "in", detail="Top 6th"))])
        assert evening.day_changes(_with_open(later), TODAY) == \
            ["St. Louis Cardinals up 3-1 vs Chicago Cubs, Top 6th"]

    def test_a_market_move_over_the_bar_in_either_direction(self):
        later = dict(MORNING, prices=[{"label": "NVDA", "price": 183.6},
                                      {"label": "BTC", "price": 58000.0}])
        lines = evening.day_changes(_with_open(later), TODAY)
        assert lines == ["NVDA +2.0% since this morning, now $183.60",
                         "BTC -3.3% since this morning, now $58,000"]

    def test_a_headline_not_on_the_page_this_morning(self):
        later = dict(MORNING, headlines=MORNING["headlines"] + [
            {"title": "new story", "url": "https://a.example/new", "topic": "AI news",
             "source": "apnews.com"}])
        assert evening.day_changes(_with_open(later), TODAY) == \
            ["New on AI news: new story (apnews.com)"]

    def test_new_headlines_are_capped(self):
        heads = [{"title": f"s{i}", "url": f"https://a.example/{i}", "topic": "t"} for i in range(6)]
        lines = evening.day_changes(_with_open(dict(MORNING, headlines=heads)), TODAY)
        assert len(lines) == evening.MAX_NEW_HEADLINES

    def test_the_order_is_fixed_scores_then_markets_then_news(self):
        later = dict(MORNING,
                     scores=[_row(_game(5, 2, "post"))],
                     prices=[{"label": "NVDA", "price": 190.0}],
                     headlines=[{"title": "new", "url": "https://a.example/n", "topic": "t"}])
        lines = evening.day_changes(_with_open(later), TODAY)
        assert lines[0].startswith("St. Louis Cardinals")
        assert lines[1].startswith("NVDA")
        assert lines[2].startswith("New on")

    def test_a_ticker_added_since_the_morning_has_no_baseline_and_is_skipped(self):
        later = dict(MORNING, prices=MORNING["prices"] + [{"label": "AAPL", "price": 230.0}])
        assert evening.day_changes(_with_open(later), TODAY) == []


class TestTheDiffNeedsTodaysMorning:
    """A baseline from yesterday is not a baseline. Markets and news are
    skipped; a game needs no baseline to have a result."""

    def test_yesterdays_open_does_not_count(self):
        later = dict(MORNING, prices=[{"label": "NVDA", "price": 250.0}],
                     headlines=[{"title": "new", "url": "https://a.example/n", "topic": "t"}])
        stale = _with_open(later, day=date(2026, 9, 3))
        assert evening.day_changes(stale, TODAY) == []

    def test_no_open_at_all_does_not_count(self):
        later = dict(MORNING, prices=[{"label": "NVDA", "price": 250.0}])
        assert evening.day_changes(later, TODAY) == []

    def test_but_a_final_still_reports(self):
        later = dict(MORNING, scores=[_row(_game(5, 2, "post"))])
        assert evening.day_changes(later, TODAY) == ["St. Louis Cardinals beat Chicago Cubs 5-2"]


class TestComposeShape:
    def _compose(self, line="Cards took it 5-2. NVDA +2.0% on the day."):
        later = dict(MORNING, scores=[_row(_game(5, 2, "post"))],
                     prices=[{"label": "NVDA", "price": 183.6}, {"label": "BTC", "price": 60000.0}])
        with patch("home.ensure_fresh", return_value=URL), \
             patch("home.load", return_value=_with_open(later)), \
             patch("home.home_token", return_value="tok"), \
             patch.object(evening, "get_profile", return_value={"timezone": "America/Chicago"}), \
             patch("timeutil.local_today", return_value=TODAY), \
             patch.object(evening, "generate_evening_line", return_value=line) as draft:
            msg, carries = evening._compose_evening(PHONE)
        return msg, carries, draft

    def test_url_is_last_with_nothing_after_it(self):
        msg, carries, _ = self._compose()
        assert carries is True
        assert msg.endswith(URL)
        assert msg.count("http") == 1

    def test_the_drafter_gets_the_change_lines_not_the_page(self):
        _, _, draft = self._compose()
        changes = draft.call_args.args[1]
        assert changes[0] == "St. Louis Cardinals beat Chicago Cubs 5-2"
        assert changes[1].startswith("NVDA +2.0%")

    def test_no_app_url_is_silence_not_a_text_briefing(self):
        with patch("home.ensure_fresh", return_value="/h/tok"), \
             patch.object(evening, "get_profile", return_value={}):
            assert evening._compose_evening(PHONE) == (None, False)


class _Block:
    def __init__(self, t): self.text = t


class _Resp:
    def __init__(self, t): self.content = [_Block(t)]


def _draft_returning(*texts, changes=("St. Louis Cardinals beat Chicago Cubs 5-2",
                                      "NVDA +2.0% since this morning, now $183.60")):
    calls = []

    def _create(**kw):
        calls.append(kw)
        return _Resp(texts[min(len(calls) - 1, len(texts) - 1)])

    with patch("agent._build_system", return_value="sys"), \
         patch.object(evening.client.messages, "create", side_effect=_create):
        return evening.generate_evening_line(PHONE, list(changes)), calls


class TestTheDraft:
    def test_goes_through_build_system_on_sonnet(self):
        """One voice: every user-facing message carries the calibrated prompt."""
        _, calls = _draft_returning("Cards won 5-2. NVDA up 2%.")
        assert calls[0]["system"] == "sys"
        assert calls[0]["model"] == evening.SONNET_MODEL

    def test_the_prompt_forbids_anything_off_the_list(self):
        _, calls = _draft_returning("Cards won 5-2.")
        body = calls[0]["messages"][0]["content"].lower()
        assert "no weather" in body and "no check-in" in body and "no question" in body
        assert "nothing that is not on the list" in body

    def test_naming_the_link_is_redrafted_once(self):
        out, calls = _draft_returning("Cards won, details on your page.", "Cards won 5-2.")
        assert out == "Cards won 5-2."
        assert len(calls) == 2

    def test_a_model_failure_ships_the_plain_lines(self):
        with patch("agent._build_system", return_value="sys"), \
             patch.object(evening.client.messages, "create", side_effect=RuntimeError("down")):
            out = evening.generate_evening_line(PHONE, ["St. Louis Cardinals beat Chicago Cubs 5-2"])
        assert out == "St. Louis Cardinals beat Chicago Cubs 5-2."

    def test_long_output_is_trimmed_on_a_word_boundary(self):
        out, _ = _draft_returning("word " * 200)
        assert len(out) <= evening.EVENING_LINE_MAX and not out.endswith("wor")

    def test_meta_commentary_raises_to_the_fallback(self):
        out, _ = _draft_returning("Not sending the game since they asked me to skip sports.")
        assert out == ("St. Louis Cardinals beat Chicago Cubs 5-2. "
                       "NVDA +2.0% since this morning, now $183.60.")


class TestSendWindow:
    def _now(self, h, m=0):
        return datetime(2026, 9, 4, h, m, tzinfo=ZoneInfo("America/Chicago"))

    def test_default_is_six_pm(self):
        assert not evening._in_send_window(self._now(17, 55), None)
        assert evening._in_send_window(self._now(18, 0), None)
        assert evening._in_send_window(self._now(19, 30), None)
        assert not evening._in_send_window(self._now(20, 1), None)

    def test_a_chosen_time_is_honoured(self):
        assert evening._in_send_window(self._now(20, 5), "20:00")
        assert not evening._in_send_window(self._now(18, 5), "20:00")

    def test_an_unreadable_time_falls_back_to_six_not_seven_am(self):
        assert evening._in_send_window(self._now(18, 5), "later-ish")
        assert not evening._in_send_window(self._now(7, 5), "later-ish")


class TestTheGate:
    def test_pausing_mornings_pauses_the_evening_too(self):
        assert not evening._wants_evening({"morning_onboarded": True, "morning_enabled": False})

    def test_the_evening_has_its_own_switch(self):
        assert evening._wants_evening({"morning_onboarded": True})
        assert not evening._wants_evening({"morning_onboarded": True, "evening_enabled": False})

    def test_not_onboarded_gets_nothing(self):
        assert not evening._wants_evening({})


class TestTheJob:
    def _run(self, tmp_path, monkeypatch, *, message, sent=True, profile=None):
        monkeypatch.setattr(db, "_DB_PATH", tmp_path / "job.db")
        db.init_db()
        db.upsert_profile(PHONE, profile or {"morning_onboarded": True,
                                             "timezone": "America/Chicago"})
        rec = {"sms": []}
        with patch.object(evening, "get_all_profiles",
                          return_value=[(PHONE, db.get_profile(PHONE))]), \
             patch("timeutil.local_now", return_value=self._six_pm()), \
             patch.object(evening, "_compose_evening",
                          return_value=(message, bool(message))), \
             patch("sms_util.send_sms", side_effect=lambda p, t, **k: rec["sms"].append(t) or sent):
            evening.send_evening_messages()
        return rec["sms"], db.get_profile(PHONE), [m for m in db.get_history(PHONE)
                                                   if m["role"] == "assistant"]

    def _six_pm(self):
        return datetime(2026, 9, 4, 18, 3, tzinfo=ZoneInfo("America/Chicago"))

    def test_a_change_is_sent_recorded_and_claims_the_day(self, tmp_path, monkeypatch):
        sms, profile, history = self._run(tmp_path, monkeypatch, message=f"Cards won. {URL}")
        assert sms == [f"Cards won. {URL}"]
        assert profile["evening_sent_date"] == "2026-09-04"
        assert len(history) == 1

    def test_a_quiet_day_sends_nothing_and_still_claims_the_day(self, tmp_path, monkeypatch):
        """Recomputing the same empty diff every five minutes until midnight
        would be the same answer at a cost."""
        sms, profile, history = self._run(tmp_path, monkeypatch, message=None)
        assert sms == [] and history == []
        assert profile["evening_sent_date"] == "2026-09-04"

    def test_a_failed_send_releases_the_claim(self, tmp_path, monkeypatch):
        sms, profile, history = self._run(tmp_path, monkeypatch, message="x", sent=False)
        assert sms == ["x"] and history == []
        assert profile.get("evening_sent_date") is None

    def test_a_link_message_opts_out_of_the_status_callback(self, tmp_path, monkeypatch):
        """The /sms-status shorten-and-retry would cut the URL in half."""
        monkeypatch.setattr(db, "_DB_PATH", tmp_path / "cb.db")
        db.init_db()
        db.upsert_profile(PHONE, {"morning_onboarded": True, "timezone": "America/Chicago"})
        seen = {}
        with patch.object(evening, "get_all_profiles",
                          return_value=[(PHONE, db.get_profile(PHONE))]), \
             patch("timeutil.local_now", return_value=self._six_pm()), \
             patch.object(evening, "_compose_evening", return_value=(f"x {URL}", True)), \
             patch("sms_util.send_sms", side_effect=lambda p, t, **k: seen.update(k) or True):
            evening.send_evening_messages()
        assert seen.get("add_status_callback") is False

    def test_it_runs_on_the_five_minute_interval_like_the_morning(self):
        with patch("apscheduler.schedulers.background.BackgroundScheduler.start"):
            import main
        from apscheduler.triggers.interval import IntervalTrigger
        jobs = [j for j in main._scheduler.get_jobs() if j.func is evening.send_evening_messages]
        assert len(jobs) == 1 and isinstance(jobs[0].trigger, IntervalTrigger)


class TestTheMorningRecordsTheOpen:
    def test_a_delivered_morning_calls_record_day_open(self):
        import inspect
        import morning
        src = inspect.getsource(morning.send_morning_messages)
        assert "record_day_open" in src
        # Inside the delivered branch, not before the send.
        assert src.index('kind="morning"') < src.index("record_day_open(")


class TestTheToolsKnowAboutIt:
    def test_the_briefing_tool_carries_the_evening_switch(self):
        from tools_def import TOOLS
        props = next(t for t in TOOLS if t["name"] == "update_morning_briefing")["input_schema"]["properties"]
        assert "evening_enabled" in props

    def test_the_time_tool_can_move_the_evening(self):
        from tools_def import TOOLS
        props = next(t for t in TOOLS if t["name"] == "set_morning_time")["input_schema"]["properties"]
        assert props["which"]["enum"] == ["morning", "evening"]

    def test_the_dispatch_writes_evening_time(self):
        import inspect
        import agent
        block = inspect.getsource(agent.get_reply).split('"set_morning_time"')[1].split("elif b.name")[0]
        assert 'f"{which}_time"' in block

    def test_the_prompt_explains_both_updates(self):
        import prompts
        body = prompts.SYSTEM_PROMPT
        assert "SCHEDULED UPDATES" in body
        assert "EVENING update" in body and "nothing is sent" in body
        assert "evening_enabled=false" in body

    def test_the_fields_are_allowed(self):
        import userprofile
        for f in ("evening_time", "evening_enabled", "evening_sent_date"):
            assert f in userprofile.PROFILE_FIELDS
        assert "evening" not in prompts_extract()


def prompts_extract() -> str:
    import prompts
    return prompts.EXTRACT_PROMPT
