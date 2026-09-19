"""
AIMScribe Agent - Windows system tray application.

Runs in the background on every doctor PC, like a security agent: always present,
always visibly showing its state, and not casually switched off.

What makes this the shipping build rather than a demo:

* The icon reflects state - ready, recording, paused, backend unreachable,
  misconfigured - instead of being a static green dot. A recorder that never
  looks like it is recording is not defensible.
* Only one instance can run, enforced by a named mutex.
* Configuration problems are surfaced in the menu and block recording rather than
  being logged and ignored.
* Integrity alerts raise a desktop notification.
* Exit is available to administrators only. A doctor cannot silently stop
  collection, and when an administrator does, it is written to the log.
* Logs rotate daily, are capped by retention, and carry pseudonyms rather than
  patient or doctor identifiers.
"""
from __future__ import annotations

import ctypes
import logging
import logging.handlers
import os
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import config  # noqa: E402
from core import crypto  # noqa: E402

logger = logging.getLogger("aimscribe")

MUTEX_NAME = "Global\\AIMScribeAgentSingleton"

# Tray states
READY = "ready"
RECORDING = "recording"
PAUSED = "paused"
OFFLINE = "offline"
BLOCKED = "blocked"

_PALETTE = {
    READY:     ((38, 132, 90), "Ready"),
    RECORDING: ((198, 40, 40), "Recording"),
    PAUSED:    ((176, 122, 26), "Paused"),
    OFFLINE:   ((70, 100, 130), "Recording - backend unreachable"),
    BLOCKED:   ((110, 110, 118), "Not configured"),
}


# ============================================================
# Logging
# ============================================================

