"""
The AIMScribe mark, drawn rather than shipped as an image file.

One drawing, used twice: the icon compiled into `AIMScribe_Agent.exe`
(`scripts/make_icon.py`) and the tray icon the agent shows while it runs
(`main.py`). They match because they are the same code, and a mark that is
drawn cannot go missing from a build the way a stray `.png` can.

It is deliberately not version 1's mark. Both may be installed on one PC
during the changeover, and the two AIMScribes must never be mistaken for each
other in a taskbar: v3 is the dark teal rounded square, v1 the old blue circle.

Legible at 16 px is the whole design constraint. That rules out fine outlines,
text and thin waveforms, so what is left is a filled capsule, a stand, and
three bars whose height carries the idea of speech.
"""
from __future__ import annotations

from typing import Optional, Tuple

# The states the tray shows, and what each one tints the mark.
READY = "ready"
RECORDING = "recording"
PAUSED = "paused"
OFFLINE = "offline"
BLOCKED = "blocked"

INK = (14, 42, 48)                 # the square, when nothing is happening
FACE = (247, 250, 250)             # the microphone itself

STATE_COLOUR = {
    READY:     (17, 94, 89),       # deep teal - the resting colour
    RECORDING: (176, 32, 42),      # red, and it pulses
    PAUSED:    (176, 122, 26),     # amber
    OFFLINE:   (46, 82, 120),      # recording, but the server is not answering
    BLOCKED:   (96, 100, 104),     # not configured: grey, deliberately dull
}

# Windows asks for whichever of these fits the place it is drawing.
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _blend(a: Tuple[int, int, int], b: Tuple[int, int, int],
           amount: float) -> Tuple[int, int, int]:
    return tuple(int(round(x + (y - x) * amount)) for x, y in zip(a, b))


def mark(size: int = 256, state: str = READY, *, pulse: float = 0.0,
         badge: Optional[int] = None):
    """
    The mark at `size` pixels, as a PIL image with a transparent corner.

    `pulse` runs 0 → 1 and back while recording; the tray advances it every
    couple of seconds so the icon visibly breathes. It is the only moving
    thing in the agent's interface, which is what makes "this room is being
    recorded" readable from across a desk.

    `badge` is a count of pieces this PC is still holding. It is drawn only
    when there are some, because an empty badge would train people to ignore
    a full one.
    """
    from PIL import Image, ImageDraw

    # Drawn at four times the asked-for size and shrunk, which is what gives
    # smooth curves at 16 px without any anti-aliasing work of our own.
    scale = 4
    px = size * scale
    image = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    colour = STATE_COLOUR.get(state, STATE_COLOUR[READY])
    if state == RECORDING:
        # Breathe between the resting teal and the recording red.
        colour = _blend(STATE_COLOUR[RECORDING], (222, 96, 100), pulse * 0.55)

    unit = px / 64.0

    def box(*values):
        return [v * unit for v in values]

    # The rounded square.
    draw.rounded_rectangle(box(2, 2, 62, 62), radius=14 * unit,
                           fill=_blend(INK, colour, 0.82) + (255,))

    # The microphone: a capsule, the cradle under it, and a stand.
    draw.rounded_rectangle(box(26, 13, 38, 35), radius=6 * unit, fill=FACE + (255,))
    draw.arc(box(20, 24, 44, 44), start=0, end=180, fill=FACE + (255,),
             width=int(2.6 * unit))
    draw.line(box(32, 44, 32, 50), fill=FACE + (255,), width=int(2.6 * unit))
    draw.line(box(25, 50, 39, 50), fill=FACE + (255,), width=int(2.6 * unit))

    # Three bars: speech. The middle one grows with the pulse while recording,
    # so the mark reads as sound rather than as a picture of a microphone.
    grown = 3 * pulse if state == RECORDING else 0
    for x, half in ((13.5, 4.5), (18.5, 8), (45.5, 8), (50.5, 4.5)):
        reach = half + (grown if half > 6 else 0)
        draw.rounded_rectangle(box(x - 1.4, 30 - reach, x + 1.4, 30 + reach),
                               radius=1.4 * unit, fill=FACE + (205,))

    _state_glyph(draw, box, unit, state, px)

    if badge:
        _badge(draw, unit, badge)

    return image.resize((size, size), Image.LANCZOS)


def _state_glyph(draw, box, unit, state: str, px: int) -> None:
    """The small sign in the corner that says what the agent is doing."""
    white = (255, 255, 255, 255)
    if state == RECORDING:
        draw.ellipse(box(43, 43, 58, 58), fill=(214, 40, 44, 255),
                     outline=white, width=int(1.6 * unit))
    elif state == PAUSED:
        draw.rounded_rectangle(box(45, 44, 48.5, 57), radius=unit, fill=white)
        draw.rounded_rectangle(box(52, 44, 55.5, 57), radius=unit, fill=white)
    elif state == OFFLINE:
        # A broken link: recording continues, the server is not hearing it.
        draw.ellipse(box(43, 43, 58, 58), fill=(46, 82, 120, 255),
                     outline=white, width=int(1.6 * unit))
        draw.line(box(46.5, 50.5, 54.5, 50.5), fill=white, width=int(1.8 * unit))
    elif state == BLOCKED:
        draw.ellipse(box(43, 43, 58, 58), fill=(96, 100, 104, 255),
                     outline=white, width=int(1.6 * unit))
        draw.line(box(50.5, 46.5, 50.5, 52), fill=white, width=int(1.8 * unit))
        draw.ellipse(box(49.4, 53.4, 51.8, 55.8), fill=white)


def _badge(draw, unit, count: int) -> None:
    """How many pieces this PC is still holding, when it is holding any."""
    from PIL import ImageFont

    text = str(count) if count < 100 else "99+"
    left, top = 2 * unit, 2 * unit
    width = (13 if len(text) < 2 else 18) * unit
    draw.rounded_rectangle([left, top, left + width, top + 13 * unit],
                           radius=6.5 * unit, fill=(214, 40, 44, 255))
    try:
        font = ImageFont.truetype("segoeuib.ttf", int(9.5 * unit))
    except Exception:
        font = ImageFont.load_default()
    draw.text((left + width / 2, top + 6.5 * unit), text, font=font,
              fill=(255, 255, 255, 255), anchor="mm")


def write_ico(path) -> None:
    """The multi-size .ico PyInstaller compiles into the executable."""
    from PIL import Image

    largest = mark(256, READY)
    frames = [mark(size, READY) for size in ICO_SIZES if size != 256]
    largest.save(path, format="ICO",
                 sizes=[(s, s) for s in ICO_SIZES],
                 append_images=frames)
