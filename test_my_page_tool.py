"""get_my_page — Palmer hands over the user's link mid-conversation.

The page is only useful if it can be asked for. Before this, the URL went out
once a day with the morning update and there was no way to get it back short of
scrolling the thread.

The tool returns the URL and the model writes the sentence around it, so the
tests that matter are: the tool exists and is routed, dispatch returns a live
URL rather than a stale or invented one, and the no-APP_URL case tells the model
to shut up about the page instead of promising a link it can't send.
"""
from unittest.mock import patch

import pytest

import agent
import prompts
from tools_def import TOOLS

URL = "https://palmer.example.com/h/AbC123xyz"


def _tool(name):
    return next((t for t in TOOLS if t["name"] == name), None)


class TestSchema:
    def test_the_tool_exists(self):
        assert _tool("get_my_page") is not None

    def test_it_takes_no_arguments(self):
        """The caller is the user. There is nothing to pass and nothing to
        get wrong — in particular no phone number the model could invent."""
        schema = _tool("get_my_page")["input_schema"]
        assert schema["properties"] == {} and schema["required"] == []

    def test_the_description_covers_how_people_actually_ask(self):
        d = _tool("get_my_page")["description"].lower()
        for phrase in ("send me my page", "link", "dashboard", "resend"):
            assert phrase in d

    def test_the_description_pins_the_url_to_the_end(self):
        d = _tool("get_my_page")["description"].lower()
        assert "end" in d and "preview" in d

    def test_it_is_routed_in_the_system_prompt(self):
        """Tool routing is strict in this codebase — a tool the prompt does not
        name is a tool the model will not reliably reach for."""
        assert "get_my_page" in prompts.SYSTEM_PROMPT

    def test_the_prompt_forbids_typing_a_url_from_memory(self):
        block = prompts.SYSTEM_PROMPT.split("get_my_page:")[1].split("\n")[0].lower()
        assert "never type" in block or "from memory" in block


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, content, stop_reason):
        self.content, self.stop_reason = content, stop_reason


def _drive(reply="here you go", url=URL):
    """Run get_reply through one get_my_page tool call and capture what the
    model was handed back."""
    calls = []
    responses = [
        _Resp([_Block(type="tool_use", name="get_my_page", id="t1", input={})], "tool_use"),
        _Resp([_Block(type="text", text=reply)], "end_turn"),
    ]

    def _create(**kw):
        calls.append(kw)
        return responses[len(calls) - 1]

    with patch.object(agent, "_build_system", return_value="sys"), \
         patch.object(agent, "get_history", return_value=[]), \
         patch.object(agent, "get_profile", return_value={"timezone": "America/Chicago"}), \
         patch("home.ensure_fresh", return_value=url) as ensure, \
         patch.object(agent.client.messages, "create", side_effect=_create):
        text, _gif = agent.get_reply("+1555", "send me my page")
    # the tool_result the model saw, on the second request
    result = calls[1]["messages"][-1]["content"][0]["content"]
    return text, result, ensure


class TestDispatch:
    def test_it_returns_the_live_url(self):
        _, result, _ = _drive()
        assert URL in result

    def test_it_refreshes_the_page_for_this_caller(self):
        """ensure_fresh, not home_url — a link to a 404 or to yesterday's data
        is worse than no link."""
        _, _, ensure = _drive()
        ensure.assert_called_once_with("+1555")

    def test_it_tells_the_model_where_to_put_the_url(self):
        _, result, _ = _drive()
        assert "end of your reply" in result.lower()

    def test_the_reply_reaches_the_user(self):
        text, _, _ = _drive(reply=f"all yours {URL}")
        assert text.endswith(URL)

    def test_no_app_url_does_not_promise_a_link(self):
        _, result, _ = _drive(url="/h/tok")
        assert URL not in result
        assert "not mention a page" in result.lower()

    def test_no_app_url_still_answers_instead_of_erroring(self):
        text, _, _ = _drive(reply="not much going on", url="/h/tok")
        assert text == "not much going on"


class TestPriceTopicNormalization:
    """"add Nvidia to my site" has to end up as something the Markets section
    can resolve. The drafting model often writes the ticker itself and
    sometimes doesn't, and the failure was silent — the topic appeared under
    "Watching" with no price anywhere."""

    def test_a_topic_the_map_already_covers_is_untouched(self):
        """No model call: it already resolves."""
        with patch("llm.client") as client:
            assert agent._normalize_price_topic("Nvidia stock") == "Nvidia stock"
        client.messages.create.assert_not_called()

    def test_an_unmapped_company_gains_its_ticker(self):
        with patch("tickers.resolve_company_ticker", return_value="LULU"):
            assert agent._normalize_price_topic("Lululemon shares") == "Lululemon shares (LULU)"

    def test_an_unverifiable_company_gains_nothing(self):
        """No ticker is appended when nothing tradeable can be confirmed."""
        with patch("tickers.resolve_company_ticker", return_value=None):
            assert agent._normalize_price_topic("Stripe stock") == "Stripe stock"

    def test_a_news_topic_never_pays_for_a_lookup(self):
        with patch("llm.client") as client:
            for t in ("AI news", "St. Louis Cardinals", "Kirkwood weather"):
                assert agent._normalize_price_topic(t) == t
        client.messages.create.assert_not_called()

    def test_an_unresolvable_topic_is_left_alone(self):
        with patch("tickers.resolve_company_ticker", return_value=None):
            assert agent._normalize_price_topic("some obscure stock") == "some obscure stock"

    def test_empty_input_is_safe(self):
        assert agent._normalize_price_topic("") == ""

    def test_it_runs_on_the_add_path(self):
        """Normalization has to happen where topics are SAVED, since the read
        path runs on every page view and must stay free."""
        import inspect
        src = inspect.getsource(agent.get_reply)
        block = src.split('update_morning_briefing"')[1].split("elif b.name")[0]
        assert "_normalize_price_topic" in block


