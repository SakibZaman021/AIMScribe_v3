"""
3.9.11 (26 Nov 2026): the glass strip.

The owner's design: two clear glass tiles with no white card and no dot; after
a Stop reason both tiles go for the rest of that recording and come back with
the next patient; while a sensitive part is marked the Mark tile pulses.
"""
from __future__ import annotations

import pytest

from ui.overlay_model import OverlayState

PIL = pytest.importorskip("PIL")


def recording(session="S1", **extra):
    return {"is_recording": True, "is_paused": False, "session_id": session,
            "consent_open": True, **extra}


# ---------------------------------------------------------------- the rules

def test_a_stop_reason_hides_both_buttons_for_that_recording_only():
    state = OverlayState()
    assert state.view(recording("S1")).visible
    state.stop_chosen("S1")
    view = state.view(recording("S1"))
    assert view.stopped and not view.visible
    # The next patient's recording: both buttons are back.
    view = state.view(recording("S2"))
    assert view.visible and not view.stopped
    assert state.stopped_session is None


def test_an_open_menu_still_shows_after_stop_until_it_closes():
    state = OverlayState()
    state.stop_chosen("S1")
    state.menu_open = True
    assert state.view(recording("S1")).visible
    state.menu_open = False
    assert not state.view(recording("S1")).visible


def test_the_mark_button_pulses_while_a_part_is_marked():
    state = OverlayState()
    assert not state.view(recording()).pulse
    marked = recording(sensitive={"reason": "interruption", "label": "Interruption",
                                  "seconds": 3})
    view = state.view(marked)
    assert view.pulse and view.marked == "Interruption" and view.visible
    assert not state.view({}).pulse            # nothing recording: nothing pulses


# ---------------------------------------------------------------- the drawing

def test_the_strip_is_two_tiles_on_a_see_through_picture():
    from ui import glass

    box = glass.layout(1.0)
    picture = glass.strip(1.0)
    assert picture.mode == "RGBA" and picture.size == (box.width, box.height)
    alpha = picture.getchannel("A")
    # Corners: nothing to see (only the 1-of-255 grip that lets it be dragged).
    assert alpha.getpixel((0, 0)) <= glass.GRIP_ALPHA
    assert alpha.getpixel((box.width - 1, box.height - 1)) <= glass.GRIP_ALPHA
    # The middle of each tile: solid colour, not white.
    for face, colour in ((box.stop, glass.RED), (box.mark, glass.BLUE)):
        x, y = face[0] + 4, (face[1] + face[3]) // 2
        r, g, b, a = picture.getpixel((x, y))
        assert a > 200
        assert abs(r - colour[0]) < 80 and abs(b - colour[2]) < 80, (r, g, b)


def test_a_marked_part_turns_the_mark_tile_amber():
    from ui import glass

    box = glass.layout(1.0)
    picture = glass.strip(1.0, mark_tint=glass.AMBER)
    x, y = box.mark[0] + 4, (box.mark[1] + box.mark[3]) // 2
    r, g, b, a = picture.getpixel((x, y))
    assert r > 200 and b < 90, (r, g, b)


def test_the_strip_grows_with_the_display_scale():
    from ui import glass

    small, large = glass.layout(1.0), glass.layout(1.5)
    assert large.width == round(small.width * 1.5) or abs(large.width - small.width * 1.5) <= 2
    assert glass.strip(1.5).size == (large.width, large.height)


def test_the_pointer_finds_each_tile():
    from ui import glass

    box = glass.layout(1.0)
    assert glass.hit(box, box.stop[0] + 2, box.stop[1] + 2) == "stop"
    assert glass.hit(box, box.mark[2] - 2, box.mark[3] - 2) == "mark"
    assert glass.hit(box, 0, 0) == ""


def test_windows_gets_premultiplied_blue_green_red_alpha():
    from PIL import Image
    from ui.layered import premultiplied_bgra

    pixel = Image.new("RGBA", (1, 1), (200, 100, 50, 128))
    b, g, r, a = premultiplied_bgra(pixel)
    assert a == 128
    assert (r, g, b) == (round(200 * 128 / 255), round(100 * 128 / 255), round(50 * 128 / 255))
