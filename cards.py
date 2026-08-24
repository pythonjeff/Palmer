"""Renders the morning briefing as a newspaper-style dashboard image.

Sized 1200x630 so one asset serves both jobs: the MMS card sent into the SMS
thread, and the og:image for the link preview. Drawn with Pillow rather than a
headless browser — Chromium will not fit a 512MB Basic dyno.

Everything here is driven by the structured snapshots (weather.weather_snapshot,
traffic.traffic_snapshot, datafeeds.price_snapshot), not by parsing prose. The
WMO weather_code no longer picks illustrated art — flat paper and hairline
rules read as "newspaper", not icons — but still gates the one-word condition
label; the traffic live/free-flow ratio drives the meter; price deltas colour
the market rows. Colour is rationed to temperature and the commute marker, the
same two spots page.py spends it, so the MMS preview and the page read as one
publication.

Every section degrades independently: a missing snapshot leaves its panel out
rather than failing the render, because a briefing that arrives without a card
is fine and a briefing that fails to send is not.
"""
from __future__ import annotations

import io
import os
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630
PAD = 60

# palette — flat paper white, ink-black type, hairline rules. Colour is spent
# only on temperature (warm/cool), the commute marker, and market deltas.
# Pillow flattens to RGB at save time without alpha compositing, so every
# colour here is a solid RGB triplet — no translucent overlays to fake.
PAPER = (247, 245, 239)
INK = (22, 21, 16)
MUTED = (92, 88, 76)
RULE = (214, 210, 198)
UP, DOWN = (31, 110, 58), (163, 39, 31)
WARM = (168, 70, 26)
COOL = (31, 90, 140)
AMBER = (138, 90, 16)

_FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",                  # heroku slug
    "/System/Library/Fonts/Supplemental",                # macOS
    "/Library/Fonts",
    "/usr/share/fonts/truetype",
)
# Serif first for the newspaper feel; DejaVuSans/Helvetica/Arial are the
# fallback chain when a serif face isn't installed, so the render never fails
# for want of a font — it just loses the editorial look.
_FONT_FILES = {
    False: ("DejaVuSerif.ttf", "Georgia.ttf", "Times New Roman.ttf",
             "DejaVuSans.ttf", "Helvetica.ttc", "Arial.ttf"),
    True: ("DejaVuSerif-Bold.ttf", "Georgia Bold.ttf", "Times New Roman Bold.ttf",
            "DejaVuSans-Bold.ttf", "Helvetica-Bold.ttf", "Arial Bold.ttf"),
}
_MONO_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
    "/usr/share/fonts/truetype",
)
_MONO_FILES = {
    False: ("DejaVuSansMono.ttf", "Menlo.ttc", "Consolas.ttf", "DejaVuSans.ttf"),
    True: ("DejaVuSansMono-Bold.ttf", "Menlo-Bold.ttc", "Consolas Bold.ttf", "DejaVuSans-Bold.ttf"),
}
_font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}
_mono_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def _resolve(dirs, files_by_bold, cache, size: int, bold: bool) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in cache:
        return cache[key]
    for d in dirs:
        for name in files_by_bold[bold]:
            path = os.path.join(d, name)
            if os.path.exists(path):
                try:
                    f = ImageFont.truetype(path, size)
                    cache[key] = f
                    return f
                except Exception:
                    continue
    f = ImageFont.load_default()
    cache[key] = f
    return f


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Serif face for headline-style text. Resolves across slug and local dev;
    falls back to Pillow's builtin so a missing font never takes the render down."""
    return _resolve(_FONT_DIRS, _FONT_FILES, _font_cache, size, bold)


