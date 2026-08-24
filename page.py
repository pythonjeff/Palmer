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

# Mirrors the card's palette so the preview and the page feel like one thing.
CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#090d1a;color:#f0f4ff;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
 -webkit-font-smoothing:antialiased;padding:0 0 48px}
.wrap{max-width:640px;margin:0 auto;padding:28px 20px}
.bg{position:fixed;inset:0;z-index:-1;background:
 radial-gradient(120% 70% at 85% -10%,rgba(60,90,160,.45),transparent 60%),
 linear-gradient(#090d1a,#161e36)}
.eyebrow{color:#8a98ba;font-size:13px;letter-spacing:.14em;font-weight:700;text-transform:uppercase}
.date{color:#606e91;font-size:12px;letter-spacing:.12em;text-transform:uppercase;margin-top:4px}
.hero{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin:18px 0 4px}
.temp{font-size:76px;font-weight:800;letter-spacing:-.03em;line-height:1}
.desc{font-size:20px;color:#ced9f2;margin-top:6px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0 4px}
.chip{background:rgba(255,255,255,.08);border-radius:999px;padding:8px 14px;font-size:13px;font-weight:700;color:#8a98ba}
.chip.cool{color:#7db9f5}.chip.warm{color:#facc15}
.card{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.06);
 border-radius:18px;padding:18px;margin-top:14px}
.label{color:#8a98ba;font-size:12px;letter-spacing:.14em;font-weight:700;text-transform:uppercase}
.big{font-size:34px;font-weight:800;margin-top:6px}
.note{font-size:14px;font-weight:700}
.up{color:#4ade80}.down{color:#f87171}.amber{color:#facc15}
a{color:inherit;text-decoration:none}
a.row{display:flex;align-items:center;justify-content:space-between;gap:14px;
 padding:14px 0;border-top:1px solid rgba(255,255,255,.07)}
a.row:first-of-type{border-top:0}
a.row:active{opacity:.6}
.tick{font-size:15px;line-height:1.35}
.src{color:#606e91;font-size:12px;margin-top:4px;text-transform:uppercase;letter-spacing:.08em}
.chev{color:#4a5878;flex:0 0 auto}
.as{color:#4a5878;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;float:right}
.trk{padding:12px 0;border-top:1px solid rgba(255,255,255,.07)}
.trk:first-of-type{border-top:0}
.trk .t{font-size:15px}
.ask{display:block;background:rgba(125,185,245,.10);border:1px solid rgba(125,185,245,.28);
 border-radius:16px;padding:14px 16px;margin-top:16px}
.ask:active{opacity:.6}
.ask .h{font-weight:700;font-size:15px;color:#dce8fb}
.ask .s{color:#7db9f5;font-size:13px;margin-top:3px;font-weight:600}
.foot{color:#4a5878;font-size:12px;text-align:center;margin-top:26px;line-height:1.6}
"""


def _spark(series, colour: str, w: int = 96, h: int = 34) -> str:
    pts = [p for p in (series or []) if isinstance(p, (int, float))]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1.0
    step = w / (len(pts) - 1)
    coords = " ".join(f"{i*step:.1f},{h - ((p-lo)/rng)*h:.1f}" for i, p in enumerate(pts))
    last = coords.split()[-1]
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" fill="none">'
            f'<polyline points="{coords}" stroke="{colour}" stroke-width="2.5" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
            f'<circle cx="{last.split(",")[0]}" cy="{last.split(",")[1]}" r="3" fill="{colour}"/></svg>')


def _gauge(ratio: float) -> str:
    """Commute gauge — same needle-on-a-graded-scale metaphor as the card."""
    span = max(0.0, min(1.0, (ratio - 1.0) / 0.5))
    x = 4 + span * 92
    return (
        '<svg width="100%" height="18" viewBox="0 0 100 18" preserveAspectRatio="none" style="margin-top:14px">'
        '<defs><linearGradient id="g" x1="0" x2="1">'
        '<stop offset="0" stop-color="#4ade80"/><stop offset="0.5" stop-color="#facc15"/>'
        '<stop offset="1" stop-color="#f87171"/></linearGradient></defs>'
        '<rect x="0" y="5" width="100" height="8" rx="4" fill="url(#g)" opacity="0.42"/>'
        f'<rect x="{x-1.6:.1f}" y="1" width="3.2" height="16" rx="1.6" fill="#fff"/></svg>'
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
        '<meta name="theme-color" content="#090d1a">',
        f"<title>{e(title)}</title>",
        f'<meta property="og:title" content="{e(title)}">',
        f'<meta property="og:description" content="{e(desc)}">',
        f'<meta property="og:image" content="{e(image_url)}">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        f'<meta property="og:url" content="{e(page_url)}">',
        '<meta property="og:type" content="website">',
        '<meta name="twitter:card" content="summary_large_image">',
        f"<style>{CSS}</style></head><body><div class=bg></div><div class=wrap>",
        f'<div class=eyebrow>{e(eyebrow)}</div>',
        f'<div class=date>{e(subhead)}</div>',
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
        out.append('<div class=hero><div>')
        if temp is not None:
            out.append(f'<div class=temp>{temp:.0f}°</div>')
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
        cls, note = ("up", "clear") if delay < 2 else ("amber", f"+{delay} min vs normal")
        out += [f'<div class=card><div class=label>Commute'
                f'<span class=as>{e(_ago(fetched.get("traffic")))}</span></div>',
                f'<div class=big>{e(t.get("live_min", 0))} min '
                f'<span class="note {cls}">{e(note)}</span></div>',
                _gauge(t.get("ratio") or 1.0), "</div>"]

    if prices:
        out.append('<div class=card><div class=label>Markets'
                   f'<span class=as>{e(_ago(fetched.get("prices")))}</span></div>')
        for p in prices:
            pct = p.get("pct_24h") or 0.0
            cls = "up" if pct >= 0 else "down"
            colour = "#4ade80" if pct >= 0 else "#f87171"
            price = p.get("price") or 0
            ptxt = f"${price:,.0f}" if price >= 1000 else f"${price:,.2f}"
            out += [f'<a class=row href="{e(_price_link(p))}" target="_blank" rel="noopener noreferrer">',
                    f'<div><div style="font-weight:700">{e(p.get("label",""))}</div>',
                    f'<div class="note {cls}" style="margin-top:2px">{pct:+.1f}%</div></div>',
                    f'<div style="display:flex;align-items:center;gap:12px">{_spark(p.get("series"), colour)}',
                    f'<div style="font-weight:800;font-size:18px">{e(ptxt)}</div>{_CHEV}</div></a>']
        out.append("</div>")

    if heads:
        out.append('<div class=card><div class=label>Today'
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
        out.append('<div class=card><div class=label>Palmer is watching</div>')
        for w in watches:
            out.append(f'<div class=trk><div class=t>{e(w.get("description", ""))}</div>'
                       f'<div class=src>news watch</div></div>')
        for w in pwatches:
            bits = []
            if w.get("last_seen") is not None:
                bits.append(f'now ${w["last_seen"]:,.2f}')
            if w.get("target") is not None:
                bits.append(f'target ${w["target"]:,.2f}')
            out.append(f'<div class=trk><div class=t>{e(w.get("product", ""))}</div>'
                       f'<div class=src>{e(" · ".join(bits) or "price watch")}</div></div>')
        if topics:
            out.append(f'<div class=trk><div class=t>{e(", ".join(topics[:6]))}</div>'
                       f'<div class=src>in your morning update'
                       f'{e(" · " + tracking["morning_time"]) if tracking.get("morning_time") else ""}'
                       f'</div></div>')
        out.append("</div>")

    out.append('<div class=foot>Palmer keeps this current<br>tap anything to open the source</div>')
    out.append("</div></body></html>")
    return "".join(out)
