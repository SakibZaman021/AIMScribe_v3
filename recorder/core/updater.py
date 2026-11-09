"""
The recorder's own updates: install a new AIMScribeSetup.exe with no one at
the PC, and only one that AIMS LAB made and the AIMS LAB server asked for.

Two halves, in two processes:

  The agent (the doctor's account, least privilege) passes on what the
  server's heartbeat reply says. A reply with an "update" is written as it
  came to state/update_notice.json; a reply without one removes that file -
  which is how a paused rollout stops a PC that has not begun. The agent
  also writes state/agent_status.json each heartbeat - recording or not -
  and puts the updater's last report (state/update_status.json) into the
  next heartbeat.

  The updater (scheduled task "AIMScribe Updater", SYSTEM, at start-up and
  every 30 minutes: AIMScribe_Agent.exe --update) installs. It runs as SYSTEM
  because the agent cannot install software; and because it does, everything
  the doctor's account can write - the notice, the agent's status - is input
  it checks, never instructions it trusts. It installs only when ALL hold:

    1. the notice's signature checks against the server key pinned under
       Program Files, under this module's own domain - a trusted-origins list
       or a purge receipt signed with the same key can never pass for one;
    2. the notice is for this PC (its device id), and its release number is
       not lower than one this PC has already seen: an old notice replayed
       is refused, and so is one older than a week;
    3. the version is newer than the one installed - or the server signed it
       as an explicit rollback;
    4. the downloaded file's SHA-256 is the one in the notice;
    5. its Authenticode signature is valid and its signer is AIMS LAB;
    6. no consultation is recording (it waits, and tries again later).

  Then it runs AIMScribeSetup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART,
  detached, and exits: the setup replaces this very program, and stops the
  agent politely itself. The next run - which setup asks for when it ends -
  sees the version it now runs and reports success, or reads setup's exit
  code and log and reports why not. A version that has failed three times is
  not tried again; a new release (a new number) is.

  The download, the attempt count and the highest release seen live in the
  SYSTEM account's own profile folder, which the doctor's account cannot
  read or write: nothing can swap the file between its check and its run.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from cryptography.exceptions import InvalidSignature

from core.crypto import canonical_json, digest, iso_utc

logger = logging.getLogger(__name__)

# MUST match backend/src/integrity.py UPDATE_DOMAIN.
DOMAIN = b"aimscribe.recorder-update.v1"
SCOPE = "recorder_update"

NOTICE_FILE = "update_notice.json"       # state dir, written by the agent
STATUS_FILE = "update_status.json"       # state dir, written by the updater
AGENT_STATUS_FILE = "agent_status.json"  # state dir, written by the agent
LIVE_FILE = "live_consultation.json"     # state dir, core.start_keeper
IDENTITY_FILE = "device.json"            # state dir, core.enrollment

TASK_NAME = "AIMScribe Updater"
MAX_ATTEMPTS = 3
NOTICE_MAX_AGE = timedelta(days=7)
NOTICE_MAX_AHEAD = timedelta(days=1)     # a PC clock behind by up to a day
AGENT_FRESH_SECONDS = 180                # three heartbeats
IDLE_POLL_SECONDS = 30
IDLE_WAIT_SECONDS = 25 * 60              # then the next run (every 30 min)
SETUP_TIMEOUT = timedelta(minutes=30)    # no exit code by then: it failed
MAX_DOWNLOAD_BYTES = 400 * 1024 * 1024
SETUP_ARGS = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART"

_VERSION = re.compile(r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class NoticeRefused(Exception):
    """The notice is not one to act on. Not counted against the version."""


class UpdateFailed(Exception):
    """This version could not be installed this time. Counted."""


@dataclass(frozen=True)
class Notice:
    release_id: int
    version: str
    url: str
    sha256: str
    rollback: bool
    issued_at: datetime

    @property
    def key(self) -> str:
        """What attempts are counted against: this release, this file."""
        return f"{self.release_id}:{self.version}:{self.sha256}"


def version_tuple(value: Any) -> Tuple[int, ...]:
    """'3.8.4' -> (3, 8, 4); anything unreadable sorts below every real version."""
    parts = []
    for piece in str(value or "").strip().lstrip("v").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)


def signing_input(payload: Dict[str, Any]) -> bytes:
    return digest(DOMAIN, canonical_json(payload))


def _parse_time(text: Any) -> Optional[datetime]:
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ============================================================
# 1-3: the notice
# ============================================================

def verify_notice(bundle: Any, public_key, *, device_id: str, installed_version: str,
                  highest_release_id: int = 0,
                  now: Optional[datetime] = None) -> Notice:
    """
    The notice, if it may be acted on; NoticeRefused with the reason if not.
    The signature is checked before anything in the payload is believed.
    """
    if not isinstance(bundle, dict):
        raise NoticeRefused("not a notice")
    payload, signature = bundle.get("payload"), bundle.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, str):
        raise NoticeRefused("not a notice")
    if public_key is None:
        raise NoticeRefused("no server key to check it with")
    try:
        public_key.verify(bytes.fromhex(signature), signing_input(payload))
    except (InvalidSignature, ValueError):
        raise NoticeRefused("its signature does not check against the AIMS LAB server key")
    if payload.get("scope") != SCOPE:
        raise NoticeRefused("signed, but not as a recorder update")
    if not device_id or str(payload.get("device_id", "")) != str(device_id):
        raise NoticeRefused("addressed to another PC")

    release_id, version = payload.get("release_id"), payload.get("version")
    url, sha = payload.get("url"), payload.get("sha256")
    rollback = payload.get("rollback")
    if not isinstance(release_id, int) or isinstance(release_id, bool) or release_id < 1:
        raise NoticeRefused("no release number")
    if not isinstance(version, str) or not _VERSION.match(version):
        raise NoticeRefused("no readable version")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise NoticeRefused("the download is not https")
    if not isinstance(sha, str) or not _SHA256.match(sha):
        raise NoticeRefused("no SHA-256")
    if not isinstance(rollback, bool):
        raise NoticeRefused("rollback is not stated")
    issued = _parse_time(payload.get("issued_at"))
    now = now or datetime.now(timezone.utc)
    if issued is None or issued < now - NOTICE_MAX_AGE or issued > now + NOTICE_MAX_AHEAD:
        raise NoticeRefused("issued too long ago (or in the future): an old notice replayed")
    if release_id < highest_release_id:
        raise NoticeRefused(f"release {release_id} is older than release {highest_release_id} "
                            f"already seen: an old notice replayed")

    installed, target = version_tuple(installed_version), version_tuple(version)
    if rollback:
        if target == installed:
            raise NoticeRefused(f"{version} is already installed")
    elif target <= installed:
        raise NoticeRefused(f"{version} is not newer than {installed_version}")
    return Notice(release_id=release_id, version=version, url=url, sha256=sha,
                  rollback=rollback, issued_at=issued)


# ============================================================
# 4-5: the file
# ============================================================

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def check_sha256(path: Path, expected: str) -> None:
    found = sha256_of(path)
    if found != expected.lower():
        raise UpdateFailed(f"the download's SHA-256 is {found[:16]}..., not {expected[:16]}... "
                           f"as the server said")


def subject_names(subject: str) -> Dict[str, str]:
    """'CN=AIMS LAB, O="AIMS LAB, UIU", C=BD' -> {'CN': 'AIMS LAB', 'O': 'AIMS LAB, UIU', ...}."""
    names: Dict[str, str] = {}
    for match in re.finditer(r'\s*([A-Za-z0-9.]+)=("(?:[^"]|"")*"|[^,]*)\s*(?:,|$)', subject or ""):
        key, value = match.group(1).upper(), match.group(2).strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1].replace('""', '"')
        names.setdefault(key, value)
    return names


def check_publisher(signature: Dict[str, Any], publisher: str) -> None:
    """
    `signature`: what Windows says of the file - its Authenticode status and
    the signer's subject. Valid, and signed by `publisher` exactly (the
    certificate's common name or organisation): a certificate that merely
    mentions the name is not enough.
    """
    status = str(signature.get("status") or "")
    if status != "Valid":
        raise UpdateFailed(f"its Authenticode signature is not valid ({status or 'unsigned'})")
    names = subject_names(str(signature.get("subject") or ""))
    wanted = publisher.strip().casefold()
    if not wanted or wanted not in (names.get("CN", "").casefold(), names.get("O", "").casefold()):
        raise UpdateFailed(f"it is signed by {signature.get('subject') or 'nobody'}, "
                           f"not {publisher}")


_POWERSHELL_CHECK = (
    "$s = Get-AuthenticodeSignature -LiteralPath $args[0]; "
    "[pscustomobject]@{status = [string]$s.Status; "
    "subject = [string]$s.SignerCertificate.Subject; "
    "timestamped = ($null -ne $s.TimeStamperCertificate)} | ConvertTo-Json -Compress")


def windows_signature(path: Path) -> Dict[str, Any]:
    """Ask Windows (WinVerifyTrust, through PowerShell) about the file's signature."""
    root = os.environ.get("SystemRoot", r"C:\Windows")
    powershell = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    script = "& { " + _POWERSHELL_CHECK + " } '" + str(path).replace("'", "''") + "'"
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        capture_output=True, text=True, timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        return json.loads(result.stdout.strip() or "{}")
    except ValueError:
        return {"status": "Unknown", "subject": "", "error": result.stderr.strip()[:200]}