def _mono(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Monospace face for numeric data — temps, prices, times — the one
    "digital" texture against the serif editorial type."""
    return _resolve(_MONO_DIRS, _MONO_FILES, _mono_cache, size, bold)


def _tw(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    return int(draw.textlength(text, font=font))


def _background() -> Image.Image:
    """Flat paper white — no gradient, no glow. The masthead rule and section
    hairlines carry the structure instead."""
    return Image.new("RGB", (W, H), PAPER)


def _hrule(d: ImageDraw.ImageDraw, x0: int, x1: int, y: int, fill=RULE, width: int = 1) -> None:
    d.line([(x0, y), (x1, y)], fill=fill, width=width)


def _meter(img: Image.Image, x: int, y: int, w: int, ratio: float) -> tuple[int, int, int]:
    """Congestion gauge: a plain grey hairline with a single coloured marker.

    A proportional fill bar was the first attempt and it misread badly — a
    free-flowing commute (ratio ~1.02) rendered as a nearly empty track, which
    looks like a stalled progress bar rather than good news. A marker on a
    graded scale says "you are here, and here is good" — and it's the only
    colour this row spends.
    """
    span = max(0.0, min(1.0, (ratio - 1.0) / 0.5))       # 1.0..1.5 -> 0..1
    colour = UP if span < 0.34 else (WARM if span < 0.67 else DOWN)

    d = ImageDraw.Draw(img)
    d.line([(x, y), (x + w, y)], fill=RULE, width=2)
    nx = x + int(span * (w - 12)) + 6
    d.ellipse([nx - 7, y - 7, nx + 7, y + 7], fill=colour)
    return colour


def _sparkline(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
               series: list[float], colour) -> None:
    pts = [p for p in (series or []) if isinstance(p, (int, float))]
    if len(pts) < 2:
        return
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1.0
    step = w / (len(pts) - 1)
    coords = [(x + i * step, y + h - ((p - lo) / rng) * h) for i, p in enumerate(pts)]
    d.line(coords, fill=colour, width=2, joint="curve")
    d.ellipse([coords[-1][0] - 3, coords[-1][1] - 3, coords[-1][0] + 3, coords[-1][1] + 3], fill=colour)


# Markets columns the card has room for. Four fits the width but the sparklines
# start overdrawing the price text ("$214.72" with a line through it), so three
# is the layout's real limit. This is a rendering cap, not a data cap: the
# payload may carry more (see home.MAX_PRICES) and the page shows all of them,
# because a vertical list has room where a fixed-width row does not.
MAX_PRICES = 3


def render_dashboard(*, city: str, weather: dict | None, traffic: dict | None,
                     prices: list[dict] | None, headlines: list[str] | None,
                     when: datetime | None = None) -> bytes:
    """The briefing as a 1200x630 PNG. Returns encoded bytes.

    Flat paper, ink type, hairline rules — no gradients, no glass panels, no
    illustrated weather art. Colour appears in exactly three places: the
    temperature (hot/cold), the commute marker, and market deltas — the same
    accents page.py spends, so the MMS card and the page read as one thing.
    """
    img = _background()
    d = ImageDraw.Draw(img)
    now = when or datetime.now()

    # --- masthead -----------------------------------------------------------
    d.text((PAD, PAD - 24), (city or "Today").upper(), font=_font(30, True), fill=INK)
    date_txt = now.strftime("%A, %B %-d").upper()
    df = _mono(16)
    d.text((W - PAD - _tw(d, date_txt, df), PAD - 6), date_txt, font=df, fill=MUTED)
    _hrule(d, PAD, W - PAD, PAD + 22, fill=INK, width=3)
    _hrule(d, PAD, W - PAD, PAD + 28, fill=INK, width=1)

    content_top = PAD + 70
    right_x = PAD + 620
    right_w = W - PAD - right_x

    # --- weather hero (left column) -----------------------------------------
    if weather:
        temp = weather.get("temp_now")
        if temp is not None:
            temp_colour = WARM if temp >= 80 else (COOL if temp <= 40 else INK)
            fh = _mono(118, True)
            d.text((PAD - 4, content_top), f"{temp:.0f}°", font=fh, fill=temp_colour)
        desc = (weather.get("description") or "").capitalize()
        d.text((PAD, content_top + 138), desc, font=_font(28), fill=MUTED)

        chips = []
        hi, lo = weather.get("high"), weather.get("low")
        if hi is not None and lo is not None:
            chips.append((f"H {hi:.0f}° L {lo:.0f}°", MUTED))
        if weather.get("rain_pct"):
            chips.append((f"{weather['rain_pct']}% RAIN", COOL))
        if weather.get("wind") is not None:
            chips.append((f"{weather['wind']:.0f} MPH WIND", MUTED))
        if weather.get("feels_like") is not None and temp is not None \
                and abs(weather["feels_like"] - temp) >= 3:
            chips.append((f"FEELS {weather['feels_like']:.0f}°", WARM))
        cx, cy = PAD, content_top + 190
        cf = _mono(16, True)
        for label, colour in chips[:3]:
            cw = _tw(d, label, cf) + 28
            d.rounded_rectangle([cx, cy, cx + cw, cy + 34], radius=3, outline=colour, width=1)
            d.text((cx + 14, cy + 9), label, font=cf, fill=colour)
            cx += cw + 10

    # --- commute (right column, top) ----------------------------------------
    commute_bottom = content_top
    if traffic:
        d.text((right_x, content_top), "COMMUTE", font=_font(18, True), fill=MUTED)
        mins = f"{traffic.get('live_min', 0)} min"
        mf = _mono(42, True)
        d.text((right_x, content_top + 24), mins, font=mf, fill=INK)
        delay = traffic.get("delay_min") or 0
        span = max(0.0, min(1.0, ((traffic.get("ratio") or 1.0) - 1.0) / 0.5))
        tier_colour = UP if span < 0.34 else (WARM if span < 0.67 else DOWN)
        note = "clear" if span < 0.34 else f"+{delay} min vs normal"
        d.text((right_x + _tw(d, mins, mf) + 16, content_top + 40), note,
               font=_mono(18), fill=tier_colour)
        _meter(img, right_x, content_top + 88, right_w, traffic.get("ratio") or 1.0)
        commute_bottom = content_top + 100
        _hrule(d, right_x, right_x + right_w, commute_bottom + 20)

    # --- markets (right column, below commute) ------------------------------
    if prices:
        m_top = commute_bottom + 44
        d.text((right_x, m_top), "MARKETS", font=_font(18, True), fill=MUTED)
        row_y = m_top + 32
        lf, pf, xf = _font(19, True), _mono(20, True), _mono(15)
        for p in prices[:MAX_PRICES]:
            pct = p.get("pct_24h") or 0.0
            colour = UP if pct >= 0 else DOWN
            d.text((right_x, row_y + 4), (p.get("label", "") or "")[:14], font=lf, fill=INK)
            price = p.get("price") or 0
            ptxt = f"${price:,.0f}" if price >= 1000 else f"${price:,.2f}"
            pct_txt = f"{pct:+.1f}%"
            pw = max(_tw(d, ptxt, pf), _tw(d, pct_txt, xf))
            d.text((right_x + right_w - pw, row_y - 2), ptxt, font=pf, fill=INK)
            d.text((right_x + right_w - _tw(d, pct_txt, xf), row_y + 24), pct_txt, font=xf, fill=colour)
            _sparkline(d, right_x + right_w - pw - 108, row_y + 6, 84, 24,
                      p.get("series") or [], colour)
            row_y += 42
        _hrule(d, right_x, right_x + right_w, row_y + 2)

    # --- headlines (footer band, full width) --------------------------------
    if headlines:
        band = H - 90
        _hrule(d, PAD, W - PAD, band, fill=INK, width=1)
        d.text((PAD, band + 10), "TODAY", font=_font(15, True), fill=MUTED)
        f = _font(21)
        y = band + 32
        for h in headlines[:2]:
            clipped = h if _tw(d, h, f) < W - 2 * PAD else h[:110] + "…"
            d.text((PAD, y), clipped, font=f, fill=INK)
            y += 26

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
