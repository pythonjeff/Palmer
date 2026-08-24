"""Renders the morning briefing as a dashboard image.

Sized 1200x630 so one asset serves both jobs: the MMS card sent into the SMS
thread, and the og:image for the link preview. Drawn with Pillow rather than a
headless browser — Chromium will not fit a 512MB Basic dyno.

Everything here is driven by the structured snapshots (weather.weather_snapshot,
traffic.traffic_snapshot, datafeeds.price_snapshot), not by parsing prose. The
WMO weather_code picks the art; the traffic live/free-flow ratio drives the
meter; price deltas colour the market rows.

Every section degrades independently: a missing snapshot leaves its panel out
rather than failing the render, because a briefing that arrives without a card
is fine and a briefing that fails to send is not.
"""
from __future__ import annotations

import io
import os
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1200, 630
PAD = 56

# palette
BG_TOP, BG_BOT = (9, 13, 26), (22, 30, 54)
INK = (240, 244, 255)
MUTED = (138, 152, 186)
PANEL = (255, 255, 255, 10)
UP, DOWN = (74, 222, 128), (248, 113, 113)
WARM = (250, 204, 21)
COOL = (125, 185, 245)

_FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",                  # heroku slug
    "/System/Library/Fonts/Supplemental",                # macOS
    "/Library/Fonts",
    "/usr/share/fonts/truetype",
)
_FONT_FILES = {
    False: ("DejaVuSans.ttf", "Helvetica.ttc", "Arial.ttf"),
    True: ("DejaVuSans-Bold.ttf", "Helvetica-Bold.ttf", "Arial Bold.ttf"),
}
_font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Resolve a font across slug and local dev. Falls back to Pillow's builtin
    so a missing font never takes the render down."""
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    for d in _FONT_DIRS:
        for name in _FONT_FILES[bold]:
            path = os.path.join(d, name)
            if os.path.exists(path):
                try:
                    f = ImageFont.truetype(path, size)
                    _font_cache[key] = f
                    return f
                except Exception:
                    continue
    f = ImageFont.load_default()
    _font_cache[key] = f
    return f


def _tw(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    return int(draw.textlength(text, font=font))


def _background() -> Image.Image:
    """Vertical gradient with a soft glow where the weather art sits."""
    img = Image.new("RGB", (W, H), BG_TOP)
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOT)))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([W - 520, -240, W + 120, 300], fill=(60, 90, 160, 90))
    glow = glow.filter(ImageFilter.GaussianBlur(110))
    img = Image.alpha_composite(img.convert("RGBA"), glow)
    return img.convert("RGB")


def _panel(img: Image.Image, box, radius: int = 22) -> None:
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle(box, radius=radius, fill=PANEL)
    img.alpha_composite(overlay) if img.mode == "RGBA" else img.paste(
        Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"), (0, 0)
    )


def _weather_art(img: Image.Image, cx: int, cy: int, code: int | None, scale: float = 1.0) -> None:
    """Draw conditions from the WMO code onto `img`.

    Deliberately geometric — flat shapes read better at MMS thumbnail size than
    detailed illustration. Glow is a blurred layer composited underneath, not
    concentric outlines (which render as visible wireframe rings)."""
    c = code if code is not None else 0
    sunny = c in (0, 1)
    cloudy = c in (2, 3, 45, 48)
    rainy = c in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82)
    snowy = c in (71, 73, 75, 77, 85, 86)
    stormy = c in (95, 96, 99)
    s = lambda v: int(v * scale)  # noqa: E731

    if sunny or cloudy:
        glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        r = s(78)
        gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*WARM, 150))
        img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(s(46))))

    d = ImageDraw.Draw(img)
    if sunny or cloudy:
        r = s(60)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=WARM)

    if cloudy or rainy or snowy or stormy:
        base = (166, 178, 204) if not stormy else (98, 106, 132)
        for dx, dy, rr in ((-52, 18, 38), (0, 2, 50), (54, 20, 34)):
            d.ellipse([cx + s(dx) - s(rr), cy + s(dy) - s(rr),
                       cx + s(dx) + s(rr), cy + s(dy) + s(rr)], fill=base)
        d.rounded_rectangle([cx - s(90), cy + s(22), cx + s(88), cy + s(60)],
                            radius=s(19), fill=base)

    if rainy or stormy:
        for i in range(9):
            x = cx - s(74) + i * s(19)
            d.line([(x, cy + s(74)), (x - s(8), cy + s(106))], fill=COOL, width=max(2, s(4)))
    if snowy:
        for i in range(7):
            x = cx - s(64) + i * s(22)
            d.ellipse([x - s(5), cy + s(80), x + s(5), cy + s(90)], fill=(226, 236, 255))
    if stormy:
        d.polygon([(cx + s(4), cy + s(64)), (cx - s(18), cy + s(106)), (cx + s(2), cy + s(106)),
                   (cx - s(9), cy + s(136)), (cx + s(29), cy + s(92)), (cx + s(7), cy + s(92))],
                  fill=WARM)


def _meter(img: Image.Image, x: int, y: int, w: int, ratio: float) -> tuple[int, int, int]:
    """Congestion gauge: a green→amber→red track with a needle marking today.

    A proportional fill bar was the first attempt and it misread badly — a
    free-flowing commute (ratio ~1.02) rendered as a nearly empty track, which
    looks like a stalled progress bar rather than good news. A needle on a
    graded scale says "you are here, and here is good."
    """
    h = 16
    span = max(0.0, min(1.0, (ratio - 1.0) / 0.5))       # 1.0..1.5 -> 0..1
    colour = UP if span < 0.34 else (WARM if span < 0.67 else DOWN)

    track = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    td = ImageDraw.Draw(track)
    for i in range(w):                                    # graded scale
        t = i / max(1, w - 1)
        if t < 0.5:
            k = t / 0.5
            col = tuple(int(a + (b - a) * k) for a, b in zip(UP, WARM))
        else:
            k = (t - 0.5) / 0.5
            col = tuple(int(a + (b - a) * k) for a, b in zip(WARM, DOWN))
        td.line([(i, 0), (i, h)], fill=(*col, 70))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=h // 2, fill=255)
    img.paste(track, (x, y), mask)

    d = ImageDraw.Draw(img)
    nx = x + int(span * (w - 6)) + 3
    d.rounded_rectangle([nx - 4, y - 6, nx + 4, y + h + 6], radius=4, fill=(255, 255, 255))
    d.rounded_rectangle([nx - 2, y - 4, nx + 2, y + h + 4], radius=2, fill=colour)
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
    d.line(coords, fill=colour, width=3, joint="curve")
    d.ellipse([coords[-1][0] - 4, coords[-1][1] - 4, coords[-1][0] + 4, coords[-1][1] + 4], fill=colour)


# Markets columns the card has room for. Four fits the width but the sparklines
# start overdrawing the price text ("$214.72" with a line through it), so three
# is the layout's real limit. This is a rendering cap, not a data cap: the
# payload may carry more (see home.MAX_PRICES) and the page shows all of them,
# because a vertical list has room where a fixed-width row does not.
MAX_PRICES = 3


def render_dashboard(*, city: str, weather: dict | None, traffic: dict | None,
                     prices: list[dict] | None, headlines: list[str] | None,
                     when: datetime | None = None) -> bytes:
    """The briefing as a 1200x630 PNG. Returns encoded bytes."""
    img = _background().convert("RGBA")
    d = ImageDraw.Draw(img)
    now = when or datetime.now()

    # --- header -----------------------------------------------------------
    d.text((PAD, PAD - 14), (city or "Today").upper(), font=_font(26, True), fill=MUTED)
    d.text((PAD, PAD + 20), now.strftime("%A, %B %-d").upper(), font=_font(20), fill=(96, 110, 145))

    # --- weather hero -----------------------------------------------------
    if weather:
        _weather_art(img, W - 268, PAD + 132, weather.get("weather_code"), scale=1.25)
        d = ImageDraw.Draw(img)
        temp = weather.get("temp_now")
        if temp is not None:
            # Degree glyph inline at hero size — drawn separately at a smaller
            # size it renders as a tiny detached ring floating beside the number.
            fh = _font(150, True)
            d.text((PAD - 6, PAD + 62), f"{temp:.0f}°", font=fh, fill=INK)
        desc = (weather.get("description") or "").capitalize()
        d.text((PAD, PAD + 222), desc, font=_font(34), fill=(206, 218, 242))

        # stat chips — these fill the gap the hero used to leave empty
        chips = []
        hi, lo = weather.get("high"), weather.get("low")
        if hi is not None and lo is not None:
            chips.append((f"H {hi:.0f}°  L {lo:.0f}°", MUTED))
        if weather.get("rain_pct"):
            chips.append((f"{weather['rain_pct']}% rain", COOL))
        if weather.get("wind") is not None:
            chips.append((f"{weather['wind']:.0f} mph wind", MUTED))
        if weather.get("feels_like") is not None and temp is not None \
                and abs(weather["feels_like"] - temp) >= 3:
            chips.append((f"feels {weather['feels_like']:.0f}°", WARM))
        cx = PAD
        cf = _font(24, True)
        for label, colour in chips[:3]:
            cw = _tw(d, label, cf) + 34
            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(overlay).rounded_rectangle(
                [cx, PAD + 268, cx + cw, PAD + 312], radius=22, fill=(255, 255, 255, 16))
            img.alpha_composite(overlay)
            d = ImageDraw.Draw(img)
            d.text((cx + 17, PAD + 279), label, font=cf, fill=colour)
            cx += cw + 12

    # --- commute meter ----------------------------------------------------
    top = PAD + 336
    if traffic:
        _panel(img, [PAD - 18, top - 18, PAD + 470, top + 108])
        d = ImageDraw.Draw(img)
        d.text((PAD, top), "COMMUTE", font=_font(20, True), fill=MUTED)
        mins = f"{traffic.get('live_min', 0)} min"
        d.text((PAD, top + 30), mins, font=_font(52, True), fill=INK)
        delay = traffic.get("delay_min") or 0
        note = "clear" if delay < 2 else f"+{delay} min vs normal"
        d.text((PAD + _tw(d, mins, _font(52, True)) + 18, top + 48), note,
               font=_font(24), fill=UP if delay < 2 else WARM)
        _meter(img, PAD, top + 94, 434, traffic.get("ratio") or 1.0)
        d = ImageDraw.Draw(img)

    # --- markets ----------------------------------------------------------
    if prices:
        x0 = PAD + 512
        _panel(img, [x0 - 18, top - 18, W - PAD + 18, top + 108])
        d = ImageDraw.Draw(img)
        d.text((x0, top), "MARKETS", font=_font(20, True), fill=MUTED)
        col_w = (W - PAD - x0) // max(1, min(len(prices), MAX_PRICES))
        for i, p in enumerate(prices[:MAX_PRICES]):
            cx = x0 + i * col_w
            pct = p.get("pct_24h") or 0.0
            colour = UP if pct >= 0 else DOWN
            d.text((cx, top + 32), p.get("label", "")[:9], font=_font(22, True), fill=(200, 212, 238))
            price = p.get("price") or 0
            ptxt = f"${price:,.0f}" if price >= 1000 else f"${price:,.2f}"
            d.text((cx, top + 58), ptxt, font=_font(30, True), fill=INK)
            d.text((cx, top + 94), f"{pct:+.1f}%", font=_font(22, True), fill=colour)
            _sparkline(d, cx + 96, top + 60, col_w - 118, 34, p.get("series") or [], colour)

    # --- headline ticker --------------------------------------------------
    if headlines:
        band = H - 92
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rectangle([0, band, W, H], fill=(255, 255, 255, 12))
        img.alpha_composite(overlay)
        d = ImageDraw.Draw(img)
        f = _font(24)
        x = PAD
        for i, h in enumerate(headlines):
            if x > W - 180:
                break
            if i:
                d.text((x, band + 34), "•", font=f, fill=(90, 104, 140))
                x += 26
            clipped = h if _tw(d, h, f) < W - x - 90 else h[: max(8, int((W - x - 110) / 11))] + "…"
            d.text((x, band + 32), clipped, font=f, fill=(206, 218, 244))
            x += _tw(d, clipped, f) + 20

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()
