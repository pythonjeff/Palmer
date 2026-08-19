"""Inbound reaction ("tapback") parsing.

iMessage and Google Messages both degrade reactions to plain text when the
recipient is on SMS, which is what Palmer's Twilio number is. An iPhone tapback
arrives as:

    Liked "The audacity of it. Every single week."

Palmer used to treat that as an ordinary inbound message and reply to it, which
is exactly the "continuing a topic after they've closed it" failure SYSTEM_PROMPT
bans. A reaction is a closer. main.py now short-circuits on these: no reply, and
no FALLBACK_SMS either.

The reaction is not thrown away — it's the single best calibration signal there
is. A like on a dry joke confirms irony tolerance; a dislike is a miss. Because
the quoting device includes the original text, we know exactly WHICH of Palmer's
lines earned it. Recorded on the profile and surfaced by agent._build_system
alongside the CALIBRATION section.

Three stages: parse_reaction() detects (free regex), interpret_reaction()
decides what the reaction is DOING (Haiku, in context, per person), and main.py
acts on the verdict. Silence is the default and the failure default; the one
exception is a reaction that answers a question Palmer actually asked.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from agent import client, HAIKU_MODEL, _parse_json
from db import get_profile, upsert_profile

MAX_STORED_REACTIONS = 10
# Fold reactions into communication_style once this many have piled up since
# the last fold. Reactions bypass get_reply, so the per-turn extractor in
# agent._update_profile never sees them — without this they dead-end.
CONSOLIDATE_EVERY = 5

VALID_FUNCTIONS = ("answer", "closer", "applause", "objection", "emotional")
# A topic has to get thumbs-downed this many times before Palmer drops it. One
# stray tap must never silently delete a topic from someone's mornings.
NEGATIVE_STREAK_FOR_AVOID = 3
# Pacing: 1.0 is normal cadence, higher means back off. Capped so a bad week
# slows Palmer down without muting him permanently.
MAX_PACING_FACTOR = 3.0
_QUOTE_TRUNCATE = 120

# Apple's six classic tapbacks. Case-sensitive on purpose: "Liked" is Apple's
# wording, but "liked this idea" from a human should never match.
_APPLE_KINDS = {
    "Liked": ("liked", "positive"),
    "Loved": ("loved", "positive"),
    "Laughed at": ("laughed", "positive"),
    "Emphasized": ("emphasized", "positive"),
    "Disliked": ("disliked", "negative"),
    "Questioned": ("questioned", "neutral"),
}

# Straight and curly quotes both appear depending on OS version.
_Q = r"[\"“”]"

_APPLE_RE = re.compile(
    r"^(" + "|".join(re.escape(k) for k in _APPLE_KINDS) + r")\s+" + _Q + r"(.*?)" + _Q + r"?\s*$",
    re.DOTALL,
)

# iOS 18+ arbitrary-emoji tapback: Reacted 😂 to "..."
_REACTED_RE = re.compile(r"^Reacted\s+(.+?)\s+to\s+" + _Q + r"(.*?)" + _Q + r"?\s*$", re.DOTALL)

# Google Messages / RCS-to-SMS degrade: 👍 to "..."
_EMOJI_TO_RE = re.compile(r"^(.{1,8}?)\s+to\s+" + _Q + r"(.*?)" + _Q + r"?\s*$", re.DOTALL)

# Fallback only — used when the Haiku read in interpret_reaction is unavailable.
# Emoji meaning is person-dependent (💀 is praise to plenty of people), so these
# sets are deliberately not the source of truth.
_POSITIVE_EMOJI = set("\U0001f44d❤\U0001f60d\U0001f602\U0001f923\U0001f525\U0001f4af"
                      "\U0001f929\U0001f60a\U0001f64c\U0001f44f♥\U0001f970\U0001f973")
_NEGATIVE_EMOJI = set("\U0001f44e\U0001f621\U0001f620\U0001f612\U0001f61e\U0001f622\U0001f92c")

# Codepoint ranges that count as emoji for "is this body nothing but emoji".
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
    (0x1F1E6, 0x1F1FF),
)
# Modifiers that ride along with emoji and shouldn't disqualify a body.
_EMOJI_JOINERS = {0x200D, 0xFE0E, 0xFE0F, 0x20E3}


def _is_emoji_char(ch: str) -> bool:
    cp = ord(ch)
    if cp in _EMOJI_JOINERS:
        return True
    return any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES)


def is_emoji_only(text: str) -> bool:
    """True if text is nothing but emoji (and whitespace). Empty is not emoji."""
    stripped = "".join(text.split())
    if not stripped:
        return False
    return all(_is_emoji_char(c) for c in stripped)


def _emoji_sentiment(emoji: str) -> str:
    if any(c in _NEGATIVE_EMOJI for c in emoji):
        return "negative"
    if any(c in _POSITIVE_EMOJI for c in emoji):
        return "positive"
    return "neutral"


def _clean_quote(raw: str) -> str:
    q = raw.strip().strip("…").strip()
    return q[:_QUOTE_TRUNCATE]


def parse_reaction(body: str | None) -> dict | None:
    """Parse an inbound SMS body as a reaction.

    Returns {"kind", "sentiment", "quoted", "emoji"} or None if it's a normal
    message. `quoted` is the text being reacted to ("" when the sender's
    platform doesn't include it, as with a bare emoji).
    """
    if not body:
        return None
    text = body.strip()
    if not text:
        return None

    m = _APPLE_RE.match(text)
    if m:
        kind, sentiment = _APPLE_KINDS[m.group(1)]
        return {"kind": kind, "sentiment": sentiment,
                "quoted": _clean_quote(m.group(2)), "emoji": ""}

    m = _REACTED_RE.match(text)
    if m and is_emoji_only(m.group(1)):
        emoji = m.group(1).strip()
        return {"kind": "emoji", "sentiment": _emoji_sentiment(emoji),
                "quoted": _clean_quote(m.group(2)), "emoji": emoji}

    m = _EMOJI_TO_RE.match(text)
    if m and is_emoji_only(m.group(1)):
        emoji = m.group(1).strip()
        return {"kind": "emoji", "sentiment": _emoji_sentiment(emoji),
                "quoted": _clean_quote(m.group(2)), "emoji": emoji}

    # Bare emoji with no quoted original. Ambiguous at THIS layer — a lone
    # thumbs-up may be applause or may be "yes, do it" answering a question
    # Palmer just asked. interpret_reaction() resolves that against history;
    # do not try to decide it here.
    if is_emoji_only(text):
        return {"kind": "emoji", "sentiment": _emoji_sentiment(text),
                "quoted": "", "emoji": text}

    return None


def record_reaction(phone: str, reaction: dict, verdict: dict | None = None) -> None:
    """Append to the rolling reaction log on the profile. Never raises —
    a bookkeeping failure must not turn into a reply the user shouldn't get."""
    try:
        profile = get_profile(phone)
        log = list(profile.get("reactions") or [])
        log.append({
            "kind": reaction["kind"],
            "sentiment": (verdict or {}).get("sentiment") or reaction["sentiment"],
            "function": (verdict or {}).get("function", "closer"),
            "about": (verdict or {}).get("about", ""),
            "quoted": reaction.get("quoted", ""),
            "emoji": reaction.get("emoji", ""),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        upsert_profile(phone, {"reactions": log[-MAX_STORED_REACTIONS:]})
    except Exception as e:
        print(f"record_reaction failed for {phone}: {e}")


_INTERPRET_PROMPT = """Someone reacted to a text instead of replying to it. Work out what the reaction is DOING.

The last thing you (Palmer) said to them:
{last_assistant}

Their reaction: {label}{quoted_line}

What you know about them:
{profile}

Classify the FUNCTION — exactly one:
- answer: you asked a question or offered to do something, and this reaction IS their reply. A thumbs up on "want me to add that to your morning?" means yes. This is the only function that needs a response.
- closer: an acknowledgment. They saw it, they're done, nothing more is wanted.
- applause: the line landed — it was funny or well put, and they're saying so.
- objection: they disagree, or it missed. A dislike, or an emoji that reads as pushback.
- emotional: they're responding to the FEELING of what you said, not its content.

Then judge SENTIMENT for THIS person, in context — not from a generic emoji chart. The same emoji means different things to different people: a skull can mean "that was hilarious", crying can be laughter or actual sadness. Use what you know about them and what was actually said.

For ABOUT, name the subject they reacted to. These are the topics Palmer actually sends this person:
{topics}
If the reaction is about one of those, copy that topic string EXACTLY, character for character. Only invent a short label if it matches none of them. Leave it empty if there is no clear subject.

Return ONLY a JSON object:
{{"function": "...", "sentiment": "positive|negative|neutral", "about": "..."}}"""


def _fallback_verdict(reaction: dict) -> dict:
    """What we return when the model call fails: treat it as a closer and stay
    silent. That is exactly the pre-interpretation behavior, so an outage
    degrades to the old system instead of to unwanted texts."""
    return {
        "function": "closer",
        "sentiment": reaction.get("sentiment", "neutral"),
        "about": "",
        "needs_reply": False,
    }


def interpret_reaction(reaction: dict, last_assistant: str = "",
                       profile: dict | None = None) -> dict:
    """Decide what a reaction is doing, in context and for this person.

    Returns the parsed verdict plus `needs_reply`, which is True only for the
    `answer` function. Never raises — any failure falls back to silence.
    """
    if not reaction:
        return _fallback_verdict({})
    if not (last_assistant or "").strip():
        # Nothing to react to that we can see; there is no question to answer.
        return _fallback_verdict(reaction)

    label = reaction.get("emoji") or reaction.get("kind", "reacted")
    quoted = reaction.get("quoted") or ""
    quoted_line = f'\nThey reacted to this specific line: "{quoted}"' if quoted else ""
    prof = {k: v for k, v in (profile or {}).items()
            if k in ("name", "communication_style", "vibe", "interests", "job")}
    # Closed vocabulary for `about`. Free-form labels drift ("Bitcoin ETF record"
    # vs "crypto ETF headlines") and never group, so repeated dislikes on the same
    # subject would never reach the avoid threshold.
    topics = [t for t in ((profile or {}).get("morning_topics") or []) if t]

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=120,
            messages=[{"role": "user", "content": _INTERPRET_PROMPT.format(
                last_assistant=last_assistant[:600],
                label=label,
                quoted_line=quoted_line,
                profile=prof or "nothing yet",
                topics="\n".join(f"- {t}" for t in topics) if topics else "- (none set)",
            )}],
        )
        parsed = _parse_json(response.content[0].text)
    except Exception as e:
        print(f"interpret_reaction failed: {type(e).__name__}: {e}")
        return _fallback_verdict(reaction)

    if not isinstance(parsed, dict):
        return _fallback_verdict(reaction)

    function = str(parsed.get("function", "")).strip().lower()
    if function not in VALID_FUNCTIONS:
        return _fallback_verdict(reaction)
    sentiment = str(parsed.get("sentiment", "")).strip().lower()
    if sentiment not in ("positive", "negative", "neutral"):
        sentiment = reaction.get("sentiment", "neutral")

    return {
        "function": function,
        "sentiment": sentiment,
        "about": str(parsed.get("about", "") or "")[:40],
        "needs_reply": function == "answer",
    }


_CONSOLIDATE_PROMPT = """These are reactions someone sent instead of replying — the clearest signal there is about which of Palmer's messages land with them and which miss.

Reactions (newest last):
{log}

Current communication_style: {style}

Update communication_style to account for what these reactions show — what register works with this person, what falls flat. Keep it one dense line, keep whatever still holds, drop what these reactions contradict. If they add nothing new, return the existing value unchanged.

Return ONLY: {{"communication_style": "..."}}"""


def maybe_consolidate(phone: str) -> None:
    """Fold accumulated reactions into communication_style every CONSOLIDATE_EVERY.

    Reactions never reach agent._update_profile (they short-circuit before
    get_reply), so without this the log rolls over and the signal is lost.
    Never raises."""
    try:
        profile = get_profile(phone)
        log = profile.get("reactions") or []
        seen = int(profile.get("reactions_folded_count") or 0)
        if len(log) < CONSOLIDATE_EVERY or (len(log) - seen) < CONSOLIDATE_EVERY:
            return

        lines = "\n".join(
            f"- {r.get('emoji') or r.get('kind')} ({r.get('sentiment')}) on: {r.get('quoted') or r.get('about') or '?'}"
            for r in log
        )
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": _CONSOLIDATE_PROMPT.format(
                log=lines, style=profile.get("communication_style") or "unknown",
            )}],
        )
        parsed = _parse_json(response.content[0].text)
        updates = {"reactions_folded_count": len(log)}
        if isinstance(parsed, dict):
            style = str(parsed.get("communication_style", "") or "").strip()
            if style:
                updates["communication_style"] = style[:400]
        upsert_profile(phone, updates)
    except Exception as e:
        print(f"maybe_consolidate failed for {phone}: {e}")


