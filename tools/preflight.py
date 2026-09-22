"""
Check a clinic PC before anybody records on it.

    python tools/preflight.py                      this machine
    python tools/preflight.py --server http://desktop-s3as8qk:6060
    python tools/preflight.py --cmed-key <key>     also test Channel B

Every check here exists because the thing it checks for has already gone
wrong once, quietly, in a way that looked like something else:

    the microphone was muted          ten minutes of silence, recorded
    the port was one browsers refuse  the page said the server was down
    the address had changed overnight every upload refused
    the host name was in capitals     SignatureDoesNotMatch on every piece
    CMED's message never arrived      the recording was never archived
    the page's origin was not trusted "AIMScribe is not running"

None of those announce themselves during a consultation. This does, in
thirty seconds, before a patient is in the room.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional, Tuple

AGENT = "http://127.0.0.1:5050"

# Ports browsers and Node refuse to open, whatever is listening on them.
BLOCKED_PORTS = {
    1719, 1720, 1723, 2049, 3659, 4045, 5060, 5061, 6000, 6566, 6665, 6666,
    6667, 6668, 6669, 6697, 10080,
}

PASS, WARN, FAIL = "pass", "warn", "fail"


class Report:
    def __init__(self) -> None:
        self.rows: List[Tuple[str, str, str]] = []

    def add(self, verdict: str, what: str, detail: str = "") -> None:
        self.rows.append((verdict, what, detail))
        mark = {PASS: "  ok  ", WARN: " warn ", FAIL: " FAIL "}[verdict]
        print(f"[{mark}] {what}")
        if detail:
            for line in detail.splitlines():
                print(f"          {line}")

    @property
    def failed(self) -> int:
        return sum(1 for verdict, _, _ in self.rows if verdict == FAIL)

    @property
    def warned(self) -> int:
        return sum(1 for verdict, _, _ in self.rows if verdict == WARN)


def get(url: str, headers: Optional[dict] = None, timeout: int = 10):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read() or b"{}")


def post(url: str, body: dict, headers: Optional[dict] = None, timeout: int = 20):
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


# ============================================================
# The checks
# ============================================================

def check_agent(report: Report) -> dict:
    """Is the recorder running, and is it the version we think?"""
    try:
        _, health = get(f"{AGENT}/health")
    except Exception as exc:
        report.add(FAIL, "The recorder is not answering on this PC",
                   f"{exc}\nStart AIMScribe_Agent.exe. Only one may run at a time;\n"
                   "if another AIMScribe is running, exit it from the tray first.")
        return {}

    problems = health.get("problems") or []
    if health.get("status") == "healthy" and not problems:
        report.add(PASS, f"Recorder {health.get('version')} is running and configured")
    else:
        report.add(FAIL, f"Recorder {health.get('version')} is not ready",
                   "\n".join(str(p) for p in problems))
    return health


def check_microphone(report: Report, seconds: float, threshold: int) -> None:
    """
    Can it hear the room?

    The one fault that leaves a perfect-looking recording of nothing at all.
    """
    try:
        import audioop
        import pyaudio
    except ImportError:
        report.add(WARN, "Microphone not tested",
                   "pyaudio is not installed in this interpreter, so this check\n"
                   "was skipped. The agent itself still warns during a recording.")
        return

    pa = pyaudio.PyAudio()
    try:
        device = pa.get_default_input_device_info()
        stream = pa.open(format=pyaudio.paInt16, channels=1, rate=44100,
                         input=True, frames_per_buffer=2048)
        peak = 0
        for _ in range(int(44100 / 2048 * seconds)):
            peak = max(peak, audioop.rms(
                stream.read(2048, exception_on_overflow=False), 2))
        stream.close()
    except Exception as exc:
        report.add(FAIL, "The microphone could not be opened", str(exc))
        return
    finally:
        pa.terminate()

    where = f"{device['name']} - peak loudness {peak}, the silence line is {threshold}"
    if peak >= threshold:
        report.add(PASS, "The microphone is hearing the room", where)
    else:
        report.add(FAIL, "The microphone is silent", where + "\n"
                   "Muted, turned down, or the wrong input device. Windows:\n"
                   "Settings > System > Sound > Input. Speak while this runs.")


def check_server(report: Report, server: str) -> dict:
    """Is the server there, is it the right one, and is its port dialable?"""
    port = 80
    host = server.split("//", 1)[-1]
    if ":" in host:
        host, _, text = host.partition(":")
        port = int(text.split("/")[0] or 80)

    if port in BLOCKED_PORTS:
        report.add(FAIL, f"The server is on port {port}, which browsers refuse",
                   "Chrome, Firefox and Node all refuse this port whatever is\n"
                   "listening on it. curl and the recorder will work; the\n"
                   "dashboard and CMED's page will not. Move it: AIMS_PORT.")

    try:
        socket.gethostbyname(host)
    except Exception as exc:
        report.add(FAIL, f"The server's name does not resolve here: {host}", str(exc))
        return {}

    if host != host.lower():
        report.add(FAIL, "The server's name has capital letters in it",
                   "Upload links are signed with the host inside them and are\n"
                   "sent lower-cased, so every piece is refused with\n"
                   "SignatureDoesNotMatch. Use " + host.lower())

    try:
        _, health = get(f"{server.rstrip('/')}/health")
    except Exception as exc:
        report.add(FAIL, f"The server did not answer at {server}", str(exc))
        return {}

    system = health.get("system", "")
    if "v3" in system:
        report.add(PASS, f"Server answered: {system}",
                   f"database {health.get('database')}, redis {health.get('redis')}, "
                   f"storage {health.get('minio')}")
    else:
        report.add(FAIL, "That is not the v3 server",
                   f"It says: {system or health.get('version')}. Version 1 answers\n"
                   "like this. Check the address the recorder is pointed at.")
    for part in ("database", "redis", "minio"):
        if health.get(part) not in (None, "connected"):
            report.add(FAIL, f"The server cannot reach its {part}", str(health.get(part)))
    return health


def check_agent_points_at_server(report: Report, env: Optional[Path],
                                 server: str) -> None:
    """The recorder's own settings, read from the file it actually uses."""
    if env is None or not env.is_file():
        report.add(WARN, "The recorder's settings file was not given",
                   "Pass --agent <the folder holding AIMScribe_Agent.exe> to\n"
                   "check the address and origins it is configured with.")
        return

    values = {}
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()

    configured = values.get("AIMS_BACKEND_URL", "")
    if configured.rstrip("/") == server.rstrip("/"):
        report.add(PASS, f"The recorder is pointed at {configured}")
    else:
        report.add(FAIL, "The recorder is pointed somewhere else",
                   f"it says {configured or '(nothing)'}, this check used {server}")

    origins = values.get("AIMS_ALLOWED_ORIGINS", "")
    page_host = server.split("//", 1)[-1].split(":")[0]
    expected = f"http://{page_host}:3000"
    if expected in origins or page_host in ("localhost", "127.0.0.1"):
        report.add(PASS, "The recorder trusts CMED's page")
    else:
        report.add(FAIL, "The recorder will refuse CMED's page",
                   f"it trusts {origins or '(nothing)'}\n"
                   f"but the page is served from {expected}. The page reports\n"
                   'that refusal as "AIMScribe is not running".')


