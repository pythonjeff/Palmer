"""Tests for the story-arc gate in watches.py.

The semantic dedup that answers 'the user already knows this — is the new
candidate ADVANCING the story, or just a rehash?' — separate from the
title-based recent_summaries gate.
"""
from unittest.mock import patch, MagicMock

import watches as watches_mod
import watches


def _haiku_reply(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


def _capture_prompt(reply_text: str):
    captured: list[str] = []

    def _create(**kwargs):
        captured.append(kwargs["messages"][0]["content"])
        return _haiku_reply(reply_text)

    return _create, captured


class TestCheckWatchHitStoryBlock:
    def test_no_story_state_omits_story_block(self):
        create, captured = _capture_prompt("YES")
        with patch("watches.client") as mock_client:
            mock_client.messages.create.side_effect = create
            watches_mod._check_watch_hit(
                results="something happened",
                description="Cardinals",
                recent_summaries=[],
                engaged=False,
                genre="sports_team",
                story_state=None,
            )
        prompt = captured[0]
        assert "Current story state" not in prompt

    def test_story_state_included_in_prompt(self):
        create, captured = _capture_prompt("NO")
        with patch("watches.client") as mock_client:
            mock_client.messages.create.side_effect = create
            watches_mod._check_watch_hit(
                results="Cardinals win again to extend streak to 7",
                description="Cardinals",
                recent_summaries=[],
                engaged=False,
                genre="sports_team",
                story_state="Cardinals are on a six-game winning streak, moved into first place.",
            )
        prompt = captured[0]
        assert "Current story state" in prompt
        assert "six-game winning streak" in prompt
        # Prompt should also frame the ADVANCE-vs-rehash question
        assert "advance" in prompt.lower()

    def test_yes_reply_still_fires(self):
        with patch("watches.client") as mock_client:
            mock_client.messages.create.return_value = _haiku_reply("YES")
            assert watches_mod._check_watch_hit(
                results="candidate", description="d", recent_summaries=[],
                engaged=False, genre="sports_team",
                story_state="prior state",
            ) is True


class TestUpdateStoryState:
    def test_persists_haiku_summary(self):
        with patch("watches.client") as mock_client, \
             patch("watches.update_watch_story") as mock_update:
            mock_client.messages.create.return_value = _haiku_reply(
                "Cardinals extended their winning streak to seven with a 4-2 win over the Cubs."
            )
            watches_mod._update_story_state(
                watch_id=42,
                previous_state="Cardinals on a six-game streak, in first place.",
                new_alert_title="Cardinals beat Cubs 4-2 for seventh straight win",
                new_alert_content="The Cardinals defeated the Cubs 4-2 Tuesday...",
            )
        mock_update.assert_called_once()
        args = mock_update.call_args.args
        assert args[0] == 42
        assert "seven" in args[1].lower() or "streak" in args[1].lower()

    def test_no_previous_state_still_seeds(self):
        with patch("watches.client") as mock_client, \
             patch("watches.update_watch_story") as mock_update:
            mock_client.messages.create.return_value = _haiku_reply(
                "Cardinals moved into first place with a win over the Cubs."
            )
            watches_mod._update_story_state(
                watch_id=1, previous_state=None,
                new_alert_title="Cardinals in first place",
                new_alert_content="",
            )
        mock_update.assert_called_once()
        # First-alert seeding: prompt contains 'first alert' marker so Haiku knows
        prompt = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "first alert" in prompt.lower()

    def test_haiku_failure_swallowed(self):
        with patch("watches.client") as mock_client, \
             patch("watches.update_watch_story") as mock_update:
            mock_client.messages.create.side_effect = RuntimeError("boom")
            # Must not raise — the alert already went out; losing the state
            # update only costs us dedup benefit on the next tick.
            watches_mod._update_story_state(
                watch_id=1, previous_state=None,
                new_alert_title="t", new_alert_content="c",
            )
        mock_update.assert_not_called()

    def test_empty_haiku_reply_no_persist(self):
        with patch("watches.client") as mock_client, \
             patch("watches.update_watch_story") as mock_update:
            mock_client.messages.create.return_value = _haiku_reply("   ")
            watches_mod._update_story_state(
                watch_id=1, previous_state=None,
                new_alert_title="t", new_alert_content="c",
            )
        mock_update.assert_not_called()

    def test_summary_truncated_to_400_chars(self):
        long_text = "x" * 900
        with patch("watches.client") as mock_client, \
             patch("watches.update_watch_story") as mock_update:
            mock_client.messages.create.return_value = _haiku_reply(long_text)
            watches_mod._update_story_state(
                watch_id=1, previous_state=None,
                new_alert_title="t", new_alert_content="c",
            )
        args = mock_update.call_args.args
        assert len(args[1]) == 400


class TestTheWatchAlertHasAVoice:
    """This was the one user-facing message in the system with no Palmer in
    it: a bare `title\\nurl`, no system prompt, no calibration — against the
    rule that anything the user reads is drafted through _build_system. Its
    sibling alerts.py has always done this; the two paths simply diverged."""

    TOP = {"title": "Eagles sign Hurts to an extension",
           "url": "https://example.com/a",
           "content": "The deal runs five years."}
    WATCH = {"id": 1, "description": "Eagles roster news"}

    def _draft(self, text):
        resp = MagicMock()
        resp.content = [MagicMock(text=text)]
        return resp

    def _run(self, text, **kw):
        with patch.object(watches, "_build_system", return_value="sys") as bs, \
             patch.object(watches.client.messages, "create",
                          return_value=self._draft(text), **kw) as create:
            out = watches._draft_alert("+15550001111", self.WATCH, self.TOP,
                                       fallback="FALLBACK")
        return out, bs, create

    def test_it_is_drafted_through_build_system_on_sonnet(self):
        _, bs, create = self._run("Hurts got his extension. Five years.")
        bs.assert_called_once()
        assert create.call_args.kwargs["system"] == "sys"
        assert create.call_args.kwargs["model"] == watches.SONNET_MODEL

    def test_the_url_is_last_and_alone(self):
        """Message apps only draw the preview when one URL sits at a boundary."""
        out, _, _ = self._run("Hurts got his extension. Five years.")
        assert out.endswith(self.TOP["url"])
        assert len(watches.URL_RE.findall(out)) == 1

    def test_a_model_invented_url_is_stripped(self):
        """Two links draw no preview, and the invented one may not resolve."""
        out, _, _ = self._run("Big deal, read it at https://spam.example.com/x")
        assert "spam.example.com" not in out
        assert out.endswith(self.TOP["url"])

    def test_the_prompt_says_it_can_see_only_the_headline(self):
        _, _, create = self._run("Hurts got his extension.")
        sent = create.call_args.kwargs["messages"][0]["content"]
        assert "NOTHING else" in sent
        assert "do not invent" in sent.lower()

    def test_the_prompt_forbids_writing_a_url(self):
        _, _, create = self._run("Hurts got his extension.")
        assert "Do NOT write a URL" in create.call_args.kwargs["messages"][0]["content"]

    def test_a_failed_draft_still_sends_the_headline(self):
        """The alert goes out either way — the fallback is what production
        sent before this existed."""
        with patch.object(watches, "_build_system", return_value="sys"), \
             patch.object(watches.client.messages, "create",
                          side_effect=RuntimeError("api down")):
            out = watches._draft_alert("+15550001111", self.WATCH, self.TOP,
                                       fallback="FALLBACK")
        assert out == "FALLBACK"

    def test_an_empty_draft_falls_back(self):
        out, _, _ = self._run("   ")
        assert out == "FALLBACK"

    def test_a_failed_system_prompt_falls_back_to_the_base_one(self):
        """Not "no system prompt" — that drops the voice, the calibration AND
        every NEVER rule from a message still going out."""
        with patch.object(watches, "_build_system", side_effect=RuntimeError("no db")), \
             patch.object(watches.client.messages, "create",
                          return_value=self._draft("Hurts got paid.")) as create:
            watches._draft_alert("+15550001111", self.WATCH, self.TOP, fallback="FB")
        system = create.call_args.kwargs["system"]
        assert "Palmer" in system
        assert "{profile_block}" not in system

    def test_the_dedup_gates_still_see_the_facts(self):
        """The subject is the news, not Palmer's phrasing — and drafting
        before the gates would spend a Sonnet call on every candidate they
        throw away, at a 30-minute cadence across every watch."""
        import inspect
        src = inspect.getsource(watches.run_watches)
        assert src.index("_is_duplicate_subject") < src.index("_draft_alert")
        assert src.index("claim_watch_alert") < src.index("_draft_alert")

    def test_what_is_sent_is_what_is_saved(self):
        import inspect
        src = inspect.getsource(watches.run_watches)
        assert 'send_sms(watch["phone"], body)' in src
        assert 'save_message(watch["phone"], "assistant", body, kind="watch")' in src

    def test_the_recent_summaries_stay_factual(self):
        """They are fed back into _check_watch_hit's "already sent" block,
        where a voiced paraphrase would degrade the match."""
        import inspect
        src = inspect.getsource(watches.run_watches)
        assert 'title = (top.get("title") or alert)[:120]' in src

    def test_the_send_does_not_re_decide_the_status_callback(self):
        """send_sms sees the URL and turns it off itself; a caller passing
        True here would put the shorten-and-retry back on a message with a
        link in it."""
        import inspect
        assert "add_status_callback=" not in inspect.getsource(watches.run_watches)