def _negatives(log: list) -> list:
    return [r for r in log
            if r.get("sentiment") == "negative" or r.get("function") == "objection"]


def pacing_factor(profile: dict) -> float:
    """How much to slow unprompted messages down for this person. 1.0 = normal.

    Read off the rolling reaction log, so it decays on its own: as positive
    reactions push old negatives out of the window, the factor returns to 1.0
    without needing a reset job."""
    log = (profile or {}).get("reactions") or []
    factor = 1.0 + 0.5 * len(_negatives(log))
    return min(factor, MAX_PACING_FACTOR)


def maybe_learn_preferences(phone: str) -> None:
    """Drop a topic from morning briefings after repeated negative reactions.

    morning.py already reads morning_prefs["avoid"] (and protects weather and
    safety via always_include); this is the first writer. Sets a pending notice
    so Palmer can mention the change rather than silently going quiet on a
    topic — an unexplained disappearance is worse than the noise it prevents.
    Never raises."""
    try:
        profile = get_profile(phone)
        log = profile.get("reactions") or []
        prefs = dict(profile.get("morning_prefs") or {})
        avoid = list(prefs.get("avoid") or [])

        # Only topics actually in their briefing can be dropped from it. This also
        # discards the drifting free-form labels the model emits for one-offs.
        briefing = {t.strip().lower(): t for t in (profile.get("morning_topics") or []) if t}
        counts: dict[str, int] = {}
        for r in _negatives(log):
            topic = (r.get("about") or "").strip().lower()
            if topic in briefing:
                counts[topic] = counts.get(topic, 0) + 1

        already = {a.strip().lower() for a in avoid}
        added = [briefing[t] for t, n in counts.items()
                 if n >= NEGATIVE_STREAK_FOR_AVOID and t not in already]
        if not added:
            return

        prefs["avoid"] = avoid + added
        upsert_profile(phone, {
            "morning_prefs": prefs,
            "pending_preference_notice": added[0],
        })
        print(f"Learned avoid-topic(s) for {phone}: {added}")
    except Exception as e:
        print(f"maybe_learn_preferences failed for {phone}: {e}")


def learn_from_reactions(phone: str) -> None:
    """Everything a reaction should teach, in one call. Never raises."""
    maybe_consolidate(phone)
    maybe_learn_preferences(phone)


def reaction_block(profile: dict) -> str:
    """Calibration evidence for _build_system. Empty string when there's none."""
    log = (profile or {}).get("reactions") or []
    if not log:
        return ""
    lines = []
    for r in log[-5:]:
        label = r.get("emoji") or r.get("kind", "reacted")
        quoted = r.get("quoted") or ""
        lines.append(f"- {label} on: {quoted}" if quoted else f"- {label}")
    return (
        "\n\nHOW THEY'VE REACTED (they tapped a reaction instead of replying — "
        "the clearest read you get on what actually lands with them; a like on a "
        "dry line means keep going, a dislike means that register missed):\n"
        + "\n".join(lines)
    )
