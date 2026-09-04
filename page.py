"""The briefing as an interactive page.

This is the half an MMS card cannot be. The card is a bitmap with no tap targets
anywhere in it, so the thread gets exactly one tap target — the card or its link
preview — and everything tappable lives here: headlines open their source,
tickers open their chart.

Rendered from the same payload as the card (see artifacts.py) so the two can
never disagree. Self-contained by design: inline CSS and inline SVG, no external
requests, no fonts to fetch, no JS required to read it. It opens instantly on a
phone over a cell connection, which is the only place it will ever be opened.
"""
from __future__ import annotations

import html
import os
from datetime import datetime
from urllib.parse import quote

# A newspaper page, not a dashboard: flat paper white, ink-black type, thin
# hairline rules instead of glass panels. Color is rationed to the two places
# a reader actually needs it at a glance — the temperature and the commute
# gauge — everything else stays black-and-white. Mirrors cards.py's palette
# so the MMS/og:image preview and the page read as one publication.
CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{
 --paper:#f7f5ef;--ink:#161510;--ink2:#5c584c;--rule:rgba(22,21,16,.16);
 --warm:#a8461a;--cool:#1f5a8c;--up:#1f6e3a;--down:#a3271f;--amber:#8a5a10;
 --serif:Georgia,"Iowan Old Style","Times New Roman",Times,serif;
 --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
}
body{background:var(--paper);color:var(--ink);font:16px/1.5 var(--serif);
 -webkit-font-smoothing:antialiased;padding:0 0 48px}
.wrap{max-width:640px;margin:0 auto;padding:28px 22px}
.masthead{text-align:center;padding-bottom:16px}
.eyebrow{font-family:var(--serif);font-weight:700;font-size:28px;letter-spacing:.01em;text-transform:uppercase}
.rule{border:0;border-top:3px double var(--ink);margin-top:10px}
.date{font-family:var(--mono);color:var(--ink2);font-size:11px;letter-spacing:.16em;
 text-transform:uppercase;margin-top:9px}
.hero{display:flex;align-items:baseline;justify-content:space-between;gap:14px;
 margin-top:26px;padding-bottom:18px;border-bottom:1px solid var(--rule)}
.temp{font-family:var(--mono);font-size:60px;font-weight:600;letter-spacing:-.02em;line-height:1}
.temp.warm{color:var(--warm)}.temp.cool{color:var(--cool)}
.desc{font-family:var(--serif);font-style:italic;font-size:18px;color:var(--ink2);margin-top:8px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
.chip{border:1px solid var(--rule);border-radius:2px;padding:5px 10px;
 font-family:var(--mono);font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--ink2)}
.chip.cool{color:var(--cool);border-color:var(--cool)}
.chip.warm{color:var(--warm);border-color:var(--warm)}
.chip.link{border-color:var(--ink);color:var(--ink)}
.chip.link:active{opacity:.55}
.card{padding:20px 0;border-bottom:1px solid var(--rule)}
.label{color:var(--ink);font-family:var(--serif);font-size:13px;letter-spacing:.13em;
 font-weight:700;text-transform:uppercase;display:flex;justify-content:space-between;align-items:baseline}
.big{font-family:var(--mono);font-size:38px;font-weight:600;margin-top:10px;letter-spacing:-.01em}
.note{font-family:var(--mono);font-size:14px;font-weight:600;margin-left:10px}
.up{color:var(--up)}.down{color:var(--down)}.amber{color:var(--amber)}
a{color:inherit;text-decoration:none}
a.row{display:flex;align-items:center;justify-content:space-between;gap:14px;
 padding:14px 0;border-top:1px solid var(--rule)}
a.row:first-of-type{border-top:0}
a.row:active{opacity:.55}
.tick{font-family:var(--serif);font-size:16px;line-height:1.4}
.src{color:var(--ink2);font-family:var(--mono);font-size:10px;margin-top:5px;
 text-transform:uppercase;letter-spacing:.07em}
.chev{color:var(--ink2);flex:0 0 auto}
.as{color:var(--ink2);font-family:var(--mono);font-size:10px;font-weight:400;
 letter-spacing:.05em;text-transform:uppercase}
