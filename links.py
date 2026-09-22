"""Every public URL Palmer hands out, built in one place.

There were six of these f-strings — `home.rotate`, `home.ensure_fresh`,
`onboard.start`, both `/h/` handlers in `main.py`, plus a dead pair in
`artifacts.py` — each spelling `{APP_URL}/h/{token}` by hand. They agreed, but
only by coincidence: this repo has already paid for a caller composing a shape
it did not own (the card's cache key, keyed on `built_at` by its caller and
frozen for good). A URL shape is the same kind of thing. Changing it should be
one edit, not six, and the six should be impossible to find out of step.

TWO HOSTS, DELIBERATELY, AND THEY ARE NOT INTERCHANGEABLE
---------------------------------------------------------
`APP_URL` is where the app actually runs. Twilio's status callbacks post to it
and Twilio must be able to reach it; it is infrastructure, and it is not
addressed to a person.

`LINK_DOMAIN` is the host a *user* reads in a text message. It is optional and
falls back to `APP_URL`, so nothing here changes until a domain exists, and
when one does it is one environment variable rather than a code change.

THE SHORT DOMAIN MUST SERVE, NOT REDIRECT
------------------------------------------
This is the constraint that decides what `LINK_DOMAIN` may point at, so it is
recorded here rather than in a runbook nobody reads.

A link preview is drawn by a scraper fetching the URL and reading the og tags
out of the HTML `<head>`. Put a redirecting shortener in front of that — the
Bitly/Twilio-Link-Shortening shape, where the short URL 30x's to the real one —
and the preview depends on the scraper chasing the redirect and attributing the
result to the short URL. Some do; iMessage's is the one that matters and it is
the least forgiving thing in this pipeline already (see the `?v=` fingerprint
on the og:image). A redirect is the standard way a working preview silently
stops working, and the preview is most of the value of the morning send.

So `LINK_DOMAIN` is an ALIAS for the app — a CNAME onto the same dyno, serving
`/h/{token}` itself with a real certificate. It is not a shortener, and Twilio
Link Shortening in particular must stay off the Messaging Service that carries
these links: it rewrites URLs in the body into its own redirecting host, which
is exactly the failure above.

WHY THE TOKEN IS NOT SHORTENED
-------------------------------
A branded shortlink's path is ~10 characters; Palmer's is 22. That gap is not
worth closing. Reebok's link is disposable, scoped to one order, and expires;
Palmer's is a permanent unauthenticated key to a page naming someone's city,
their commute and the hour they leave the house. The token IS the authentication
(see artifacts.py), so its 128 bits are a security property and not a style
choice. The length that a reader actually notices is the host, and that is the
half this module makes changeable.
"""
from __future__ import annotations

import os
from urllib.parse import quote


def _app_base() -> str:
    """Where the app runs. Twilio callbacks, and the fallback for reader links.

    Read per call rather than captured at import: the tests set APP_URL around
    individual cases, and a module-level constant would freeze whichever value
    happened to be set when the first import ran.
    """
    return os.environ.get("APP_URL", "").rstrip("/")


def public_base() -> str:
    """The host a user sees. LINK_DOMAIN when set, else wherever the app runs.

    A bare domain in LINK_DOMAIN is promoted to https. The scheme is not
    optional: iMessage refuses to load a preview image over http, so a link
    that lost its scheme would still resolve and still never draw a card.
    """
    domain = os.environ.get("LINK_DOMAIN", "").strip().rstrip("/")
    if not domain:
        return _app_base()
    if not domain.startswith(("http://", "https://")):
        domain = f"https://{domain}"
    return domain.rstrip("/")


def page_url(token: str) -> str:
    """A user's Palmer Home. The one URL that goes out in a text message."""
    return f"{public_base()}/h/{token}"


def image_url(token: str, stamp: str | None = None) -> str:
    """The og:image for a user's page.

    `stamp` is the card's content fingerprint and belongs in the URL rather
    than in a header. Link-preview scrapers cache og:images by URL and have no
    reason to refetch one they have already seen, so a fixed
    `/h/{token}.png` meant every morning's message showed whichever card was
    scraped first. A fingerprint makes each new day a different image to a
    cache, and an unchanged day stays cheap because the fingerprint is
    unchanged too.
    """
    base = f"{public_base()}/h/{token}.png"
    return f"{base}?v={stamp}" if stamp else base


def vcard_url() -> str:
    """Palmer's contact card. One address for everyone — it carries the sending
    number and the brand mark, and nothing about the person fetching it."""
    return f"{public_base()}/palmer.vcf"


def icon_url(size: int | None = None) -> str:
    """The brand mark as a PNG, for apple-touch-icon and the vCard photo."""
    return f"{public_base()}/icon.png" + (f"?s={size}" if size else "")


def sms_link(body: str) -> str | None:
    """An `sms:` URI that opens Messages to Palmer with `body` already typed.

    The page has no auth and takes exactly one POST, so this is how it offers an
    edit control at all: the affordance is a pre-written text back to Palmer.

    quote(), never quote_plus(): the sms: scheme has no form encoding, so a "+"
    is a literal plus. quote_plus once sent people into Messages with
    "My+name+is+" already in the box, and that is exactly what Palmer received.

    The separator is `?&`, which looks like a typo and is not. iOS wants
    `sms:<number>&body=`, Android wants `sms:<number>?body=`, and `?&` is the
    one spelling both parse. Both call sites in page.py already used it; it is
    preserved here verbatim rather than tidied.

    Returns None when there is no number to text, so a caller renders plain text
    rather than a dead tap target.
    """
    number = os.environ.get("TWILIO_PHONE_NUMBER", "")
    if not number:
        return None
    return f"sms:{number}?&body={quote(body)}"


def configured() -> bool:
    """True when there is somewhere to serve a page from.

    Callers gate on this before promising a link: an unset APP_URL yields a
    relative path, and a user is never handed a link to nothing."""
    return bool(_app_base())