# ============================================================
# 6: the consultation
# ============================================================

def agent_busy(state_dir: Path, *, now: Optional[float] = None) -> bool:
    """
    True while a consultation is (or may be) recording. A status the agent
    wrote more than three heartbeats ago means no agent is running - nobody is
    signed in, or it died - and so nothing is recording.
    """
    now = time.time() if now is None else now
    status = _read_json(Path(state_dir) / AGENT_STATUS_FILE)
    if isinstance(status, dict):
        try:
            fresh = now - float(status.get("at") or 0) < AGENT_FRESH_SECONDS
        except (TypeError, ValueError):
            fresh = False
        if fresh and (str(status.get("state") or "idle") != "idle" or status.get("session_id")):
            return True
    # A consultation CMED asked for and the agent keeps alive (core.start_keeper).
    live = Path(state_dir) / LIVE_FILE
    try:
        return now - live.stat().st_mtime < AGENT_FRESH_SECONDS
    except OSError:
        return False


def wait_until_idle(state_dir: Path, *, max_wait: float = IDLE_WAIT_SECONDS,
                    poll: float = IDLE_POLL_SECONDS,
                    clock: Callable[[], float] = time.time,
                    sleep: Callable[[float], None] = time.sleep) -> bool:
    """
    True once two looks `poll` seconds apart both find nothing recording -
    so a consultation that is just starting is not cut off. False if that
    has not happened within `max_wait`.
    """
    deadline = clock() + max_wait
    quiet_once = False
    while True:
        if agent_busy(state_dir, now=clock()):
            quiet_once = False
        elif quiet_once:
            return True
        else:
            quiet_once = True
        if clock() + poll > deadline:
            return False
        sleep(poll)


