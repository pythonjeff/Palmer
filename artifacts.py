"""Public, unguessable URLs for rendered artifacts.

Both Twilio (fetching MMS media) and the recipient's phone (fetching an
og:image) require URLs that are publicly reachable and unauthenticated. There is
no way around that, so the token is the whole protection:

  * 128 bits of CSPRNG entropy in the path
  * a TTL, after which the row reads as missing
  * nothing in an artifact that the user did not already receive over SMS

These are read-only artifacts, not credentials. Nothing is consumed on fetch and
no session is minted, so the link-prefetch problem that breaks magic links does
not apply here — that arrives later, if the page ever authenticates.
"""
from __future__ import annotations

import os
import secrets

from db import save_artifact, get_artifact

_APP_URL = os.environ.get("APP_URL", "").rstrip("/")

TTL_HOURS = 48


def new_token() -> str:
    """128-bit URL-safe token."""
    return secrets.token_urlsafe(16)


def publish_png(body: bytes, ttl_hours: int = TTL_HOURS) -> tuple[str, str]:
    """Store a PNG and return (token, absolute_url). The URL ends in .png
    because some carriers sniff the extension rather than the content type."""
    token = new_token()
    save_artifact(token, "png", body, ttl_hours=ttl_hours)
    return token, f"{_APP_URL}/c/{token}.png"


def fetch(token: str) -> tuple[str, bytes] | None:
    return get_artifact(token)
