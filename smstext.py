"""SMS text hygiene: cleaning, shortening, and small parse helpers.

All outbound text passes through _sms_clean (see sms_util.send_sms).
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
})

def _sms_clean(text: str) -> str:
    """Normalize Unicode to ASCII and strip markdown so messages render cleanly as SMS.
    Does not enforce a length limit — sms_util.send_sms handles chunking long text into
    multiple messages instead of truncating it."""
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
    return text.strip()

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

def shorten_message(text: str, max_chars: int = 320) -> str:
    """Use Haiku to shorten a message that failed to send."""
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=150,
            messages=[{"role": "user", "content": f"Shorten this to under {max_chars} characters. Keep the key point, cut everything else. No explanation, just the shortened message:\n\n{text}"}],
        )
        result = _sms_clean(response.content[0].text.strip())
        return result[:max_chars] if len(result) > max_chars else result
    except Exception:
        return _sms_clean(text)[:max_chars]
