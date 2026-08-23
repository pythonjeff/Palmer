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


def render_png(token: str, payload: dict) -> bytes:
    """The payload as a card, memoised per token."""
    with _cache_lock:
        hit = _png_cache.get(token)
    if hit is not None:
        return hit
    from cards import render_dashboard
    png = render_dashboard(
        city=payload.get("city", ""),
        weather=payload.get("weather"),
        traffic=payload.get("traffic"),
        prices=payload.get("prices"),
        headlines=[h.get("title", "") for h in (payload.get("headlines") or [])],
    )
    with _cache_lock:
        _png_cache[token] = png
        if len(_png_cache) > 64:          # bounded; single dyno, low volume
            _png_cache.pop(next(iter(_png_cache)))
    return png
