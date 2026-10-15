"""A check-in is about something Palmer actually knows, and not too often.

followup.py fetches nothing of its own — subjects are copied from the profile
(ongoing_threads), from the page's Scores rows (a followed team's game) and
from the page's stored headlines — so every guard has to be structural.

Two defects made the old version the largest source of "random thoughts":
_pick_thread returned whatever Haiku emitted and handed it straight to the
drafter, so an invented thread was written up as though it were real; and the
draft prompt asked for "a statement that just shows you remembered", which is
an instruction to invent specificity. Separately, every bail path nulled
followup_sent_date instead of restoring it, which silently voided the pacing
gap. The gap is longer now (GAP_DAYS) because with teams and news in the pool
there is nearly always a candidate, so the gap is the whole rate limit.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from palmer import db
from palmer import followup
from palmer.followup import Subject


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