.ask{display:block;border:1px solid var(--ink);padding:15px 16px;margin-top:18px}
.ask:active{opacity:.55}
.ask .h{font-family:var(--serif);font-weight:700;font-size:15px}
.ask .s{color:var(--ink2);font-family:var(--mono);font-size:11px;margin-top:5px;
 text-transform:uppercase;letter-spacing:.04em}
.foot{color:var(--ink2);font-family:var(--mono);font-size:11px;text-align:center;
 margin-top:28px;line-height:1.7;letter-spacing:.03em;text-transform:uppercase}
"""


def _spark(series, colour: str, w: int = 92, h: int = 28) -> str:
    pts = [p for p in (series or []) if isinstance(p, (int, float))]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1.0
    step = w / (len(pts) - 1)
    coords = " ".join(f"{i*step:.1f},{h - ((p-lo)/rng)*h:.1f}" for i, p in enumerate(pts))
    last = coords.split()[-1]
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" fill="none">'
            f'<polyline points="{coords}" stroke="{colour}" stroke-width="1.75" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
            f'<circle cx="{last.split(",")[0]}" cy="{last.split(",")[1]}" r="2.5" fill="{colour}"/></svg>')


def _traffic_tier(ratio: float) -> tuple[str, float]:
    """Same graded 0..1 span the meter draws on, collapsed to a 3-way tier so
    the marker colour and the note text always agree with each other and with
    the card's _meter()."""
    span = max(0.0, min(1.0, (ratio - 1.0) / 0.5))
    tier = "up" if span < 0.34 else ("amber" if span < 0.67 else "down")
    return tier, span


_TIER_HEX = {"up": "#1f6e3a", "amber": "#8a5a10", "down": "#a3271f"}


def _gauge(ratio: float, tier: str, span: float) -> str:
    """Commute gauge — a plain grey rule with a single coloured marker, not a
    rainbow track. The colour is the one accent this row gets."""
    colour = _TIER_HEX[tier]
    x = 2 + span * 96
    return (
        '<svg width="100%" height="14" viewBox="0 0 100 14" preserveAspectRatio="none" style="margin-top:14px">'
        '<line x1="0" y1="7" x2="100" y2="7" stroke="rgba(22,21,16,.18)" stroke-width="1.5"/>'
        f'<circle cx="{x:.1f}" cy="7" r="4" fill="{colour}"/></svg>'
    )


def _ago(ts: float | None, now: float | None = None) -> str:
    """Relative freshness. A page showing this morning's commute at 4pm is worse
    than showing nothing, so every live section says how old it is."""
    import time
    if not ts:
        return ""
    delta = int((now or time.time()) - ts)
    if delta < 90:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _local_day(tz_name: str | None) -> str:
    from timeutil import local_now
    try:
        if tz_name:
            return local_now(tz_name).strftime("%A, %B %d")
    except Exception:
        pass
    return datetime.utcnow().strftime("%A, %B %d")


def _price_link(p: dict) -> str:
    # `symbol` is the real coingecko id / Yahoo ticker (added to the payload by
    # datafeeds.price_snapshot). `label` is a display string for humans — "S&P
    # 500", "Avalanche", "Btc" — and building the link from it instead 404s for
    # every index and most of _CRYPTO_IDS, where the slug the site actually
    # uses doesn't match a naive lowercase of the label. Older cached payloads
    # written before `symbol` existed fall back to the label as before.
    symbol = p.get("symbol") or p.get("label", "")
    if p.get("is_crypto"):
        return f"https://www.coingecko.com/en/coins/{quote(symbol.lower())}"
    return f"https://finance.yahoo.com/quote/{quote(symbol.upper())}"


_CHEV = ('<svg class="chev" width="18" height="18" viewBox="0 0 24 24" fill="none" '
         'stroke="currentColor" stroke-width="2.5"><path d="M9 6l6 6-6 6"/></svg>')

# "Watching" caps: watches/price watches are user-authored and
# usually few (1-3 typical), so 4 comfortably shows "everything" for most
# users while bounding the worst case. Topics keeps the section's prior cap.
# Imported rather than restated: opening.py already bounds the payload
# (MAX_LOCAL + MAX_SCREENS), and a second number here silently truncated the
# last row when those were raised. Same reasoning as home importing
# cards.MAX_PRICES — the page and the producer render from one payload and must
# not disagree about how much of it survives.
from opening import MAX_ROWS as OPENING_ROW_CAP
WATCH_CHIP_CAP = 4
PWATCH_CHIP_CAP = 4
TOPIC_CHIP_CAP = 6
CHIP_TEXT_MAX = 40

