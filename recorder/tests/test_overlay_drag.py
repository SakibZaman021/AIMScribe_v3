"""
The on-screen card can be dragged, never off the screen, and stays where it
was left. Built with real tkinter; skipped where there is no display.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ui import overlay as ov


@pytest.fixture(scope="module")
def window():
    """One real window for the module: creating and destroying a Tk root per
    test now and then fails on Windows, which would make these tests flaky."""
    runtime = SimpleNamespace(cfg=SimpleNamespace(pause=SimpleNamespace(reasons=[])),
                              controller=None, loop=None)
    o = ov.Overlay(runtime)
    try:
        o.build()
    except Exception as exc:                     # no display, or no Tcl/Tk
        pytest.skip(f"no display for tkinter: {exc}")
    o.window.deiconify()
    o.root.update()
    yield o
    o.root.destroy()


@pytest.fixture
def card(window):
    """The window as a fresh card: no spot remembered, no drag, normal height."""
    window._spot = window._grab = None
    window._height = ov.HEIGHT
    window._place()
    window.root.update()
    return window


def press(o, widget_x, widget_y):
    """A press at a point of the card, given in the card's own coordinates."""
    return SimpleNamespace(x_root=o.window.winfo_rootx() + widget_x,
                           y_root=o.window.winfo_rooty() + widget_y)


def where(o):
    o.root.update()
    return o.window.winfo_rootx(), o.window.winfo_rooty()


def test_it_starts_at_the_top_right(card):
    x, y = where(card)
    assert x == card.window.winfo_screenwidth() - ov.WIDTH - ov.MARGIN and y == ov.MARGIN


def test_dragging_moves_it_and_it_stays_there(card):
    start = where(card)
    card._drag_start(press(card, 30, 40))
    grabbed = press(card, 30, 40)
    card._drag_move(SimpleNamespace(x_root=grabbed.x_root - 300, y_root=grabbed.y_root + 200))
    card._drag_end()
    moved = where(card)
    assert moved == (start[0] - 300, start[1] + 200)

    # Hidden after the consultation and shown for the next: still there.
    card.window.withdraw()
    card._place()
    card.window.deiconify()
    assert where(card) == moved
    # Growing by a line of words does not send it back to the corner either.
    card._height = ov.HEIGHT + ov.NOTE_HEIGHT
    card._place()
    assert where(card) == moved


def test_it_cannot_be_dragged_off_the_screen(card):
    screen_w = card.window.winfo_screenwidth()
    screen_h = card.window.winfo_screenheight()
    card._drag_start(press(card, 10, 10))
    card._drag_move(SimpleNamespace(x_root=-5000, y_root=-5000))
    assert where(card) == (0, 0)
    card._drag_move(SimpleNamespace(x_root=screen_w + 5000, y_root=screen_h + 5000))
    assert where(card) == (screen_w - ov.WIDTH, screen_h - card._height)
    card._drag_end()


def test_the_buttons_are_not_drag_handles(card):
    # The drag is bound on the card's canvas only; Stop and Pause keep their press.
    assert card.canvas.bind("<B1-Motion>")
    assert not card.stop_button.bind("<B1-Motion>")
    assert card.stop_button.bind("<Button-1>")
