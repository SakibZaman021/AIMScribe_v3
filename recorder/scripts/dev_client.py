"""
Development WebSocket client - drives the agent without CMED or a backend.

Lets you exercise the whole local pipeline (grant check, capture, segmenting,
spooling, supervised pause) before the server side exists. Segments will fail to
upload and stay sealed in the spool, which is exactly the offline behaviour the
design promises.

    python scripts/dev_client.py

Commands: start <patient> | pause <reason> | resume | stop | status | quit
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets

from config import config

SCRIPT_DIR = Path(__file__).resolve().parent


def mint_grant(patient: str) -> str:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "dev_make_grant.py"), "--patient", patient],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


async def reader(socket) -> None:
    async for raw in socket:
        message = json.loads(raw)
        event = message.get("event", "?")
        if event == "error":
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
        print("Connected. Commands: start <patient> | pause <reason> | resume | stop | status | quit")

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
                await socket.send(json.dumps({
                    "command": "start",
                    "grant": mint_grant(patient),
                    "session": {"patient_name": "Development Patient"},
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
