"""First-run setup: the form a new user fills in, on the address that then
becomes their page.

The problem this solves is not cosmetic. Until now the only way a fact reached
a profile was `userprofile._update_profile` — a Haiku pass reading the chat
*after* the reply had already gone out. That ordering is the root of several
separate defects: `city` lands too late for `update_morning_briefing` to seed
local news, the name/city question can mark itself asked without asking, and a
model that shrugs writes nothing at all. A form has none of those properties.
The user types the field, the field is written, and everything downstream reads
a value that is simply there.

The address is the one the user already has. `/h/{token}` renders this form
while the token's payload says `setup_pending`, and their real page forever
after — one link, sent once, that turns into the thing. That is also the whole
security model: the token is the only protection the page has ever had (see
home.py), and a form turns a read key into a write key, so the write is
**one-shot**. `apply()` clears `setup_pending` before it does anything else, and
a second POST to the same token is refused. A forwarded screenshot can no more
overwrite a profile than it can today.

The link goes out at the setup moment, not on message one. `get_my_page`'s
dispatch in `agent.py` calls `start()` when the profile has no city — after the
what-I-do list, or when someone says "set me up" before Palmer knows where they
are — and the model closes its reply with the URL exactly as it does for a
built page. A stranger's first "hey" gets a hello, not a form. Everyone who
already has a city has a page, so nothing here reaches existing users. The
conversational name/city ask in `agent._build_system` stays exactly as it is,
because plenty of people will not tap a link from an unknown number, and for
them nothing about onboarding has changed.
"""
from __future__ import annotations

import html
import re
import threading

import brand

# "What do you follow" is one free-text box, not a row of chips. A chip's
# label is a category ("Sports", "Health") and a category is exactly the kind
# of topic the search answers worst — CLAUDE.md's own numbers: "US politics"
# tops out at 0.36 relevance and never clears the floor, while "Philadelphia
# Eagles" and "Nvidia stock" return something most days and light up Markets.
# A box people type into gets the specific thing they actually follow, in the
# same words they would text Palmer, so the topic is subject-shaped from the
# start. Each entry goes through `agent._normalize_price_topic`, the same pass
# a texted topic gets, so "nvidia stock" resolves to a ticker here too.

# Total topics kept from setup. morning.MAX_TOPICS pulls six per briefing, so
# anything past that is stored and never read on a given day.
TOPIC_MAX = 6
FOLLOWS_MAX = 200

NAME_MAX = 60
CITY_MAX = 80


def needs_setup(payload: dict | None) -> bool:
    """True while this token's address should render the form rather than a page."""
    return bool(payload and payload.get("setup_pending"))


def start(phone: str) -> str | None:
    """Mint the user's token, park a stub payload on it, and return the URL.

    The stub is what makes the token resolvable: `/h/{token}` has always
    404'd on a token with no payload, and it needs the phone number to know
    whose form this is. Returns None when there is nowhere to serve the page,
    so the caller sends an ordinary intro with no link rather than a dead one.
    Never raises — it sits in the path of a user's very first reply."""
    from links import configured, page_url
    if not configured():
        return None
    try:
        from home import home_token, load, save
        token = home_token(phone)
        if load(token) is None:
            save(token, {"phone": phone, "setup_pending": True})
        return page_url(token)
    except Exception as e:
        print(f"onboard.start failed for {phone}: {type(e).__name__}: {e}")
        return None


def apply(token: str, payload: dict, form: dict) -> bool:
    """Write a submitted form to the profile and start the page build.

    Returns False if this token has already been submitted — the one-shot rule
    above. Topics are stored before name and city so that `_seed_local_topic`,
    which fires inside `_apply_profile_updates` when the city first lands, sees
    an onboarded list to add to. The eager page build there is skipped — the
    parked stub is still on the token at that point — and `_build_async` below
    is the one build."""
    from home import save
    from userprofile import _apply_profile_updates
    from db import get_profile

    phone = payload.get("phone")
    if not phone or not payload.get("setup_pending"):
        return False
    # Close the window first. A double submit — a double-tap, a refresh, a
    # forwarded link — must not be able to rewrite a profile.
    save(token, {"phone": phone, "setup_pending": False})

    name = (form.get("name") or "").strip()[:NAME_MAX]
    city = (form.get("city") or "").strip()[:CITY_MAX]
    follows = (form.get("follows") or "")[:FOLLOWS_MAX]
    mornings = bool(form.get("mornings"))

    profile = get_profile(phone) or {}
    topics = _topics_for(city, follows)
    updates: dict = {"morning_topics": topics, "morning_onboarded": True,
                     "morning_enabled": mornings, "setup_done": True}
    from db import upsert_profile
    upsert_profile(phone, updates)

    # name/city go through _apply_profile_updates rather than a direct write so
    # they pick up the timezone derivation that lives there — without it the
    # morning job has no local clock to aim at and every local_today() call in
    # the codebase quietly degrades to UTC.
    ident = {k: v for k, v in (("name", name), ("city", city)) if v}
    if ident:
        _apply_profile_updates(phone, profile, ident)

    _build_async(phone)
    return True


def _topics_for(city: str, follows: str) -> list[str]:
    """The seeded topic list: the local + national baseline, then what they typed.

    `morning.default_topics` is called with the city the user just typed, which
    is the whole point of collecting it here — the same call made from
    `update_morning_briefing`'s dispatch runs before the extractor has written
    a city and so silently seeds national news alone."""
    from morning import default_topics
    topics = list(default_topics(city or None))
    for item in split_follows(follows):
        if not any(item.lower() == t.lower() for t in topics):
            topics.append(item)
    return topics[:TOPIC_MAX]


