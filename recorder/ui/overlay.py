"""
The on-screen Stop and Pause control, drawn with tkinter (SRS 3.2 §7.8).

A small always-on-top window at the top right of the screen, shown only while
a consultation is recording or paused. Two controls: a red circular Stop and a
blue rectangular Pause (SRS-UIX-03). It needs nothing from CMED (SRS-UIX-12).

tkinter runs on its own thread, which owns every widget. Button presses are
handed to the agent's event loop, as the tray menu's are; events from the
controller arrive through a queue this thread drains a few times a second.
What to show is decided in overlay_model.py.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
from typing import Any, Dict, Optional

from ui.overlay_model import (DELETE_QUESTION, NEEDS_DELETE_CONFIRMATION, OverlayActions,
                              OverlayState, pause_reasons)

logger = logging.getLogger(__name__)

RED = "#c62828"
BLUE = "#1f5fbf"
INK = "#1b1b1f"
MUTED = "#5f5f68"
PAPER = "#ffffff"
FONT = "Segoe UI"


class Overlay:
    POLL_MS = 300

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
        win = tk.Toplevel(self.root, bg=PAPER, highlightthickness=1,
                          highlightbackground="#c9c9cf")
        win.overrideredirect(True)                  # fixed, not draggable (SRS-UIX-02)
        win.attributes("-topmost", True)
        win.withdraw()
        self.window = win

        text = tk.Frame(win, bg=PAPER)
        text.pack(side="left", padx=(12, 8), pady=8)
        self.headline = tk.Label(text, text="", font=(FONT, 11, "bold"), fg=INK, bg=PAPER,
                                 anchor="w", width=22)
        self.headline.pack(anchor="w")
        self.note = tk.Label(text, text="", font=(FONT, 9), fg=MUTED, bg=PAPER, anchor="w",
                             width=30, wraplength=230, justify="left")
        self.note.pack(anchor="w")

        self.stop_button = self._control(win, width=54, draw=self._draw_stop,
                                         action=self._on_stop, name="Stop")
        self.stop_button.pack(side="left", padx=4, pady=8)
        self.pause_button = self._control(win, width=78, draw=self._draw_pause,
                                          action=self._on_pause, name="Pause")
        self.pause_button.pack(side="left", padx=(4, 12), pady=8)
        self._pause_text = "Pause"
        self._place()

    def _control(self, parent, *, width, draw, action, name):
        """A shaped button that works by mouse and by keyboard (SRS-UIX-11)."""
        import tkinter as tk

        canvas = tk.Canvas(parent, width=width, height=54, bg=PAPER, highlightthickness=2,
                           highlightbackground=PAPER, highlightcolor=INK, takefocus=1,
                           cursor="hand2")
        canvas.enabled = True
        canvas.draw = lambda: draw(canvas)
        for sequence in ("<Button-1>", "<Return>", "<space>"):
            canvas.bind(sequence, lambda _event, c=canvas: action() if c.enabled else None)
        canvas.draw()
        return canvas

    def _draw_stop(self, canvas) -> None:
        canvas.delete("all")
        colour = RED if canvas.enabled else "#d9a3a3"
        canvas.create_oval(5, 3, 49, 47, fill=colour, outline=colour)
        canvas.create_text(27, 25, text="Stop", fill=PAPER, font=(FONT, 9, "bold"))

    def _draw_pause(self, canvas) -> None:
        canvas.delete("all")
        colour = BLUE if canvas.enabled else "#a9bddf"
        canvas.create_rectangle(4, 8, 74, 44, fill=colour, outline=colour)
        canvas.create_text(39, 26, text=getattr(self, "_pause_text", "Pause"),
                           fill=PAPER, font=(FONT, 9, "bold"))

    def _place(self) -> None:
        self.window.update_idletasks()
        width = self.window.winfo_reqwidth()
        x = self.window.winfo_screenwidth() - width - 16
        self.window.geometry(f"+{x}+16")

    # ---- refresh ----

    def _tick(self) -> None:
        try:
            while True:
                event, data = self._events.get_nowait()
                self.state.on_event(event, data)
        except queue.Empty:
            pass
        try:
            self.apply(self.state.view(self._status()))
        except Exception as exc:
            logger.debug("On-screen control refresh failed: %s", exc)
        self.root.after(self.POLL_MS, self._tick)

    def apply(self, view) -> None:
        self.headline.configure(text=view.headline)
        self.note.configure(text=view.note)
        self.stop_button.enabled = view.stop_enabled
        self.pause_button.enabled = view.pause_enabled
        self._pause_text = view.pause_label
        self.stop_button.draw()
        self.pause_button.draw()
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
        form = tk.Toplevel(self.window, bg=PAPER, padx=16, pady=12)
        form.title(title)
        form.attributes("-topmost", True)
        form.transient(self.window)
        form.resizable(False, False)
        self.form = form

        tk.Label(form, text=title, font=(FONT, 11, "bold"), bg=PAPER, fg=INK
                 ).pack(anchor="w", pady=(0, 6))
        # Not "": tkinter draws every radio button as selected while the
        # variable is empty, which would look as if a reason were chosen.
        unset = "__none__"
        choice = tk.StringVar(value=unset)
        for value, label in choices:
            tk.Radiobutton(form, text=label, value=value, variable=choice, bg=PAPER,
                           fg=INK, font=(FONT, 10), anchor="w", activebackground=PAPER
                           ).pack(fill="x", anchor="w")
        tk.Label(form, text="Comment (needed for Other)", font=(FONT, 9), bg=PAPER,
                 fg=MUTED).pack(anchor="w", pady=(8, 0))
        comment = tk.Entry(form, font=(FONT, 10), width=40)
        comment.pack(fill="x")
        problem = tk.Label(form, text="", font=(FONT, 9), bg=PAPER, fg=RED, anchor="w")
        problem.pack(fill="x", pady=(4, 0))

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
        buttons.pack(fill="x", pady=(10, 0))
        tk.Button(buttons, text="Confirm", width=12, command=on_confirm, default="active"
                  ).pack(side="right")
        tk.Button(buttons, text="Cancel", width=12, command=on_cancel).pack(side="right",
                                                                            padx=6)
        # Closing the window is cancelling the action entirely, never a silent
        # confirmation (SRS-UIX-07).
        form.protocol("WM_DELETE_WINDOW", on_cancel)
        form.bind("<Escape>", on_cancel)
        form.bind("<Return>", on_confirm)
        form.update_idletasks()
        x = self.window.winfo_rootx() + self.window.winfo_width() - form.winfo_reqwidth()
        y = self.window.winfo_rooty() + self.window.winfo_height() + 8
        form.geometry(f"+{max(0, x)}+{y}")
        form.grab_set()
        form.focus_force()


__all__ = ["Overlay"]
