"""
The on-screen buttons as glass tiles (3.9.11, 26 Nov 2026).

The owner's design: no white card, no blurred edges - two clear, lively tiles
in the iOS style, floating straight on CMED's or Aalo's page:

    Stop   - a red tile with a white square.
    Mark   - a blue tile with two white bars; amber and pulsing while a
             sensitive part is marked.

Every pixel carries its own transparency (RGBA), so the tiles' rounded edges
and soft shadows blend into whatever page is behind them. ui/overlay.py shows
the image in a per-pixel see-through window (ui/layered.py). Each tile is drawn
four times larger and scaled down, so its edges are smooth at any display scale.

No tkinter here: drawing is tested on its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Tuple

SS = 4                                  # drawn this many times larger, then scaled down

RED = (240, 68, 56)
BLUE = (0, 122, 255)
AMBER = (255, 149, 0)

TILE = 40                               # a tile's side at 100% display scale (26 Nov: smaller)
PAD = 14                                # room around a tile for its shadow and glow
GAP = 12                                # between the two tiles
# Behind everything, so the strip can be grabbed and dragged between the tiles:
# 1 of 255 is invisible on any page but still catches the pointer.
GRIP_ALPHA = 1

Box = Tuple[int, int, int, int]


@dataclass(frozen=True)
class Layout:
    """The strip's size and where each tile's face sits in it, in screen pixels."""
    width: int
    height: int
    stop: Box
    mark: Box


def layout(scale: float = 1.0) -> Layout:
    tile, pad, gap = round(TILE * scale), round(PAD * scale), round(GAP * scale)
    width = tile + 2 * pad
    stop = (pad, pad, pad + tile, pad + tile)
    top = pad + tile + gap
    mark = (pad, top, pad + tile, top + tile)
    return Layout(width=width, height=top + tile + pad, stop=stop, mark=mark)


def _mix(colour, other, amount):
    return tuple(int(colour[i] * (1 - amount) + other[i] * amount) for i in range(3))


@lru_cache(maxsize=64)
def tile(kind: str, size: int, tint: Tuple[int, int, int], *, glow: bool = False,
         hover: bool = False):
    """
    One tile as an RGBA image of (size + 2 * pad) pixels square, the face
    centred: a rounded square of the tint, lighter at the top, a faint sheen,
    a hairline bright rim, a soft shadow (or a coloured glow), and the white
    glyph. kind: "stop" | "mark".
    """
    from PIL import Image, ImageDraw, ImageFilter

    pad = max(4, round(size * PAD / TILE))
    s, p = size * SS, pad * SS
    full = s + 2 * p
    out = Image.new("RGBA", (full, full), (0, 0, 0, 0))
    radius = int(s * 0.28)

    # The shadow below the tile, or the glow around it while it pulses.
    shade = Image.new("RGBA", (full, full), (0, 0, 0, 0))
    drop = 0 if glow else round(s * 0.06)
    ImageDraw.Draw(shade).rounded_rectangle(
        (p, p + drop, p + s, p + s + drop), radius=radius,
        fill=tint + (200,) if glow else (0, 0, 0, 60))
    out.alpha_composite(shade.filter(ImageFilter.GaussianBlur(s * (0.12 if glow else 0.07))))

    # The face: the tint, lighter at the top; nearly opaque, so it reads as
    # clearly on CMED's teal as on Aalo's white.
    top = _mix(tint, (255, 255, 255), 0.32 if hover else 0.2)
    bottom = _mix(tint, (0, 0, 0), 0.14)
    face = Image.new("RGBA", (s, s))
    draw = ImageDraw.Draw(face)
    for y in range(s):
        t = y / max(1, s - 1)
        draw.line((0, y, s, y), fill=_mix(top, bottom, t) + (int(236 - 14 * t),))
    shape = Image.new("L", (s, s), 0)
    ImageDraw.Draw(shape).rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=255)
    face.putalpha(Image.composite(face.getchannel("A"), Image.new("L", (s, s), 0), shape))

    # A faint sheen across the upper part.
    sheen = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(sheen).ellipse((-s * 0.6, -s * 1.35, s * 1.6, s * 0.38),
                                  fill=(255, 255, 255, 46 if hover else 34))
    sheen.putalpha(Image.composite(sheen.getchannel("A"), Image.new("L", (s, s), 0), shape))
    face.alpha_composite(sheen)

    # A hairline bright rim, strongest at the top.
    rim = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(rim).rounded_rectangle((SS, SS, s - SS - 1, s - SS - 1),
                                          radius=max(1, radius - SS),
                                          outline=(255, 255, 255, 150 if hover else 120),
                                          width=SS)
    fade = Image.linear_gradient("L").resize((s, s)).point(lambda v: 255 - int(v * 0.75))
    rim.putalpha(Image.composite(rim.getchannel("A"), Image.new("L", (s, s), 0), fade))
    face.alpha_composite(rim)

    # The glyph, white, with a faint shadow under it.
    glyph = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    g = ImageDraw.Draw(glyph)
    c = s / 2
    white = (255, 255, 255, 255)
    if kind == "stop":
        h = s * 0.17
        g.rounded_rectangle((c - h, c - h, c + h, c + h), radius=int(s * 0.06), fill=white)
    else:
        bar, half, gap = s * 0.085, s * 0.2, s * 0.075
        g.rounded_rectangle((c - gap - bar, c - half, c - gap, c + half),
                            radius=int(bar / 2), fill=white)
        g.rounded_rectangle((c + gap, c - half, c + gap + bar, c + half),
                            radius=int(bar / 2), fill=white)
    under = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    under.paste((0, 0, 0, 60), (0, SS), glyph.getchannel("A"))
    face.alpha_composite(under.filter(ImageFilter.GaussianBlur(1.5 * SS)))
    face.alpha_composite(glyph)

    out.alpha_composite(face, (p, p))
    return out.resize((size + 2 * pad, size + 2 * pad), Image.LANCZOS)


def strip(scale: float = 1.0, *, mark_tint: Tuple[int, int, int] = BLUE,
          glow: bool = False, hover: str = "") -> "Image.Image":
    """
    The whole control: the Stop tile above the Mark tile, as one RGBA image of
    layout(scale) size. hover: "stop" | "mark" | "" - the tile under the pointer.
    """
    from PIL import Image

    box = layout(scale)
    size = box.stop[2] - box.stop[0]
    image = Image.new("RGBA", (box.width, box.height), (0, 0, 0, GRIP_ALPHA))
    for kind, face, tint, lit in (("stop", box.stop, RED, False),
                                  ("mark", box.mark, mark_tint, glow)):
        art = tile(kind, size, tint, glow=lit, hover=hover == kind)
        pad = (art.width - size) // 2
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        layer.paste(art, (face[0] - pad, face[1] - pad))
        image.alpha_composite(layer)
    return image


def flatten(image, matte: Tuple[int, int, int]):
    """The image laid on a plain colour: for a window that cannot show alpha."""
    from PIL import Image

    base = Image.new("RGBA", image.size, matte + (255,))
    base.alpha_composite(image)
    return base.convert("RGB")


def hit(box: Layout, x: int, y: int) -> str:
    """Which tile a point of the strip is on: "stop", "mark" or ""."""
    for kind, face in (("stop", box.stop), ("mark", box.mark)):
        if face[0] <= x < face[2] and face[1] <= y < face[3]:
            return kind
    return ""


__all__ = ["RED", "BLUE", "AMBER", "Layout", "layout", "tile", "strip", "flatten", "hit"]
