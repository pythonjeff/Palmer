"""What Palmer looks like, in one place.

The page and the card already share a design — a newspaper, not a dashboard:
flat paper white, ink-black type, hairline rules, colour rationed to the two
places a reader needs it at a glance. They did not share a *definition* of it.
`page.py` carried `--paper:#f7f5ef` and `cards.py` carried
`PAPER = (247, 245, 239)`, the same colour written twice in two notations and
kept in step by hand. That is the shape of drift this repo has a standing rule
against — the card and the page render from one payload precisely so they can
never disagree, and then the palette was left to agree by good intentions.

So the values live here and both render from them. Hex for CSS, RGB tuples for
Pillow, derived from the same source rather than transcribed.

This module imports nothing from Palmer, for the reason `sources.py` and
`timeutil.py` don't: it sits at the bottom of the dependency graph, so anything
may use it without a cycle. In particular `cards.py` imports it, which is why
the mark below resolves its own font instead of borrowing `cards._font` — that
would point the arrow back up.
"""
from __future__ import annotations

NAME = "Palmer"

# One sentence, used where a platform wants to describe the sender: the vCard's
# note, og:site_name's company, and the RBM agent description if Palmer is ever
# registered as one (that field caps at 100 characters, which is why this is
# short).
TAGLINE = "Your morning, by text."

# --- palette ----------------------------------------------------------------
# Hex is the source. The card gets RGB through rgb() so the two notations can
# never be edited apart.
PAPER = "#f7f5ef"   # flat newsprint white, the background everywhere
INK = "#161510"     # near-black type
INK2 = "#5c584c"    # secondary type: labels, timestamps, sub-lines

# A hairline is ink at low alpha on the page. The card cannot say that: Pillow
# flattens to RGB at save time with no alpha compositing, so it needs the
# already-blended colour. RULE_ALPHA is what CSS uses, RULE_SOLID is the same
# line as a solid triplet. They are two spellings of one hairline, which is
# exactly why they belong beside each other rather than in two modules.
RULE_ALPHA = "rgba(22,21,16,.16)"
RULE_SOLID = "#d6d2c6"

WARM = "#a8461a"    # a hot temperature
COOL = "#1f5a8c"    # a cold one
UP = "#1f6e3a"      # a price up, a clear commute
DOWN = "#a3271f"    # a price down, a bad one
AMBER = "#8a5a10"   # the middle of the commute gauge


def rgb(hex_color: str) -> tuple[int, int, int]:
    """'#f7f5ef' -> (247, 245, 239). Pillow wants tuples; CSS wants the string."""
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


# --- the mark ---------------------------------------------------------------
# A serif P on paper inside a hairline-ruled square: the masthead reduced to
# something legible at 32px. It exists for three jobs that currently have no
# artwork at all — the favicon, the apple-touch-icon, and the photo on Palmer's
# contact card, which is the one that puts a face beside the name in Messages.
#
# It is generated rather than committed as a binary. That is the same call
# `cards.py` makes about the card itself, and it means the mark cannot fall out
# of step with the palette above.

def mark_svg(size: int = 64) -> str:
    """The mark as SVG, for the favicon.

    The glyph is set in whatever serif the *client* has, because a favicon is
    rendered by the browser and there is no server font to embed. Georgia first
    to match the page, then the generic serif — the shape degrades gracefully in
    a way the PNG below cannot rely on.
    """
    s = size
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {s} {s}" width="{s}" height="{s}">'
        f'<rect width="{s}" height="{s}" fill="{PAPER}"/>'
        f'<rect x="1.5" y="1.5" width="{s - 3}" height="{s - 3}" fill="none" '
        f'stroke="{INK}" stroke-width="{max(1, s // 32)}"/>'
        f'<text x="50%" y="50%" dy=".35em" text-anchor="middle" fill="{INK}" '
        f'font-family="Georgia,&quot;Times New Roman&quot;,serif" font-weight="700" '
        f'font-size="{int(s * 0.62)}">P</text>'
        "</svg>"
    )


# Serif first, to match the page. The same families cards.py looks for, and for
# the same reason: production (the Heroku slug) has DejaVu, macOS has Georgia,
# and a mark reviewed locally should look like the one that ships.
_MARK_FONTS = (
    "DejaVuSerif-Bold.ttf", "Georgia Bold.ttf", "Times New Roman Bold.ttf",
    "DejaVuSans-Bold.ttf", "Helvetica-Bold.ttc", "Arial Bold.ttf",
)
_MARK_DIRS = (
    "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/truetype",
    "/usr/share/fonts", "/Library/Fonts", "/System/Library/Fonts/Supplemental",
)


def _mark_font(size: int):
    """The heaviest serif available, or Pillow's builtin as a last resort."""
    import os
    from PIL import ImageFont
    for directory in _MARK_DIRS:
        for name in _MARK_FONTS:
            path = os.path.join(directory, name)
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size)
                except Exception:
                    continue
    # Pillow's builtin does not scale, so the glyph comes out small. The square
    # and the rule still read as the mark, which is why this is a fallback
    # rather than a failure.
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def mark_png(size: int = 224) -> bytes:
    """The mark as a PNG.

    Defaults to 224x224 because that is what RCS Business Messaging wants for
    an agent logo (<=50KB), so the same call serves the favicon, the
    apple-touch-icon, the vCard photo and — if Palmer is ever registered as an
    RBM agent — the brand asset itself, with no second drawing to keep in step.
    """
    import io
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (size, size), rgb(PAPER))
    d = ImageDraw.Draw(img)
    border = max(2, size // 28)
    d.rectangle([border, border, size - border - 1, size - border - 1],
                outline=rgb(INK), width=max(1, size // 64))

    font = _mark_font(int(size * 0.62))
    if font is not None:
        # anchor="mm" centres on the glyph's own box, which matters here: a
        # serif P has more space under it than over it, and centring the box
        # instead would sit the letter visibly high.
        try:
            d.text((size // 2, int(size * 0.52)), "P", font=font,
                   fill=rgb(INK), anchor="mm")
        except Exception:
            d.text((size // 2, int(size * 0.52)), "P", font=font, fill=rgb(INK))

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