class TestAddingToTheSiteRefreshesIt:
    """The morning list and the page are one list, so a topic change has to be
    visible on the page immediately — not after the 5-minute price cooldown."""

    def test_the_add_path_expires_the_price_cache(self):
        import inspect
        src = inspect.getsource(agent.get_reply)
        block = src.split('update_morning_briefing"')[1].split("elif b.name")[0]
        assert "invalidate" in block, "a topic change must expire the cached prices"

    def test_a_failure_to_invalidate_does_not_break_the_reply(self):
        import inspect
        src = inspect.getsource(agent.get_reply)
        block = src.split('update_morning_briefing"')[1].split("elif b.name")[0]
        assert "except" in block, "the reply matters more than the cache stamp"

    def test_the_tool_description_covers_site_vocabulary(self):
        from tools_def import TOOLS
        d = next(t for t in TOOLS if t["name"] == "update_morning_briefing")["description"].lower()
        for word in ("markets", "site", "page", "morning"):
            assert word in d, f"users say {word!r} and mean this tool"

    def test_the_prompt_separates_asking_a_price_from_tracking_one(self):
        import prompts
        block = prompts.SYSTEM_PROMPT.lower()
        assert "one-off" in block and "update_morning_briefing" in block


class TestPalmerCanSeeTheReminders:
    """Watches and price watches were both listed in the system prompt.
    Reminders — the one thing the user explicitly asked to happen at a named
    time — were the table the model could not read at all. So "what have I got
    on today" had nothing to answer from, and "cancel my 4pm one" was a guess
    against twenty messages of history, against a tool that deletes."""

    PHONE = "+15550009999"

    def _system(self, pending):
        from unittest.mock import patch
        import agent
        with patch.object(agent, "get_profile",
                          return_value={"timezone": "America/Chicago"}), \
             patch("db.get_pending_reminders", return_value=pending), \
             patch.object(agent, "get_user_watches", return_value=[]), \
             patch.object(agent, "get_user_price_watches", return_value=[]):
            return agent._build_system(self.PHONE)

    def test_pending_reminders_reach_the_prompt(self):
        out = self._system([{"id": 1, "text": "move the car",
                             "due_at": "2026-09-04T21:00:00+00:00", "recurrence": None}])
        assert "move the car" in out

    def test_they_are_shown_on_the_users_clock(self):
        """21:00Z is 4pm in Chicago. Showing UTC is how a confirmation ends up
        naming an hour the user never said."""
        out = self._system([{"id": 1, "text": "move the car",
                             "due_at": "2026-09-04T21:00:00+00:00", "recurrence": None}])
        assert "4:00 PM" in out
        assert "21:00" not in out

    def test_a_repeat_says_so(self):
        out = self._system([{"id": 1, "text": "take the bins out",
                             "due_at": "2026-09-04T21:00:00+00:00", "recurrence": "weekly"}])
        assert "repeats weekly" in out

    def test_the_bulk_cancel_hazard_is_stated(self):
        out = self._system([{"id": 1, "text": "move the car",
                             "due_at": "2026-09-04T21:00:00+00:00", "recurrence": None}])
        assert "no text_match takes" in out
        assert "ask which" in out

    def test_no_reminders_adds_nothing(self):
        assert "Reminders they have set" not in self._system([])

    def test_a_failed_read_does_not_break_the_turn(self):
        from unittest.mock import patch
        import agent
        with patch.object(agent, "get_profile", return_value={}), \
             patch("db.get_pending_reminders", side_effect=RuntimeError("db down")), \
             patch.object(agent, "get_user_watches", return_value=[]), \
             patch.object(agent, "get_user_price_watches", return_value=[]):
            assert agent._build_system(self.PHONE)


class TestACancelSaysWhatItTook:
    """cancel_reminders returned a bare count on a tool whose no-argument form
    deletes every pending reminder, and whose text_match is a substring — so
    "call" takes "call mom" and "call the vet" together."""

    PHONE = "+15550008888"

    def _seed(self):
        import db
        db.cancel_reminders(self.PHONE)
        for text in ("call mom", "call the vet", "move the car"):
            db.save_reminder(self.PHONE, text, "2099-01-01T12:00:00+00:00")

    def test_it_returns_the_texts_that_went(self):
        import db
        self._seed()
        gone = db.cancel_reminders_named(self.PHONE, "call")
        assert sorted(gone) == ["call mom", "call the vet"]

    def test_the_unmatched_one_survives(self):
        import db
        self._seed()
        db.cancel_reminders_named(self.PHONE, "call")
        left = [r["text"] for r in db.get_pending_reminders(self.PHONE)]
        assert left == ["move the car"]

    def test_the_count_form_still_works(self):
        import db
        self._seed()
        assert db.cancel_reminders(self.PHONE) == 3

    def test_the_dispatch_names_them(self):
        import inspect, agent
        block = inspect.getsource(agent.get_reply).split('"cancel_reminders"')[1] \
                                                  .split("elif b.name")[0]
        assert "cancel_reminders_named" in block
        assert "Say which ones went" in block