# The sections a user may reorder or hide by texting Palmer ("put markets
# first", "hide the commute"). The canonical names live HERE because page.py is
# the module that knows what a section is — agent.py's arrange_page dispatch
# imports them, so the arranger and the renderer can never disagree about what
# a section is called. The masthead, the temperature hero, the name-ask block
# and the footer are deliberately not in the list: they are the page's
# identity, not cards. "weather" is the extra-locations card — the hero always
# shows the primary city.
DEFAULT_SECTION_ORDER = ("weather", "commute", "scores", "markets", "news", "opening", "watching")

# The words users actually reach for, folded to canonical section names.
# Same contract as opening.KIND_WORDS: an unknown word is surfaced by the
# dispatch so Palmer can ask, never guessed. Kind words ("movies", "concerts")
# are deliberately absent — those route to opening_add/opening_remove, and
# mapping them here would let "hide movies" silently hide the whole Opening
# section instead of trimming a kind.
SECTION_WORDS = {
    "weather": "weather", "weather locations": "weather", "temps": "weather",
    "commute": "commute", "traffic": "commute", "drive": "commute",
    "scores": "scores", "score": "scores", "sports": "scores", "games": "scores",
    "teams": "scores", "my team": "scores",
    "markets": "markets", "market": "markets", "stocks": "markets",
    "prices": "markets", "tickers": "markets", "crypto": "markets",
    "news": "news", "headlines": "news", "stories": "news",
    "opening": "opening", "openings": "opening", "what's opening": "opening",
    "watching": "watching", "watches": "watching", "tracking": "watching",
}


def _chip_text(s: str, limit: int = CHIP_TEXT_MAX) -> str:
    """Keyword-length chip text. Watch descriptions are short user-authored
    phrases but nothing enforces that at write time, so this is the backstop."""
    s = (s or "").strip()
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def _chip(e, text: str, url: str | None) -> str:
    """One tag/pill: an ink-bordered link when a source URL exists, a plain
    muted chip otherwise. `e` is the caller's html.escape closure."""
    label = e(_chip_text(text))
    if url:
        return (f'<a class="chip link" href="{e(url)}" target="_blank" '
                f'rel="noopener noreferrer">{label} &#8599;</a>')
    return f'<span class=chip>{label}</span>'


