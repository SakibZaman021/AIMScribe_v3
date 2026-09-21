"""
Drive AIMScribe_Agent.exe from the command line - what CMED's page does.

The agent listens on this machine only, on port 5050. CMED's page opens a
WebSocket to it and sends commands; this does the same thing, so you can run a
whole consultation without a browser.

    python tools/drive_recorder.py health
    python tools/drive_recorder.py status
    python tools/drive_recorder.py start --patient P0001 --doctor DR01
    python tools/drive_recorder.py built  --patient P0001
    python tools/drive_recorder.py stop

    python tools/drive_recorder.py consultation --minutes 2 \\
           --patient P0001 --doctor DR01 --cmed-key <key>

The last one is the whole thing end to end: it opens the patient, sends CMED's
API 2 to the server, records for two minutes, sends the prescription, and
stops - which is what the archive worker then picks up. Give it `--cmed-key`
(printed by `deploy/local/bootstrap.py`) or the recording stays unconfirmed and
is never archived, exactly as a real consultation CMED never described.

The five fields identify the visit and must match character for character
between Channel A and Channel B; that is why `start_time` is generated once and
reused rather than taken twice from the clock.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

AGENT = "http://127.0.0.1:5050"
AGENT_WS = "ws://127.0.0.1:5050/ws"
# The agent refuses a WebSocket without an Origin it was told to expect, so a
# page from anywhere else cannot drive a recording. This is CMED's origin on a
# bench; on a clinic PC it is CMED's real address.
ORIGIN = "http://localhost:3000"


# ============================================================
# Talking to the server, as CMED's server does (Channel B)
# ============================================================

def channel_b(server: str, key: str, kind: str, body: Dict[str, Any]) -> str:
    path = "patient-information" if kind == "api2" else "prescription"
    request = urllib.request.Request(
        f"{server.rstrip('/')}/api/v2/clinical/{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-CMED-Key": key})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            answer = json.loads(response.read() or b"{}")
            return f"{response.status} {answer.get('code', '')}"
    except urllib.error.HTTPError as exc:
        return f"{exc.code} {exc.read()[:120].decode(errors='replace')}"
    except Exception as exc:                      # the server is not running
        return f"could not be sent: {exc}"


# ============================================================
# Talking to the agent (Channel A)
# ============================================================

async def send(command: str, message: Dict[str, Any], *,
               quiet: bool = False) -> Dict[str, Any]:
    try:
        import websockets
    except ImportError:
        raise SystemExit("This needs the websockets package:  pip install websockets")

    request_id = f"cli-{datetime.now().strftime('%H%M%S%f')}"
    message = {"request_id": request_id, "command": command, **message}
    try:
        async with websockets.connect(AGENT_WS, origin=ORIGIN) as ws:
            await ws.send(json.dumps(message))
            while True:
                # The agent also pushes status events nobody asked for; the
                # reply is the one carrying our request_id.
                reply = json.loads(await asyncio.wait_for(ws.recv(), 30))
                if reply.get("request_id") == request_id:
                    break
    except OSError as exc:
        raise SystemExit(f"The agent did not answer on {AGENT_WS} ({exc}).\n"
                         "Is AIMScribe_Agent.exe running?")

    if not quiet:
        print(f"  {reply.get('code', '?')}  {reply.get('message', '')}".rstrip())
        if reply.get("data"):
            print(f"  {json.dumps(reply['data'])}")
    return reply


def five_fields(args, start: Optional[datetime] = None) -> Dict[str, str]:
    start = start or datetime.now(timezone(timedelta(hours=6))).replace(microsecond=0)
    return {"patient_id": args.patient, "doctor_id": args.doctor,
            "hospital_id": args.hospital,
            "start_time": start.isoformat(), "date": start.date().isoformat()}


# ============================================================
# The agent's HTTP side
# ============================================================

def http_get(path: str, key: str = "") -> str:
    request = urllib.request.Request(f"{AGENT}{path}",
                                     headers={"X-API-Key": key} if key else {})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.dumps(json.loads(response.read() or b"{}"), indent=2)
    except urllib.error.HTTPError as exc:
        return f"{exc.code}: {exc.read()[:200].decode(errors='replace')}"
    except Exception as exc:
        return f"The agent did not answer ({exc}). Is AIMScribe_Agent.exe running?"


def local_key(args) -> str:
    """AIMS_LOCAL_API_KEY, from the agent's own .env if it was not given."""
    if args.key:
        return args.key
    if args.agent:
        env = Path(args.agent) / ".env"
        if env.is_file():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("AIMS_LOCAL_API_KEY="):
                    return line.split("=", 1)[1].strip()
    return ""


