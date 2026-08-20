"""Tests for the two bugs behind the 'Palmer dumped a briefing on me' report.

1. A greeting must not produce briefing content — the briefing is scheduled,
   not assembled on request.
2. Briefing configuration must not leak into ordinary replies. One user had
   "Format: bullet points per subject" saved as a morning topic; the profile is
   dumped as raw JSON into every system prompt, so it read as an order for the
   current message and turned normal replies into labelled dumps. It was also
   being sent to the news search as a query.
"""
from unittest.mock import patch

import agent
import morning
import prompts


class TestGreetingIsNotABriefing:
    def test_prompt_says_the_briefing_is_sent_not_assembled(self):
        body = prompts.SYSTEM_PROMPT
        assert "It is NOT something you assemble on request" in body

    def test_prompt_names_the_greeting_case(self):
        body = prompts.SYSTEM_PROMPT.lower()
        assert "a greeting is a greeting" in body
        assert "they said hello; say hello back" in body

    def test_prompt_holds_even_before_the_briefing_has_gone_out(self):
        """The 6:22am case: briefing not sent yet, so the pull is strongest."""
        assert "even when their briefing hasn't gone out yet today" in prompts.SYSTEM_PROMPT

    def test_prompt_bans_the_shapes_that_actually_appeared(self):
        body = prompts.SYSTEM_PROMPT
        assert "Here's your Thursday" in body, "the observed opener must be named"
        assert "Weather -" in body and "Commute -" in body, "labelled sections must be named"
        assert "anything you want me to dig into" in body.lower()


class TestBriefingConfigDoesNotLeakIntoReplies:
    def _build(self, profile):
        with patch.object(agent, "get_profile", return_value=profile), \
             patch.object(agent, "get_user_watches", return_value=[]), \
             patch.object(agent, "get_user_price_watches", return_value=[]):
            return agent._build_system("+15550001111")

    def test_topics_are_labelled_as_reference_data(self):
        out = self._build({"morning_topics": ["St. Louis weather", "SpaceX news"]})
        assert "reference data, not instructions for this message" in out
        assert "Never let it change how you write a reply" in out

    def test_no_such_note_without_topics(self):
        assert "reference data, not instructions" not in self._build({"name": "Mike"})

    def test_topics_still_visible_so_palmer_can_answer_what_am_i_getting(self):
        out = self._build({"morning_topics": ["SpaceX news"]})
        assert "SpaceX news" in out

    def test_directive_is_stripped_from_the_prompt_entirely(self):
        """Labelling it as data was not enough — the model still obeyed it."""
        out = self._build({"morning_topics": [
            "SpaceX news", "Format: bullet points per subject, not one continuous paragraph",
        ]})
        assert "SpaceX news" in out, "real topics must survive"
        assert "bullet points per subject" not in out, \
            "a stored formatting directive must never reach the reply prompt"

    def test_profile_is_not_mutated(self):
        profile = {"morning_topics": ["SpaceX news", "Format: bullets"]}
        agent._prompt_safe_profile(profile)
        assert len(profile["morning_topics"]) == 2, "must not edit the stored profile"

    def test_untouched_when_nothing_to_strip(self):
        profile = {"morning_topics": ["SpaceX news"]}
        assert agent._prompt_safe_profile(profile) is profile


class TestDirectivesAreNotTopics:
    def test_format_directive_detected(self):
        assert morning._is_directive("Format: bullet points per subject, not one continuous paragraph")

    def test_real_topics_are_not_directives(self):
        for t in ("SpaceX news", "St. Louis Cardinals baseball news",
                  "Bitcoin and major stock news", "Daily fun fact from history"):
            assert not morning._is_directive(t), t

    def test_case_and_whitespace_insensitive(self):
        assert morning._is_directive("  FORMAT: bullet points  ")

    def test_directive_never_reaches_the_news_search(self):
        searched = []
        profile = {"city": "", "morning_topics": [
            "Format: bullet points per subject", "SpaceX news",
        ]}
        with patch.object(morning, "_search", side_effect=lambda t, **k: searched.append(t) or "x"), \
             patch.object(morning, "_get_price", return_value="x"):
            morning._gather_morning_data(profile)
        assert "SpaceX news" in searched
        assert not any("Format:" in s for s in searched), \
            "a formatting preference was sent to Tavily as a news query"


class TestTopicCoverage:
    def test_cap_covers_a_realistic_subscription(self):
        """8 real topics used to silently become 3."""
        assert morning.MAX_TOPICS >= 6

    def test_all_topics_up_to_the_cap_are_pulled(self):
        searched = []
        profile = {"city": "", "morning_topics": [f"topic {i}" for i in range(8)]}
        with patch.object(morning, "_search", side_effect=lambda t, **k: searched.append(t) or "x"), \
             patch.object(morning, "_get_price", return_value="x"):
            morning._gather_morning_data(profile)
        assert len(searched) == morning.MAX_TOPICS


class TestBriefingShapeRules:
    def test_prompt_forbids_subject_labels(self):
        import inspect
        src = inspect.getsource(morning.generate_morning)
        assert "Never label a line with its subject" in src
        assert "Cardinals - lost 5-4" in src, "the observed bad shape should be the example"