def check_channel_b(report: Report, server: str, key: str, clinic: str) -> None:
    """Can CMED's messages actually be delivered? This is the 502 we hit."""
    if not key:
        report.add(WARN, "Channel B not tested",
                   "Pass --cmed-key to prove CMED's messages can be delivered.\n"
                   "Undelivered, a recording is never confirmed and never archived.")
        return

    status, answer = post(f"{server.rstrip('/')}/api/v2/clinical/patient-information",
                          {"patient_id": "PREFLIGHT", "doctor_id": "PREFLIGHT",
                           "hospital_id": clinic,
                           "start_time": "2000-01-01T00:00:00+06:00",
                           "date": "2000-01-01",
                           "demographics": {"name": "Preflight check",
                                            "sex": "female", "age_years": 1,
                                            "phone": "01700000000",
                                            "address": "Preflight"},
                           "paramedic": {"recorded_at": "2000-01-01T00:00:00+06:00",
                                         "weight_kg": 1, "height_cm": 1,
                                         "blood_pressure": "120/80",
                                         "pulse_bpm": 60, "temperature_c": 36.6,
                                         "spo2_percent": 98},
                           "previous_visit": None},
                          headers={"X-CMED-Key": key})
    if status in (200, 202):
        report.add(PASS, f"CMED's messages are accepted ({answer.get('code')})",
                   "A dated-2000 check visit was stored; it matches no recording.")
    elif status == 401:
        report.add(FAIL, "CMED's key is not accepted", "Re-issue it from bootstrap.py.")
    else:
        report.add(FAIL, f"Channel B answered {status}", json.dumps(answer)[:200])


