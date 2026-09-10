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

New users only, and that falls out of the gate rather than being enforced here:
`main._handle_sms_inner` appends the link on the FIRST inbound message alone, so
every existing user — all of whom already carry `intro_sent` — never sees it.
The conversational ask in `agent._build_system` stays exactly as it is, because
plenty of people will not tap a link from an unknown number, and for them
nothing about onboarding has changed.
"""
from __future__ import annotations

import html
import os
import threading

# Interest chips. Each is (key, label, topic).
#
# `label` is what the human taps; `topic` is what gets stored in
# `morning_topics` — and they are deliberately not the same string. A topic is
# not a tag, it is a **search query**: `datafeeds._search_raw` matches on query
# text, so phrasing decides whether the topic returns anything at all. The
# repository's own scar tissue on this is specific — "Top national news"
# returned "Clemson Army ROTC earns top national honors" on a literal word
# match, and "world news" and "breaking news" return nothing whatsoever. So the
# labels stay human and the topics stay subject-shaped, in the same voice as
# `morning.default_topics`, which is the one phrasing already proven against
# the live index.
#
# Keep this list short. Every topic is one Tavily search per briefing and
# `morning.MAX_TOPICS` only pulls six, so a long list costs money and pushes
# everything else into rotation. TOPIC_MAX below is the real bound.
INTERESTS: tuple[tuple[str, str, str], ...] = (
    ("tech", "Tech & AI", "AI and technology news"),
    ("business", "Business", "Business and markets news"),
    ("sports", "Sports", "Sports news"),
    ("science", "Science", "Science and research news"),
    ("music", "Music", "Music industry news"),
    ("film", "Film & TV", "Film and television industry news"),
    ("food", "Food", "Restaurant and food news"),
    ("health", "Health", "Health and medicine news"),
)

_INTEREST_TOPICS = {key: topic for key, _, topic in INTERESTS}

# Total topics kept from setup. morning.MAX_TOPICS pulls six per briefing, so
# anything past that is stored and never read on a given day.
TOPIC_MAX = 6

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
    app_url = os.environ.get("APP_URL", "").rstrip("/")
    if not app_url:
        return None
    try:
        from home import home_token, load, save
        token = home_token(phone)
        if load(token) is None:
            save(token, {"phone": phone, "setup_pending": True})
        return f"{app_url}/h/{token}"
    except Exception as e:
        print(f"onboard.start failed for {phone}: {type(e).__name__}: {e}")
        return None


def apply(token: str, payload: dict, form: dict) -> bool:
    """Write a submitted form to the profile and start the page build.

    Returns False if this token has already been submitted — the one-shot rule
    above. The write order matters: topics are stored BEFORE name and city, so
    that when `_apply_profile_updates` fires `_eager_build_home` on the city
    landing for the first time, the topic list it builds against is the one the
    user just chose rather than an empty one."""
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
    picked = [k for k in form.getlist("interests")] if hasattr(form, "getlist") else list(
        form.get("interests") or [])
    mornings = bool(form.get("mornings"))

    profile = get_profile(phone) or {}
    topics = _topics_for(city, picked)
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


def _topics_for(city: str, picked: list[str]) -> list[str]:
    """The seeded topic list: the local + national baseline, then their picks.

    `morning.default_topics` is called with the city the user just typed, which
    is the whole point of collecting it here — the same call made from
    `update_morning_briefing`'s dispatch runs before the extractor has written
    a city and so silently seeds national news alone."""
    from morning import default_topics
    topics = list(default_topics(city or None))
    for key in picked:
        topic = _INTEREST_TOPICS.get(key)
        if topic and topic not in topics:
            topics.append(topic)
    return topics[:TOPIC_MAX]


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
.picks{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}
.picks label{display:inline-block;border:1px solid var(--rule);border-radius:2px;
 padding:8px 12px;font-family:var(--mono);font-size:12px;letter-spacing:.05em;
 text-transform:uppercase;color:var(--ink2);cursor:pointer;margin:0}
.picks input{position:absolute;opacity:0;pointer-events:none}
.picks input:checked+span{color:var(--ink)}
.picks label:has(input:checked){border-color:var(--ink);color:var(--ink)}
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
    picks = "".join(
        f'<label><input type=checkbox name=interests value="{e(key)}">'
        f"<span>{e(label)}</span></label>"
        for key, label, _ in INTERESTS
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="robots" content="noindex,nofollow">'
        '<meta name="referrer" content="no-referrer">'
        '<meta name="theme-color" content="#f7f5ef">'
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
        "<div class=field><label>What are you into</label>"
        f"<div class=picks>{picks}</div>"
        "<div class=hint>Pick a few. You can add anything else by texting him</div></div>"
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
