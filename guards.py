"""Post-draft checks for rules the model breaks even when told not to.

`SYSTEM_PROMPT` has forbidden redirecting users to competing products since the
beginning, in as many words: "Palmer is the product — don't send people
elsewhere... Do NOT suggest 'just Google it' or 'check Google Maps' — ever."
Palmer did it anyway, in production, repeatedly — once while quoting the rule
back: "I'd point you to Google Flights but I know that's not helpful coming
from me."

So this is the same situation `morning._NAMES_THE_LINK` already handles: a rule
the prompt states clearly and the model still breaks under pressure, enforced
with a check and exactly one redraft.

The hard part is precision, not recall. Palmer legitimately says these words all
the time — "Rezolve AI's first commercial deal, and it's with Google Cloud",
"ChatGPT has hundreds of millions of users", a merchant URL carrying
`utm_source=google`, "Google Shopping" as a source label. Matching the brand
name would gag Palmer on half the tech news it exists to report. So this matches
the *shape of a handoff* — a directive aimed at a competitor — and nothing else.
"""
from __future__ import annotations

import re

# Products that compete for the job Palmer does. Deliberately not a list of
# every company: a news story about Google is fine, being sent to Google is not.
_RIVAL = (
    r"(?:google(?:\s+(?:maps|flights|shopping|search|news|finance))?"
    r"|apple\s+maps|waze|mapquest"
    r"|chat\s?gpt|perplexity|copilot|gemini|claude\.ai"
    r"|siri|alexa"
    r"|yelp|tripadvisor|expedia|kayak|skyscanner|booking\.com)"
)

# A handoff has a shape: an instruction, a recommendation, or a claim that the
# rival is where the user should go to get it. Present-tense statements ABOUT a
# rival are news and must survive — "ChatGPT has hundreds of millions of users"
# is a fact Palmer should be able to report, so bare "<rival> has" cannot be a
# trigger. What distinguishes a handoff is that it is future-facing and aimed at
# the reader: what the rival *will* give *them*.
#
# There is deliberately NO pattern for "<Brand>'s app has ...". It was written
# and removed: it caught "Target's app actually has aisle locations built in"
# (a real violation) but also "Anthropic's site lists the new model IDs" and
# "the team's site has the full injury report" — pointing at a primary source,
# which is the opposite of a handoff and something Palmer should do freely. A
# guard that gags Palmer on citing sources is worse than one that misses a case
# the capability-honesty rule and the failure strings should have prevented
# upstream anyway. Precision over recall: a false positive here costs a redraft
# and, if it repeats, Palmer's ability to say true things.
_PATTERNS = (
    # "check Google Maps", "try Waze", "hit up Google Flights", "ask Siri"
    rf"\b(?:check|try|use|visit|open|search|ask|hit\s+up|head\s+(?:to|over\s+to)|go\s+to|pull\s+up|look\s+(?:at|on|it\s+up\s+on))\b[^.!?\n]{{0,24}}\b{_RIVAL}",
    # "I'd point you to X", "your best bet is X", "the better source is X"
    rf"\b(?:point(?:ing)?\s+you\s+(?:to|at)|recommend|suggest|best\s+(?:bet|move|source)\s+is|better\s+source)\b[^.!?\n]{{0,32}}\b{_RIVAL}",
    # "Google Maps will give you a live read", "Google will have estimates"
    rf"\b{_RIVAL}\b[^.!?\n]{{0,20}}\b(?:will\s+(?:give|have|show|tell|get)|is\s+(?:better|your\s+best)|would\s+(?:give|have|show|tell))\b",
    # the bare imperative
    r"\bjust\s+google\s+(?:it|that|them)\b",
    r"\bgoogle\s+(?:it|that)\b",
)
_REDIRECT = re.compile("|".join(_PATTERNS), re.IGNORECASE)

# URLs carry brand names for reasons that have nothing to do with a handoff —
# utm_source=google on a merchant link is the common one. Strip them first.
_URL = re.compile(r"https?://\S+")


def redirects_elsewhere(text: str) -> bool:
    """True if the draft hands the user off to a competing product."""
    if not text:
        return False
    return bool(_REDIRECT.search(_URL.sub(" ", text)))


REDIRECT_CORRECTION = (
    "\n\nYou just wrote: {draft!r}\n"
    "That sends them to another product, which is the one thing you never do — "
    "Palmer is the product. Write it again. If you genuinely cannot pull "
    "something, say so plainly in your own voice and offer either to try again "
    "or to do the nearest thing you CAN do. Check your tools before you claim a "
    "limit: you can pull flights, hotels, weather, traffic, prices, products and "
    "news. Do not name a competitor, do not describe where else the answer "
    "lives, and do not apologise for it."
)
