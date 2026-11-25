"""
The on-screen control, drawn with tkinter (3.9.8).

A slim, see-through strip at the top right of the screen until the doctor drags
it elsewhere, shown only while a consultation is recording. One small dot says
a recording is on; below it two icons, one above the other:

    Stop   - a square in a circle.
    Mark   - two bars in a circle (a sensitive part).

There is no clock. Hovering over an icon, or tapping it, opens its menu beside
the strip; one more click chooses. A tap alone does nothing. What each choice
does, and what the menus say, is decided in overlay_model.py, which has no
tkinter in it and is tested on its own; this file only draws.

25 Nov 2026: the strip is a solid white card with rounded corners, fully
opaque, and the icons are drawn smooth against the card's own white - before,
they were see-through and their edges blended into a colour the screen behind
did not have, so they looked blurred. It shows no words of its own: no notes,
no "Checking with CMED", no microphone or server messages; those reach the
AIMS LAB server, where the data collectors and researchers act on them. It can
be dragged by the card but never off the screen, and stays where it was left.

tkinter runs on its own thread, which owns every widget. Choices are handed to
the agent's event loop, as the tray menu's are; events from the controller
arrive through a queue this thread drains several times a second.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import sys
import threading
import time
from typing import Any, Dict, Optional

from ui.overlay_model import (DELETE_QUESTION, MARK, NEEDS_DELETE_CONFIRMATION, STOP,
                              OverlayActions, OverlayState, menu)

logger = logging.getLogger(__name__)

PAPER = "#ffffff"
EDGE = "#d9dde1"
INK = "#1b1b1f"
MUTED = "#6b7178"
RED = "#d92d3a"
RED_DEEP = "#a8202b"
BLUE = "#1f5fbf"
AMBER = "#b5791a"
TEAL = "#17836f"
OFF = "#b8bec4"
HOVER = "#eef3fb"
FONT = "Segoe UI"

KEY = "#010203"                # the strip's background: drawn see-through

# 3.9.8: two looks, by clinic. Only the dot and the two icons are drawn; around
# them the screen shows through. Their edges are smoothed against the colour
# they sit on, so they are sharp at any display scale.
#   sidebar - Amader Susastho: at the left edge, over the EHR's dark teal
#             sidebar, below its own icons.
#   light   - Aalo Clinic and everything else: top right, over a white page.
# 25 Nov: in both looks the icons sit on a solid white card (`card`, edged with
# `card_edge`); only the screen outside the card's rounded corners (`key`) is
# see-through. The icons blend into the card's white, which is always there.
THEMES = {
    "sidebar": {"matte": "#00566f", "key": "#00566e", "side": "left", "height": 0.42,
                "dot_off": "#b8bec4", "card": "#ffffff", "card_edge": "#8fb8c4"},
    "light": {"matte": "#ffffff", "key": "#fefefd", "side": "right", "height": None,
              "dot_off": "#b8bec4", "card": "#ffffff", "card_edge": "#c9d0d8"},
}
CARD_RADIUS = 16               # the card's corners


def theme_for(hospital_id, override: str = "") -> str:
    """Which look a PC uses: AIMS_OVERLAY_THEME when set, else by its clinic."""
    if override in THEMES:
        return override
    return "sidebar" if "SUSASTHO" in str(hospital_id or "").upper() else "light"
WIDTH, HEIGHT = 56, 132        # the strip
NOTE_HEIGHT = 0                # kept for older callers: notes now sit beside it
MARGIN = 20                    # from the screen's right edge, where the strip starts
TOP = 120                      # from the top: below a browser's own close button
ICON = 44                      # each icon's diameter
DOT = 10
IDLE_ALPHA = 1.0               # 25 Nov: always solid (see-through looked blurred)
NEAR_ALPHA = 1.0
MENU_WIDTH = 300
MENU_KEY = "#fdfdfc"           # around the menu's rounded corners: see-through
MENU_RADIUS = 14               # the menu's corners
BUTTON_RADIUS = 9              # each choice's corners
BUTTON_FILL = "#f3f6fa"
BUTTON_EDGE = "#cfd6df"
BUTTON_HOVER_FILL = "#e4edfb"
BUTTON_HOVER_EDGE = BLUE
FADED_FILL = "#fafbfc"         # a choice that is closed: faded out, not pressable
FADED_EDGE = "#eef1f4"
FADED_INK = "#c9ced5"
CLOSE_DELAY_MS = 450           # the pointer may cross the gap to the menu

SCALE = 1.0                    # display scale, set once by _enable_dpi_awareness()


def _enable_dpi_awareness() -> float:
    """
    Tell Windows this process draws its own pixels, and return the display scale.
    Without it Windows stretches the window at any scaling above 100% and every
    edge is smeared.
    """
    global SCALE
    if sys.platform != "win32":
        return 1.0
    import ctypes

    user32 = ctypes.windll.user32
    attempts = (
        lambda: user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),  # per-monitor v2
        lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),             # per-monitor
        lambda: user32.SetProcessDPIAware(),                                # system-wide
    )
    for attempt in attempts:
        try:
            if attempt():
                break
        except Exception:
            continue
    dpi = 96
    try:
        dpi = user32.GetDpiForSystem() or 96
    except Exception:
        pass
    SCALE = max(1.0, min(3.0, dpi / 96.0))
    return SCALE


def _apply_scale() -> None:
    """Grow the strip to match the display scale (fonts follow `tk scaling`)."""
    global WIDTH, HEIGHT, ICON, DOT, MENU_WIDTH, TOP, MENU_RADIUS, BUTTON_RADIUS, CARD_RADIUS
    if SCALE == 1.0:
        return
    MENU_RADIUS, BUTTON_RADIUS = round(MENU_RADIUS * SCALE), round(BUTTON_RADIUS * SCALE)
    CARD_RADIUS = round(CARD_RADIUS * SCALE)
    WIDTH, HEIGHT = round(WIDTH * SCALE), round(HEIGHT * SCALE)
    TOP = round(TOP * SCALE)
    ICON, DOT = round(ICON * SCALE), round(DOT * SCALE)
    MENU_WIDTH = round(MENU_WIDTH * SCALE)


class Overlay:
    POLL_MS = 250
    IDLE_POLL_MS = 500

    def __init__(self, runtime, *, visible: bool = True):
        self.runtime = runtime
        self.state = OverlayState()
        self.actions = OverlayActions(self._submit)
        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._visible = visible
        self._thread: Optional[threading.Thread] = None
        self.root = None
        self.window = None
        self.canvas = None
        self.menu_window = None
        self.menu_kind: Optional[str] = None
        self.toast = None
        self._toast_text = ""
        self._view = None
        self._height = HEIGHT
        self._spot: Optional[tuple] = None      # where the doctor left the strip
        self._grab: Optional[tuple] = None      # the pointer's offset while dragging
        self._close_job = None
        self._reminded: Optional[str] = None    # the marked part already asked about
        self._near = False
        import os
        self._theme_override = os.environ.get("AIMS_OVERLAY_THEME", "").strip().lower()
        self._theme = theme_for("", self._theme_override)

    # ---- lifecycle ----

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="Overlay", daemon=True)
        self._thread.start()

    def post(self, event: str, data: Dict[str, Any]) -> None:
        """Called from any thread."""
        self._events.put((event, dict(data or {})))

    def _run(self) -> None:
        try:
            self.build()
            self.root.after(self.POLL_MS, self._tick)
            self.root.mainloop()
        except Exception as exc:
            # Recording does not depend on this window; the tray still works.
            logger.error("On-screen control unavailable: %s", exc, exc_info=True)

    # ---- controller calls ----

    def _submit(self, make_coro) -> None:
        controller = self.runtime.controller
        loop = self.runtime.loop
        if controller is None or loop is None:
            logger.warning("On-screen choice ignored: the recorder is still starting")
            return
        future = asyncio.run_coroutine_threadsafe(make_coro(controller), loop)
        future.add_done_callback(self._report)

    def _report(self, future) -> None:
        exc = future.exception()
        if exc is not None:
            logger.warning("On-screen control action failed: %s", exc)

    def _status(self) -> Dict[str, Any]:
        controller = self.runtime.controller
        if controller is None:
            return {}
        try:
            return controller.status()
        except Exception:
            return {}

    # ---- the window ----

    def build(self) -> None:
        import tkinter as tk

        _enable_dpi_awareness()
        _apply_scale()
        self._height = HEIGHT                       # the scaled height, not the 100% one

        self.root = tk.Tk()
        self.root.withdraw()
        try:
            self.root.tk.call("tk", "scaling", (96.0 * SCALE) / 72.0)
        except Exception:
            pass

        key = THEMES[self._theme]["key"]
        win = tk.Toplevel(self.root, bg=key)
        win.overrideredirect(True)                  # no title bar: no minimise, no close
        win.attributes("-topmost", True)
        win.withdraw()
        try:
            win.attributes("-transparentcolor", key)
            self._backdrop = key
            self._see_through = True
        except Exception:
            self._backdrop = THEMES[self._theme]["matte"]
            self._see_through = False
        self._set_alpha(win, IDLE_ALPHA)
        self.window = win

        self.canvas = tk.Canvas(win, width=WIDTH, height=HEIGHT, bg=self._backdrop,
                                highlightthickness=0, bd=0, cursor="fleur")
        self.canvas.pack()
        # Dragged by the strip itself; the icons are their own widgets, so a
        # press on one opens its menu and never starts a drag.
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        win.bind("<Enter>", lambda _e: self._come_near(True))
        win.bind("<Leave>", lambda _e: self._come_near(False))

        left = (WIDTH - ICON) // 2
        self.stop_button = self._icon(win, STOP)
        self.stop_button.place(x=left, y=round(26 * SCALE), width=ICON, height=ICON)
        self.mark_button = self._icon(win, MARK)
        self.mark_button.place(x=left, y=round(26 * SCALE) + ICON + round(14 * SCALE),
                               width=ICON, height=ICON)
        self._place()

    @staticmethod
    def _set_alpha(window, alpha: float) -> None:
        try:
            window.attributes("-alpha", alpha)
        except Exception:
            pass

    def _come_near(self, near: bool) -> None:
        self._near = near
        self._set_alpha(self.window, NEAR_ALPHA if near or self.menu_window else IDLE_ALPHA)

    def _icon(self, parent, kind: str):
        """An icon that opens its menu on hover or tap, and does nothing else."""
        import tkinter as tk

        canvas = tk.Canvas(parent, bg=THEMES[self._theme]["card"], highlightthickness=0,
                           bd=0, takefocus=1, cursor="hand2")
        canvas.kind = kind
        canvas.hover = False
        canvas.draw = lambda: self._draw_icon(canvas)

        def opened(_event=None):
            canvas.hover = True
            canvas.draw()
            self._come_near(True)
            self._open_menu(kind)

        canvas.bind("<Enter>", opened)
        for sequence in ("<Button-1>", "<Return>", "<space>"):
            canvas.bind(sequence, opened)
        canvas.bind("<Leave>", lambda _e: (setattr(canvas, "hover", False), canvas.draw(),
                                           self._close_menu_soon()))
        return canvas

    # ---- drawing ----

    def _icon_colours(self, kind: str):
        view = self._view
        if view is None or not view.visible:
            return OFF, PAPER
        if kind == MARK and view.marked:
            if view.alarm and int(time.monotonic() * 2) % 2 == 0:
                return RED, RED            # blinking: the part has run two minutes
            return AMBER, AMBER            # a sensitive part is marked
        return (RED if kind == STOP else BLUE), PAPER

    def _draw_icon(self, canvas) -> None:
        canvas.delete("all")
        size = canvas.winfo_width() or ICON
        ring, inside = self._icon_colours(canvas.kind)
        if canvas.hover:
            inside = HOVER if inside == PAPER else inside
        theme = THEMES[self._theme]
        art = self._icon_image(canvas.kind, size, ring, inside, matte=theme["card"])
        if art is not None:
            # Held on the widget: a PhotoImage only the canvas refers to is
            # collected, and the icon then draws as nothing.
            canvas.art = art
            canvas.create_image(0, 0, anchor="nw", image=art)
            return
        thick = max(2, round(3 * SCALE))
        pad = thick / 2 + 1
        canvas.create_oval(pad, pad, size - pad, size - pad, fill=inside, outline=ring,
                           width=thick)
        glyph = PAPER if inside != PAPER and inside != HOVER else ring
        third = size / 3
        if canvas.kind == STOP:
            canvas.create_rectangle(third, third, size - third, size - third,
                                    fill=glyph, outline=glyph)
        else:
            bar = size / 9
            top, bottom = third * 0.95, size - third * 0.95
            canvas.create_rectangle(size / 2 - 2.2 * bar, top, size / 2 - 0.8 * bar,
                                    bottom, fill=glyph, outline=glyph)
            canvas.create_rectangle(size / 2 + 0.8 * bar, top, size / 2 + 2.2 * bar,
                                    bottom, fill=glyph, outline=glyph)

    @staticmethod
    def _icon_image(kind: str, size: int, ring: str, inside: str, *,
                    matte: str = PAPER, key: Optional[str] = None):
        """
        The icon drawn four times larger and scaled down, so the circle has a
        smooth edge (tkinter's own circles are drawn as a staircase). Its edge
        blends into `matte`, the colour it sits on; outside the circle the
        pixels are `key`, which the window shows as see-through. None without
        Pillow; the plain shapes are drawn instead.
        """
        if size < 8:
            return None
        try:
            from PIL import Image, ImageDraw, ImageTk
        except ImportError:
            return None

        def rgb(colour):
            return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))

        ss = 4
        big = size * ss
        image = Image.new("RGB", (big, big), rgb(matte))
        draw = ImageDraw.Draw(image)
        thick = max(2, round(3 * SCALE)) * ss
        pad = thick // 2 + ss
        draw.ellipse([pad, pad, big - pad, big - pad], fill=rgb(inside), outline=rgb(ring),
                     width=thick)
        glyph = rgb(PAPER) if inside not in (PAPER, HOVER) else rgb(ring)
        third = big / 3
        if kind == STOP:
            draw.rounded_rectangle([third, third, big - third, big - third],
                                   radius=round(2 * SCALE) * ss, fill=glyph)
        else:
            bar, top, bottom = big / 9, third * 0.95, big - third * 0.95
            for left in (big / 2 - 2.2 * bar, big / 2 + 0.8 * bar):
                draw.rounded_rectangle([left, top, left + 1.4 * bar, bottom],
                                       radius=round(1.5 * SCALE) * ss, fill=glyph)
        small = image.resize((size, size), Image.LANCZOS)
        if key is not None:
            # Wholly outside the circle: see-through. The edge keeps its blend.
            mask = Image.new("L", (big, big), 0)
            ImageDraw.Draw(mask).ellipse([pad - thick // 2, pad - thick // 2,
                                          big - pad + thick // 2, big - pad + thick // 2],
                                         fill=255)
            mask = mask.resize((size, size), Image.LANCZOS)
            small.paste(Image.new("RGB", (size, size), rgb(key)), (0, 0),
                        mask.point(lambda v: 255 if v == 0 else 0))
        return ImageTk.PhotoImage(small)

    def _set_theme(self, theme: str) -> None:
        """Change look when the PC's clinic is known (or changes)."""
        if theme == self._theme or theme not in THEMES:
            return
        self._theme = theme
        if self._see_through:
            key = THEMES[theme]["key"]
            self._backdrop = key
            try:
                self.window.configure(bg=key)
                self.window.attributes("-transparentcolor", key)
            except Exception:
                pass
        else:
            self._backdrop = THEMES[theme]["matte"]
        self.canvas.configure(bg=self._backdrop)
        for widget in (self.stop_button, self.mark_button):
            widget.configure(bg=THEMES[theme]["card"])
        if self._spot is None:
            self._place()

    def _paint(self, view) -> None:
        """The solid card, and on it the dot that says a recording is on."""
        canvas = self.canvas
        canvas.delete("all")
        theme = THEMES[self._theme]
        art = self._rounded_image(WIDTH, self._height, CARD_RADIUS, fill=theme["card"],
                                  edge=theme["card_edge"], matte=theme["card_edge"],
                                  key=self._backdrop if self._see_through else None)
        if art is not None:
            canvas.art = art
            canvas.create_image(0, 0, anchor="nw", image=art)
        else:
            canvas.create_rectangle(0, 0, WIDTH, HEIGHT, fill=theme["card"],
                                    outline=theme["card_edge"])
        colour = {"recording": RED, "waiting": AMBER, "problem": RED}.get(
            view.dot, THEMES[self._theme]["dot_off"])
        centre = WIDTH / 2
        top = round(9 * SCALE)
        dot = self._dot_image(DOT, colour, theme["card"])
        if dot is not None:
            canvas.dot_art = dot
            canvas.create_image(round(centre - DOT / 2), top, anchor="nw", image=dot)
        else:
            canvas.create_oval(centre - DOT / 2, top, centre + DOT / 2, top + DOT,
                               fill=colour, outline=colour)

    @staticmethod
    def _dot_image(size: int, colour: str, matte: str):
        """The dot, smooth-edged against the card's white. None without Pillow."""
        if size < 4:
            return None
        try:
            from PIL import Image, ImageDraw, ImageTk
        except ImportError:
            return None
        ss = 4
        image = Image.new("RGB", (size * ss, size * ss), matte)
        ImageDraw.Draw(image).ellipse([0, 0, size * ss - 1, size * ss - 1], fill=colour)
        return ImageTk.PhotoImage(image.resize((size, size), Image.LANCZOS))

    @staticmethod
    def _round_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
        points = [
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, **kwargs)

    # ---- where the strip is ----

    def _place(self) -> None:
        """Where the doctor left it, or the top right; always wholly on the screen."""
        self.window.update_idletasks()
        x, y = self._spot or self._home()
        x, y = self._on_screen(x, y)
        self.window.geometry(f"{WIDTH}x{self._height}+{x}+{y}")

    def _home(self) -> tuple:
        """Where the strip starts for this look, before the doctor moves it."""
        theme = THEMES[self._theme]
        if theme["side"] == "left":
            # Centred on the EHR's sidebar, below its own icons.
            sidebar = round(86 * SCALE)
            return (max(0, (sidebar - WIDTH) // 2),
                    round(self.window.winfo_screenheight() * theme["height"]))
        return self.window.winfo_screenwidth() - WIDTH - MARGIN, TOP

    def _on_screen(self, x: int, y: int) -> tuple:
        right = self.window.winfo_screenwidth() - WIDTH
        bottom = self.window.winfo_screenheight() - self._height
        return max(0, min(int(x), right)), max(0, min(int(y), bottom))

    def _drag_start(self, event) -> None:
        self._grab = (event.x_root - self.window.winfo_rootx(),
                      event.y_root - self.window.winfo_rooty())

    def _drag_move(self, event) -> None:
        if self._grab is None:
            return
        x, y = self._on_screen(event.x_root - self._grab[0], event.y_root - self._grab[1])
        self._spot = (x, y)
        self.window.geometry(f"+{x}+{y}")
        self._close_menu()

    def _drag_end(self, _event=None) -> None:
        self._grab = None

    # ---- refresh ----

    def _tick(self) -> None:
        try:
            while True:
                event, data = self._events.get_nowait()
                self.state.on_event(event, data)
        except queue.Empty:
            pass
        status = None
        try:
            status = self._status()
            self.apply(self.state.view(status), status)
        except Exception as exc:
            logger.debug("On-screen control refresh failed: %s", exc)
        recording = bool(status) and (status.get("is_recording") or status.get("is_paused"))
        self.root.after(self.POLL_MS if recording else self.IDLE_POLL_MS, self._tick)

    def apply(self, view, status: Optional[Dict[str, Any]] = None) -> None:
        status = status if status is not None else {}
        if status.get("hospital_id"):
            self._set_theme(theme_for(status.get("hospital_id"), self._theme_override))
        previous = self._view
        self._view = view
        self.stop_button.draw()
        self.mark_button.draw()
        self._paint(view)
        self._show_note(view.note)

        # A marked part that has run two minutes: its menu opens by itself, once.
        mark_key = f"{status.get('session_id')}:{view.marked}" if view.marked else None
        if view.alarm and mark_key and self._reminded != mark_key:
            self._reminded = mark_key
            self._open_menu(MARK, stay=True)
        if not view.marked:
            self._reminded = None
        # The refusal closed or a part ended while its menu was open: redraw it.
        if self.menu_window is not None and previous is not None and \
                (previous.consent_open, previous.marked, previous.alarm) != \
                (view.consent_open, view.marked, view.alarm):
            self._open_menu(self.menu_kind, stay=True)

        if view.visible and self._visible:
            if self.window.state() == "withdrawn":
                self._place()
                self.window.deiconify()
            self._keep_on_top(active=bool(status.get("is_recording")
                                          or status.get("is_paused")))
        elif self.window.state() != "withdrawn":
            logger.info("On-screen control hidden: recording=%s session=%s menu_open=%s",
                        status.get("is_recording"), status.get("session_id"),
                        self.state.menu_open)
            self._close_menu()
            self.window.withdraw()

    def _keep_on_top(self, *, active: bool = False) -> None:
        """
        Stay in front for as long as the consultation lasts. Windows drops the
        flag when another window takes focus in certain ways, and tk keeps
        reporting the flag it set; so while a consultation is open the flag and
        the lift are re-applied every tick.
        """
        try:
            if active:
                self.window.attributes("-topmost", True)
                self.window.lift()
                self._recover_if_offscreen()
            elif not self.window.attributes("-topmost"):
                self.window.attributes("-topmost", True)
                self.window.lift()
        except Exception as exc:
            logger.debug("Could not keep the control on top: %s", exc)

    def _recover_if_offscreen(self) -> None:
        """Put the strip back if it is no longer on a screen (a monitor unplugged)."""
        try:
            x, y = self.window.winfo_rootx(), self.window.winfo_rooty()
            width = self.window.winfo_screenwidth()
            height = self.window.winfo_screenheight()
            if x < -40 or y < -40 or x > width - 40 or y > height - 40:
                logger.info("On-screen control was off-screen at %s,%s; replacing it", x, y)
                self._place()
        except Exception:
            pass

    # ---- the note beside the strip ----

    def _show_note(self, text: str) -> None:
        """A line of words beside the strip; none when there is nothing to say."""
        import tkinter as tk

        if self.menu_window is not None or not text or self.window.state() == "withdrawn":
            if self.toast is not None:
                self.toast.destroy()
                self.toast, self._toast_text = None, ""
            return
        if self.toast is not None and self._toast_text == text:
            return
        if self.toast is not None:
            self.toast.destroy()
        toast = tk.Toplevel(self.window, bg=EDGE)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        colour = RED if ("no signal" in text or "not working" in text) else \
            TEAL if text == "Confirmed" else INK
        tk.Label(toast, text=text, bg=PAPER, fg=colour, font=(FONT, 9), padx=10, pady=6,
                 wraplength=MENU_WIDTH - 20, justify="left").pack(padx=1, pady=1)
        self._beside(toast, self.window.winfo_rooty())
        self.toast, self._toast_text = toast, text

    def _beside(self, popup, y: int) -> None:
        """To the left of the strip, or to its right where there is no room."""
        popup.update_idletasks()
        width, height = popup.winfo_reqwidth(), popup.winfo_reqheight()
        x = self.window.winfo_rootx() - width - round(8 * SCALE)
        if x < 0:
            x = self.window.winfo_rootx() + WIDTH + round(8 * SCALE)
        y = max(0, min(y, self.window.winfo_screenheight() - height))
        popup.geometry(f"+{x}+{y}")

    # ---- the menus ----

    def _open_menu(self, kind: Optional[str], *, stay: bool = False) -> None:
        """An icon's menu, beside it: a title saying what happens, and the choices."""
        import tkinter as tk

        if kind is None or self._view is None or not self._view.visible:
            return
        self._cancel_close()
        if self.menu_window is not None and self.menu_kind == kind and not stay:
            return
        self._close_menu()

        title, items = menu(kind, self._view)
        pad = round(14 * SCALE)
        inner_width = MENU_WIDTH - 2 * pad

        # A rounded card: the window's own background is see-through, and the
        # card is drawn on it as an image with smooth corners.
        box = tk.Toplevel(self.window, bg=MENU_KEY)
        box.overrideredirect(True)
        box.attributes("-topmost", True)
        try:
            box.attributes("-transparentcolor", MENU_KEY)
        except Exception:
            pass
        card = tk.Canvas(box, bg=MENU_KEY, highlightthickness=0, bd=0)
        card.pack()
        inner = tk.Frame(card, bg=PAPER)
        tk.Label(inner, text=title, bg=PAPER, fg=MUTED, font=(FONT, 9),
                 wraplength=inner_width, justify="left", anchor="w"
                 ).pack(fill="x", anchor="w", pady=(0, round(8 * SCALE)))
        for value, words, enabled in items:
            button = self._menu_button(inner, words, inner_width, enabled=enabled,
                                       action=lambda k=kind, v=value: self._choose(k, v))
            button.pack(anchor="w", pady=(0, round(6 * SCALE)))
        inner.update_idletasks()
        width = inner_width + 2 * pad
        height = inner.winfo_reqheight() + 2 * pad - round(6 * SCALE)
        card.configure(width=width, height=height)
        art = self._rounded_image(width, height, MENU_RADIUS, fill=PAPER, edge=EDGE,
                                  matte=EDGE, key=MENU_KEY)
        if art is not None:
            card.art = art
            card.create_image(0, 0, anchor="nw", image=art)
        else:
            self._round_rect(card, 1, 1, width - 1, height - 1, MENU_RADIUS,
                             fill=PAPER, outline=EDGE)
        card.create_window(pad, pad, anchor="nw", window=inner)
        box.bind("<Enter>", lambda _e: self._cancel_close())
        box.bind("<Leave>", lambda _e: self._close_menu_soon())

        icon = self.stop_button if kind == STOP else self.mark_button
        self.menu_window, self.menu_kind = box, kind
        self.state.menu_open = True
        self._show_note("")                  # the menu takes the note's place
        self._beside(box, icon.winfo_rooty())
        self._set_alpha(self.window, NEAR_ALPHA)

    def _menu_button(self, parent, words: str, width: int, *, enabled: bool, action):
        """
        One choice drawn as a rounded button, so it is plain where to click.
        The whole button is the target; it lights up under the pointer.
        """
        import tkinter as tk
        import tkinter.font as tkfont

        font = tkfont.Font(family=FONT, size=10, weight="bold" if enabled else "normal",
                           slant="roman" if enabled else "italic")
        padx, pady = round(12 * SCALE), round(8 * SCALE)
        lines = self._wrap(words, font, width - 2 * padx)
        height = len(lines) * font.metrics("linespace") + 2 * pady
        button = tk.Canvas(parent, width=width, height=height, bg=PAPER,
                           highlightthickness=0, bd=0,
                           cursor="hand2" if enabled else "arrow")
        ink = INK if enabled else FADED_INK

        def draw(hover: bool = False) -> None:
            button.delete("all")
            if not enabled:
                fill, edge = FADED_FILL, FADED_EDGE      # faded out: closed
            elif hover:
                fill, edge = BUTTON_HOVER_FILL, BUTTON_HOVER_EDGE
            else:
                fill, edge = BUTTON_FILL, BUTTON_EDGE
            art = self._rounded_image(width, height, BUTTON_RADIUS, fill=fill, edge=edge,
                                      matte=PAPER, key=None)
            if art is not None:
                button.art = art
                button.create_image(0, 0, anchor="nw", image=art)
            else:
                self._round_rect(button, 1, 1, width - 1, height - 1, BUTTON_RADIUS,
                                 fill=fill, outline=edge)
            button.create_text(padx, height / 2, text="\n".join(lines), anchor="w",
                               fill=ink, font=font, justify="left")

        draw()
        if enabled:
            button.bind("<Enter>", lambda _e: draw(True))
            button.bind("<Leave>", lambda _e: draw(False))
            button.bind("<Button-1>", lambda _e: action())
        return button

    @staticmethod
    def _wrap(text: str, font, width: int):
        """Break the words into lines that fit the button."""
        lines, line = [], ""
        for word in text.split():
            trial = f"{line} {word}".strip()
            if line and font.measure(trial) > width:
                lines.append(line)
                line = word
            else:
                line = trial
        return lines + [line] if line else lines or [""]

    @staticmethod
    def _rounded_image(width: int, height: int, radius: int, *, fill: str, edge: str,
                       matte: str, key: Optional[str]):
        """
        A rounded rectangle drawn four times larger and scaled down, so its
        corners are smooth; outside the corners the pixels are `key` (shown
        see-through) when given. None without Pillow.
        """
        if width < 4 or height < 4:
            return None
        try:
            from PIL import Image, ImageDraw, ImageTk
        except ImportError:
            return None

        def rgb(colour):
            return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))

        ss = 4
        big = (width * ss, height * ss)
        image = Image.new("RGB", big, rgb(matte))
        line = max(1, round(1.2 * SCALE)) * ss
        box = [line // 2, line // 2, big[0] - 1 - line // 2, big[1] - 1 - line // 2]
        ImageDraw.Draw(image).rounded_rectangle(box, radius=radius * ss, fill=rgb(fill),
                                                outline=rgb(edge), width=line)
        small = image.resize((width, height), Image.LANCZOS)
        if key is not None:
            mask = Image.new("L", big, 0)
            ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius * ss, fill=255)
            mask = mask.resize((width, height), Image.LANCZOS)
            small.paste(Image.new("RGB", (width, height), rgb(key)), (0, 0),
                        mask.point(lambda v: 255 if v == 0 else 0))
        return ImageTk.PhotoImage(small)

    def _choose(self, kind: str, value: str) -> None:
        consent_open = bool(self._view and self._view.consent_open)
        result = self.actions.choose(kind, value, consent_open=consent_open)
        if result == NEEDS_DELETE_CONFIRMATION:
            from tkinter import messagebox
            title, question = DELETE_QUESTION
            self._cancel_close()
            confirmed = messagebox.askyesno(title, question, icon="warning",
                                            parent=self.menu_window or self.window,
                                            default="no")
            result = self.actions.choose(kind, value, consent_open=consent_open,
                                         deletion_confirmed=True) if confirmed else ""
        if result:
            logger.info("On-screen choice %s/%s not sent: %s", kind, value, result)
        self._close_menu()

    def _close_menu_soon(self) -> None:
        self._cancel_close()
        if self.window is not None:
            self._close_job = self.window.after(CLOSE_DELAY_MS, self._close_if_away)

    def _cancel_close(self) -> None:
        if self._close_job is not None and self.window is not None:
            try:
                self.window.after_cancel(self._close_job)
            except Exception:
                pass
        self._close_job = None

    def _close_if_away(self) -> None:
        """Close the menu unless the pointer is on it or on an icon."""
        self._close_job = None
        try:
            x, y = self.window.winfo_pointerxy()
            under = self.window.winfo_containing(x, y)
        except Exception:
            under = None
        keep = (self.menu_window, self.stop_button, self.mark_button)
        while under is not None:
            if under in keep:
                return
            under = getattr(under, "master", None)
        self._close_menu()

    def _close_menu(self) -> None:
        self._cancel_close()
        if self.menu_window is not None:
            try:
                self.menu_window.destroy()
            except Exception:
                pass
        self.menu_window, self.menu_kind = None, None
        self.state.menu_open = False
        if self.window is not None:
            self._set_alpha(self.window, NEAR_ALPHA if self._near else IDLE_ALPHA)


__all__ = ["Overlay"]