# ============================================================
# Files
# ============================================================

def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    partial.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    os.replace(partial, path)


def _remove(path: Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not remove %s: %s", path, exc)


# ---- the agent's half ----

def keep_notice(state_dir: Optional[Path], notice: Any) -> None:
    """The heartbeat reply's "update", for the updater; none removes the one held."""
    if not state_dir:
        return
    path = Path(state_dir) / NOTICE_FILE
    try:
        if isinstance(notice, dict):
            if _read_json(path) != notice:
                _write_json(path, notice)
                logger.info("The server offers recorder %s",
                            (notice.get("payload") or {}).get("version"))
        elif path.exists():
            _remove(path)
            logger.info("The server no longer offers a recorder update")
    except OSError as exc:
        logger.warning("Could not keep the update notice: %s", exc)


def note_agent_state(state_dir: Optional[Path], state: str,
                     session_id: Optional[str]) -> None:
    """Whether a consultation is recording, for the updater to wait on."""
    if not state_dir:
        return
    try:
        _write_json(Path(state_dir) / AGENT_STATUS_FILE,
                    {"state": state, "session_id": session_id, "at": time.time(),
                     "pid": os.getpid()})
    except OSError as exc:
        logger.debug("Could not write the agent's state for the updater: %s", exc)


def last_report(state_dir: Optional[Path]) -> Optional[Dict[str, Any]]:
    """The updater's last report, for the heartbeat."""
    if not state_dir:
        return None
    report = _read_json(Path(state_dir) / STATUS_FILE)
    return report if isinstance(report, dict) and report.get("state") else None


# ============================================================
# The updater
# ============================================================

def private_dir() -> Path:
    """
    A folder only this account (SYSTEM) and administrators can open: its own
    local profile, C:\\Windows\\System32\\config\\systemprofile\\AppData\\Local.
    Asked of Windows, not read from the environment.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            buffer = ctypes.create_unicode_buffer(260)
            # CSIDL_LOCAL_APPDATA (0x1c)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x1C, None, 0, buffer) == 0 \
                    and buffer.value:
                return Path(buffer.value) / "AIMScribe" / "updater"
        except Exception:
            pass
    return Path(os.getenv("LOCALAPPDATA") or Path.home()) / "AIMScribe" / "updater"


def is_administrator() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def download(url: str, target: Path, *, limit: int = MAX_DOWNLOAD_BYTES) -> None:
    """https only, redirects included (GitHub sends releases to its file host)."""
    class HttpsOnly(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not str(newurl).startswith("https://"):
                raise UpdateFailed(f"the download was redirected away from https: {newurl}")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(HttpsOnly)
    request = urllib.request.Request(url, headers={"User-Agent": "AIMScribe-Updater"})
    _remove(target)
    received = 0
    # O_EXCL: a file that appeared at this name since the line above is not written into.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "wb") as out, opener.open(request, timeout=60) as reply:
        while True:
            block = reply.read(1 << 20)
            if not block:
                break
            received += len(block)
            if received > limit:
                raise UpdateFailed(f"the download is larger than {limit // 2**20} MB")
            out.write(block)


def launch_setup(setup: Path, log: Path, exit_file: Path) -> None:
    """
    Start setup detached and return at once. It is run through cmd so that
    its exit code is kept (exit_file) and the updater is asked to run again
    when it ends, to report. The few seconds' pause first lets this process -
    the program setup is about to replace - exit before setup looks for it.
    """
    comspec = os.environ.get("ComSpec") or str(
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe")
    inner = (f'ping -n 6 127.0.0.1 >nul & "{setup}" {SETUP_ARGS} /LOG="{log}" '
             f'& echo !errorlevel!>"{exit_file}" '
             f'& schtasks /Run /TN "{TASK_NAME}" >nul 2>&1')
    command = f'"{comspec}" /d /s /v:on /c "{inner}"'
    base = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    breakaway = 0x01000000                                  # CREATE_BREAKAWAY_FROM_JOB
    for flags in (base | breakaway, base):
        try:
            subprocess.Popen(command, creationflags=flags, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return
        except OSError as exc:                              # the task's job forbids breakaway
            last = exc
    raise UpdateFailed(f"setup could not be started: {last}")


def _setup_log_reason(log: Path) -> str:
    """The last line of setup's log that says what went wrong."""
    try:
        lines = Path(log).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines[-200:]):
        lowered = line.lower()
        if any(word in lowered for word in ("error", "incomplete", "failed", "abort")):
            return line.strip()[-200:]
    return ""