def split_follows(text: str) -> list[str]:
    """"Eagles, Nvidia stock, AI" -> three topics, each normalized the way a
    texted one is. Commas and newlines separate; "and" does not, because
    "Simon and Garfunkel" is one thing."""
    out: list[str] = []
    for raw in re.split(r"[,\n;]+", text or ""):
        item = raw.strip(" .")[:60]
        if not item:
            continue
        try:
            from agent import _normalize_price_topic
            item = _normalize_price_topic(item)
        except Exception:
            pass
        if item.lower() not in {o.lower() for o in out}:
            out.append(item)
    return out


def _build_async(phone: str) -> None:
    """Build the real page off the request thread.

    A full rebuild is two paid searches and a Haiku curation pass — ten to
    thirty seconds — and the user is holding a phone waiting for a page. The
    POST redirects immediately and `/h/{token}` shows the holding page until
    `built_at` appears."""
    def _run():
        try:
            from home import rebuild
            rebuild(phone, refresh_news=True)
        except Exception as e:
            print(f"onboard build failed for {phone}: {type(e).__name__}: {e}")
    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------- rendering

# Deliberately page.py's stylesheet, not a second one. This address becomes
# their page the moment they submit, and a form that looks like a different
# product until then would make the transformation read as a redirect to
# somewhere else.
_FORM_CSS = """
form{margin-top:26px}
.field{margin-bottom:20px}
.field label{display:block;font-family:var(--mono);font-size:11px;letter-spacing:.13em;
 text-transform:uppercase;color:var(--ink2);margin-bottom:7px}
.field input[type=text]{width:100%;font:16px/1.4 var(--serif);color:var(--ink);
 background:transparent;border:0;border-bottom:1px solid var(--ink);padding:8px 2px}
.field input[type=text]:focus{outline:0;border-bottom-width:2px}
.hint{font-family:var(--mono);font-size:10px;color:var(--ink2);margin-top:6px;
 letter-spacing:.04em}
.toggle{display:flex;align-items:flex-start;gap:10px;font:15px/1.45 var(--serif)}
.toggle input{margin-top:3px;accent-color:var(--ink)}
button{width:100%;margin-top:10px;padding:15px 16px;background:var(--ink);
 color:var(--paper);border:0;font-family:var(--serif);font-weight:700;font-size:16px;
 letter-spacing:.02em;cursor:pointer}
button:active{opacity:.7}
.intro{font:16px/1.55 var(--serif);color:var(--ink2);margin-top:22px}
"""


def render_setup(token: str, *, action: str) -> str:
    """The form. No JS, no external requests — same constraints as page.py,
    because it opens on a phone over a cell connection and nowhere else."""
    from page import CSS
    e = lambda v: html.escape(str(v), quote=True)  # noqa: E731
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="robots" content="noindex,nofollow">'
        '<meta name="referrer" content="no-referrer">'
        f'<meta name="theme-color" content="{brand.PAPER}">'
        "<title>Set up Palmer</title>"
        f"<style>{CSS}{_FORM_CSS}</style></head><body><div class=wrap>"
        "<div class=masthead><div class=eyebrow>Palmer</div>"
        "<hr class=rule>"
        "<div class=date>Setting up</div></div>"
        "<p class=intro>Three things and this page becomes yours &mdash; weather where "
        "you actually are, what is worth doing near you, and the stuff you care about.</p>"
        f'<form method=post action="{e(action)}">'
        '<div class=field><label for=name>What should Palmer call you</label>'
        '<input type=text id=name name=name autocomplete="given-name" '
        'autocapitalize=words maxlength=60 required></div>'
        '<div class=field><label for=city>City and state</label>'
        '<input type=text id=city name=city autocomplete="address-level2" '
        'autocapitalize=words maxlength=80 placeholder="Austin, TX" required>'
        '<div class=hint>Sets your forecast, your commute and your local news</div></div>'
        '<div class=field><label for=follows>What do you follow</label>'
        '<input type=text id=follows name=follows autocapitalize=words maxlength=200 '
        'placeholder="Eagles, Nvidia stock, AI">'
        "<div class=hint>Teams, stocks, subjects &mdash; a few words each, commas between. "
        "You can add more by texting him</div></div>"
        "<div class=field><label class=toggle><input type=checkbox name=mornings checked>"
        "<span>Text me a short rundown every morning at 7</span></label></div>"
        "<button type=submit>Build my page</button>"
        "</form>"
        "<div class=foot>Palmer &middot; no account, no password</div>"
        "</div></body></html>"
    )


def render_building(page_url: str) -> str:
    """Shown between submit and the first real payload. Refreshes itself rather
    than making the user tap anything — the build is seconds away and the point
    of the flow is that the page appears without further work."""
    from page import CSS
    e = lambda v: html.escape(str(v), quote=True)  # noqa: E731
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        '<meta name="referrer" content="no-referrer">'
        f'<meta http-equiv="refresh" content="4;url={e(page_url)}">'
        "<title>Building your page</title>"
        f"<style>{CSS}{_FORM_CSS}</style></head><body><div class=wrap>"
        "<div class=masthead><div class=eyebrow>Palmer</div>"
        "<hr class=rule><div class=date>Building your page</div></div>"
        "<p class=intro>Pulling your weather, your local news and what is on near you. "
        "This takes a few seconds &mdash; it will load itself.</p>"
        "</div></body></html>"
    )
