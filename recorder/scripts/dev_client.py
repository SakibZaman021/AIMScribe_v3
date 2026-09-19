"""
Development WebSocket client - drives the agent without CMED or a backend.

Sends the v3 Channel A messages (SRS 3.2 §6.1): a five-field trigger, and
`prescription_built` to arm the gate. Run the agent with AIMS_REQUIRE_GRANT=false
until the server's grant endpoint exists; with it true, `start` is answered
503 AGENT_NOT_READY. Segments fail to upload without a backend and stay sealed
in the spool, which is the offline behaviour the design promises.

    python scripts/dev_client.py

Commands: start <patient> | built | pause <reason> | resume | stop | status | quit
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets

from config import config

DHAKA = timezone(timedelta(hours=6))

# The open consultation, as the page would remember it.
current = {"patient_id": None, "session_id": None}


def trigger(patient: str) -> dict:
    """API 1. In CMED, start_time comes from CMED's server; here we make one."""
    now = datetime.now(DHAKA).replace(microsecond=0)
    return {
        "patient_id": patient,
        "doctor_id": "DR_DEV",
        "hospital_id": "CMED-DEV-01",
        "start_time": now.isoformat(),
        "date": now.date().isoformat(),
    }


async def reader(socket) -> None:
    async for raw in socket:
        message = json.loads(raw)
        event = message.get("event", "?")
        if message.get("code") == "RECORDING_STARTED":
            current["session_id"] = (message.get("data") or {}).get("session_id")
        if event in ("ack", "error") and "code" in message:
            print(f"  [{message.get('command')}] {message['status']} {message['code']}"
                  f" - {message.get('message')}")
        elif event == "error":
            print(f"  [{event}] {message.get('message')}")
        elif event == "status":
            print(f"  [status] state={message.get('state')} "
                  f"session={message.get('session_id')} "
                  f"segments={message.get('segment_count')} "
                  f"spool={message.get('upload', {}).get('spool_bytes', 0)}B")
        else:
            print(f"  [{event}] " + json.dumps(
                {k: v for k, v in message.items()
                 if k not in ("event", "timestamp", "upload")}, ensure_ascii=False))


async def main() -> int:
    url = f"ws://{config.security.bind_host}:{config.security.bind_port}/ws"
    origin = sorted(config.security.allowed_origins)[0] if config.security.allowed_origins else None
    if not origin:
        print("AIMS_ALLOWED_ORIGINS is empty; the agent will refuse the connection.")
        return 1

    print(f"Connecting to {url} as origin {origin}")
    async with websockets.connect(url, origin=origin) as socket:
        asyncio.create_task(reader(socket))
        print("Connected. Commands: start <patient> | built | pause <reason> | resume | "
              "stop | status | quit")

        loop = asyncio.get_running_loop()
        while True:
            line = (await loop.run_in_executor(None, sys.stdin.readline)).strip()
            if not line:
                continue
            verb, _, rest = line.partition(" ")
            verb = verb.lower()

            if verb == "quit":
                return 0
            if verb == "start":
                patient = rest.strip() or "P12345"
                current["patient_id"] = patient
                await socket.send(json.dumps({
                    "command": "start",
                    "request_id": f"dev-{uuid.uuid4().hex[:8]}",
                    "trigger": trigger(patient),
                }))
            elif verb == "built":
                if not current["session_id"]:
                    print("  nothing to arm - start a consultation first")
                    continue
                await socket.send(json.dumps({
                    "command": "prescription_built",
                    "request_id": f"dev-{uuid.uuid4().hex[:8]}",
                    "patient_id": current["patient_id"],
                    "session_id": current["session_id"],
                    "occurred_at": datetime.now(DHAKA).isoformat(),
                }))
            elif verb == "pause":
                await socket.send(json.dumps({
                    "command": "pause",
                    "reason": rest.strip() or "patient_declined",
                    "reason_detail": "raised from the development client",
                    "expected_seconds": 60,
                }))
            elif verb in ("resume", "stop", "status"):
                await socket.send(json.dumps({"command": verb}))
            else:
                print(f"  unknown command: {verb}")


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
