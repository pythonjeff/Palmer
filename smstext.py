"""SMS text hygiene: cleaning, shortening, and small parse helpers.

All outbound text passes through _sms_clean (see sms_util.send_sms).

A URL is not prose and must survive this module untouched. That was not true
until recently and it is the single reason "bad links" was a complaint: the
markdown scrub deleted a link's target outright, and the ASCII fold silently
dropped non-ASCII bytes out of the middle of a path, producing a URL that looks
fine and goes nowhere. Every transform here now holds URLs out and puts them
back, and the two truncating paths refuse to cut inside one.
"""
import re
from datetime import datetime, timezone

from llm import client, HAIKU_MODEL


_UNICODE_MAP = str.maketrans({
    '‘': "'", '’': "'",   # curly single quotes
    '“': '"', '”': '"',   # curly double quotes
    '–': '-', '—': '-',   # en/em dash
    '…': '...', '·': '.', # ellipsis, middle dot
    '•': '-', ' ': ' ',   # bullet, non-breaking space
    '→': '->', '×': 'x',  # arrow, multiplication sign
    '≈': '~', '°': ' deg',  # approx, degree
})

# The one definition of "a URL" in this module. Trailing sentence punctuation is
# not part of it — "see https://x.com/a." ends a sentence.
URL_RE = re.compile(r"""https?://[^\s<>"']+[^\s<>"'.,;:!?)\]]""")


def _protect_urls(text: str) -> tuple[str, list[str]]:
    """Swap every URL for a sentinel that survives the ASCII fold, unharmed.

    NUL is used deliberately: it is ASCII, so `encode('ascii', 'ignore')` keeps
    it, and no drafter will ever emit one."""
    found: list[str] = []

    def _stash(m):
        found.append(m.group(0))
        return f"\x00{len(found) - 1}\x00"

    return URL_RE.sub(_stash, text), found


def _restore_urls(text: str, found: list[str]) -> str:
    """Put the URLs back, percent-encoded so a non-ASCII path is a valid ASCII
    URL rather than the silently-truncated wrong one the fold used to leave."""
    from urllib.parse import quote
    for i, url in enumerate(found):
        safe = quote(url, safe=":/?#[]@!$&\'()*+,;=%~")
        text = text.replace(f"\x00{i}\x00", safe)
    return text

def _sms_clean(text: str) -> str:
    """Normalize Unicode to ASCII and strip markdown so messages render cleanly as SMS.
    Does not enforce a length limit — sms_util.send_sms handles chunking long text into
    multiple messages instead of truncating it."""
    # Markdown links first, and BEFORE the URL stash, so the target is kept
    # rather than deleted. The old rule was `[text](anything) -> text`, which
    # silently threw the URL away: a reply reading "[your page](https://...)"
    # reached the user as "your page" and no link, with nothing logged. Keep an
    # http(s) target; a non-http one (`[x](#)`) is still dropped below.
    text = re.sub(r'\[([^\]]+)\]\((https?://[^)\s]+)\)', r'\1 \2', text)

    # Hold URLs out of everything that follows. The ASCII fold in particular
    # deletes non-ASCII bytes rather than failing, which turns a working link
    # into a plausible-looking dead one.
    text, _urls = _protect_urls(text)

    text = text.translate(_UNICODE_MAP)
    text = text.encode('ascii', 'ignore').decode('ascii')  # strip emoji and remaining non-GSM-7
    # Strip tool-call / tool-response blocks that background drafters can
    # accidentally leak when Sonnet gets the full system prompt (with tool
    # routing rules) via a plain messages.create call that has no tools= array.
    # Without this scrub, a thin summary would produce SMS text containing raw
    # <tool_call>...</tool_call> XML plus a fabricated <tool_response>.
    text = re.sub(r'<tool_(?:call|response|use|result)>.*?</tool_(?:call|response|use|result)>',
                  '', text, flags=re.DOTALL | re.IGNORECASE)
    # Strip markdown
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    return _restore_urls(text, _urls).strip()

def _normalize_hhmm(raw) -> str | None:
    """Validate a 24-hour HH:MM string; returns zero-padded form or None."""
    m = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$", str(raw))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None

def _parse_published(value) -> datetime | None:
    """Parse a Tavily published_date into an aware UTC datetime, or None."""
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(str(value))
    except Exception:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def truncate_preserving_urls(text: str, max_chars: int) -> str:
    """Trim to max_chars without ever cutting inside a URL.

    A bare `text[:320]` is how a link became garbage: the slice lands mid-path
    and ships half a URL, which reads as a link and resolves to nothing. If a
    single URL cannot fit the budget it is returned alone — the link is the
    payload, and half of it is worth less than none of the prose."""
    if not text or len(text) <= max_chars:
        return text
    spans = [m.span() for m in URL_RE.finditer(text)]
    cut = max_chars
    for start, end in spans:
        if start < cut < end:
            # The cut lands inside this URL: keep the whole thing if it fits
            # from the start of the message, otherwise stop before it.
            cut = end if end <= max_chars else start
            break
    trimmed = text[:cut].rstrip()
    if not trimmed and spans:
        return text[spans[0][0]:spans[0][1]]
    return trimmed


def shorten_message(text: str, max_chars: int = 320) -> str:
    """Use Haiku to shorten a message that failed to send.

    URLs never reach the model. Nothing in the old prompt told Haiku a link was
    inviolable, so it would paraphrase one away or truncate it - and this runs
    on the /sms-status retry path, i.e. on messages that already went out once
    carrying a link the user was meant to tap. Asking the model to preserve a
    placeholder was tried and is a worse bet than not asking it at all: a
    dropped marker loses the link silently.

    So the prose is shortened alone and the links are re-appended after it, last
    and in order - the same shape morning.py uses, and the one that lets a
    message app draw a link preview."""
    urls = URL_RE.findall(text or "")
    prose = URL_RE.sub(" ", text or "")
    prose = " ".join(prose.split())
    tail = (" " + " ".join(urls)) if urls else ""
    budget = max_chars - len(tail)

    if budget < 40:
        # No room to shorten prose around the links; the links are the payload.
        return truncate_preserving_urls(_sms_clean(text), max_chars)
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=150,
            messages=[{"role": "user", "content": f"Shorten this to under {budget} characters. Keep the key point, cut everything else. No explanation, just the shortened message:\n\n{prose}"}],
        )
        short = _sms_clean(response.content[0].text.strip())
    except Exception:
        short = _sms_clean(prose)
    short = truncate_preserving_urls(short, budget).rstrip()
    return _sms_clean(short + tail)