def render(payload: dict, *, token: str, image_url: str, page_url: str) -> str:
    """Full HTML document. Escapes every interpolated value — headlines come
    from news search, which is untrusted input."""
    e = lambda v: html.escape(str(v), quote=True)  # noqa: E731
    city = payload.get("city") or "Today"
    w = payload.get("weather") or {}
    t = payload.get("traffic") or {}
    prices = payload.get("prices") or []
    heads = payload.get("headlines") or []
    fetched = payload.get("fetched") or {}
    tracking = payload.get("tracking") or {}
    name = (payload.get("name") or "").strip()
    # Falls back to a neutral label rather than leaving the page anonymous; the
    # prompt below turns the gap into an invitation instead of a blank.
    eyebrow = name if name else "Your briefing"
    # The user's local day, not the server's. Heroku runs UTC, so a plain
    # datetime.now() shows tomorrow's date to anyone west of it by evening —
    # the one thing on a "live" page nobody would forgive being wrong.
    subhead = " · ".join(x for x in (city if name else None,
                                     _local_day(payload.get("timezone"))) if x)

    temp = w.get("temp_now")
    where = city if city and city != "Today" else "your briefing"
    # The link preview's headline. When Palmer knows who this is, the name is
    # the whole point — it is what makes the card read as *yours* in a thread,
    # and it is the user-visible proof that Palmer actually stored the name
    # rather than just reading it back out of the conversation.
    title = name or (f"{temp:.0f}° in {where}" if temp is not None
                     else f"{where}".capitalize())
    bits = []
    if t.get("live_min"):
        bits.append(f"{t['live_min']} min commute")
    if prices:
        p0 = prices[0]
        bits.append(f"{p0.get('label')} {p0.get('pct_24h', 0):+.1f}%")
    if heads:
        bits.append(heads[0].get("title", ""))
    desc = " · ".join(b for b in bits if b)[:200]

    out = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">',
        '<meta name="robots" content="noindex,nofollow">',
        '<meta name="referrer" content="no-referrer">',
        '<meta name="theme-color" content="#f7f5ef">',
        f"<title>{e(title)}</title>",
        f'<meta property="og:title" content="{e(title)}">',
        f'<meta property="og:description" content="{e(desc)}">',
        f'<meta property="og:image" content="{e(image_url)}">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        f'<meta property="og:url" content="{e(page_url)}">',
        '<meta property="og:type" content="website">',
        '<meta name="twitter:card" content="summary_large_image">',
        f"<style>{CSS}</style></head><body><div class=wrap>",
        '<div class=masthead>',
        f'<div class=eyebrow>{e(eyebrow)}</div>',
        '<hr class=rule>',
        f'<div class=date>{e(subhead)}</div>',
        '</div>',
    ]

    if not name:
        # No form here on purpose — the page has no auth and nothing to POST to.
        # The product is SMS, so the affordance is a pre-filled text back to
        # Palmer. Tapping opens Messages with the body already written.
        sms_num = os.environ.get("TWILIO_PHONE_NUMBER", "")
        # quote(), not quote_plus(): the sms: URI scheme has no form encoding,
        # so a "+" is a literal plus. quote_plus sent people into Messages with
        # "My+name+is+" already typed, and that is exactly what Palmer received.
        body = quote("My name is ")
        href = f"sms:{sms_num}?&body={body}" if sms_num else ""
        opener = f'<a class=ask href="{e(href)}">' if href else '<div class=ask>'
        closer = "</a>" if href else "</div>"
        out.append(
            f'{opener}<div class=h>Palmer doesn\'t know your name yet</div>'
            f'<div class=s>{"Tap to tell him &rarr;" if href else "Text Palmer your name"}</div>{closer}'
        )

    if w:
        # Temperature is one of two places the page spends real color — hot
        # reads warm, cold reads cool, anything in between stays plain ink.
        temp_cls = ""
        if temp is not None:
            temp_cls = " warm" if temp >= 80 else (" cool" if temp <= 40 else "")
        out.append('<div class=hero><div>')
        if temp is not None:
            out.append(f'<div class="temp{temp_cls}">{temp:.0f}°</div>')
        if w.get("description"):
            out.append(f'<div class=desc>{e(w["description"].capitalize())}</div>')
        out.append("</div></div><div class=chips>")
        if w.get("high") is not None and w.get("low") is not None:
            # A contested high shows as a range here too — the page, the card
            # and the text render from one payload and must not disagree about
            # how sure Palmer is.
            hi = (f'{w["high_low_est"]}-{w["high_high_est"]}'
                  if w.get("high_confident") is False and w.get("high_low_est") is not None
                  else f'{w["high"]:.0f}')
            out.append(f'<span class=chip>H {e(hi)}° &nbsp;L {w["low"]:.0f}°</span>')
        if w.get("rain_pct"):
            out.append(f'<span class="chip cool">{e(w["rain_pct"])}% rain</span>')
        if w.get("wind") is not None:
            out.append(f'<span class=chip>{w["wind"]:.0f} mph wind</span>')
        out.append("</div>")

    # Every .card section renders into this dict and ships in the user's
    # preferred order below. Building the HTML unconditionally and ordering at
    # the end is what lets arrange_page reorder or hide sections without this
    # function growing a second copy of any of them.
    sections: dict[str, str] = {}

    extra_w = payload.get("weather_extra") or []
    if extra_w:
        sec = ['<div class=card><div class=label>Weather'
               f'<span class=as>{e(_ago(fetched.get("weather_extra")))}</span></div>']
        for ew in extra_w:
            etemp = ew.get("temp_now")
            place = ew.get("resolved") or ""
            ttxt = f'{etemp:.0f}°' if etemp is not None else "—"
            edesc = (ew.get("description") or "").capitalize()
            sec.append(
                '<div style="display:flex;align-items:center;justify-content:space-between;'
                f'padding:12px 0;border-top:1px solid var(--rule)">'
                f'<div><div class=tick style="font-weight:700">{e(place)}</div>'
                f'{f"<div class=src>{e(edesc)}</div>" if edesc else ""}</div>'
                f'<div style="font-family:var(--mono);font-weight:700;font-size:20px">{e(ttxt)}</div></div>'
            )
        sec.append("</div>")
        sections["weather"] = "".join(sec)

    if t:
        delay = t.get("delay_min") or 0
        tier, span = _traffic_tier(t.get("ratio") or 1.0)
        note = "clear" if tier == "up" else f"+{delay} min vs normal"
        sections["commute"] = "".join(
            [f'<div class=card><div class=label>Commute'
             f'<span class=as>{e(_ago(fetched.get("traffic")))}</span></div>',
             f'<div class=big>{e(t.get("live_min", 0))} min '
             f'<span class="note {tier}">{e(note)}</span></div>',
             _gauge(t.get("ratio") or 1.0, tier, span), "</div>"])

    scores = payload.get("scores") or []
    if scores:
        # Yesterday's result and today's game, one row per followed team. The
        # same rows the morning and evening texts are drafted from; a team
        # with nothing on either day is simply absent (home._fetch_scores).
        from sports import result_line
        sec = ['<div class=card><div class=label>Scores'
               f'<span class=as>{e(_ago(fetched.get("scores")))}</span></div>']
        for row in scores:
            team = {"abbrev": row.get("abbrev"), "name": row.get("team")}
            lines = []
            if row.get("today"):
                lines.append(("Today", result_line(row["today"], team)))
            if row.get("last"):
                lines.append(("Yesterday", result_line(row["last"], team)))
            inner = f'<div><div class=tick style="font-weight:700">{e(row.get("team") or "")}</div>'
            for when, text in lines:
                inner += f'<div class=src>{e(when)} &middot; {e(text)}</div>'
            inner += "</div>"
            sec.append(f'<div class=row style="display:flex;padding:14px 0;'
                       f'border-top:1px solid var(--rule)">{inner}</div>')
        sec.append("</div>")
        sections["scores"] = "".join(sec)

    if prices:
        sec = ['<div class=card><div class=label>Markets'
               f'<span class=as>{e(_ago(fetched.get("prices")))}</span></div>']
        for p in prices:
            pct = p.get("pct_24h") or 0.0
            cls = "up" if pct >= 0 else "down"
            colour = _TIER_HEX["up"] if pct >= 0 else _TIER_HEX["down"]
            price = p.get("price") or 0
            ptxt = f"${price:,.0f}" if price >= 1000 else f"${price:,.2f}"
            sec += [f'<a class=row href="{e(_price_link(p))}" target="_blank" rel="noopener noreferrer">',
                    f'<div><div class=tick style="font-weight:700">{e(p.get("label",""))}</div>',
                    f'<div class="note {cls}" style="margin-top:2px">{pct:+.1f}%</div></div>',
                    f'<div style="display:flex;align-items:center;gap:12px">{_spark(p.get("series"), colour)}',
                    f'<div style="font-family:var(--mono);font-weight:700;font-size:17px">{e(ptxt)}</div>{_CHEV}</div></a>']
        sec.append("</div>")
        sections["markets"] = "".join(sec)

    if heads:
        sec = ['<div class=card><div class=label>News'
               f'<span class=as>{e(_ago(fetched.get("headlines")))}</span></div>']
        for h in heads:
            t_ = h.get("title", "")
            url = h.get("url")
            src = h.get("source") or ""
            inner = (f'<div><div class=tick>{e(t_)}</div>'
                     f'{f"<div class=src>{e(src)}</div>" if src else ""}</div>')
            if url:
                sec.append(f'<a class=row href="{e(url)}" target="_blank" rel="noopener noreferrer">'
                           f'{inner}{_CHEV}</a>')
            else:
                sec.append(f'<div class=row>{inner}</div>')
        sec.append("</div>")
        sections["news"] = "".join(sec)

    opening = payload.get("opening") or []
    if opening:
        sec = ['<div class=card><div class=label>Opening'
               f'<span class=as>{e(_ago(fetched.get("opening")))}</span></div>']
        for o in opening[:OPENING_ROW_CAP]:
            sub = o.get("subtitle") or ""
            # when and source share one muted line: "Friday - ticketmaster.com".
            # Escape each piece before joining with the raw entity, not after —
            # e() on the joined string turns "&middot;" into "&amp;middot;",
            # which ships to the page as the literal text "&middot;" instead of
            # a middle dot.
            meta = " &middot; ".join(e(x) for x in (o.get("when") or "", o.get("source") or "") if x)
            inner = (f'<div><div class=tick>{e(o.get("title", ""))}</div>'
                     f'{f"<div class=src>{e(sub)}</div>" if sub else ""}'
                     f'{f"<div class=src>{meta}</div>" if meta else ""}</div>')
            url = o.get("url")
            if url:
                sec.append(f'<a class=row href="{e(url)}" target="_blank" rel="noopener noreferrer">'
                           f'{inner}{_CHEV}</a>')
            else:
                sec.append(f'<div class=row>{inner}</div>')
        sec.append("</div>")
        sections["opening"] = "".join(sec)

    watches = tracking.get("watches") or []
    pwatches = tracking.get("price_watches") or []
    topics = tracking.get("topics") or []
    if watches or pwatches or topics:
        ann = ""
        if topics and tracking.get("morning_time"):
            ann = f'<span class=as>morning &middot; {e(tracking["morning_time"])}</span>'
        sec = [f'<div class=card><div class=label>Watching{ann}</div><div class=chips>']
        for w in watches[:WATCH_CHIP_CAP]:
            sec.append(_chip(e, w.get("description", ""), w.get("url")))
        for w in pwatches[:PWATCH_CHIP_CAP]:
            product = _chip_text(w.get("product", ""), 28)
            price, target = w.get("last_seen"), w.get("target")
            if price is not None:
                text = f'{product} ${price:,.2f}'
            elif target is not None:
                text = f'{product} → ${target:,.2f}'
            else:
                text = product
            sec.append(_chip(e, text, w.get("url")))
        head_by_topic = {}
        for h in heads:
            key = h.get("topic")
            if key and key not in head_by_topic:
                head_by_topic[key] = h
        for topic in topics[:TOPIC_CHIP_CAP]:
            h = head_by_topic.get(topic)
            sec.append(_chip(e, topic, h.get("url") if h else None))
        sec.append("</div></div>")
        sections["watching"] = "".join(sec)

    # The user's arrangement, carried on the payload by home.rebuild /
    # home._refresh_identity (the episode_alerts pattern) so this render never
    # needs a profile read. Sections they named come first in their order;
    # anything unnamed keeps its default position after them, so a partial
    # instruction ("put markets first") never silently drops a section.
    prefs = payload.get("page_prefs") or {}
    order = [s for s in (prefs.get("section_order") or []) if s in sections]
    order += [s for s in DEFAULT_SECTION_ORDER if s in sections and s not in order]
    hidden = set(prefs.get("hidden_sections") or [])
    shown = [s for s in order if s not in hidden]
    out += [sections[s] for s in shown]

    # The product is SMS, so the "edit button" is a pre-filled text back to
    # Palmer — same affordance as the name ask above, and for the same reason:
    # the page has no auth and nothing to POST to. quote(), not quote_plus():
    # the sms: URI scheme has no form encoding, so a "+" is a literal plus.
    arrange_num = os.environ.get("TWILIO_PHONE_NUMBER", "")
    if arrange_num:
        arrange_href = f'sms:{arrange_num}?&body={quote("Arrange my page: ")}'
        out.append(
            f'<a class=ask href="{e(arrange_href)}">'
            '<div class=h>Want this arranged differently?</div>'
            '<div class=s>Tap to tell Palmer &rarr;</div></a>'
        )

    out.append('<div class=foot>Palmer keeps this current<br>tap anything to open the source')
    # Required by TMDB's terms wherever their data is shown — so only when a
    # screen row actually renders: present in the payload AND not hidden by the
    # user's arrangement.
    if "opening" in shown and any(o.get("kind") == "screen" for o in opening):
        out.append('<br>This product uses the TMDB API but is not endorsed or certified by TMDB.')
    out.append('</div>')
    out.append("</div></body></html>")
    return "".join(out)