class Updater:
    """
    One run of the scheduled task. Everything that touches Windows or the
    network is passed in, so each decision can be tested on its own.
    """

    def __init__(self, *, state_dir: Path, work_dir: Path, public_key,
                 installed_version: str, publisher: str = "AIMS LAB",
                 fetch: Callable[[str, Path], None] = download,
                 signature_of: Callable[[Path], Dict[str, Any]] = windows_signature,
                 launch: Callable[[Path, Path, Path], None] = launch_setup,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 idle_wait: float = IDLE_WAIT_SECONDS):
        self.state_dir = Path(state_dir)
        self.work_dir = Path(work_dir)
        self.key = public_key
        self.installed = installed_version
        self.publisher = publisher
        self.fetch, self.signature_of, self.launch = fetch, signature_of, launch
        self.clock, self.sleep, self.idle_wait = clock, sleep, idle_wait

    # ---- what it keeps ----

    @property
    def _ledger_path(self) -> Path:
        return self.work_dir / "ledger.json"

    def ledger(self) -> Dict[str, Any]:
        data = _read_json(self._ledger_path)
        data = data if isinstance(data, dict) else {}
        data.setdefault("attempts", {})
        data.setdefault("highest_release_id", 0)
        data.setdefault("pending", None)
        return data

    def _save(self, ledger: Dict[str, Any]) -> None:
        _write_json(self._ledger_path, ledger)

    def report(self, state: str, *, notice: Optional[Notice] = None, reason: str = "",
               attempts: Optional[int] = None, version: Optional[str] = None) -> Dict[str, Any]:
        status: Dict[str, Any] = {"state": state, "installed": self.installed,
                                  "at": iso_utc(datetime.fromtimestamp(self.clock(), timezone.utc))}
        if notice is not None:
            status.update(version=notice.version, release_id=notice.release_id)
        if version:
            status["version"] = version
        if reason:
            status["reason"] = reason[:300]
        if attempts is not None:
            status["attempts"] = attempts
        try:
            _write_json(self.state_dir / STATUS_FILE, status)
        except OSError as exc:
            logger.warning("Could not write the update status: %s", exc)
        log = logger.warning if state in ("failed", "gave_up", "refused") else logger.info
        log("Update: %s%s%s", state, f" {status.get('version')}" if status.get("version") else "",
            f" - {reason}" if reason else "")
        return status

    def device_id(self) -> str:
        data = _read_json(self.state_dir / IDENTITY_FILE)
        return str(data.get("device_id") or "") if isinstance(data, dict) else ""

    # ---- one run ----

    def run_once(self) -> Dict[str, Any]:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        ledger = self.ledger()

        if ledger["pending"]:
            return self._settle(ledger)

        previous = last_report(self.state_dir) or {}
        bundle = _read_json(self.state_dir / NOTICE_FILE)
        if bundle is None:
            if previous.get("state") in ("installed", "failed", "gave_up"):
                return previous                    # keep saying it until there is news
            return self.report("idle")
        try:
            notice = verify_notice(bundle, self.key, device_id=self.device_id(),
                                   installed_version=self.installed,
                                   highest_release_id=int(ledger["highest_release_id"]),
                                   now=datetime.fromtimestamp(self.clock(), timezone.utc))
        except NoticeRefused as exc:
            if "already installed" in str(exc) or "not newer" in str(exc):
                # A notice from before the install, until the next heartbeat clears it.
                if previous.get("state") == "installed":
                    return previous
                return self.report("idle", reason=str(exc))
            return self.report("refused", reason=str(exc))

        if notice.release_id > int(ledger["highest_release_id"]):
            ledger["highest_release_id"] = notice.release_id
            self._save(ledger)
        tried = int(ledger["attempts"].get(notice.key, 0))
        if tried >= MAX_ATTEMPTS:
            return self.report("gave_up", notice=notice, attempts=tried,
                               reason=f"failed {tried} times; not tried again")

        self.report("waiting", notice=notice, attempts=tried)
        if not wait_until_idle(self.state_dir, max_wait=self.idle_wait,
                               clock=self.clock, sleep=self.sleep):
            return self.report("waiting", notice=notice, attempts=tried,
                               reason="a consultation is recording; trying again later")

        setup = self.work_dir / f"AIMScribeSetup-{notice.version}.exe"
        try:
            self.report("downloading", notice=notice, attempts=tried)
            try:
                self.fetch(notice.url, setup)
            except UpdateFailed:
                raise
            except Exception as exc:
                # The network: not this version's fault, so not counted.
                _remove(setup)
                return self.report("failed", notice=notice, attempts=tried,
                                   reason=f"download failed: {exc}"[:300])
            check_sha256(setup, notice.sha256)
            check_publisher(self.signature_of(setup), self.publisher)
        except UpdateFailed as exc:
            _remove(setup)
            return self._count_failure(ledger, notice, str(exc))

        # The consultation may have begun while the file came down.
        if agent_busy(self.state_dir, now=self.clock()):
            return self.report("waiting", notice=notice, attempts=tried,
                               reason="a consultation began; trying again later")
        try:
            check_sha256(setup, notice.sha256)      # the file about to run is the one checked
        except UpdateFailed as exc:
            _remove(setup)
            return self._count_failure(ledger, notice, str(exc))

        log, exit_file = self.work_dir / "setup.log", self.work_dir / "setup.exit"
        _remove(log)
        _remove(exit_file)
        ledger["attempts"][notice.key] = tried + 1
        ledger["pending"] = {"key": notice.key, "version": notice.version,
                             "release_id": notice.release_id, "from": self.installed,
                             "started_at": self.clock(), "setup": str(setup),
                             "log": str(log), "exit": str(exit_file)}
        self._save(ledger)
        try:
            self.launch(setup, log, exit_file)
        except Exception as exc:
            ledger["pending"] = None
            self._save(ledger)
            return self.report("failed", notice=notice, attempts=tried + 1,
                               reason=f"setup could not be started: {exc}"[:300])
        return self.report("installing", notice=notice, attempts=tried + 1)

    def _count_failure(self, ledger: Dict[str, Any], notice: Notice, reason: str) -> Dict[str, Any]:
        tried = int(ledger["attempts"].get(notice.key, 0)) + 1
        ledger["attempts"][notice.key] = tried
        self._save(ledger)
        state = "gave_up" if tried >= MAX_ATTEMPTS else "failed"
        return self.report(state, notice=notice, attempts=tried, reason=reason)

    def _settle(self, ledger: Dict[str, Any]) -> Dict[str, Any]:
        """What became of the setup the last run started."""
        pending = ledger["pending"]
        version, key = pending.get("version"), pending.get("key")
        tried = int(ledger["attempts"].get(key, 0))
        exit_file, log = Path(pending.get("exit", "")), Path(pending.get("log", ""))
        if version_tuple(self.installed) == version_tuple(version):
            ledger["pending"] = None
            ledger["attempts"].pop(key, None)
            self._save(ledger)
            _remove(Path(pending.get("setup", "")))
            _remove(exit_file)
            _remove(self.state_dir / NOTICE_FILE)  # done with; the next heartbeat says more
            return self.report("installed", version=version)
        code = None
        if exit_file.is_file():
            try:
                code = int((exit_file.read_text(encoding="ascii", errors="ignore").strip()
                            or "-1").split()[0])
            except ValueError:
                code = -1
        elif self.clock() - float(pending.get("started_at") or 0) < SETUP_TIMEOUT.total_seconds():
            return self.report("installing", version=version, attempts=tried)
        ledger["pending"] = None
        self._save(ledger)
        _remove(Path(pending.get("setup", "")))
        if code == 0:
            # Setup said it worked, yet this program is still the old one.
            reason = f"setup finished but {self.installed} is still installed"
        elif code is None:
            reason = "setup did not finish within 30 minutes"
        else:
            reason = f"setup exit code {code}"
        detail = _setup_log_reason(log)
        if detail:
            reason = f"{reason}: {detail}"
        # Reported, and this run ends there: the next run (or the server's
        # next word) tries again, so the reason is seen before it is replaced.
        state = "gave_up" if tried >= MAX_ATTEMPTS else "failed"
        return self.report(state, version=version, attempts=tried, reason=reason)


