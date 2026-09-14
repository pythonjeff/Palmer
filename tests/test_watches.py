"""Watches, rubrics, corroboration, subject dedup and the check-in.

Merged from test_watches_story.py, test_watches_genre.py, test_corroboration.py, test_subject_dedup.py, test_rubrics.py, test_followup.py; each section keeps its
original file's notes, because those carry the incident that led to the test.
"""
from unittest.mock import patch, MagicMock
from palmer import watches as watches_mod, watches, rubrics, userprofile, followup, db
from tests.helpers import llm_reply
from palmer.watches import corroborated
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from palmer.followup import Subject


# ============================================================================
# from test_watches_story.py
# ============================================================================
#
# Tests for the story-arc gate in watches.py.
#
# The semantic dedup that answers 'the user already knows this — is the new
# candidate ADVANCING the story, or just a rehash?' — separate from the
# title-based recent_summaries gate.

def _capture_prompt(reply_text: str):
    captured: list[str] = []

    def _create(**kwargs):
        captured.append(kwargs["messages"][0]["content"])
        return llm_reply(reply_text)

    return _create, captured


class TestCheckWatchHitStoryBlock:
    def test_no_story_state_omits_story_block(self):
        create, captured = _capture_prompt("YES")
        with patch("palmer.watches.client") as mock_client:
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
        with patch("palmer.watches.client") as mock_client:
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
        with patch("palmer.watches.client") as mock_client:
            mock_client.messages.create.return_value = llm_reply("YES")
            assert watches_mod._check_watch_hit(
                results="candidate", description="d", recent_summaries=[],
                engaged=False, genre="sports_team",
                story_state="prior state",
            ) is True


