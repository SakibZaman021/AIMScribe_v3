"""
The on-screen control, drawn with tkinter (SRS 3.2 §7.8).

A card at the top right of the screen, shown only while a consultation is
recording or paused. It answers, without anyone having to click anything:

    is it recording        the state chip, and a dot that pulses
    for how long           the elapsed time, large enough to read across a desk
    who                    the patient this recording belongs to
    can it hear the room   a live meter driven by the microphone itself
    is it safe             how many pieces are sealed, and whether CMED has
                           confirmed the visit

and it carries the two controls a doctor may need: Stop and Pause
(SRS-UIX-03). It needs nothing from CMED to work (SRS-UIX-12), and it does not
move (SRS-UIX-02) - a control that can be dragged can be dragged off-screen,
and then Stop is not where anybody left it.

tkinter runs on its own thread, which owns every widget. Button presses are
handed to the agent's event loop, as the tray menu's are; events from the
controller arrive through a queue this thread drains several times a second.
What to show is decided in overlay_model.py, which has no tkinter in it and is
tested on its own; this file only draws.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import queue
import threading
from typing import Any, Dict, Optional

from ui.overlay_model import (DELETE_QUESTION, NEEDS_DELETE_CONFIRMATION, OverlayActions,
                              OverlayState, format_duration, pause_reasons)

logger = logging.getLogger(__name__)

# The card. Dark, because it sits over whatever CMED is showing and has to be
# separate from it at a glance - and because the same teal is the agent's mark.
CARD = "#12252b"
CARD_EDGE = "#1d3a42"
TEXT = "#f2f7f7"
DIM = "#8fa8ae"
RED = "#e0424c"
RED_DEEP = "#b4232c"
AMBER = "#e0a02a"
TEAL = "#2bb39b"
BLUE = "#4f8ff0"
PAPER = "#ffffff"
INK = "#1b1b1f"
MUTED = "#5f5f68"
FONT = "Segoe UI"

WIDTH, HEIGHT = 330, 186
BARS = 38                      # how many bars the meter keeps


class Overlay:
    # Fast enough that the meter looks alive and the seconds never appear to
    # stick; still only a few hundred cheap redraws a minute.
    POLL_MS = 120

    def __init__(self, runtime, *, visible: bool = True):
        self.runtime = runtime
        self.state = OverlayState()
        self.actions = OverlayActions(self._submit, pause_reasons(runtime.cfg.pause.reasons))
        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._visible = visible
        self._thread: Optional[threading.Thread] = None
        self.root = None
        self.window = None
        self.form = None
        self.canvas = None
        self._levels = collections.deque([0.0] * BARS, maxlen=BARS)
        self._phase = 0.0
        self._pause_text = "Pause"

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
            self.post("overlay_message", {"text": "The recorder is still starting."})
            return
        future = asyncio.run_coroutine_threadsafe(make_coro(controller), loop)
        future.add_done_callback(self._report)

    def _report(self, future) -> None:
        exc = future.exception()
        if exc is not None:
            logger.warning("On-screen control action failed: %s", exc)
            self.post("overlay_message", {"text": str(exc)[:160]})

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

        self.root = tk.Tk()
        self.root.withdraw()

        win = tk.Toplevel(self.root, bg=CARD)
        win.overrideredirect(True)                  # fixed, not draggable (SRS-UIX-02)
        win.attributes("-topmost", True)
        win.withdraw()
        try:
            # Rounded corners need the space outside them to disappear. Where
            # the window manager cannot do it the card is simply square, which
            # is a matter of looks and nothing else.
            win.attributes("-transparentcolor", "#010203")
            self._backdrop = "#010203"
        except Exception:
            self._backdrop = CARD
        self.window = win

        self.canvas = tk.Canvas(win, width=WIDTH, height=HEIGHT, bg=self._backdrop,
                                highlightthickness=0, bd=0)
        self.canvas.pack()

        self.stop_button = self._button(win, "Stop", RED, filled=True,
                                        action=self._on_stop)
        self.stop_button.place(x=18, y=HEIGHT - 56, width=136, height=40)
        self.pause_button = self._button(win, "Pause", BLUE, filled=False,
                                         action=self._on_pause)
        self.pause_button.place(x=WIDTH - 154, y=HEIGHT - 56, width=136, height=40)

        self._place()

    def _button(self, parent, label, colour, *, filled, action):
        """
        A pill that works by mouse and by keyboard (SRS-UIX-11).

        Drawn rather than themed: tk's own buttons take the operating system's
        look, which on a clinical PC is whatever that PC happens to be set to,
        and next to this card they look like a mistake.
        """
        import tkinter as tk

        canvas = tk.Canvas(parent, bg=CARD, highlightthickness=0, bd=0,
                           takefocus=1, cursor="hand2")
        canvas.enabled = True
        canvas.label = label
        canvas.colour = colour
        canvas.filled = filled
        canvas.hover = False
        canvas.pressed = False
        canvas.focused = False
        canvas.draw = lambda: self._draw_button(canvas)

        def press(_event=None):
            if not canvas.enabled:
                return
            canvas.pressed = True
            canvas.draw()
            canvas.after(90, lambda: (setattr(canvas, "pressed", False), canvas.draw()))
            action()

        for sequence in ("<Button-1>", "<Return>", "<space>"):
            canvas.bind(sequence, press)
        canvas.bind("<Enter>", lambda _e: (setattr(canvas, "hover", True), canvas.draw()))
        canvas.bind("<Leave>", lambda _e: (setattr(canvas, "hover", False), canvas.draw()))
        canvas.bind("<FocusIn>", lambda _e: (setattr(canvas, "focused", True), canvas.draw()))
        canvas.bind("<FocusOut>", lambda _e: (setattr(canvas, "focused", False), canvas.draw()))
        return canvas

    # ---- drawing ----

    @staticmethod
    def _round_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
        """A rounded rectangle, which tkinter does not have."""
        points = [
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, **kwargs)

    def _draw_button(self, canvas) -> None:
        canvas.delete("all")
        width = canvas.winfo_width() or 136
        height = canvas.winfo_height() or 40
        colour = canvas.colour if canvas.enabled else "#4a5c62"

        if canvas.filled:
            shade = colour
            if canvas.pressed:
                shade = RED_DEEP if colour == RED else shade
            elif canvas.hover and canvas.enabled:
                shade = self._lighten(colour, 0.12)
            self._round_rect(canvas, 1, 1, width - 1, height - 1, 12,
                             fill=shade, outline=shade)
            text_colour = "#ffffff"
        else:
            fill = self._lighten(CARD, 0.10) if (canvas.hover and canvas.enabled) else CARD
            self._round_rect(canvas, 1, 1, width - 1, height - 1, 12,
                             fill=fill, outline=colour, width=2)
            text_colour = colour if canvas.enabled else "#4a5c62"

        if canvas.focused and canvas.enabled:
            self._round_rect(canvas, 3, 3, width - 3, height - 3, 10,
                             fill="", outline="#ffffff", width=1)

        canvas.create_text(width / 2, height / 2, text=canvas.label,
                           fill=text_colour, font=(FONT, 11, "bold"))

    @staticmethod
    def _lighten(colour: str, amount: float) -> str:
        red, green, blue = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
        mix = lambda c: int(round(c + (255 - c) * amount))    # noqa: E731
        return f"#{mix(red):02x}{mix(green):02x}{mix(blue):02x}"

    def _paint(self, view, status: Dict[str, Any]) -> None:
        """The card itself, redrawn from scratch each tick."""
        canvas = self.canvas
        canvas.delete("all")

        recording = bool(status.get("is_recording"))
        paused = bool(status.get("is_paused"))
        accent = AMBER if paused else (RED if recording else DIM)

        canvas.create_rectangle(0, 0, WIDTH, HEIGHT, fill=self._backdrop,
                                outline=self._backdrop)
        self._round_rect(canvas, 1, 1, WIDTH - 1, HEIGHT - 1, 16,
                         fill=CARD, outline=CARD_EDGE, width=1)
        # A band of the state's colour down the left edge: the card's state is
        # then readable even from the corner of an eye.
        canvas.create_rectangle(1, 18, 5, HEIGHT - 18, fill=accent, outline=accent)

        # ---- the state, and the dot that pulses with it ----
        import math

        if recording and not paused:
            self._phase += self.POLL_MS / 1000.0
            glow = 0.5 + 0.5 * math.sin(self._phase * 2.4)
        else:
            glow = 0.0
        radius = 5 + 2.2 * glow
        canvas.create_oval(22 - radius, 30 - radius, 22 + radius, 30 + radius,
                           fill=accent, outline=self._lighten(accent, 0.4 * glow))

        label = "PAUSED" if paused else ("RECORDING" if recording else "READY")
        canvas.create_text(38, 30, text=label, anchor="w", fill=accent,
                           font=(FONT, 10, "bold"))

        elapsed = format_duration(status.get("duration_seconds", 0))
        canvas.create_text(WIDTH - 20, 30, text=elapsed, anchor="e", fill=TEXT,
                           font=("Consolas", 19, "bold"))

        # ---- who ----
        patient = (status.get("patient_name") or status.get("patient_ref") or "").strip()
        if patient:
            canvas.create_text(22, 55, text=patient[:34], anchor="w", fill=TEXT,
                               font=(FONT, 11))

        # ---- what the microphone is hearing ----
        self._paint_meter(canvas, status, accent, paused)

        # ---- what is safe, and what CMED has said ----
        pieces = int(status.get("segment_count", 0) or 0)
        held = int((status.get("upload") or {}).get("pending_segments", 0) or 0)
        sent = max(0, pieces - held)
        canvas.create_text(22, 118, anchor="w", fill=DIM, font=(FONT, 9),
                           text=f"{sent} of {pieces} piece(s) safe at UIU" if pieces
                           else "No pieces sealed yet")

        confirmation = status.get("confirmation")
        if confirmation == "confirmed":
            self._chip(canvas, WIDTH - 20, 118, "CMED confirmed", TEAL)
        elif confirmation == "confirming":
            self._chip(canvas, WIDTH - 20, 118, "Checking with CMED", AMBER)
        elif recording or paused:
            self._chip(canvas, WIDTH - 20, 118, "Not confirmed", AMBER)

        # ---- anything the agent needs to say ----
        note = (view.note or "")[:64]
        if note:
            canvas.create_text(22, 136, text=note, anchor="w", fill=DIM,
                               font=(FONT, 9))

    def _paint_meter(self, canvas, status: Dict[str, Any], accent: str,
                     paused: bool) -> None:
        """
        A bar per tick, scrolling left: the room, as the microphone hears it.

        A flat line is the fault this catches - a muted or unplugged microphone
        looks exactly like a working one until somebody plays the recording
        back, and by then the consultation is over.
        """
        level = 0.0 if paused else float(status.get("level", 0.0) or 0.0)
        self._levels.append(level)

        left, right, middle = 22, WIDTH - 20, 88
        span = (right - left) / BARS
        for index, value in enumerate(self._levels):
            height = 1.5 + value * 17
            x = left + index * span
            fade = 0.25 + 0.75 * (index / max(1, BARS - 1))
            colour = self._mix(CARD, accent if value > 0.02 else DIM, fade)
            canvas.create_rectangle(x, middle - height, x + span - 1.6, middle + height,
                                    fill=colour, outline=colour)

        if paused:
            canvas.create_text((left + right) / 2, middle, text="paused",
                               fill=DIM, font=(FONT, 9, "italic"))
        elif max(self._levels) < 0.02:
            # Said plainly, because it is the one thing on this card that means
            # the consultation is being lost.
            canvas.create_text((left + right) / 2, middle, text="no sound from the microphone",
                               fill=AMBER, font=(FONT, 9, "bold"))

    @staticmethod
    def _mix(base: str, colour: str, amount: float) -> str:
        a = [int(base[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
        return "#" + "".join(f"{int(round(x + (y - x) * amount)):02x}"
                             for x, y in zip(a, b))

    def _chip(self, canvas, x, y, text, colour) -> None:
        """A small right-aligned label in its own tinted pill."""
        item = canvas.create_text(x - 8, y, text=text, anchor="e", fill=colour,
                                  font=(FONT, 8, "bold"))
        x1, y1, x2, y2 = canvas.bbox(item)
        self._round_rect(canvas, x1 - 7, y1 - 3, x2 + 7, y2 + 3, 8,
                         fill=self._mix(CARD, colour, 0.16), outline="")
        canvas.tag_raise(item)

    def _place(self) -> None:
        self.window.update_idletasks()
        x = self.window.winfo_screenwidth() - WIDTH - 20
        self.window.geometry(f"{WIDTH}x{HEIGHT}+{x}+20")

    # ---- refresh ----

    def _tick(self) -> None:
        try:
            while True:
                event, data = self._events.get_nowait()
                self.state.on_event(event, data)
        except queue.Empty:
            pass
        try:
            status = self._status()
            self.apply(self.state.view(status), status)
        except Exception as exc:
            logger.debug("On-screen control refresh failed: %s", exc)
        self.root.after(self.POLL_MS, self._tick)

    def apply(self, view, status: Optional[Dict[str, Any]] = None) -> None:
        status = status if status is not None else {}
        self.stop_button.enabled = view.stop_enabled
        self.pause_button.enabled = view.pause_enabled
        self.pause_button.label = view.pause_label
        self._pause_text = view.pause_label
        self.stop_button.draw()
        self.pause_button.draw()
        self._paint(view, status)

        if view.visible and self._visible:
            if self.window.state() == "withdrawn":
                self._place()
                self.window.deiconify()
                self.window.attributes("-topmost", True)
        elif self.window.state() != "withdrawn":
            self.window.withdraw()

    # ---- presses ----

    def _on_stop(self) -> None:
        if self.form is not None:
            return
        self.actions.press_stop()                    # the microphone cuts now
        self._open_form("Why are you stopping?", self.actions.stop_choices,
                        confirm=self._confirm_stop, cancel=self.actions.cancel_stop)

    def _on_pause(self) -> None:
        if self.form is not None:
            return
        if self._pause_text == "Resume":
            self.actions.resume()
            return
        self._open_form("Why are you pausing?", self.actions.pause_choices,
                        confirm=self.actions.confirm_pause, cancel=None)

    def _confirm_stop(self, reason, detail):
        result = self.actions.confirm_stop(reason, detail)
        if result != NEEDS_DELETE_CONFIRMATION:
            return result
        from tkinter import messagebox
        title, question = DELETE_QUESTION
        if messagebox.askyesno(title, question, icon="warning", parent=self.form,
                               default="no"):
            return self.actions.confirm_stop(reason, detail, deletion_confirmed=True)
        return ""                                    # back to the reason list (SRS-CNS-08)

    # ---- the reason form (SRS-UIX-04..07) ----

    def _open_form(self, title, choices, *, confirm, cancel) -> None:
        import tkinter as tk

        self.state.form_open = True
        form = tk.Toplevel(self.window, bg=PAPER)
        form.title(title)
        form.attributes("-topmost", True)
        form.transient(self.window)
        form.resizable(False, False)
        self.form = form

        # A band of the card's colour, so the form reads as part of the same
        # thing rather than as a Windows dialog that appeared from nowhere.
        header = tk.Frame(form, bg=CARD, height=44)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text=title, font=(FONT, 11, "bold"), bg=CARD, fg=TEXT
                 ).pack(anchor="w", padx=16, pady=10)

        body = tk.Frame(form, bg=PAPER, padx=16, pady=12)
        body.pack(fill="both", expand=True)

        # Not "": tkinter draws every radio button as selected while the
        # variable is empty, which would look as if a reason were chosen.
        unset = "__none__"
        choice = tk.StringVar(value=unset)
        for value, label in choices:
            tk.Radiobutton(body, text=label, value=value, variable=choice, bg=PAPER,
                           fg=INK, font=(FONT, 10), anchor="w", activebackground=PAPER,
                           selectcolor=PAPER, padx=0
                           ).pack(fill="x", anchor="w", pady=1)
        tk.Label(body, text="Comment (needed for Other)", font=(FONT, 9), bg=PAPER,
                 fg=MUTED).pack(anchor="w", pady=(10, 2))
        comment = tk.Entry(body, font=(FONT, 10), width=40, relief="solid", bd=1)
        comment.pack(fill="x", ipady=4)
        problem = tk.Label(body, text="", font=(FONT, 9), bg=PAPER, fg=RED_DEEP,
                           anchor="w")
        problem.pack(fill="x", pady=(6, 0))

        def close():
            self.state.form_open = False
            self.form = None
            form.grab_release()
            form.destroy()

        def on_confirm(_event=None):
            chosen = choice.get()
            outcome = confirm(None if chosen == unset else chosen, comment.get())
            if outcome is None:
                close()
            else:
                problem.configure(text=outcome)

        def on_cancel(_event=None):
            if cancel is not None:
                cancel()
            close()

        buttons = tk.Frame(body, bg=PAPER)
        buttons.pack(fill="x", pady=(12, 0))
        tk.Button(buttons, text="Confirm", width=12, command=on_confirm,
                  default="active", bg=RED_DEEP, fg="#ffffff", relief="flat",
                  activebackground=RED, activeforeground="#ffffff",
                  font=(FONT, 10, "bold"), cursor="hand2"
                  ).pack(side="right", ipady=3)
        tk.Button(buttons, text="Cancel", width=12, command=on_cancel, relief="flat",
                  bg="#e8eaec", fg=INK, font=(FONT, 10), cursor="hand2"
                  ).pack(side="right", padx=8, ipady=3)
        # Closing the window is cancelling the action entirely, never a silent
        # confirmation (SRS-UIX-07).
        form.protocol("WM_DELETE_WINDOW", on_cancel)
        form.bind("<Escape>", on_cancel)
        form.bind("<Return>", on_confirm)
        form.update_idletasks()
        x = self.window.winfo_rootx() + WIDTH - form.winfo_reqwidth()
        y = self.window.winfo_rooty() + HEIGHT + 10
        form.geometry(f"+{max(0, x)}+{y}")
        form.grab_set()
        form.focus_force()


__all__ = ["Overlay"]
