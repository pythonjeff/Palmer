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
    "Palmer is watching" with no price anywhere."""

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