class TestUpdateStoryState:
    def test_persists_haiku_summary(self):
        with patch("palmer.watches.client") as mock_client, \
             patch("palmer.watches.update_watch_story") as mock_update:
            mock_client.messages.create.return_value = llm_reply(
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
        with patch("palmer.watches.client") as mock_client, \
             patch("palmer.watches.update_watch_story") as mock_update:
            mock_client.messages.create.return_value = llm_reply(
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
        with patch("palmer.watches.client") as mock_client, \
             patch("palmer.watches.update_watch_story") as mock_update:
            mock_client.messages.create.side_effect = RuntimeError("boom")
            # Must not raise — the alert already went out; losing the state
            # update only costs us dedup benefit on the next tick.
            watches_mod._update_story_state(
                watch_id=1, previous_state=None,
                new_alert_title="t", new_alert_content="c",
            )
        mock_update.assert_not_called()

    def test_empty_haiku_reply_no_persist(self):
        with patch("palmer.watches.client") as mock_client, \
             patch("palmer.watches.update_watch_story") as mock_update:
            mock_client.messages.create.return_value = llm_reply("   ")
            watches_mod._update_story_state(
                watch_id=1, previous_state=None,
                new_alert_title="t", new_alert_content="c",
            )
        mock_update.assert_not_called()

    def test_summary_truncated_to_400_chars(self):
        long_text = "x" * 900
        with patch("palmer.watches.client") as mock_client, \
             patch("palmer.watches.update_watch_story") as mock_update:
            mock_client.messages.create.return_value = llm_reply(long_text)
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
    sibling, the retired daily-alert job, always did this; the two paths simply diverged."""

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


# ============================================================================
# from test_watches_genre.py
# ============================================================================
#
# Tests for lazy genre classification + rubric-in-prompt inside watches.py.
#
# Haiku is mocked; we're testing that:
#   1. _watch_genre returns the stored genre without classifying when set.
#   2. _watch_genre classifies + persists via set_watch_genre when missing.
#   3. _check_watch_hit splices the correct rubric into the Haiku prompt.

class TestWatchGenre:
    def setup_method(self):
        rubrics._reset_cache_for_tests()

    def test_stored_genre_is_returned_verbatim(self):
        with patch("palmer.watches.classify_genre") as mock_classify, \
             patch("palmer.watches.set_watch_genre") as mock_set:
            watch = {"id": 1, "description": "Cardinals", "genre": "sports_team"}
            assert watches_mod._watch_genre(watch) == "sports_team"
            mock_classify.assert_not_called()
            mock_set.assert_not_called()

    def test_missing_genre_triggers_classify_and_persist(self):
        with patch("palmer.watches.classify_genre", return_value="sports_team") as mock_classify, \
             patch("palmer.watches.set_watch_genre") as mock_set:
            watch = {"id": 42, "description": "Cardinals winning streak", "genre": None}
            assert watches_mod._watch_genre(watch) == "sports_team"
            mock_classify.assert_called_once_with("Cardinals winning streak")
            mock_set.assert_called_once_with(42, "sports_team")
            # The dict gets updated in place so a second call skips both
            assert watch["genre"] == "sports_team"

    def test_persist_failure_does_not_raise(self):
        with patch("palmer.watches.classify_genre", return_value="market_instrument"), \
             patch("palmer.watches.set_watch_genre", side_effect=RuntimeError("db down")):
            watch = {"id": 7, "description": "Bitcoin", "genre": None}
            # Should still return the classified genre and not blow up
            assert watches_mod._watch_genre(watch) == "market_instrument"
            assert watch["genre"] == "market_instrument"


class TestCheckWatchHitPrompt:
    def _capture_prompt(self):
        """Return (client_patch_ctx, captured) so callers can .append to captured."""
        captured: list[str] = []

        def _create(**kwargs):
            captured.append(kwargs["messages"][0]["content"])
            return llm_reply("YES")

        return _create, captured

    def test_prompt_contains_genre_rubric_text(self):
        create, captured = self._capture_prompt()
        with patch("palmer.watches.client") as mock_client:
            mock_client.messages.create.side_effect = create
            hit = watches_mod._check_watch_hit(
                results="Cardinals moved into first place today.",
                description="St. Louis Cardinals",
                recent_summaries=[],
                engaged=False,
                genre="sports_team",
            )
        assert hit is True
        prompt = captured[0]
        assert "Genre: sports_team" in prompt
        # A distinctive line from the sports rubric — proves the rubric text was spliced in
        assert "streaks starting to matter" in prompt.lower()
        # And NOT rubric text from an unrelated genre
        assert "mortgage" not in prompt.lower()

    def test_prompt_uses_other_rubric_when_genre_unknown(self):
        create, captured = self._capture_prompt()
        with patch("palmer.watches.client") as mock_client:
            mock_client.messages.create.side_effect = create
            watches_mod._check_watch_hit(
                results="Something happened.",
                description="Weird watch",
                recent_summaries=[],
                engaged=False,
                genre="not_a_real_genre",
            )
        prompt = captured[0]
        # rubric_for() falls back to 'other'; assert the header line still lists genre
        assert "Genre: not_a_real_genre" in prompt
        assert "genuinely notable, time-sensitive development" in prompt.lower()

    def test_engaged_footer_lowers_bar(self):
        create, captured = self._capture_prompt()
        with patch("palmer.watches.client") as mock_client:
            mock_client.messages.create.side_effect = create
            watches_mod._check_watch_hit(
                results="Some update.",
                description="Cardinals",
                recent_summaries=[],
                engaged=True,
                genre="sports_team",
            )
        prompt = captured[0]
        assert "following this closely" in prompt.lower()

    def test_no_reply_is_no(self):
        with patch("palmer.watches.client") as mock_client:
            mock_client.messages.create.return_value = llm_reply("NO — routine game")
            hit = watches_mod._check_watch_hit(
                results="Cardinals lost 5-4 to the Reds.",
                description="Cardinals",
                recent_summaries=[],
                engaged=False,
                genre="sports_team",
            )
        assert hit is False


# ============================================================================
# from test_corroboration.py
# ============================================================================
#
# Tests for the news-quality gate in watches.corroborated(). Every unprompted
# news text — a watch alert, a headline on the page — has to clear it.

def _r(url: str, score: float = 0.7) -> dict:
    return {"url": url, "title": "t", "content": "c", "score": score,
            "published_date": "Tue, 18 Aug 2026 12:00:00 GMT"}


class TestCorroborated:
    def test_two_distinct_domains_pass(self):
        results = [_r("https://something.example/a"), _r("https://other.example/b")]
        assert corroborated(results)

    def test_single_tier1_passes_alone(self):
        # apnews.com is tier 1 in trusted_sources.json
        assert corroborated([_r("https://apnews.com/story/xyz")])

    def test_gov_domain_counts_as_tier1(self):
        # .gov is tier-1 via _source_tier
        assert corroborated([_r("https://weather.gov/warning/123")])

    def test_single_unknown_domain_fails(self):
        assert not corroborated([_r("https://rumor.example/x")])

    def test_multiple_urls_same_canonical_domain_fails(self):
        results = [
            _r("https://rumor.example/one"),
            _r("https://rumor.example/two"),
            _r("https://sub.rumor.example/three"),
        ]
        assert not corroborated(results)

    def test_empty_fails(self):
        assert not corroborated([])


# ============================================================================
# from test_subject_dedup.py
# ============================================================================
#
# Tests for cross-job subject-dedup: userprofile._is_duplicate_subject and its wiring
# into watches.py and followup.py. Pure logic + mocked LLM/DB — no real network
# or LLM calls. Run: pytest test_subject_dedup.py

def _haiku_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    return resp


class TestIsDuplicateSubject:
    def test_no_recent_messages_returns_false_without_llm_call(self):
        with patch("palmer.db.get_recent_assistant_messages", return_value=[]), \
             patch("palmer.userprofile.client") as mock_client:
            assert userprofile._is_duplicate_subject("+15551234567", "new message") is False
        mock_client.messages.create.assert_not_called()

    def test_haiku_says_yes_returns_true(self):
        with patch("palmer.db.get_recent_assistant_messages", return_value=["Hurts practice update"]), \
             patch("palmer.userprofile.client") as mock_client:
            mock_client.messages.create.return_value = _haiku_response("YES")
            assert userprofile._is_duplicate_subject("+15551234567", "Hurts camp footage") is True

    def test_haiku_says_no_returns_false(self):
        with patch("palmer.db.get_recent_assistant_messages", return_value=["weather update"]), \
             patch("palmer.userprofile.client") as mock_client:
            mock_client.messages.create.return_value = _haiku_response("NO")
            assert userprofile._is_duplicate_subject("+15551234567", "bitcoin price") is False

    def test_llm_failure_fails_open(self):
        with patch("palmer.db.get_recent_assistant_messages", return_value=["something"]), \
             patch("palmer.userprofile.client") as mock_client:
            mock_client.messages.create.side_effect = Exception("API down")
            assert userprofile._is_duplicate_subject("+15551234567", "new message") is False


def _watch(**overrides):
    base = {
        "id": 1, "phone": "+15551234567", "description": "Eagles Hurts injury",
        "queries": ["Jalen Hurts injury"], "cooldown_hours": 4,
        "last_alerted": None, "last_alert_summary": None,
        "daily_alert_count": 0, "daily_alert_date": None, "recent_summaries": [],
    }
    base.update(overrides)
    return base


_RAW_RESULTS = [
    {"title": "Hurts injury update", "url": "https://example1.com/a",
     "content": "details", "published_date": "2026-08-13", "score": 1.0},
    {"title": "Hurts injury update 2", "url": "https://example2.com/b",
     "content": "more details", "published_date": "2026-08-13", "score": 1.0},
]


class TestWatchesSubjectDedup:
    def test_skips_send_when_duplicate_subject(self):
        with patch("palmer.watches.get_active_watches", return_value=[_watch()]), \
             patch("palmer.watches._search_raw", return_value=_RAW_RESULTS), \
             patch("palmer.watches._check_watch_hit", return_value=True), \
             patch("palmer.watches._best_result", return_value=_RAW_RESULTS[0]), \
             patch("palmer.watches._is_duplicate_subject", return_value=True) as dedup, \
             patch("palmer.watches.claim_watch_alert") as mock_claim, \
             patch("palmer.watches.update_watch_alerted") as mock_update, \
             patch("palmer.sms_util.send_sms") as mock_send:
            watches.run_watches()
        dedup.assert_called_once()
        mock_claim.assert_not_called()
        mock_send.assert_not_called()
        mock_update.assert_not_called()

    def test_sends_normally_when_not_duplicate(self):
        with patch("palmer.watches.get_active_watches", return_value=[_watch()]), \
             patch("palmer.watches._search_raw", return_value=_RAW_RESULTS), \
             patch("palmer.watches._check_watch_hit", return_value=True), \
             patch("palmer.watches._best_result", return_value=_RAW_RESULTS[0]), \
             patch("palmer.watches._is_duplicate_subject", return_value=False), \
             patch("palmer.watches.claim_watch_alert", return_value=True), \
             patch("palmer.watches._draft_alert", return_value="Hurts is out. https://example.com/a"), \
             patch("palmer.watches.update_watch_alerted") as mock_update, \
             patch("palmer.sms_util.send_sms") as mock_send:
            watches.run_watches()
        mock_send.assert_called_once()
        mock_update.assert_called_once()
        # What goes out is the drafted line, not the raw headline.
        assert mock_send.call_args[0][1] == "Hurts is out. https://example.com/a"

    def test_alert_persists_the_fired_url_and_domain(self):
        """The 'Watching' page section links a watch to the article
        that fired it — update_watch_alerted must be called with that URL/domain,
        not just the dedup title."""
        with patch("palmer.watches.get_active_watches", return_value=[_watch()]), \
             patch("palmer.watches._search_raw", return_value=_RAW_RESULTS), \
             patch("palmer.watches._check_watch_hit", return_value=True), \
             patch("palmer.watches._best_result", return_value=_RAW_RESULTS[0]), \
             patch("palmer.watches._is_duplicate_subject", return_value=False), \
             patch("palmer.watches.claim_watch_alert", return_value=True), \
             patch("palmer.watches._draft_alert", return_value="drafted line https://example.com/a"), \
             patch("palmer.watches.update_watch_alerted") as mock_update, \
             patch("palmer.sms_util.send_sms"):
            watches.run_watches()
        _, kwargs = mock_update.call_args
        assert kwargs["url"] == "https://example1.com/a"
        assert kwargs["domain"] == "example1.com"


class TestFollowupSubjectDedup:
    def _profile(self):
        return {"morning_onboarded": True, "timezone": "America/Chicago",
                "ongoing_threads": ["a big life event"]}

    def test_skips_send_and_releases_claim_when_duplicate(self):
        with patch("palmer.followup.get_all_profiles",
                   return_value=[("+15551234567", self._profile())]), \
             patch("palmer.followup._should_send_followup", return_value=True), \
             patch("palmer.followup.claim_daily_guard", return_value=True), \
             patch("palmer.followup.get_history", return_value=[]), \
             patch("palmer.followup._candidates", return_value=[followup.Subject("thread", "a big life event")]), \
             patch("palmer.followup._pick_subject", return_value=followup.Subject("thread", "a big life event")), \
             patch("palmer.followup._draft_followup", return_value="hey how'd that go?"), \
             patch("palmer.followup._is_duplicate_subject", return_value=True) as dedup, \
             patch("palmer.followup.upsert_profile") as mock_upsert, \
             patch("palmer.followup._local_today") as mock_today, \
             patch("palmer.sms_util.send_sms") as mock_send:
            mock_today.return_value.isoformat.return_value = "2026-08-13"
            followup.run_followups()
        dedup.assert_called_once()
        mock_send.assert_not_called()
        assert any(
            c.args[1] == {"followup_sent_date": None} for c in mock_upsert.call_args_list
        ), "expected the daily claim to be released"

    def test_sends_normally_when_not_duplicate(self):
        with patch("palmer.followup.get_all_profiles",
                   return_value=[("+15551234567", self._profile())]), \
             patch("palmer.followup._should_send_followup", return_value=True), \
             patch("palmer.followup.claim_daily_guard", return_value=True), \
             patch("palmer.followup.get_history", return_value=[]), \
             patch("palmer.followup._candidates", return_value=[followup.Subject("thread", "a big life event")]), \
             patch("palmer.followup._pick_subject", return_value=followup.Subject("thread", "a big life event")), \
             patch("palmer.followup._draft_followup", return_value="hey how'd that go?"), \
             patch("palmer.followup._is_duplicate_subject", return_value=False), \
             patch("palmer.followup.save_message") as mock_save, \
             patch("palmer.followup._local_today") as mock_today, \
             patch("palmer.sms_util.send_sms") as mock_send:
            mock_today.return_value.isoformat.return_value = "2026-08-13"
            followup.run_followups()
        mock_send.assert_called_once()
        mock_save.assert_called_once()


# ============================================================================
# from test_rubrics.py
# ============================================================================
#
# Tests for the genre classifier and per-genre rubrics.
#
# Haiku is mocked; we're testing that:
#   1. The classifier normalizes Haiku's reply into a VALID_GENRES value.
#   2. In-process memoization actually short-circuits the second call.
#   3. Every declared genre has a non-empty rubric.
#   4. rubric_for() falls back safely on unknown or None input.

class TestRubricCoverage:
    def test_every_genre_has_content(self):
        for g in rubrics.VALID_GENRES:
            body = rubrics.GENRE_RUBRICS.get(g, "")
            assert body.strip(), f"genre {g!r} has empty rubric"
            # Rubric must have both sides ("would text" and "wouldn't")
            low = body.lower()
            assert "would text about" in low, f"genre {g!r} missing 'would text about' block"
            assert "nobody would text" in low or g == "other", \
                f"genre {g!r} missing 'nobody would text' block"

    def test_rubric_for_known_genre(self):
        assert rubrics.rubric_for("sports_team") == rubrics.GENRE_RUBRICS["sports_team"]

    def test_rubric_for_none_falls_back_to_other(self):
        assert rubrics.rubric_for(None) == rubrics.GENRE_RUBRICS["other"]

    def test_rubric_for_unknown_falls_back_to_other(self):
        assert rubrics.rubric_for("kittens") == rubrics.GENRE_RUBRICS["other"]


class TestClassifyGenre:
    def setup_method(self):
        rubrics._reset_cache_for_tests()

    def test_exact_reply_maps(self):
        with patch("palmer.rubrics.client") as mock_client:
            mock_client.messages.create.return_value = llm_reply("sports_team")
            assert rubrics.classify_genre("Cardinals") == "sports_team"

    def test_normalizes_reply_with_punctuation(self):
        with patch("palmer.rubrics.client") as mock_client:
            mock_client.messages.create.return_value = llm_reply("Category: market_instrument.")
            assert rubrics.classify_genre("AAPL stock") == "market_instrument"

    def test_uppercase_reply_maps(self):
        with patch("palmer.rubrics.client") as mock_client:
            mock_client.messages.create.return_value = llm_reply("GEOPOLITICS")
            assert rubrics.classify_genre("Iran conflict") == "geopolitics"

    def test_unknown_reply_falls_back_to_other(self):
        with patch("palmer.rubrics.client") as mock_client:
            mock_client.messages.create.return_value = llm_reply("nonsense reply here")
            assert rubrics.classify_genre("something") == "other"

    def test_api_failure_falls_back_to_other(self):
        with patch("palmer.rubrics.client") as mock_client:
            mock_client.messages.create.side_effect = RuntimeError("boom")
            assert rubrics.classify_genre("anything") == "other"

    def test_empty_topic_returns_other_without_api_call(self):
        with patch("palmer.rubrics.client") as mock_client:
            assert rubrics.classify_genre("") == "other"
            assert rubrics.classify_genre("   ") == "other"
            mock_client.messages.create.assert_not_called()

    def test_memoization_short_circuits_second_call(self):
        with patch("palmer.rubrics.client") as mock_client:
            mock_client.messages.create.return_value = llm_reply("sports_team")
            rubrics.classify_genre("Cardinals")
            rubrics.classify_genre("Cardinals")
            rubrics.classify_genre("cardinals")   # case-insensitive
            rubrics.classify_genre("  Cardinals  ")   # trim
            assert mock_client.messages.create.call_count == 1

    def test_memoization_distinct_topics_call_separately(self):
        with patch("palmer.rubrics.client") as mock_client:
            mock_client.messages.create.side_effect = [
                llm_reply("sports_team"),
                llm_reply("market_instrument"),
            ]
            assert rubrics.classify_genre("Cardinals") == "sports_team"
            assert rubrics.classify_genre("AAPL") == "market_instrument"
            assert mock_client.messages.create.call_count == 2

    def test_failure_result_is_cached(self):
        """Once 'other' is cached for a topic, a later successful reply won't override it.
        This is deliberate — repeated Haiku hits on the same string are wasted budget."""
        with patch("palmer.rubrics.client") as mock_client:
            mock_client.messages.create.side_effect = RuntimeError("boom")
            assert rubrics.classify_genre("weird") == "other"
        with patch("palmer.rubrics.client") as mock_client:
            mock_client.messages.create.return_value = llm_reply("sports_team")
            assert rubrics.classify_genre("weird") == "other"
            mock_client.messages.create.assert_not_called()


# ============================================================================
# from test_followup.py
# ============================================================================
#
# A check-in is about something Palmer actually knows, and not too often.
#
# followup.py fetches nothing of its own — subjects are copied from the profile
# (ongoing_threads), from the page's Scores rows (a followed team's game) and
# from the page's stored headlines — so every guard has to be structural.
#
# Two defects made the old version the largest source of "random thoughts":
# _pick_thread returned whatever Haiku emitted and handed it straight to the
# drafter, so an invented thread was written up as though it were real; and the
# draft prompt asked for "a statement that just shows you remembered", which is
# an instruction to invent specificity. Separately, every bail path nulled
# followup_sent_date instead of restoring it, which silently voided the pacing
# gap. The gap is longer now (GAP_DAYS) because with teams and news in the pool
# there is nearly always a candidate, so the gap is the whole rate limit.

PHONE = "+15550001111"
THREADS = ["interviewing at Stripe next week", "sister's wedding in Portland"]
CARDS = {"abbrev": "STL", "name": "St. Louis Cardinals", "league": "mlb"}
GAME_LAST = {"id": "1", "league": "mlb", "state": "post", "detail": "Final",
             "home": {"abbrev": "STL", "name": "St. Louis Cardinals", "score": 5},
             "away": {"abbrev": "CHC", "name": "Chicago Cubs", "score": 2}}

# The one instant every clock-dependent test here is frozen to. _local_now and
# _local_today must be patched TOGETHER off this value: _should_send_followup
# reads the first, but the claim_daily_guard write in run_followups reads the
# second, so patching only _local_now left the assertion comparing a frozen day
# against the real clock.
FROZEN = datetime(2026, 8, 30, 15, 0, tzinfo=ZoneInfo("America/Chicago"))

# The last real send, as the bail tests expect to find it restored. Derived
# from FROZEN so it stays outside the maximum pacing gap whatever FROZEN is.
PRIOR_SENT = (FROZEN - timedelta(days=followup.GAP_MAX_DAYS + 1)).date().isoformat()


def _freeze(monkeypatch):
    monkeypatch.setattr(followup, "_local_now", lambda tz: FROZEN)
    monkeypatch.setattr(followup, "_local_today", lambda tz: FROZEN.date())


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test_followup.db")
    db.init_db()


def _haiku(text):
    return patch.object(followup.client.messages, "create",
                        return_value=MagicMock(content=[MagicMock(text=text)]))


def _threads(*texts):
    return [Subject("thread", t) for t in texts]


class TestPacing:
    def test_the_gap_is_longer_than_it_was(self):
        """3 days read as a drumbeat. Ten is roughly three a month."""
        assert followup.GAP_DAYS >= 7
        assert followup.GAP_MAX_DAYS > followup.GAP_DAYS

    def test_inside_the_gap_is_silence(self, monkeypatch):
        _freeze(monkeypatch)
        recent = (FROZEN - timedelta(days=followup.GAP_DAYS - 1)).date().isoformat()
        assert not followup._should_send_followup({
            "morning_onboarded": True, "timezone": "America/Chicago",
            "ongoing_threads": THREADS, "followup_sent_date": recent})

    def test_past_the_gap_is_due(self, monkeypatch):
        _freeze(monkeypatch)
        assert followup._should_send_followup({
            "morning_onboarded": True, "timezone": "America/Chicago",
            "ongoing_threads": THREADS, "followup_sent_date": PRIOR_SENT})

    def test_a_team_or_a_topic_is_enough_to_be_eligible(self, monkeypatch):
        _freeze(monkeypatch)
        base = {"morning_onboarded": True, "timezone": "America/Chicago"}
        assert not followup._should_send_followup(base)
        assert followup._should_send_followup(dict(base, followed_teams=[CARDS]))
        assert followup._should_send_followup(dict(base, morning_topics=["AI news"]))


class TestCandidatesAreCopiedFromData:
    def test_threads_teams_and_headlines_all_become_subjects(self):
        profile = {"ongoing_threads": THREADS, "followed_teams": [CARDS]}
        payload = {"fetched": {"headlines": datetime.now().timestamp()},
                   "headlines": [{"title": "Fed holds rates", "url": "https://reuters.com/x",
                                  "source": "reuters.com", "topic": "Fed"}]}
        with patch("palmer.sports.team_day", return_value={"last": GAME_LAST, "today": None}):
            got = followup._candidates(profile, payload, FROZEN.date())
        kinds = [s.kind for s in got]
        assert kinds == ["thread", "thread", "team", "news"]
        assert got[2].text == "St. Louis Cardinals: yesterday beat Chicago Cubs 5-2"
        assert got[3].text == "Fed holds rates (Fed)" and got[3].url == "https://reuters.com/x"

    def test_stale_headlines_are_not_brought_up(self):
        old = datetime.now().timestamp() - (followup.NEWS_MAX_AGE_HOURS + 1) * 3600
        payload = {"fetched": {"headlines": old},
                   "headlines": [{"title": "Fed holds rates", "url": "u", "topic": "Fed"}]}
        assert followup._candidates({}, payload, FROZEN.date()) == []

    def test_a_team_with_no_game_adds_nothing(self):
        with patch("palmer.sports.team_day", return_value={"last": None, "today": None}):
            assert followup._candidates({"followed_teams": [CARDS]}, None, FROZEN.date()) == []

    def test_a_sports_outage_costs_only_the_team_rows(self):
        with patch("palmer.sports.team_day", side_effect=RuntimeError("espn down")):
            got = followup._candidates({"ongoing_threads": THREADS, "followed_teams": [CARDS]},
                                       None, FROZEN.date())
        assert [s.kind for s in got] == ["thread", "thread"]

    def test_the_last_subject_is_left_out(self):
        profile = {"ongoing_threads": THREADS, "followup_last_thread": THREADS[0]}
        assert [s.text for s in followup._candidates(profile, None, FROZEN.date())] == [THREADS[1]]

    def test_a_single_subject_is_still_available_after_being_used(self):
        """Excluding the only thing there is would end check-ins for good."""
        profile = {"ongoing_threads": [THREADS[0]], "followup_last_thread": THREADS[0]}
        assert [s.text for s in followup._candidates(profile, None, FROZEN.date())] == [THREADS[0]]

    def test_no_token_means_no_news_and_no_error(self):
        assert followup._load_payload({}) is None


class TestPickSubjectFailsClosed:
    def test_an_exact_echo_returns_the_stored_subject(self):
        with _haiku("interviewing at Stripe next week"):
            got = followup._pick_subject({}, [], _threads(*THREADS))
        assert got == Subject("thread", "interviewing at Stripe next week")

    def test_a_confabulated_subject_is_refused(self):
        """The actual failure: Haiku names something plausible that is not on
        the list, and it used to be drafted as though it were real."""
        with _haiku("how the apartment hunt is going"):
            assert followup._pick_subject({}, [], _threads(*THREADS)) is None

    def test_a_paraphrase_is_refused_too(self):
        with _haiku("the Stripe interview"):
            assert followup._pick_subject({}, [], _threads(*THREADS)) is None

    def test_none_is_none(self):
        with _haiku("NONE"):
            assert followup._pick_subject({}, [], _threads(*THREADS)) is None

    def test_no_candidates_costs_no_model_call(self):
        with patch.object(followup.client.messages, "create") as create:
            assert followup._pick_subject({}, [], []) is None
            create.assert_not_called()

    def test_a_model_failure_is_silence(self):
        with patch.object(followup.client.messages, "create",
                          side_effect=RuntimeError("down")):
            assert followup._pick_subject({}, [], _threads(*THREADS)) is None

    def test_the_prompt_shows_the_kind_and_asks_for_an_echo(self):
        with _haiku("NONE") as create:
            followup._pick_subject({}, [], [Subject("team", "Cards: today play Cubs, 7:15 PM CT")])
        body = create.call_args.kwargs["messages"][0]["content"]
        assert "[team] Cards: today play Cubs, 7:15 PM CT" in body
        assert "copied EXACTLY" in body


class TestLifeContextAloneNeverTriggersACheckIn:
    def test_prose_about_someone_is_not_a_thread(self):
        assert not followup._should_send_followup({
            "morning_onboarded": True, "timezone": "America/Chicago",
            "life_context": "Works in finance, two kids, moved to Chicago in March",
        })


class TestThePacingGapSurvivesABail:
    """claim_daily_guard overwrites followup_sent_date with today, so nulling it
    on a bail erased the record of the last real send — and _should_send_followup
    measures the gap against exactly that field."""

    def _run(self, tmp_path, monkeypatch, *, subject, candidates=None,
             drafted="hey, how'd it go?", dup=False, sent=True):
        _fresh(tmp_path, monkeypatch)
        db.upsert_profile(PHONE, {
            "morning_onboarded": True, "timezone": "America/Chicago",
            "ongoing_threads": THREADS, "followup_sent_date": PRIOR_SENT,
        })
        _freeze(monkeypatch)
        if candidates is None:
            candidates = _threads(*THREADS)
        with patch.object(followup, "get_all_profiles",
                          return_value=[(PHONE, db.get_profile(PHONE))]), \
             patch.object(followup, "_candidates", return_value=candidates), \
             patch.object(followup, "_pick_subject", return_value=subject), \
             patch.object(followup, "_draft_followup", return_value=drafted), \
             patch.object(followup, "_is_duplicate_subject", return_value=dup), \
             patch("palmer.sms_util.send_sms", return_value=sent):
            followup.run_followups()
        return db.get_profile(PHONE)

    def test_no_candidates_restores_the_prior_date(self, tmp_path, monkeypatch):
        p = self._run(tmp_path, monkeypatch, subject=None, candidates=[])
        assert p["followup_sent_date"] == PRIOR_SENT

    def test_no_pick_restores_it(self, tmp_path, monkeypatch):
        p = self._run(tmp_path, monkeypatch, subject=None)
        assert p["followup_sent_date"] == PRIOR_SENT

    def test_an_empty_draft_restores_it(self, tmp_path, monkeypatch):
        p = self._run(tmp_path, monkeypatch, subject=_threads(THREADS[0])[0], drafted="")
        assert p["followup_sent_date"] == PRIOR_SENT

    def test_a_duplicate_restores_it(self, tmp_path, monkeypatch):
        p = self._run(tmp_path, monkeypatch, subject=_threads(THREADS[0])[0], dup=True)
        assert p["followup_sent_date"] == PRIOR_SENT

    def test_a_failed_send_restores_it(self, tmp_path, monkeypatch):
        p = self._run(tmp_path, monkeypatch, subject=_threads(THREADS[0])[0], sent=False)
        assert p["followup_sent_date"] == PRIOR_SENT

    def test_a_real_send_advances_it_and_records_the_subject(self, tmp_path, monkeypatch):
        p = self._run(tmp_path, monkeypatch, subject=_threads(THREADS[0])[0])
        assert p["followup_sent_date"] == FROZEN.date().isoformat()
        assert p["followup_last_thread"] == THREADS[0]


class TestTheDraft:
    def _drafted(self, subject, text="cards back at it tonight, 7:15."):
        with patch.object(followup, "_build_system", return_value="sys"), \
             patch.object(followup.client.messages, "create",
                          return_value=MagicMock(content=[MagicMock(text=text)])) as create:
            out = followup._draft_followup(PHONE, subject)
        return out, create.call_args.kwargs["messages"][0]["content"]

    def test_it_forbids_inventing_details(self):
        import inspect
        src = inspect.getsource(followup._draft_followup)
        assert "Do not invent" in src
        # The instruction that produced the problem.
        assert "just shows you remembered" not in src

    def test_a_news_subject_carries_its_link_last_and_alone(self):
        out, body = self._drafted(Subject("news", "Fed holds rates (Fed)",
                                          url="https://reuters.com/x", source="reuters.com"))
        assert out.endswith("\nhttps://reuters.com/x")
        assert "do not include or mention a link" in body and "reuters.com" in body

    def test_a_team_subject_is_drafted_from_the_line_only(self):
        out, body = self._drafted(Subject("team", "Cards: today play Cubs, 7:15 PM CT"))
        assert "Cards: today play Cubs, 7:15 PM CT" in body
        assert "\n" not in out

    def test_a_thread_subject_may_ask_one_question(self):
        _, body = self._drafted(Subject("thread", THREADS[0]))
        assert THREADS[0] in body and "ask one short question" in body

    def test_the_bookkeeping_field_is_allow_listed_but_not_extractable(self):
        from palmer import userprofile
        from palmer import prompts
        assert "followup_last_thread" in userprofile.PROFILE_FIELDS
        assert "followup_last_thread" not in prompts.EXTRACT_PROMPT
