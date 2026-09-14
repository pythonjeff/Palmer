"""Renders the morning briefing as a newspaper-style dashboard image.

Sized 1200x630 for one job: the og:image behind the link preview on the page
URL Palmer texts out. It was once an MMS card too, and the docstring here said
so long after that stopped being true — nothing attaches this PNG to a message
now (the only outbound media_url in the codebase is _send_gif_outbound). That
matters more than it sounds: an MMS card is tapped open at full size, while an
og:image is only ever seen shrunk into a chat bubble, and the two want
completely different layouts. See MIN_LEGIBLE_PT for the one that won.

Drawn with Pillow rather than a headless browser — Chromium will not fit a
512MB Basic dyno.

Everything here is driven by the structured snapshots (weather.weather_snapshot,
traffic.traffic_snapshot, datafeeds.price_snapshot), not by parsing prose. The
WMO weather_code no longer picks illustrated art — flat paper and hairline
rules read as "newspaper", not icons — but still gates the one-word condition
label; the traffic live/free-flow ratio drives the meter; price deltas colour
the market rows. Colour is rationed to temperature and the commute marker, the
same two spots page.py spends it, so the preview and the page read as one
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
    "/System/Library/Fonts",                             # macOS: Menlo lives here
    "/Library/Fonts",
    "/usr/share/fonts/truetype",
)
# Menlo.ttc is the last mono entry in BOTH rows on purpose: macOS ships no
# "Menlo-Bold.ttc", so a bold lookup that only listed that name fell through to
# Pillow's builtin bitmap face — which does not scale, and rendered the 118pt
# hero temperature at about 8px. Production was never affected (the slug has
# DejaVu), but the card is reviewed by rendering it locally, so a local render
# that does not look like the real one is worse than useless.
_MONO_FILES = {
    False: ("DejaVuSansMono.ttf", "Menlo.ttc", "Consolas.ttf", "DejaVuSans.ttf"),
    True: ("DejaVuSansMono-Bold.ttf", "Consolas Bold.ttf", "DejaVuSans-Bold.ttf",
           "Menlo.ttc"),
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


def _clip(draw: ImageDraw.ImageDraw, text: str, font, avail: int) -> str:
    """Trim to an ellipsis at `avail` px. Cheaper than wrapping and the right
    answer here: at preview scale a second line of anything is unreadable, so
    a band that would wrap should say less instead."""
    text = (text or "").strip()
    if not text or _tw(draw, text, font) <= avail:
        return text
    while text and _tw(draw, text + "…", font) > avail:
        text = text[:-1]
    return text.rstrip(" ,-") + "…" if text else ""


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

    Track and marker are scaled for the preview: a 2px track and a 14px dot
    came to half a pixel and three, which is nothing. At 5 and 26 the marker
    reads as a coloured dot on a grey line, which is all it ever needed to be.
    """
    span = max(0.0, min(1.0, (ratio - 1.0) / 0.5))       # 1.0..1.5 -> 0..1
    colour = UP if span < 0.34 else (WARM if span < 0.67 else DOWN)

    d = ImageDraw.Draw(img)
    d.line([(x, y), (x + w, y)], fill=RULE, width=5)
    nx = x + int(span * (w - 26)) + 13
    d.ellipse([nx - 13, y - 13, nx + 13, y + 13], fill=colour)
    return colour


# There is no sparkline here any more. A 2px polyline is half a pixel once the
# card is downscaled into a chat bubble, and it was overdrawing the price text
# it sat beside — the defect MAX_PRICES was capped at 3 to avoid. page.py keeps
# its own SVG `_spark`, which is the surface with the resolution to show one.


# --- the size everything here is actually read at ---------------------------
#
# Nothing sends this PNG as MMS media any more — grep media_url and the only
# outbound use is _send_gif_outbound. Its sole consumer is the link-preview
# scraper, via page.py's og:image. A summary_large_image bubble renders at
# roughly 300pt wide, so this 1200px canvas is downscaled 4x before a human
# ever sees it, and on-screen text needs about 11px to read comfortably.
#
# That arithmetic is the whole design constraint: 11 / 0.25 = 44pt here. The
# previous layout carried fourteen data points at 15-30pt, so thirteen of them
# landed between 4px and 7px — present in the file, grey mush in the thread.
# Anything the preview must actually convey is now at or above MIN_LEGIBLE_PT,
# and the card carries fewer things so they fit.
PREVIEW_WIDTH_PT = 300
PREVIEW_SCALE = PREVIEW_WIDTH_PT / W          # 0.25
MIN_LEGIBLE_PT = 44