def setup_logging() -> None:
    config.paths.logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = config.paths.logs_dir / "agent.log"

    handlers: list[logging.Handler] = []
    rotating = logging.handlers.TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=config.ops.log_retention_days,
        encoding="utf-8", delay=True,
    )
    rotating.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    handlers.append(rotating)

    if not getattr(sys, "frozen", False):
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)-8s %(name)s: %(message)s"))
        handlers.append(console)

    logging.basicConfig(
        level=getattr(logging, config.ops.log_level, logging.INFO),
        handlers=handlers,
        force=True,
    )
    # These log every request, including to /health, and add nothing here.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def load_log_salt() -> bytes:
    """
    Machine-local salt for log pseudonyms.

    Stable across restarts so support can correlate lines about one session, and
    machine-bound so the pseudonyms cannot be reversed off this PC.
    """
    path = config.security.device_key_path.parent / "log.salt"
    allow_plaintext = config.security.allow_plaintext_keystore
    if path.is_file():
        try:
            return crypto.unwrap_secret(path.read_bytes(), allow_plaintext=allow_plaintext)
        except Exception:
            logger.warning("Log salt unreadable; generating a new one")
    salt = secrets.token_bytes(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(crypto.wrap_secret(salt, allow_plaintext=allow_plaintext))
    except Exception as exc:
        logger.warning("Could not persist the log salt: %s", exc)
    return salt


# ============================================================
# Windows helpers
# ============================================================

def acquire_single_instance() -> Optional[object]:
    """Return a mutex handle, or None if another agent already owns it."""
    if sys.platform != "win32":
        return object()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return None
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def is_administrator() -> bool:
    if sys.platform != "win32":
        return True
    try:
        return bool(ctypes.WinDLL("shell32").IsUserAnAdmin())
    except Exception:
        return False


# ============================================================
# Icon
# ============================================================

def build_icon(state: str):
    """A ringed microphone glyph tinted by state, drawn at 64 px."""
    from PIL import Image, ImageDraw

    colour, _ = _PALETTE.get(state, _PALETTE[READY])
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.ellipse([2, 2, size - 2, size - 2], fill=colour + (255,))
    draw.ellipse([9, 9, size - 9, size - 9], fill=(252, 252, 253, 255))

    # Microphone capsule and stand.
    draw.rounded_rectangle([27, 18, 37, 36], radius=5, fill=colour + (255,))
    draw.arc([22, 27, 42, 43], start=0, end=180, fill=colour + (255,), width=3)
    draw.line([32, 43, 32, 48], fill=colour + (255,), width=3)

    if state == RECORDING:
        draw.ellipse([44, 44, 58, 58], fill=(198, 40, 40, 255),
                     outline=(255, 255, 255, 255), width=2)
    elif state == PAUSED:
        draw.rectangle([46, 45, 49, 57], fill=(176, 122, 26, 255))
        draw.rectangle([53, 45, 56, 57], fill=(176, 122, 26, 255))
    elif state in (OFFLINE, BLOCKED):
        draw.ellipse([44, 44, 58, 58], fill=colour + (255,),
                     outline=(255, 255, 255, 255), width=2)
        draw.line([48, 51, 54, 51], fill=(255, 255, 255, 255), width=2)

    return image


# ============================================================
# Tray application
# ============================================================

class AgentTray:
    def __init__(self, runtime, server):
        self.runtime = runtime
        self.server = server
        self.icon = None
        self.state = BLOCKED if runtime.problems else READY
        self.summary = "Starting…"
        self._last_alert_seen = ""
        self._stop = threading.Event()

    # ---- menu ----

    def build_menu(self):
        from pystray import Menu, MenuItem

        def status_text(_):
            return self.summary

        def detail_text(_):
            status = self._status()
            if not status.get("session_id"):
                upload = status.get("upload", {})
                return f"Queue: {upload.get('pending_segments', 0)} segment(s) waiting"
            return (f"Segments: {status.get('segment_count', 0)}   "
                    f"Duration: {_format_duration(status.get('duration_seconds', 0))}")

        def spool_text(_):
            upload = self._status().get("upload", {})
            used = upload.get("spool_bytes", 0) / (1024 ** 3)
            return f"Local buffer: {used:.1f} GB ({upload.get('spool_pressure', '-')})"

        def problem_text(_):
            if self.runtime.problems:
                return f"⚠ {len(self.runtime.problems)} configuration problem(s)"
            return "Configuration OK"

        return Menu(
            MenuItem(lambda _: f"AIMScribe Agent {config.app_version}", None, enabled=False),
            Menu.SEPARATOR,
            MenuItem(status_text, None, enabled=False),
            MenuItem(detail_text, None, enabled=False),
            MenuItem(spool_text, None, enabled=False),
            Menu.SEPARATOR,
            MenuItem(problem_text, self.on_show_problems,
                     enabled=lambda _: bool(self.runtime.problems)),
            MenuItem("Open CMED", self.on_open_cmed),
            MenuItem("Open log folder", self.on_open_logs),
            MenuItem("Copy diagnostics", self.on_copy_diagnostics),
            Menu.SEPARATOR,
            # Recording control lives here as well as in CMED. A pause still
            # carries a reason - chosen from the same fixed list, so the gap in
            # the audio is explained either way - but it no longer depends on a
            # browser being able to reach the agent. A consultation once ran for
            # an hour with no way to stop it because the page had lost its
            # patient, which is not a state a clinical recorder should allow.
            MenuItem("Pause recording", Menu(*self._pause_items(MenuItem)),
                     enabled=lambda _: self._is_recording() and not self._is_paused()),
            MenuItem("Resume recording", self.on_resume,
                     enabled=lambda _: self._is_paused()),
            MenuItem("Stop recording", self.on_stop,
                     enabled=lambda _: self._is_recording()),
            Menu.SEPARATOR,
            MenuItem(
                "Exit (administrator only)" if not is_administrator() else "Exit",
                self.on_exit,
                enabled=lambda _: is_administrator(),
            ),
        )

    # ---- recording control ----

    def _is_recording(self) -> bool:
        return bool(self._status().get("is_recording"))

    def _is_paused(self) -> bool:
        return bool(self._status().get("is_paused"))

    def _pause_items(self, MenuItem):
        """
        One entry per configured pause reason.

        A menu cannot ask for free text, so "other" is offered with a fixed
        note rather than being left blank: an unexplained gap is worse than a
        vaguely explained one, and CMED remains available for a fuller reason.
        """
        return [
            MenuItem(reason.replace("_", " ").capitalize(),
                     self._pause_action(reason))
            for reason in config.pause.reasons
        ]

    def _pause_action(self, reason: str):
        def action(icon, item):
            detail = ("paused from the tray" if reason != "other"
                      else "paused from the tray; no reason given")
            self._call(
                lambda controller: controller.pause_session(
                    reason=reason, reason_detail=detail,
                    authorised_by="", expected_seconds=0),
                f"Paused: {reason.replace('_', ' ')}",
                "Could not pause")
        return action

    def on_resume(self, icon, item):
        self._call(lambda controller: controller.resume_session(),
                   "Recording resumed", "Could not resume")

    def on_stop(self, icon, item):
        self._call(lambda controller: controller.stop_session(reason="stopped_from_tray"),
                   "Recording stopped and queued for upload", "Could not stop")

    def _call(self, make_coro, success: str, failure: str) -> None:
        """
        Run a controller coroutine from the tray thread.

        pystray callbacks run on their own thread, so the coroutine is scheduled
        onto the agent's loop rather than awaited here. The result is reported as
        a notification because a menu click that appears to do nothing is worse
        than one that says why it failed.
        """
        import asyncio

        controller = self.runtime.controller
        loop = self.runtime.loop
        if controller is None or loop is None:
            self.notify("AIMScribe", "The agent is still starting.")
            return

        try:
            future = asyncio.run_coroutine_threadsafe(make_coro(controller), loop)
            result = future.result(timeout=30)
        except Exception as exc:
            logger.error("%s: %s", failure, exc)
            self.notify(failure, str(exc)[:180])
            return

        if isinstance(result, dict) and result.get("status") == "not_recording":
            self.notify("AIMScribe", "Nothing is recording.")
            return
        self.notify("AIMScribe", success)
        # The refresh thread repaints within two seconds, but a menu that still
        # says "Recording" straight after Stop invites a second click.
        if self.icon is not None:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    # ---- actions ----

    def on_show_problems(self, icon, item):
        body = "\n".join(f"• {problem}" for problem in self.runtime.problems[:5])
        self.notify("AIMScribe is not correctly configured", body or "See the log for detail.")
        for problem in self.runtime.problems:
            logger.critical("CONFIGURATION PROBLEM: %s", problem)

    def on_open_cmed(self, icon, item):
        origins = sorted(config.security.allowed_origins)
        if origins:
            webbrowser.open(origins[0])
        else:
            self.notify("No CMED address configured",
                        "Set AIMS_ALLOWED_ORIGINS in the agent configuration.")

    def on_open_logs(self, icon, item):
        path = str(config.paths.logs_dir.resolve())
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        else:
            webbrowser.open(f"file://{path}")

    def on_copy_diagnostics(self, icon, item):
        """Put a support-ready summary on the clipboard. Contains no patient data."""
        status = self._status()
        upload = status.get("upload", {})
        report = "\n".join([
            f"AIMScribe Agent {config.app_version} (protocol {config.protocol_version})",
            f"Device:      {self.runtime.device_key.fingerprint()}",
            f"State:       {status.get('state', 'unknown')}",
            f"Backend:     {config.backend.base_url}",
            f"Online:      {upload.get('online')}  failures={upload.get('consecutive_failures')}",
            f"Last error:  {upload.get('last_error') or '-'}",
            f"Spool:       {upload.get('spool_bytes', 0) / 1024 ** 3:.2f} GB "
            f"({upload.get('spool_pressure')}), {upload.get('pending_segments', 0)} pending",
            f"Problems:    {len(self.runtime.problems)}",
            *[f"  - {p}" for p in self.runtime.problems],
            f"Warnings:    {len(self.runtime.warnings)}",
            *[f"  - {w}" for w in self.runtime.warnings],
        ])
        if _copy_to_clipboard(report):
            self.notify("Diagnostics copied", "Paste them into your support request.")
        else:
            logger.info("Diagnostics:\n%s", report)
            self.notify("Diagnostics written to the log", "Clipboard was unavailable.")

    def on_exit(self, icon, item):
        if not is_administrator():
            self.notify("Not permitted",
                        "Stopping AIMScribe requires an administrator.")
            logger.warning("Non-administrator attempted to exit the agent")
            return
        logger.warning("Agent exit requested by an administrator from the tray menu")
        self._stop.set()
        self.server.should_exit = True
        icon.stop()

    # ---- refresh ----

    def _status(self) -> dict:
        controller = self.runtime.controller
        if controller is None:
            return {"state": "starting", "upload": {}}
        try:
            return controller.status()
        except Exception:
            return {"state": "unknown", "upload": {}}

    def _derive_state(self, status: dict) -> str:
        if self.runtime.problems:
            return BLOCKED
        if status.get("is_paused"):
            return PAUSED
        if status.get("is_recording"):
            upload = status.get("upload", {})
            failures = upload.get("consecutive_failures", 0)
            return OFFLINE if failures >= 3 else RECORDING
        return READY

    def _derive_summary(self, status: dict, state: str) -> str:
        label = _PALETTE.get(state, _PALETTE[READY])[1]
        if state == PAUSED:
            pause = status.get("pause") or {}
            reason = str(pause.get("reason", "")).replace("_", " ")
            return f"Paused - {reason}" if reason else "Paused"
        if state in (RECORDING, OFFLINE):
            return f"{label} - {_format_duration(status.get('duration_seconds', 0))}"
        if state == BLOCKED:
            return f"Not configured - {len(self.runtime.problems)} problem(s)"
        return label

    def refresh_loop(self) -> None:
        while not self._stop.wait(2.0):
            try:
                status = self._status()
                state = self._derive_state(status)
                summary = self._derive_summary(status, state)

                if self.icon is not None and (state != self.state or summary != self.summary):
                    self.state = state
                    self.summary = summary
                    self.icon.icon = build_icon(state)
                    self.icon.title = f"AIMScribe - {summary}"
                    self.icon.update_menu()

                controller = self.runtime.controller
                if controller is not None and controller.last_alert != self._last_alert_seen:
                    self._last_alert_seen = controller.last_alert
                    if self._last_alert_seen:
                        self.notify("AIMScribe integrity alert", self._last_alert_seen[:200])
            except Exception as exc:
                logger.debug("Tray refresh error: %s", exc)

    def notify(self, title: str, message: str) -> None:
        try:
            if self.icon is not None:
                self.icon.notify(message, title)
        except Exception as exc:
            logger.debug("Notification failed (%s): %s - %s", exc, title, message)

    # ---- run ----

    def run(self) -> None:
        from pystray import Icon

        self.icon = Icon(
            name="AIMScribe",
            icon=build_icon(self.state),
            title="AIMScribe - starting",
            menu=self.build_menu(),
        )
        threading.Thread(target=self.refresh_loop, name="TrayRefresh", daemon=True).start()
        self.icon.run()


# ============================================================
# Helpers
# ============================================================

def _format_duration(seconds: float) -> str:
    total = int(seconds or 0)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _copy_to_clipboard(text: str) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import subprocess
        subprocess.run(["clip"], input=text.encode("utf-16-le"), check=True,
                       creationflags=0x08000000)
        return True
    except Exception:
        return False


# ============================================================
# Entry point
# ============================================================

def _ensure_std_streams() -> None:
    """
    Give the process real stdout and stderr before anything tries to log.

    A PyInstaller --windowed build has neither: sys.stdout and sys.stderr are
    None. Our own logging skips the console when frozen, but uvicorn installs
    its own handler on sys.stderr during startup, and that raises on None. The
    result is an agent that writes its banner, dies before the listener opens,
    and leaves no traceback anywhere - which is exactly how it failed the first
    time the built executable was run.

    Pointed at the log directory rather than os.devnull so that anything written
    outside the logging framework - a C extension, an unhandled exception during
    interpreter shutdown - is still recoverable from a clinical machine.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return

    try:
        config.paths.logs_dir.mkdir(parents=True, exist_ok=True)
        target = open(config.paths.logs_dir / "agent-stderr.log", "a",
                      encoding="utf-8", buffering=1)
    except OSError:
        target = open(os.devnull, "w", encoding="utf-8")

    if sys.stdout is None:
        sys.stdout = target
    if sys.stderr is None:
        sys.stderr = target


def main() -> int:
    config.ensure_directories()
    _ensure_std_streams()
    setup_logging()

    logger.info("=" * 68)
    logger.info("AIMScribe Agent %s (protocol %s) starting",
                config.app_version, config.protocol_version)
    logger.info("Audio:   %s Hz / %s ch / %s-bit WAV PCM",
                config.audio.sample_rate, config.audio.channels,
                config.audio.sample_width * 8)
    logger.info("Backend: %s", config.backend.base_url)
    logger.info("Listen:  http://%s:%s  (ws /ws)",
                config.security.bind_host, config.security.bind_port)
    logger.info("Spool:   %s (%.1f h capacity)",
                config.spool.directory, config.spool_seconds() / 3600)
    logger.info("=" * 68)

    mutex = acquire_single_instance()
    if mutex is None:
        logger.error("Another AIMScribe agent is already running; exiting")
        return 2

    try:
        import pyaudio  # noqa: F401
        import aiohttp  # noqa: F401
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
        import cryptography  # noqa: F401
    except ImportError as exc:
        logger.critical("Missing dependency: %s", exc)
        print(f"Missing dependency: {exc}\nInstall with: pip install -r requirements.txt")
        return 1

    import uvicorn
    from api.trigger_server import Runtime, create_app

    runtime = Runtime(config, log_salt=load_log_salt())
    app = create_app(runtime)

    server = uvicorn.Server(uvicorn.Config(
        app,
        host=config.security.bind_host,
        port=config.security.bind_port,
        log_level=config.ops.log_level.lower(),
        access_log=False,
        # Only ever loopback; a wider bind would expose the control plane.
        server_header=False,
        date_header=False,
    ))

    server_thread = threading.Thread(
        target=server.run, name="ControlPlane", daemon=True)
    server_thread.start()

    # Give the control plane a moment to bind so the tray does not briefly show a
    # healthy state for a server that failed to start.
    for _ in range(50):
        if getattr(server, "started", False):
            break
        time.sleep(0.1)
    if not getattr(server, "started", False):
        logger.error("Control plane did not start on %s:%s - is the port in use?",
                     config.security.bind_host, config.security.bind_port)

    tray = AgentTray(runtime, server)
    try:
        tray.run()
    except KeyboardInterrupt:
        logger.info("Interrupted")
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
        logger.info("AIMScribe Agent stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
