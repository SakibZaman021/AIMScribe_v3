"""
The on-screen Stop and Pause control, drawn with tkinter (SRS 3.2 §7.8).

A small card at the top right of the screen, shown only while a consultation
is recording or paused. Two controls, the shapes the SRS names (SRS-UIX-03):

    Stop   - a red circle.       Thick ring, white inside. The microphone cuts
                                 on the press (SRS-UIX-08); the reason form
                                 then closes the session, or Cancel resumes it.
    Pause  - a blue rectangle.   Thick border, white inside. Takes effect only
                                 after a reason (SRS-UIX-05); while paused the
                                 same control resumes.

Outlined rather than filled: a solid block of red beside a doctor's screen all
afternoon is tiring, and a ring reads as a control while a filled shape reads
as a warning. Beside them, only what has to be there - the state, the elapsed
time, and a line of words when something needs saying.

It needs nothing from CMED to work (SRS-UIX-12), and it does not move
(SRS-UIX-02): a control that can be dragged can be dragged off-screen, and
then Stop is not where anybody left it.

tkinter runs on its own thread, which owns every widget. Button presses are
handed to the agent's event loop, as the tray menu's are; events from the
controller arrive through a queue this thread drains several times a second.
What to show is decided in overlay_model.py, which has no tkinter in it and is
tested on its own; this file only draws.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
from typing import Any, Dict, Optional

from ui.overlay_model import (DELETE_QUESTION, NEEDS_DELETE_CONFIRMATION, OverlayActions,
                              OverlayState, format_duration, pause_reasons)

logger = logging.getLogger(__name__)

PAPER = "#ffffff"
EDGE = "#d9dde1"
INK = "#1b1b1f"
MUTED = "#6b7178"
RED = "#d92d3a"
RED_DEEP = "#a8202b"
BLUE = "#1f5fbf"
BLUE_DEEP = "#17478f"
AMBER = "#b5791a"
TEAL = "#17836f"
OFF = "#b8bec4"                # a control that cannot be pressed
FONT = "Segoe UI"

WIDTH, HEIGHT = 258, 84        # grows by one line when there is something to say
NOTE_HEIGHT = 20
STOP_SIZE = 50
PAUSE_W, PAUSE_H = 74, 40


class Overlay:
    POLL_MS = 250

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
        self._pause_text = "Pause"
        self._height = HEIGHT
        self._quiet_ticks = 0

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

        win = tk.Toplevel(self.root, bg=PAPER)
        win.overrideredirect(True)                  # fixed, not draggable (SRS-UIX-02)
        win.attributes("-topmost", True)
        win.withdraw()
        try:
            # So the rounded corners have nothing behind them. Where the window
            # manager cannot do this the card is simply square, which is a
            # matter of looks and nothing else.
            win.attributes("-transparentcolor", "#010203")
            self._backdrop = "#010203"
        except Exception:
            self._backdrop = PAPER
        self.window = win

        self.canvas = tk.Canvas(win, width=WIDTH, height=HEIGHT + NOTE_HEIGHT,
                                bg=self._backdrop, highlightthickness=0, bd=0)
        self.canvas.pack()

        self.stop_button = self._button(win, "Stop", RED, RED_DEEP, shape="circle",
                                        action=self._on_stop)
        self.stop_button.place(x=WIDTH - STOP_SIZE - 18, y=17,
                               width=STOP_SIZE, height=STOP_SIZE)
        self.pause_button = self._button(win, "Pause", BLUE, BLUE_DEEP, shape="rect",
                                         action=self._on_pause)
        self.pause_button.place(x=WIDTH - STOP_SIZE - PAUSE_W - 34, y=22,
                                width=PAUSE_W, height=PAUSE_H)

        self._place()

    def _button(self, parent, label, colour, deep, *, shape, action):
        """
        A shaped control that works by mouse and by keyboard (SRS-UIX-11).

        Drawn rather than themed: tk's own buttons take whatever look the PC
        happens to be set to, and these two have shapes the SRS specifies.
        """
        import tkinter as tk

        canvas = tk.Canvas(parent, bg=PAPER, highlightthickness=0, bd=0,
                           takefocus=1, cursor="hand2")
        canvas.enabled = True
        canvas.label = label
        canvas.colour = colour
        canvas.deep = deep
        canvas.shape = shape
        canvas.hover = False
        canvas.pressed = False
        canvas.focused = False
        canvas.draw = lambda: self._draw_button(canvas)

        def press(_event=None):
            if not canvas.enabled:
                return
            canvas.pressed = True
            canvas.draw()
            canvas.after(110, lambda: (setattr(canvas, "pressed", False), canvas.draw()))
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
        """Thick ring, white inside, the word in the ring's colour."""
        canvas.delete("all")
        width = canvas.winfo_width() or PAUSE_W
        height = canvas.winfo_height() or PAUSE_H

        if not canvas.enabled:
            ring, ink, inside = OFF, OFF, PAPER
        elif canvas.pressed:
            # Pressed fills, briefly: the only moment either shape is solid,
            # so a press is unmistakable without being loud.
            ring, ink, inside = canvas.deep, PAPER, canvas.deep
        else:
            ring = canvas.deep if canvas.hover else canvas.colour
            ink = ring
            inside = self._tint(canvas.colour, 0.06) if canvas.hover else PAPER

        thick = 4 if canvas.enabled else 3
        if canvas.shape == "circle":
            pad = thick / 2 + 1
            canvas.create_oval(pad, pad, width - pad, height - pad,
                               fill=inside, outline=ring, width=thick)
        else:
            pad = thick / 2 + 1
            self._round_rect(canvas, pad, pad, width - pad, height - pad, 9,
                             fill=inside, outline=ring, width=thick)

        if canvas.focused and canvas.enabled:
            if canvas.shape == "circle":
                canvas.create_oval(1, 1, width - 1, height - 1, outline=ring, width=1)
            else:
                self._round_rect(canvas, 1, 1, width - 1, height - 1, 11,
                                 fill="", outline=ring, width=1)

        canvas.create_text(width / 2, height / 2 + 0.5, text=canvas.label,
                           fill=ink, font=(FONT, 9, "bold"))

    @staticmethod
    def _tint(colour: str, amount: float) -> str:
        """The colour, mostly white - for a hover that is felt, not seen."""
        red, green, blue = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
        mix = lambda c: int(round(255 + (c - 255) * amount))     # noqa: E731
        return f"#{mix(red):02x}{mix(green):02x}{mix(blue):02x}"

    def _paint(self, view, status: Dict[str, Any]) -> None:
        """The card behind the two controls: state, time, and a line of words."""
        canvas = self.canvas
        canvas.delete("all")

        recording = bool(status.get("is_recording"))
        paused = bool(status.get("is_paused"))
        accent = AMBER if paused else (RED if recording else MUTED)

        note = self._words(view, status, recording, paused)
        height = HEIGHT + (NOTE_HEIGHT if note else 0)
        if height != self._height:
            self._height = height
            self.window.geometry(f"{WIDTH}x{height}"
                                 f"+{self.window.winfo_screenwidth() - WIDTH - 20}+20")

        canvas.create_rectangle(0, 0, WIDTH, height + NOTE_HEIGHT,
                                fill=self._backdrop, outline=self._backdrop)
        self._round_rect(canvas, 1, 1, WIDTH - 1, height - 1, 14,
                         fill=PAPER, outline=EDGE, width=1)

        canvas.create_oval(18, 26, 26, 34, fill=accent, outline=accent)
        canvas.create_text(33, 30, text="Paused" if paused else
                           ("Recording" if recording else "Ready"),
                           anchor="w", fill=INK, font=(FONT, 10, "bold"))
        canvas.create_text(18, 55, text=format_duration(status.get("duration_seconds", 0)),
                           anchor="w", fill=MUTED, font=("Consolas", 13))

        if note:
            canvas.create_text(18, height - 14, text=note[:44], anchor="w",
                               fill=self._note_colour(note), font=(FONT, 8))

    def _words(self, view, status, recording: bool, paused: bool) -> str:
        """
        The one line the card is allowed to say.

        A silent microphone comes first. It is the only fault that is invisible
        until somebody plays the recording back, by which time the consultation
        is over, so it outranks anything else waiting to be said.
        """
        level = float(status.get("level", 0.0) or 0.0)
        if recording and not paused:
            self._quiet_ticks = self._quiet_ticks + 1 if level < 0.02 else 0
        else:
            self._quiet_ticks = 0
        if self._quiet_ticks >= 8:                  # about two seconds of nothing
            return "no sound from the microphone"
        return view.note or ""

    @staticmethod
    def _note_colour(note: str) -> str:
        if "no sound" in note:
            return RED
        if "Confirmed" in note:
            return TEAL
        return MUTED

    def _place(self) -> None:
        self.window.update_idletasks()
        x = self.window.winfo_screenwidth() - WIDTH - 20
        self.window.geometry(f"{WIDTH}x{self._height}+{x}+20")

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
        form = tk.Toplevel(self.window, bg=PAPER, padx=16, pady=14)
        form.title(title)
        form.attributes("-topmost", True)
        form.transient(self.window)
        form.resizable(False, False)
        self.form = form

        tk.Label(form, text=title, font=(FONT, 11, "bold"), bg=PAPER, fg=INK
                 ).pack(anchor="w", pady=(0, 8))

        # Not "": tkinter draws every radio button as selected while the
        # variable is empty, which would look as if a reason were chosen.
        unset = "__none__"
        choice = tk.StringVar(value=unset)
        for value, label in choices:
            tk.Radiobutton(form, text=label, value=value, variable=choice, bg=PAPER,
                           fg=INK, font=(FONT, 10), anchor="w", activebackground=PAPER,
                           selectcolor=PAPER).pack(fill="x", anchor="w", pady=1)
        tk.Label(form, text="Comment (needed for Other)", font=(FONT, 9), bg=PAPER,
                 fg=MUTED).pack(anchor="w", pady=(10, 2))
        comment = tk.Entry(form, font=(FONT, 10), width=40, relief="solid", bd=1)
        comment.pack(fill="x", ipady=3)
        problem = tk.Label(form, text="", font=(FONT, 9), bg=PAPER, fg=RED, anchor="w")
        problem.pack(fill="x", pady=(5, 0))

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

        buttons = tk.Frame(form, bg=PAPER)
        buttons.pack(fill="x", pady=(12, 0))
        tk.Button(buttons, text="Confirm", width=12, command=on_confirm,
                  default="active", font=(FONT, 10, "bold"), cursor="hand2"
                  ).pack(side="right")
        tk.Button(buttons, text="Cancel", width=12, command=on_cancel,
                  font=(FONT, 10), cursor="hand2").pack(side="right", padx=8)
        # Closing the window is cancelling the action entirely, never a silent
        # confirmation (SRS-UIX-07).
        form.protocol("WM_DELETE_WINDOW", on_cancel)
        form.bind("<Escape>", on_cancel)
        form.bind("<Return>", on_confirm)
        form.update_idletasks()
        x = self.window.winfo_rootx() + WIDTH - form.winfo_reqwidth()
        y = self.window.winfo_rooty() + self._height + 10
        form.geometry(f"+{max(0, x)}+{y}")
        form.grab_set()
        form.focus_force()


__all__ = ["Overlay"]
