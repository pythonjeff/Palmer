"""arrange_page dispatch: presentation prefs merge by set arithmetic (a delta,
never a model-restated whole set), unknown section words are surfaced rather
than guessed, and the prices cache is expired only when the SORT changed —
order and visibility are render-time and need no invalidate."""
from unittest.mock import patch

from palmer import agent
from tests.helpers import drive_tool


def _drive_arrange(tool_input, profile=None):
    _, result, (upsert, invalidate) = drive_tool(
        "arrange_page", tool_input, message="arrange my page", profile=profile or {},
        patches=[patch.object(agent, "upsert_profile"), patch("palmer.home.invalidate")])
    saved = upsert.call_args[0][1]["morning_prefs"] if upsert.called else None
    return result, saved, invalidate


class TestMarketsSort:
    def test_movers_is_stored_and_expires_the_prices_cache(self):
        result, saved, invalidate = _drive_arrange({"markets_sort": "movers"})
        assert saved["markets_sort"] == "movers"
        invalidate.assert_called_once_with("+1555", ("prices",))

    def test_added_clears_the_key_rather_than_storing_a_default(self):
        """Absent means topic order already; a stored default is prompt noise."""
        _, saved, invalidate = _drive_arrange({"markets_sort": "added"},
                                      {"morning_prefs": {"markets_sort": "movers"}})
        assert "markets_sort" not in saved
        invalidate.assert_called_once_with("+1555", ("prices",))

    def test_restating_the_current_sort_does_not_expire_the_cache(self):
        _, saved, invalidate = _drive_arrange({"markets_sort": "movers"},
                                      {"morning_prefs": {"markets_sort": "movers"}})
        invalidate.assert_not_called()


class TestOrderAndVisibility:
    def test_order_words_are_canonicalized(self):
        _, saved, invalidate = _drive_arrange({"section_order": ["stocks", "headlines"]})
        assert saved["section_order"] == ["markets", "news"]

    def test_order_changes_do_not_expire_any_cache(self):
        """Render-time — carried onto the payload on every view."""
        _, _, invalidate = _drive_arrange({"section_order": ["markets"]})
        invalidate.assert_not_called()

    def test_hide_then_show_round_trips(self):
        _, saved, _ = _drive_arrange({"hide": ["traffic"]})
        assert saved["hidden_sections"] == ["commute"]
        _, saved, _ = _drive_arrange({"show": ["commute"]},
                             {"morning_prefs": {"hidden_sections": ["commute"]}})
        assert saved["hidden_sections"] == []

    def test_hide_is_a_delta_not_a_restatement(self):
        """An existing hidden section survives a hide it wasn't named in."""
        _, saved, _ = _drive_arrange({"hide": ["news"]},
                             {"morning_prefs": {"hidden_sections": ["commute"]}})
        assert set(saved["hidden_sections"]) == {"commute", "news"}

    def test_other_prefs_survive_the_merge(self):
        _, saved, _ = _drive_arrange({"hide": ["news"]},
                             {"morning_prefs": {"episode_alerts": True,
                                                "opening_kinds": ["local"]}})
        assert saved["episode_alerts"] is True
        assert saved["opening_kinds"] == ["local"]


class TestUnknownWords:
    def test_an_unknown_word_is_surfaced_not_guessed(self):
        result, saved, _ = _drive_arrange({"hide": ["horoscope"]})
        assert "horoscope" in result
        assert "ask" in result.lower()
        assert saved is None, "nothing recognizable, nothing written"

    def test_kind_words_do_not_hide_the_opening_section(self):
        """'movies' and 'concerts' are Opening KINDS (opening_remove's job);
        mapping them here would let 'hide movies' silently hide the whole
        section instead of trimming a kind."""
        result, saved, _ = _drive_arrange({"hide": ["movies"]})
        assert saved is None
        assert "movies" in result
