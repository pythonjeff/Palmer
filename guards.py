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


# --- repetition --------------------------------------------------------------
# Two different failures wear the same face, and they need opposite remedies.
#
# SUPPRESSION: an unprompted message repeating one already sent. Drew got the
# identical followup twice — "yo how'd practice look today? hurts moving like
# they said?" — because _is_duplicate_subject's window is six hours while the
# followup job runs every four and the subject stayed live for days.
#
# VARIATION: a scheduled message the user DID ask for, said the same way every
# time. Three consecutive mornings opened "103 today in Woodland Hills", "106 in
# Woodland Hills today", "111 today in Woodland Hills". Suppressing those would
# be wrong — they asked for a daily briefing — but Palmer should not sound like
# a form letter.
#
# Both are answered by the same cheap measure and no model call: stopword-
# stripped token overlap, the same shape as save_reminder's duplicate guard.

_STOPWORDS = frozenset("""a an and are as at be been but by for from had has have he her his i
if in into is it its me my not of on or our so that the their them they this to too us was we
were what when who will with you your yours today tomorrow just get got go going""".split())

_WORD = re.compile(r"[a-z0-9']+")
# Links are not content. A reply that is a sentence plus the user's page URL
# would otherwise read as near-identical to every other one.
_STRIP = re.compile(r"https?://\S+")


def _shingle(text: str) -> set[str]:
    words = _WORD.findall(_STRIP.sub(" ", (text or "").lower()))
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of meaningful words. 1.0 is verbatim, 0.0 shares nothing."""
    sa, sb = _shingle(a), _shingle(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# Verbatim-ish. A proactive message this close to one already sent is a repeat,
# not a follow-up, and no model call is needed to know it.
REPEAT_THRESHOLD = 0.62


def near_duplicate(text: str, recent: list[str], threshold: float = REPEAT_THRESHOLD) -> str | None:
    """The recent message this one repeats, or None. Never raises."""
    if not text or not recent:
        return None
    try:
        for prior in recent:
            if similarity(text, prior) >= threshold:
                return prior
    except Exception as e:
        print(f"near_duplicate check failed: {type(e).__name__}: {e}")
    return None


# Token overlap answers SUPPRESSION and is useless for VARIATION. Three
# consecutive Woodland Hills mornings — "Morning Drew - 103 today in Woodland
# Hills", "106 in Woodland Hills today, Drew", "111 today in Woodland Hills,
# Drew" — score only 0.23-0.25 against each other, because the numbers and the
# trailing clause differ every day. Nothing lexical separates them from a
# genuinely fresh morning.
#
# What actually repeats is the SHAPE of the opening: a temperature, the word
# today, the city. So normalise the numbers away and compare the first few
# words. That is the thing a reader recognises as "he says it the same way
# every morning".
# Three, not five. By the fourth word the trailing clause has diverged — "103
# today in Woodland Hills, STAY INSIDE" vs "106 in Woodland Hills today, DREW —
# HOTTEST" — and every day looks unique again. The repetition a reader actually
# notices is in the first breath.
_OPENING_WORDS = 3
_SHAPE_WORD = re.compile(r"[a-z0-9'#]+")


def opening_shape(text: str) -> str:
    """A signature for how a message starts, with numbers flattened to #."""
    body = _STRIP.sub(" ", (text or "").lower())
    body = re.sub(r"\d+(?:\.\d+)?", " # ", body)
    words = [w for w in _SHAPE_WORD.findall(body) if w == "#" or w not in _STOPWORDS]
    return " ".join(words[:_OPENING_WORDS])


def repeats_opening(text: str, recent: list[str]) -> str | None:
    """The recent message this one opens like, or None.

    Deliberately separate from near_duplicate: a morning briefing SHOULD cover
    the same ground every day, so its content recurring is correct and only its
    phrasing is the problem."""
    if not text or not recent:
        return None
    try:
        shape = opening_shape(text)
        if not shape:
            return None
        for prior in recent:
            if opening_shape(prior) == shape:
                return prior
    except Exception as e:
        print(f"repeats_opening check failed: {type(e).__name__}: {e}")
    return None


# --- internal deliberation ---------------------------------------------------
# A user received "Both of these fall into the crime/dark content category they
# explicitly asked to avoid. Skipping." and "This one's in the crime/dark
# content bucket they asked to avoid. Skipping it." — Palmer narrating its own
# filtering decision, about her, in the third person.
#
# morning.py already had a guard for this, but it lived there, so alerts,
# followups, watches and reminders never ran it — and it matched fixed phrases,
# which the model simply wrote around ("they EXPLICITLY asked" missed a rule
# looking for "they asked").
#
# The version that replaced it fired on EITHER of two signals, and that was too
# loose in both directions. Real replies were blocked and — because send_sms
# returns False and main.py answers a falsy send with FALLBACK_SMS — the user
# got "something went sideways on my end, try again" instead of their answer:
#
#   "got it, not sending those anymore"     <- a commitment, TO them
#   "they said the deal closes Friday"      <- news about a third party
#
# So a bare send-decision is not damning, and neither is a bare third-person
# sentence. What is damning is Palmer talking about its own reader as "the
# user", or reciting internal machinery no friend has words for. Everything else
# needs corroboration.

# Nobody texting a friend calls them "the user". Damning on its own.
_NAMES_THE_READER = re.compile(
    r"\b(?:the\s+user|this\s+user|the\s+recipient|the\s+reader)\b", re.I)

# The vocabulary of a filter explaining itself. Also damning on its own — these
# are words about Palmer's own plumbing, not about the reader's life.
_MACHINERY = re.compile(
    r"\b(?:doesn'?t\s+meet\s+the|below\s+the\s+threshold|meets?\s+the\s+threshold|"
    r"no\s+alert\s+needed|filtered\s+out|suppress(?:ing|ed)|"
    r"the\s+(?:criteria|threshold|rubric)|scored?\s+(?:below|under|too\s+low))\b", re.I)

# Third person ABOUT THE READER'S PREFERENCES. On its own this is ambiguous —
# "they asked for a recount" is news — so it only counts alongside a send
# decision. Note "said" is deliberately NOT here: "they said the deal closes
# Friday" is exactly the sentence Palmer exists to send.
_THIRD_PERSON = re.compile(
    r"\b(?:they)\b[^.!?\n]{0,40}"
    r"\b(?:asked|requested|wanted|prefers?|specified|don'?t\s+want|do\s+not\s+want)\b", re.I)

# An announcement that something is being withheld. Ambiguous alone — it is also
# how Palmer agrees to stop doing something — so it needs corroboration.
_SEND_DECISION = re.compile(
    r"\b(?:skipping|i'?ll\s+skip|not\s+sending|won'?t\s+send|can'?t\s+include|"
    r"leaving\s+(?:this|that|it)\s+out|omitting)\b", re.I)


def leaks_deliberation(text: str) -> bool:
    """True if the draft narrates Palmer's own decision-making to the reader.

    Two tiers, because precision matters as much as recall here: a false
    positive on the reply path costs the user their answer and hands them a
    canned apology instead."""
    if not text:
        return False
    if _NAMES_THE_READER.search(text) or _MACHINERY.search(text):
        return True
    return bool(_SEND_DECISION.search(text) and _THIRD_PERSON.search(text))


DELIBERATION_CORRECTION = (
    "\n\nYou just wrote: {draft!r}\n"
    "That narrates your own filtering out loud — talking about them in the third "
    "person, or naming criteria they never asked about. Write it again as a "
    "message TO them, in your own voice. If you are declining to do something, "
    "say so plainly as yourself; never explain the machinery behind it."
)
