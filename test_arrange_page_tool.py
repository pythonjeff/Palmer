"""arrange_page dispatch: presentation prefs merge by set arithmetic (a delta,
never a model-restated whole set), unknown section words are surfaced rather
than guessed, and the prices cache is expired only when the SORT changed —
order and visibility are render-time and need no invalidate."""
from unittest.mock import patch

import agent


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, content, stop_reason):
        self.content, self.stop_reason = content, stop_reason


def _drive(tool_input, profile=None):
    calls = []
    responses = [
        _Resp([_Block(type="tool_use", name="arrange_page", id="t1", input=tool_input)], "tool_use"),
        _Resp([_Block(type="text", text="done")], "end_turn"),
    ]

    def _create(**kw):
        calls.append(kw)
        return responses[len(calls) - 1]

    with patch.object(agent, "_build_system", return_value="sys"), \
         patch.object(agent, "get_history", return_value=[]), \
         patch.object(agent, "get_profile", return_value=profile or {}), \
         patch.object(agent, "upsert_profile") as upsert, \
         patch("home.invalidate") as invalidate, \
         patch.object(agent.client.messages, "create", side_effect=_create):
        agent.get_reply("+1555", "arrange my page")
    result = calls[1]["messages"][-1]["content"][0]["content"]
    saved = upsert.call_args[0][1]["morning_prefs"] if upsert.called else None
    return result, saved, invalidate


class TestMarketsSort:
    def test_movers_is_stored_and_expires_the_prices_cache(self):
        result, saved, invalidate = _drive({"markets_sort": "movers"})
        assert saved["markets_sort"] == "movers"
        invalidate.assert_called_once_with("+1555", ("prices",))

    def test_added_clears_the_key_rather_than_storing_a_default(self):
        """Absent means topic order already; a stored default is prompt noise."""
        _, saved, invalidate = _drive({"markets_sort": "added"},
                                      {"morning_prefs": {"markets_sort": "movers"}})
        assert "markets_sort" not in saved
        invalidate.assert_called_once_with("+1555", ("prices",))

    def test_restating_the_current_sort_does_not_expire_the_cache(self):
        _, saved, invalidate = _drive({"markets_sort": "movers"},
                                      {"morning_prefs": {"markets_sort": "movers"}})
        invalidate.assert_not_called()


class TestOrderAndVisibility:
    def test_order_words_are_canonicalized(self):
        _, saved, invalidate = _drive({"section_order": ["stocks", "headlines"]})
        assert saved["section_order"] == ["markets", "news"]

    def test_order_changes_do_not_expire_any_cache(self):
        """Render-time — carried onto the payload on every view."""
        _, _, invalidate = _drive({"section_order": ["markets"]})
        invalidate.assert_not_called()

    def test_hide_then_show_round_trips(self):
        _, saved, _ = _drive({"hide": ["traffic"]})
        assert saved["hidden_sections"] == ["commute"]
        _, saved, _ = _drive({"show": ["commute"]},
                             {"morning_prefs": {"hidden_sections": ["commute"]}})
        assert saved["hidden_sections"] == []

    def test_hide_is_a_delta_not_a_restatement(self):
        """An existing hidden section survives a hide it wasn't named in."""
        _, saved, _ = _drive({"hide": ["news"]},
                             {"morning_prefs": {"hidden_sections": ["commute"]}})
        assert set(saved["hidden_sections"]) == {"commute", "news"}

    def test_other_prefs_survive_the_merge(self):
        _, saved, _ = _drive({"hide": ["news"]},
                             {"morning_prefs": {"episode_alerts": True,
                                                "opening_kinds": ["local"]}})
        assert saved["episode_alerts"] is True
        assert saved["opening_kinds"] == ["local"]


class TestUnknownWords:
    def test_an_unknown_word_is_surfaced_not_guessed(self):
        result, saved, _ = _drive({"hide": ["horoscope"]})
        assert "horoscope" in result
        assert "ask" in result.lower()
        assert saved is None, "nothing recognizable, nothing written"

    def test_kind_words_do_not_hide_the_opening_section(self):
        """'movies' and 'concerts' are Opening KINDS (opening_remove's job);
        mapping them here would let 'hide movies' silently hide the whole
        section instead of trimming a kind."""
        result, saved, _ = _drive({"hide": ["movies"]})
        assert saved is None
        assert "movies" in result