def run_from_task(cfg) -> int:
    """`AIMScribe_Agent.exe --update`: one run of the scheduled task."""
    from core.crypto import load_public_key
    if sys.platform == "win32" and not is_administrator():
        logger.error("The updater must run as SYSTEM (scheduled task %r); it was not", TASK_NAME)
        return 5
    try:
        key = load_public_key(cfg.security.receipt_public_key_path)
    except Exception as exc:
        logger.error("No server key to check update notices with: %s", exc)
        return 4
    updater = Updater(state_dir=cfg.paths.state_dir, work_dir=private_dir(), public_key=key,
                      installed_version=cfg.app_version,
                      publisher=getattr(cfg, "update_publisher", "") or "AIMS LAB")
    try:
        updater.run_once()
    except Exception as exc:
        logger.exception("The updater stopped: %s", exc)
        updater.report("failed", reason=f"updater error: {exc}"[:300])
        return 1
    return 0


__all__ = ["DOMAIN", "MAX_ATTEMPTS", "Notice", "NoticeRefused", "UpdateFailed", "Updater",
           "agent_busy", "check_publisher", "check_sha256", "keep_notice", "last_report",
           "note_agent_state", "run_from_task", "signing_input", "subject_names",
           "verify_notice", "version_tuple", "wait_until_idle"]
