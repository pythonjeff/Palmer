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
    label = p.get("label", "")
    if p.get("is_crypto"):
        return f"https://www.coingecko.com/en/coins/{quote(label.lower())}"
    return f"https://finance.yahoo.com/quote/{quote(label.upper())}"


_CHEV = ('<svg class="chev" width="18" height="18" viewBox="0 0 24 24" fill="none" '
         'stroke="currentColor" stroke-width="2.5"><path d="M9 6l6 6-6 6"/></svg>')

# "Watching" caps: watches/price watches are user-authored and
# usually few (1-3 typical), so 4 comfortably shows "everything" for most
# users while bounding the worst case. Topics keeps the section's prior cap.
WATCH_CHIP_CAP = 4
PWATCH_CHIP_CAP = 4
TOPIC_CHIP_CAP = 6
CHIP_TEXT_MAX = 40


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
            out.append(f'<span class=chip>H {w["high"]:.0f}° &nbsp;L {w["low"]:.0f}°</span>')
        if w.get("rain_pct"):
            out.append(f'<span class="chip cool">{e(w["rain_pct"])}% rain</span>')
        if w.get("wind") is not None:
            out.append(f'<span class=chip>{w["wind"]:.0f} mph wind</span>')
        out.append("</div>")

    if t:
        delay = t.get("delay_min") or 0
        tier, span = _traffic_tier(t.get("ratio") or 1.0)
        note = "clear" if tier == "up" else f"+{delay} min vs normal"
        out += [f'<div class=card><div class=label>Commute'
                f'<span class=as>{e(_ago(fetched.get("traffic")))}</span></div>',
                f'<div class=big>{e(t.get("live_min", 0))} min '
                f'<span class="note {tier}">{e(note)}</span></div>',
                _gauge(t.get("ratio") or 1.0, tier, span), "</div>"]

    if prices:
        out.append('<div class=card><div class=label>Markets'
                   f'<span class=as>{e(_ago(fetched.get("prices")))}</span></div>')
        for p in prices:
            pct = p.get("pct_24h") or 0.0
            cls = "up" if pct >= 0 else "down"
            colour = _TIER_HEX["up"] if pct >= 0 else _TIER_HEX["down"]
            price = p.get("price") or 0
            ptxt = f"${price:,.0f}" if price >= 1000 else f"${price:,.2f}"
            out += [f'<a class=row href="{e(_price_link(p))}" target="_blank" rel="noopener noreferrer">',
                    f'<div><div class=tick style="font-weight:700">{e(p.get("label",""))}</div>',
                    f'<div class="note {cls}" style="margin-top:2px">{pct:+.1f}%</div></div>',
                    f'<div style="display:flex;align-items:center;gap:12px">{_spark(p.get("series"), colour)}',
                    f'<div style="font-family:var(--mono);font-weight:700;font-size:17px">{e(ptxt)}</div>{_CHEV}</div></a>']
        out.append("</div>")

    if heads:
        out.append('<div class=card><div class=label>News'
                   f'<span class=as>{e(_ago(fetched.get("headlines")))}</span></div>')
        for h in heads:
            t_ = h.get("title", "")
            url = h.get("url")
            src = h.get("source") or ""
            inner = (f'<div><div class=tick>{e(t_)}</div>'
                     f'{f"<div class=src>{e(src)}</div>" if src else ""}</div>')
            if url:
                out.append(f'<a class=row href="{e(url)}" target="_blank" rel="noopener noreferrer">'
                           f'{inner}{_CHEV}</a>')
            else:
                out.append(f'<div class=row>{inner}</div>')
        out.append("</div>")

    watches = tracking.get("watches") or []
    pwatches = tracking.get("price_watches") or []
    topics = tracking.get("topics") or []
    if watches or pwatches or topics:
        ann = ""
        if topics and tracking.get("morning_time"):
            ann = f'<span class=as>morning &middot; {e(tracking["morning_time"])}</span>'
        out.append(f'<div class=card><div class=label>Watching{ann}</div><div class=chips>')
        for w in watches[:WATCH_CHIP_CAP]:
            out.append(_chip(e, w.get("description", ""), w.get("url")))
        for w in pwatches[:PWATCH_CHIP_CAP]:
            product = _chip_text(w.get("product", ""), 28)
            price, target = w.get("last_seen"), w.get("target")
            if price is not None:
                text = f'{product} ${price:,.2f}'
            elif target is not None:
                text = f'{product} → ${target:,.2f}'
            else:
                text = product
            out.append(_chip(e, text, w.get("url")))
        head_by_topic = {}
        for h in heads:
            key = h.get("topic")
            if key and key not in head_by_topic:
                head_by_topic[key] = h
        for topic in topics[:TOPIC_CHIP_CAP]:
            h = head_by_topic.get(topic)
            out.append(_chip(e, topic, h.get("url") if h else None))
        out.append("</div></div>")

    out.append('<div class=foot>Palmer keeps this current<br>tap anything to open the source</div>')
    out.append("</div></body></html>")
    return "".join(out)
