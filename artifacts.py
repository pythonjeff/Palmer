"""Public, unguessable URLs for a briefing artifact.

One token, one payload, two renderings:

  /c/{token}       the interactive page — headlines and tickers are real links
  /c/{token}.png   the same briefing as a flat card, for MMS and og:image

Storing the *payload* rather than the pixels is what makes the page possible. An
MMS image is a bitmap with no tap targets anywhere in it, so interactivity can
only live on a page; keeping one source of truth means the card and the page can
never disagree.

Both URLs are public and unauthenticated by necessity — Twilio fetches MMS media
and the recipient's phone fetches the og:image, neither of which can carry auth.
So the token is the whole protection:

  * 128 bits of CSPRNG entropy in the path
  * a TTL, after which the row reads as missing
  * nothing in a briefing that the user did not already receive over SMS

These are read-only artifacts, not credentials. Nothing is consumed on fetch and
no session is minted, so the link-prefetch problem that breaks magic links does
not apply — that arrives later, if the page ever authenticates.
"""
from __future__ import annotations

import json
import os
import secrets
import threading

from db import save_artifact, get_artifact

_APP_URL = os.environ.get("APP_URL", "").rstrip("/")

TTL_HOURS = 48

# Rendered cards, keyed by token. The PNG is derived from the payload rather
# than stored, so the two can't drift; this just avoids re-rendering on every
# fetch, since Twilio and the phone both pull the same image.
_png_cache: dict[str, bytes] = {}
_cache_lock = threading.Lock()


def new_token() -> str:
    """128-bit URL-safe token."""
    return secrets.token_urlsafe(16)


def publish(payload: dict, ttl_hours: int = TTL_HOURS) -> tuple[str, str]:
    """Store a briefing payload. Returns (token, page_url)."""
    token = new_token()
    save_artifact(token, "briefing", json.dumps(payload).encode(), ttl_hours=ttl_hours)
    return token, page_url(token)


def page_url(token: str) -> str:
    return f"{_APP_URL}/c/{token}"


def image_url(token: str) -> str:
    # .png suffix because some carriers sniff the extension over the content type
    return f"{_APP_URL}/c/{token}.png"


def load(token: str) -> dict | None:
    got = get_artifact(token)
    if not got:
        return None
    kind, body = got
    if kind != "briefing":
        return None
    try:
        return json.loads(body.decode())
    except Exception:
        return None


def _card_inputs(payload: dict) -> dict:
    """Exactly what render_dashboard draws — nothing else."""
    from datetime import datetime
    return {
        "city": payload.get("city", ""),
        "weather": payload.get("weather"),
        "traffic": payload.get("traffic"),
        "prices": payload.get("prices"),
        "opening": payload.get("opening"),
        "headlines": [h.get("title", "") for h in (payload.get("headlines") or [])],
        # The masthead prints the date, so a new day is a different card even
        # when every other input is byte-identical.
        "_date": datetime.now().strftime("%Y-%m-%d"),
    }


def _card_fingerprint(payload: dict) -> str:
    """A key that changes exactly when the drawn image would change.

    The cache used to key on `built_at`, which only advances inside
    home.rebuild() — and ensure_fresh calls rebuild only when there is no
    payload at all. So after a user's very first build the key never changed
    again: the card froze on that morning's weather and stayed frozen, while
    the page beside it refreshed normally. Hashing the drawn inputs instead
    means the image regenerates when it would look different and never
    otherwise, which is what the cache was for."""
    import hashlib
    import json
    body = json.dumps(_card_inputs(payload), sort_keys=True, default=str)
    return hashlib.sha1(body.encode()).hexdigest()[:16]


def render_png(token: str, payload: dict) -> bytes:
    """The payload as a card, memoised on the token plus the drawn content.

    The caller passes the bare token — deriving the rest here is deliberate,
    since a caller composing its own key is exactly how the card came to be
    cached against a value that never changed."""
    key = f"{token}:{_card_fingerprint(payload)}"
    with _cache_lock:
        hit = _png_cache.get(key)
    if hit is not None:
        return hit
    from cards import render_dashboard
    inputs = _card_inputs(payload)
    inputs.pop("_date", None)
    png = render_dashboard(**inputs)
    with _cache_lock:
        _png_cache[key] = png
        if len(_png_cache) > 64:          # bounded; single dyno, low volume
            _png_cache.pop(next(iter(_png_cache)))
    return png