# The hero numeral. Big enough to be the thing you see before you read
# anything, and sized so the two-column band below it still clears the bottom
# margin — the previous layout ran its last row to within 8px of the edge
# while PAD is 60 everywhere else.
HERO_PT = 168
COL_GAP = 56

# Markets rows the card draws. Two, not the old three, because the rows are
# now 46pt instead of 19pt — the cap is set by the type size, not by the data.
# This is a rendering cap only: the payload carries home.MAX_PRICES (6) and the
# page shows all of them, because a vertical list has room a preview does not.
MAX_PRICES = 2

# Opening and headlines are no longer drawn on the card at all. Both were
# below the legibility floor by a factor of four (opening titles 20pt, headline
# text 21pt) and neither survives a 4x downscale as anything but texture. The
# original reason for the opening band was that it filled "the one band of the
# card that was empty" — dead space this layout no longer has. Both still
# render on the page, which is where they can be read and tapped.
#
# render_dashboard still ACCEPTS both arguments: callers are unchanged, and
# artifacts._card_inputs decides the cache key from what is drawn, so they are
# dropped there instead (a fingerprint that moves without the image moving
# would mint a new og:image URL for a byte-identical card).
CARD_OPENING_ROWS = 0


def render_dashboard(*, city: str, weather: dict | None, traffic: dict | None,
                     prices: list[dict] | None,
                     headlines: list[str] | None = None,
                     opening: list[dict] | None = None,
                     when: datetime | None = None,
                     show_date: bool = True) -> bytes:
    """The briefing as a 1200x630 PNG. Returns encoded bytes.

    Flat paper, ink type, hairline rules — no gradients, no glass panels, no
    illustrated weather art. Colour appears in exactly three places: the
    temperature (hot/cold), the commute marker, and market deltas — the same
    accents page.py spends, so the preview and the page read as one thing.

    Laid out for the size it is read at, not the size it is drawn at: see
    MIN_LEGIBLE_PT. Every band flows off one vertical cursor, so a section with
    no data collapses and the ones below it move up rather than leaving a hole
    — which is also what keeps a card with only weather on it looking composed.

    `headlines` and `opening` are accepted and deliberately not drawn; see the
    note on CARD_OPENING_ROWS.
    """
    img = _background()
    d = ImageDraw.Draw(img)
    # `when` is the READER's clock. artifacts._card_now hands back None when
    # there is no zone to read it in, and the masthead then carries no date at
    # all rather than the dyno's — the page omits it for the same reason, and
    # the two render from one payload and must not disagree about the day.
    now = when or datetime.now()

    # --- masthead -----------------------------------------------------------
    mf = _font(52, True)
    d.text((PAD, PAD - 30), (city or "Today").upper(), font=mf, fill=INK)
    if show_date:
        # Abbreviated: "WEDNESDAY, SEPTEMBER 14" at a legible size runs into the
        # city on a long name, and the weekday is the part that carries meaning.
        date_txt = now.strftime("%a %b %-d").upper()
        df = _mono(26)
        d.text((W - PAD - _tw(d, date_txt, df), PAD + 2), date_txt, font=df, fill=MUTED)
    _hrule(d, PAD, W - PAD, PAD + 44, fill=INK, width=4)
    _hrule(d, PAD, W - PAD, PAD + 54, fill=INK, width=1)

    # Columns are assigned in order of what exists, not by fixed position: a
    # user with no commute on file would otherwise get an empty left half and
    # Markets stranded out on the right, which reads as a failed render rather
    # than as a shorter card. Decided up front because the hero's placement
    # depends on whether anything follows it.
    slots = [name for name, present in (("commute", traffic), ("markets", prices)) if present]

    y = PAD + 74
    if not slots:
        # Nothing below the hero. This is the common first state — home.py
        # builds the page the moment a city lands, before there is a commute or
        # a ticker — so it is the first preview most people ever see, and
        # hanging the numeral off the masthead above two thirds of empty paper
        # is the version of it they would judge. Centre it instead.
        y = (PAD + 54 + H - HERO_PT) // 2

    # --- weather hero -------------------------------------------------------
    # Primary city only. A user's secondary weather_locations render on the
    # page (page.py) but never here: this band is one numeral and two chips by
    # design, and a second location would either halve the numeral or push the
    # bands below it off the canvas. See home._fetch_weather_extra.
    if weather:
        temp = weather.get("temp_now")
        beside_x, hero_bottom = PAD, y
        if temp is not None:
            temp_colour = WARM if temp >= 80 else (COOL if temp <= 40 else INK)
            fh = _mono(HERO_PT, True)
            txt = f"{temp:.0f}°"
            d.text((PAD - 10, y - 34), txt, font=fh, fill=temp_colour)
            beside_x = PAD - 10 + _tw(d, txt, fh) + 40
            hero_bottom = y + HERO_PT - 4

        # Condition and chips sit BESIDE the numeral rather than under it. The
        # old stack left a ~90px empty band across the middle of the card while
        # the right-hand column ran out of room, which is how a 1200px canvas
        # ended up feeling crowded.
        avail = W - PAD - beside_x
        ty = y + 26
        desc = (weather.get("description") or "").strip().capitalize()
        if desc:
            cf = _font(54)
            d.text((beside_x, ty), _clip(d, desc, cf, avail), font=cf, fill=MUTED)
            ty += 76

        # Two chips, not four. Each is a whole line of text at preview scale,
        # and the high/low is the one a reader is actually looking for.
        chips = []
        hi, lo = weather.get("high"), weather.get("low")
        if hi is not None and lo is not None:
            chips.append((f"H {hi:.0f}°  L {lo:.0f}°", MUTED))
        if weather.get("rain_pct"):
            chips.append((f"{weather['rain_pct']}% RAIN", COOL))
        if weather.get("feels_like") is not None and temp is not None \
                and abs(weather["feels_like"] - temp) >= 3:
            chips.append((f"FEELS {weather['feels_like']:.0f}°", WARM))
        cx = beside_x
        cf = _mono(32, True)
        for label, colour in chips[:2]:
            cw = _tw(d, label, cf) + 36
            if cx + cw > W - PAD:
                break
            d.rounded_rectangle([cx, ty, cx + cw, ty + 56], radius=4, outline=colour, width=2)
            d.text((cx + 18, ty + 12), label, font=cf, fill=colour)
            cx += cw + 14
        if chips:
            ty += 56
        y = max(hero_bottom, ty)

    # --- commute and markets, side by side ----------------------------------
    # Two columns rather than the old stack: at this type size neither fits the
    # full width twice over, and the pair reads as one "today" band.
    band_top = y + 30
    col_w = (W - 2 * PAD - COL_GAP) // 2
    col_x = {name: PAD + i * (col_w + COL_GAP) for i, name in enumerate(slots)}
    if slots:
        _hrule(d, PAD, W - PAD, band_top, fill=INK, width=1)
    label_f = _font(26, True)

    if traffic:
        left_x = col_x["commute"]
        d.text((left_x, band_top + 22), "COMMUTE", font=label_f, fill=MUTED)
        mins = f"{traffic.get('live_min', 0)} min"
        mfm = _mono(60, True)
        d.text((left_x, band_top + 62), mins, font=mfm, fill=INK)
        delay = traffic.get("delay_min") or 0
        span = max(0.0, min(1.0, ((traffic.get("ratio") or 1.0) - 1.0) / 0.5))
        tier_colour = UP if span < 0.34 else (WARM if span < 0.67 else DOWN)
        note = "clear" if span < 0.34 else f"+{delay} min"
        d.text((left_x + _tw(d, mins, mfm) + 24, band_top + 84), note,
               font=_mono(32), fill=tier_colour)
        meter_y = band_top + 150
        if traffic.get("depart_at"):
            # The number is a forecast for their leave time; say which one.
            from timeutil import friendly_hhmm
            d.text((left_x, band_top + 132),
                   f"{friendly_hhmm(traffic['depart_at'])} -> ~{friendly_hhmm(traffic.get('arrive_at'))}",
                   font=_mono(26), fill=MUTED)
            meter_y += 22
        _meter(img, left_x, meter_y, col_w, traffic.get("ratio") or 1.0)

    if prices:
        mx = col_x["markets"]
        d.text((mx, band_top + 22), "MARKETS", font=label_f, fill=MUTED)
        row_y = band_top + 66
        lf, pf, xf = _font(40, True), _mono(38, True), _mono(30)
        col_right = mx + col_w
        for p in prices[:MAX_PRICES]:
            pct = p.get("pct_24h") or 0.0
            colour = UP if pct >= 0 else DOWN
            pct_txt = f"{pct:+.1f}%"
            price = p.get("price") or 0
            ptxt = f"${price:,.0f}" if price >= 1000 else f"${price:,.2f}"
            # Price right-aligned, delta under it, ticker left — and no
            # sparkline. A 2px polyline is half a pixel at preview scale, and
            # it was overdrawing the price text it sat beside.
            pw = max(_tw(d, ptxt, pf), _tw(d, pct_txt, xf))
            label = _clip(d, (p.get("label") or ""), lf, col_w - pw - 24)
            d.text((mx, row_y + 2), label, font=lf, fill=INK)
            d.text((col_right - _tw(d, ptxt, pf), row_y), ptxt, font=pf, fill=INK)
            d.text((col_right - _tw(d, pct_txt, xf), row_y + 48), pct_txt, font=xf, fill=colour)
            row_y += 92

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