def check_clock(report: Report, server: str) -> None:
    """
    Clocks. The five fields include a timestamp, matched character for
    character, and a recording is filed under the clinic's date.
    """
    try:
        request = urllib.request.Request(f"{server.rstrip('/')}/health")
        with urllib.request.urlopen(request, timeout=10) as response:
            served = response.headers.get("Date")
    except Exception:
        return
    if not served:
        return
    from email.utils import parsedate_to_datetime
    from datetime import datetime, timezone
    try:
        theirs = parsedate_to_datetime(served)
    except Exception:
        return
    drift = abs((datetime.now(timezone.utc) - theirs).total_seconds())
    if drift < 60:
        report.add(PASS, f"This PC's clock agrees with the server ({drift:.0f}s apart)")
    else:
        report.add(WARN if drift < 300 else FAIL,
                   f"This PC's clock is {drift / 60:.1f} minutes from the server's",
                   "Recordings are filed under the clinic's date and matched to\n"
                   "CMED's messages by time. Set the clock to sync automatically.")


def check_disk(report: Report, path: Path) -> None:
    import shutil
    try:
        free = shutil.disk_usage(path).free / (1024 ** 3)
    except Exception:
        return
    hours = free / 0.32                       # about 318 MB an hour at 44.1 kHz
    if free < 2:
        report.add(FAIL, f"Only {free:.1f} GB free on this PC",
                   "The recorder holds audio until the server receipts it.")
    elif free < 10:
        report.add(WARN, f"{free:.1f} GB free - about {hours:.0f} hours of audio")
    else:
        report.add(PASS, f"{free:.0f} GB free - about {hours:.0f} hours of audio")


# ============================================================

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", default="", help="the AIMS LAB server")
    parser.add_argument("--agent", default="",
                        help="the folder holding AIMScribe_Agent.exe")
    parser.add_argument("--cmed-key", default="", help="CMED's key, to test Channel B")
    parser.add_argument("--clinic", default="CMED-LOCAL-01",
                        help="CMED's code for this clinic")
    parser.add_argument("--listen", type=float, default=3.0,
                        help="seconds to listen to the microphone")
    parser.add_argument("--silence", type=int, default=80,
                        help="the agent's silence line (silence_rms / 4)")
    args = parser.parse_args(argv)

    print("AIMScribe - checking this PC before a consultation\n")

    report = Report()
    health = check_agent(report)

    env = Path(args.agent) / ".env" if args.agent else None
    server = args.server
    if not server and env and env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("AIMS_BACKEND_URL="):
                server = line.split("=", 1)[1].strip()
    server = server or "http://localhost:6060"

    check_server(report, server)
    check_agent_points_at_server(report, env, server)
    check_microphone(report, args.listen, args.silence)
    check_channel_b(report, server, args.cmed_key, args.clinic)
    check_clock(report, server)
    check_disk(report, Path(args.agent) if args.agent else Path.home())

    print()
    if report.failed:
        print(f"{report.failed} thing(s) would stop a consultation. Fix those first.")
        return 1
    if report.warned:
        print(f"Ready, with {report.warned} thing(s) worth a look.")
        return 0
    print("Ready. Everything this PC needs is working.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
