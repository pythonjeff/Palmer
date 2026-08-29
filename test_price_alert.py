"""Tests for the shared price-alert drafter.

shopping.py and amazon.py used to carry near-identical copies of this. These
pin the behavior that has to stay identical across both sources, and the two
places they legitimately differ (the price line, and whether a URL is appended).
"""
from unittest.mock import patch, MagicMock

import price_alert


def _resp(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    r = MagicMock()
    r.content = [block]
    return r


CURRENT = {"price": 42.0, "merchant": "Zappos", "url": "https://www.amazon.com/dp/B0PROT"}
WATCH = {"phone": "+15550001111", "target_price": None, "baseline_price": 60.0}


class TestContextFacts:
    def test_drop_states_dollars_and_percentage(self):
        # Dollars lead — the $2 materiality rule is denominated in dollars.
        ctx = price_alert._context("Protein", CURRENT, WATCH, "drop", None)
        assert "Down $18.00 (about 30%)" in ctx and "$60.00" in ctx

    def test_rise_says_up_not_down(self):
        # A rise must never be described as a drop; $60 -> $63.50 is +$3.50.
        ctx = price_alert._context("Protein", {"price": 63.50, "merchant": "Zappos"},
                                   WATCH, "rise", None)
        assert "Up $3.50 (about 6%)" in ctx
        assert "Down" not in ctx

    def test_target_hit(self):
        watch = dict(WATCH, target_price=45.0)
        ctx = price_alert._context("Protein", CURRENT, watch, "target", None)
        assert "at or under $45.00" in ctx

    def test_shopping_price_line_names_the_merchant(self):
        ctx = price_alert._context("Protein", CURRENT, WATCH, "drop", None)
        assert "Zappos" in ctx

    def test_amazon_price_line_names_the_source(self):
        ctx = price_alert._context("Protein", CURRENT, WATCH, "drop", "Amazon")
        assert "Amazon price: $42.00" in ctx

    def test_missing_merchant_does_not_blow_up(self):
        ctx = price_alert._context("Protein", {"price": 42.0}, WATCH, "drop", None)
        assert "unknown seller" in ctx

    def test_no_baseline_no_percentage(self):
        watch = dict(WATCH, baseline_price=None)
        ctx = price_alert._context("Protein", CURRENT, watch, "drop", None)
        assert "Down" not in ctx

    def test_rise_prompt_does_not_claim_the_watch_hit(self):
        # "your price watch just hit" on a price INCREASE reads as good news.
        from unittest.mock import patch, MagicMock
        seen = {}
        def _capture(**kw):
            seen.update(kw)
            m = MagicMock(); m.content = [MagicMock(text="shake went up to 63.50")]
            return m
        with patch("price_alert.client.messages.create", side_effect=_capture), \
             patch("price_alert._build_system", return_value="sys"):
            price_alert.draft_price_alert("Protein", {"price": 63.50, "merchant": "Z"},
                                          WATCH, "rise")
        prompt = seen["messages"][0]["content"]
        assert "just hit" not in prompt
        assert "went UP" in prompt


class TestDrafting:
    def _draft(self, **kw):
        with patch.object(price_alert, "_build_system", return_value="SYS"), \
             patch.object(price_alert.client.messages, "create",
                          return_value=_resp("protein's down to $42")) as create:
            body = price_alert.draft_price_alert("Protein", CURRENT, WATCH, "drop", **kw)
        return body, create

    def test_no_link_by_default(self):
        body, _ = self._draft()
        assert body == "protein's down to $42"

    def test_link_appended_when_given(self):
        body, _ = self._draft(link=CURRENT["url"])
        assert body.endswith(CURRENT["url"])

    def test_uses_sonnet_not_haiku(self):
        """User-facing drafting belongs on Sonnet per CLAUDE.md routing."""
        _, create = self._draft()
        assert create.call_args.kwargs["model"] == price_alert.SONNET_MODEL

    def test_uses_the_real_system_prompt(self):
        """The whole point: price alerts sound like the Palmer they talk to."""
        _, create = self._draft()
        assert create.call_args.kwargs["system"] == "SYS"

    def test_prompt_suppresses_url_when_one_is_appended(self):
        _, create = self._draft(link=CURRENT["url"])
        assert "Do NOT include a URL" in create.call_args.kwargs["messages"][0]["content"]

    def test_falls_back_to_the_base_prompt_without_a_phone(self):
        """A malformed row shouldn't cost the user their alert."""
        with patch.object(price_alert, "_build_system") as bs, \
             patch.object(price_alert.client.messages, "create", return_value=_resp("x")) as create:
            price_alert.draft_price_alert("Protein", CURRENT, {"baseline_price": 60.0}, "drop")
        bs.assert_not_called()
        # Used to assert NO system prompt at all, which meant a failed profile
        # read dropped the voice, the calibration and every NEVER rule — the
        # anti-redirect one included — from a message that still went out.
        sys = create.call_args.kwargs.get("system") or ""
        assert "Palmer" in sys, "must still sound like Palmer"
        assert "{profile_block}" not in sys, "the template must be formatted, not raw"


class TestFailsSafe:
    def test_model_failure_falls_back_to_facts(self):
        with patch.object(price_alert, "_build_system", return_value="SYS"), \
             patch.object(price_alert.client.messages, "create", side_effect=RuntimeError("boom")):
            body = price_alert.draft_price_alert("Protein", CURRENT, WATCH, "drop")
        assert "42" in body and "Protein" in body

    def test_fallback_still_appends_the_link(self):
        with patch.object(price_alert, "_build_system", return_value="SYS"), \
             patch.object(price_alert.client.messages, "create", side_effect=RuntimeError("boom")):
            body = price_alert.draft_price_alert("Protein", CURRENT, WATCH, "drop",
                                                 link=CURRENT["url"], source_label="Amazon")
        assert body.endswith(CURRENT["url"])
        assert "Amazon" in body

    def test_empty_model_output_falls_back(self):
        with patch.object(price_alert, "_build_system", return_value="SYS"), \
             patch.object(price_alert.client.messages, "create", return_value=_resp("   ")):
            body = price_alert.draft_price_alert("Protein", CURRENT, WATCH, "drop")
        assert body.strip(), "must never send an empty alert"

    def test_build_system_failure_does_not_lose_the_alert(self):
        with patch.object(price_alert, "_build_system", side_effect=RuntimeError("db down")), \
             patch.object(price_alert.client.messages, "create", return_value=_resp("x")):
            body = price_alert.draft_price_alert("Protein", CURRENT, WATCH, "drop")
        assert body.strip()


class TestBothSourcesDelegate:
    def test_shopping_passes_no_link(self):
        import shopping
        with patch("price_alert.draft_price_alert", return_value="line") as d:
            shopping._draft_alert("Protein", CURRENT, WATCH, "drop")
        assert d.call_args.kwargs.get("link") is None

    def test_amazon_passes_link_and_label(self):
        import amazon
        with patch("price_alert.draft_price_alert", return_value="line") as d:
            amazon.draft_alert("Protein", CURRENT, WATCH, "drop")
        assert d.call_args.kwargs["link"] == CURRENT["url"]
        assert d.call_args.kwargs["source_label"] == "Amazon"