# ============================================================
# A whole consultation
# ============================================================

async def consultation(args) -> None:
    start = datetime.now(timezone(timedelta(hours=6))).replace(microsecond=0)
    five = five_fields(args, start)
    seconds = max(10, int(args.minutes * 60))

    print(f"Patient {args.patient}, doctor {args.doctor}, {args.minutes} minute(s)\n")

    print("1. Open patient (Channel A -> the agent)")
    reply = await send("start", {"trigger": five})
    session_id = (reply.get("data") or {}).get("session_id")
    if not session_id:
        raise SystemExit("The agent did not start a recording; nothing else to do.")

    if args.cmed_key:
        print("\n2. Patient information (Channel B -> the server)")
        print("  ", channel_b(args.server, args.cmed_key, "api2", {
            **five,
            "demographics": {"name": f"Test {args.patient}", "sex": "female",
                             "age_years": 34, "phone": "01700000000",
                             "address": "Dhaka"},
            "paramedic": {"recorded_at": five["start_time"], "weight_kg": 60,
                          "height_cm": 158, "blood_pressure": "120/80",
                          "pulse_bpm": 76, "temperature_c": 36.9,
                          "spo2_percent": 98},
            "previous_visit": None}))
    else:
        print("\n2. No --cmed-key given, so CMED never describes this visit.")
        print("   It will record, but it will not be confirmed or archived.")

    print(f"\n3. Recording for {seconds} seconds. Say something.")
    for left in range(seconds, 0, -10):
        print(f"   {left}s", end="\r", flush=True)
        await asyncio.sleep(min(10, left))
    print("   done      ")

    if args.cmed_key:
        print("4. Prescription (Channel B -> the server)")
        print("  ", channel_b(args.server, args.cmed_key, "api3", {
            **five,
            "issued_at": datetime.now(timezone(timedelta(hours=6)))
                         .replace(microsecond=0).isoformat(),
            "diagnoses": ["Test diagnosis"], "investigations": ["CBC"],
            "advice": "Rest", "follow_up": (start.date() + timedelta(days=14)).isoformat(),
            "items": [{"drug": "Napa", "dose": "500 mg", "frequency": "1+1+1",
                       "duration": "5 days", "instructions": "after food"}]}))

    print("\n5. Prescription built (Channel A) - arms the gate")
    await send("prescription_built",
               {"session_id": session_id, "patient_id": args.patient})

    print("\n6. Stop")
    await send("stop", {"reason": args.reason})

    print(f"""
Done. The recording is {session_id}.

Within about ten seconds the archive worker joins the pieces, writes the
clinical JSON beside the audio, makes the encrypted copy and issues the purge
receipts that let this PC delete its own audio:

    docker compose -f deploy/local/docker-compose.yml logs -f archive-worker
""")


# ============================================================

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action",
                        choices=["health", "status", "start", "built", "stop",
                                 "pause", "resume", "consultation"])
    parser.add_argument("--patient", default="TEST0001")
    parser.add_argument("--doctor", default="DRTEST01")
    parser.add_argument("--hospital", default="CMED-LOCAL-01",
                        help="CMED's code for the clinic, as CMED sends it")
    parser.add_argument("--minutes", type=float, default=1.0)
    parser.add_argument("--reason", default="doctor_stopped",
                        help="doctor_stopped, or patient_did_not_consent to "
                             "erase the consultation everywhere")
    parser.add_argument("--session", default="", help="for built/pause/resume")
    parser.add_argument("--server", default="http://localhost:6000",
                        help="the AIMS LAB server, for CMED's two messages")
    parser.add_argument("--cmed-key", default="",
                        help="CMED's key; without it the recording is never "
                             "confirmed and never archived")
    parser.add_argument("--agent", default="",
                        help="the folder holding AIMScribe_Agent.exe, to read "
                             "its local API key from .env")
    parser.add_argument("--key", default="", help="the agent's AIMS_LOCAL_API_KEY")
    args = parser.parse_args(argv)

    if args.action == "health":
        print(http_get("/health"))
        return 0
    if args.action == "status":
        print(http_get("/api/v1/session/status", local_key(args)))
        return 0
    if args.action == "consultation":
        asyncio.run(consultation(args))
        return 0

    commands = {
        "start": ("start", {"trigger": five_fields(args)}),
        "built": ("prescription_built",
                  {"session_id": args.session, "patient_id": args.patient}),
        "stop": ("stop", {"reason": args.reason}),
        "pause": ("pause", {"reason": "clinical"}),
        "resume": ("resume", {}),
    }
    command, message = commands[args.action]
    asyncio.run(send(command, message))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
