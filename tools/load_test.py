"""
A clinic day against a real server, before the first real patient (`AT-29`).

Fourteen rooms, eight hours, every recorder doing what a recorder does:
asking for a grant, opening a session, uploading pieces with a signed chain,
and closing. It uses the recorder's own crypto, so what the server sees here
is what it will see in a clinic - a test that passes with made-up messages
would prove nothing.

    python tools/load_test.py --server https://aimscribe.uiu.ac.bd \\
        --admin-key "$AIMS_ADMIN_KEY" --hospital HOSP003 --rooms 14 --hours 8

It writes real sessions and uploads real objects, so it belongs on a new
server before it carries patients, or on a bench - never on a live one.
`--dry-run` builds every message and sends nothing.

What it reports is what §9.1 promises a user: a grant in under 0.3 s, a
piece accepted in under 1 s, and no piece lost.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import tempfile
import time
import uuid
import wave
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "recorder"))

from core import crypto                                   # noqa: E402
from core.crypto import DeviceKey                         # noqa: E402

# What §9.1 promises, in seconds. The load test fails if the server cannot
# keep these while every room is busy.
TARGETS = {"grant": 0.3, "commit": 1.0, "open": 1.0, "close": 1.0}

SAMPLE_RATE, CHANNELS, SAMPLE_WIDTH = 44100, 1, 2


# ============================================================
# The plan - what a clinic day looks like (pure; tested)
# ============================================================

@dataclass(frozen=True)
class Consultation:
    room: int
    number: int
    patient_id: str
    doctor_id: str
    starts_at: float           # seconds into the run
    minutes: float

    @property
    def segments(self) -> int:
        return max(1, int(self.minutes * 60 // 30))      # a piece every 30 s


def plan_day(*, rooms: int, hours: float, minutes_each: float, gap_minutes: float,
             speed: float, seed: int = 7) -> List[Consultation]:
    """
    One room sees one patient at a time, with a gap between them. Rooms do
    not start together: a real clinic drifts, and a server that only copes
    when the load is evenly spread has not been tested.
    """
    if rooms < 1 or hours <= 0:
        return []
    rng = random.Random(seed)
    day_seconds = hours * 3600
    plan: List[Consultation] = []

    for room in range(1, rooms + 1):
        clock = rng.uniform(0, gap_minutes) * 60          # rooms open raggedly
        number = 0
        while clock < day_seconds:
            number += 1
            minutes = max(2.0, rng.gauss(minutes_each, minutes_each / 4))
            plan.append(Consultation(
                room=room, number=number,
                patient_id=f"LOAD{room:02d}{number:03d}",
                doctor_id=f"DRLOAD{room:02d}",
                starts_at=clock / speed,
                minutes=minutes))
            clock += (minutes + rng.uniform(gap_minutes / 2, gap_minutes)) * 60
    return sorted(plan, key=lambda c: c.starts_at)


# ============================================================
# What happened (pure; tested)
# ============================================================

@dataclass
class Timings:
    by_call: Dict[str, List[float]] = field(default_factory=dict)
    failures: List[str] = field(default_factory=list)
    pieces_sent: int = 0
    pieces_accepted: int = 0
    consultations: int = 0

    def record(self, call: str, seconds: float) -> None:
        self.by_call.setdefault(call, []).append(seconds)

    def fail(self, what: str) -> None:
        self.failures.append(what)


def percentile(values: Sequence[float], share: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))
    return ordered[index]


def report(timings: Timings) -> Dict[str, Any]:
    calls = {}
    for call, values in sorted(timings.by_call.items()):
        calls[call] = {
            "count": len(values),
            "p50": round(percentile(values, 0.50), 3),
            "p95": round(percentile(values, 0.95), 3),
            "max": round(max(values), 3),
            "target": TARGETS.get(call),
            "within_target": (percentile(values, 0.95) <= TARGETS[call]
                              if call in TARGETS else None),
        }
    lost = timings.pieces_sent - timings.pieces_accepted
    return {
        "consultations": timings.consultations,
        "pieces_sent": timings.pieces_sent,
        "pieces_accepted": timings.pieces_accepted,
        "pieces_lost": lost,
        "failures": timings.failures[:20],
        "failure_count": len(timings.failures),
        "calls": calls,
        # AT-29 asks two things: no lost pieces, and reply times within §9.1.
        "passed": lost == 0 and not timings.failures
                  and all(c["within_target"] is not False for c in calls.values()),
    }


def as_text(result: Dict[str, Any]) -> str:
    lines = [
        f"{result['consultations']} consultations, "
        f"{result['pieces_accepted']}/{result['pieces_sent']} pieces accepted",
        "",
        f"{'call':<10}{'count':>8}{'p50':>9}{'p95':>9}{'max':>9}{'target':>9}",
    ]
    for call, figures in result["calls"].items():
        target = figures["target"]
        mark = "" if figures["within_target"] is not False else "  OVER"
        lines.append(
            f"{call:<10}{figures['count']:>8}{figures['p50']:>9.3f}"
            f"{figures['p95']:>9.3f}{figures['max']:>9.3f}"
            f"{(f'{target:.1f}s' if target else '-'):>9}{mark}")
    if result["failures"]:
        lines += ["", f"{result['failure_count']} failure(s), first few:"]
        lines += [f"  {f}" for f in result["failures"]]
    lines += ["", "AT-29: PASSED" if result["passed"] else "AT-29: FAILED"]
    return "\n".join(lines)


# ============================================================
# One virtual recorder
# ============================================================

def wav_bytes(seconds: float) -> bytes:
    """A piece of audio of the right shape and size - quiet, but real WAV."""
    frames = int(seconds * SAMPLE_RATE)
    buffer = BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(frames * CHANNELS * SAMPLE_WIDTH))
    return buffer.getvalue()


class Room:
    """One enrolled PC, doing one consultation after another."""

    def __init__(self, client, room: int, hospital: str, cmed_hospital: str,
                 timings: Timings, *, dry_run: bool, key_dir: Path,
                 cmed_key: str = ""):
        self.client = client
        self.room = room
        self.hospital = hospital
        self.cmed_hospital = cmed_hospital
        self.timings = timings
        self.dry_run = dry_run
        # With CMED's key, the room also plays CMED: the visit is described
        # and prescribed over Channel B, which is what confirms a recording
        # and lets it archive. Without it the run stops at the recorder,
        # which is all AT-29 needs but leaves nothing for the worker to do.
        self.cmed_key = cmed_key
        # A real device key, made the way the recorder makes one, so the
        # chain the server checks here is signed as a clinic PC signs it.
        self.key = DeviceKey.load_or_create(key_dir / f"room{room:02d}.key",
                                            allow_plaintext=True)
        self.device_token: Optional[str] = None

    async def enrol(self, admin_key: str) -> None:
        if self.dry_run:
            self.device_token = "dry-run"
            return
        token = await self.client.post(
            "/api/v2/admin/enrollment-token",
            {"hospital_id": self.hospital, "created_by": "load-test"},
            headers={"X-Admin-Key": admin_key})
        enrolled = await self.client.post("/api/v2/device/enroll", {
            "enrollment_token": token["enrollment_token"],
            "device_pubkey": self.key.public_bytes_raw().hex(),
            "machine_name": f"LOADTEST-ROOM{self.room:02d}",
            "os_version": "load-test", "app_version": "3.3", "protocol_version": 2,
        })
        self.device_token = enrolled["device_token"]

    def headers(self) -> Dict[str, str]:
        return {"X-Device-Token": self.device_token or ""}

    async def consultation(self, plan: Consultation, speed: float) -> None:
        started = datetime.now(timezone.utc)
        session_id = uuid.uuid4().hex[:26].upper()

        # 1. The grant, from the five fields CMED's page sends.
        grant = await self.timed("grant", "/api/v2/grant/mint", {
            "patient_id": plan.patient_id, "doctor_id": plan.doctor_id,
            "hospital_id": self.cmed_hospital,
            "start_time": started.isoformat(), "date": started.date().isoformat(),
        })
        if grant is None:
            return
        five = {"patient_id": plan.patient_id, "doctor_id": plan.doctor_id,
                "hospital_id": self.cmed_hospital,
                "start_time": started.isoformat(),
                "date": started.date().isoformat()}
        await self.channel_b("patient-information", {
            **five,
            "demographics": {"name": f"Load Test {plan.patient_id}",
                             "sex": "female", "age_years": 34,
                             "phone": "01700000000", "address": "Load test"},
            "paramedic": {"recorded_at": five["start_time"], "weight_kg": 58,
                          "height_cm": 156, "blood_pressure": "120/80",
                          "pulse_bpm": 78, "temperature_c": 37.1,
                          "spo2_percent": 98},
            "previous_visit": None,
        })

        # 2. The session, with its genesis chain entry.
        genesis = crypto.build_entry(
            entry_no=0, entry_type="open", prev_hash=None, signer=self.key,
            payload=crypto.open_payload(
                device_id=session_id, doctor_id=plan.doctor_id,
                hospital_id=self.hospital, patient_ref=plan.patient_id,
                opened_at=started, sample_rate=SAMPLE_RATE, channels=CHANNELS,
                sample_width=SAMPLE_WIDTH))
        opened = await self.timed("open", "/api/v2/session/open", {
            "session_id": session_id, "opened_at": crypto.iso_utc(started),
            "doctor_id": plan.doctor_id, "hospital_id": self.hospital,
            "patient_ref": plan.patient_id, "consent_obtained": True,
            "grant_jti": (grant or {}).get("jti"),
            "audio": {"codec": "pcm_s16le", "container": "wav",
                      "sample_rate": SAMPLE_RATE, "channels": CHANNELS,
                      "sample_width": SAMPLE_WIDTH},
            "device_pubkey": self.key.public_bytes_raw().hex(),
            "genesis": genesis.to_wire(),
        })
        if opened is None:
            return

        # 3. The pieces, one every 30 seconds of consultation.
        previous, entry_no = genesis.entry_hash, 0
        audio = wav_bytes(30)
        for seq_no in range(1, plan.segments + 1):
            await asyncio.sleep(30 / speed)
            entry_no += 1
            at = datetime.now(timezone.utc)
            digest = crypto.sha256_bytes(audio)
            entry = crypto.build_entry(
                entry_no=entry_no, entry_type="segment", prev_hash=previous,
                signer=self.key,
                payload=crypto.segment_payload(
                    seq_no=seq_no, audio_sha256=digest, byte_length=len(audio),
                    duration_seconds=30.0, captured_start_at=at - timedelta(seconds=30),
                    captured_end_at=at, rms_mean=120.0,
                    is_final=seq_no == plan.segments))
            previous = entry.entry_hash
            await self.send_piece(session_id, seq_no, audio, digest, entry, at,
                                  final=seq_no == plan.segments)

        # 4. Close.
        entry_no += 1
        closed = datetime.now(timezone.utc)
        close_entry = crypto.build_entry(
            entry_no=entry_no, entry_type="close", prev_hash=previous, signer=self.key,
            payload=crypto.close_payload(
                closed_at=closed, duration_seconds=plan.minutes * 60,
                paused_seconds=0.0, segment_count=plan.segments,
                reason="prescription_built"))
        await self.timed("close", "/api/v2/session/close", {
            "session_id": session_id, "closed_at": crypto.iso_utc(closed),
            "duration_seconds": plan.minutes * 60, "paused_seconds": 0,
            "segment_count": plan.segments, "reason": "prescription_built",
            "chain_entry": close_entry.to_wire(),
        })
        await self.channel_b("prescription", {
            **five,
            "issued_at": crypto.iso_utc(closed),
            "diagnoses": ["Load test diagnosis"],
            "investigations": ["Load test investigation"],
            "advice": "Load test advice",
            "follow_up": (closed.date() + timedelta(days=30)).isoformat(),
            "items": [{"drug": "Load Test Medicine", "dose": "5 mg",
                       "frequency": "1+0+0", "duration": "30 days",
                       "instructions": "after food"}],
        })
        self.timings.consultations += 1

    async def channel_b(self, path: str, body: Dict[str, Any]) -> None:
        """CMED's half: API 2 and API 3, with CMED's key, not the device's."""
        if self.dry_run or not self.cmed_key:
            return
        started = time.perf_counter()
        try:
            await self.client.post(f"/api/v2/clinical/{path}", body,
                                   headers={"X-CMED-Key": self.cmed_key})
        except Exception as exc:
            self.timings.fail(f"{path}: {exc}")
        finally:
            self.timings.record(path, time.perf_counter() - started)

    async def send_piece(self, session_id: str, seq_no: int, audio: bytes,
                         digest: bytes, entry, at: datetime, *, final: bool) -> None:
        self.timings.pieces_sent += 1
        place = await self.timed("authorize", "/api/v2/segment/authorize", {
            "session_id": session_id, "seq_no": seq_no,
            "bytes": len(audio), "sha256": digest.hex()})
        if place is None:
            return
        if not self.dry_run:
            if not await self.client.put(place["upload_url"], audio):
                self.timings.fail(f"upload of piece {seq_no} failed")
                return
        committed = await self.timed("commit", "/api/v2/segment/commit", {
            "session_id": session_id, "seq_no": seq_no,
            "object_key": place.get("object_key", "dry-run"),
            "sha256": digest.hex(), "bytes": len(audio), "duration_seconds": 30.0,
            "captured_start_at": crypto.iso_utc(at - timedelta(seconds=30)),
            "captured_end_at": crypto.iso_utc(at), "rms_mean": 120.0,
            "is_final": final, "chain_entry": entry.to_wire()})
        if committed is not None:
            self.timings.pieces_accepted += 1

    async def timed(self, call: str, path: str,
                    body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if self.dry_run:
            json.dumps(body)                       # prove it is a valid message
            self.timings.record(call, 0.0)
            return {"jti": "dry-run", "upload_url": "", "object_key": "dry-run"}
        started = time.perf_counter()
        try:
            result = await self.client.post(path, body, headers=self.headers())
        except Exception as exc:
            self.timings.fail(f"room{self.room} {call}: {exc}")
            return None
        finally:
            self.timings.record(call, time.perf_counter() - started)
        return result


class Client:
    def __init__(self, base: str, session):
        self.base = base.rstrip("/")
        self.session = session

    async def post(self, path: str, body: Dict[str, Any],
                   headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"{self.base}{path}"
        async with self.session.post(url, json=body, headers=headers or {}) as response:
            if response.status >= 300:
                raise RuntimeError(f"{path} answered {response.status}: "
                                   f"{(await response.text())[:200]}")
            return await response.json()

    async def put(self, url: str, data: bytes) -> bool:
        async with self.session.put(url, data=data) as response:
            return response.status < 300


# ============================================================
# Running it
# ============================================================

async def run(args) -> Dict[str, Any]:
    plan = plan_day(rooms=args.rooms, hours=args.hours, minutes_each=args.minutes,
                    gap_minutes=args.gap, speed=args.speed)
    timings = Timings()
    print(f"{len(plan)} consultations across {args.rooms} room(s), "
          f"{args.hours}h of clinic at {args.speed}x "
          f"(about {args.hours * 3600 / args.speed / 60:.0f} minutes to run)")

    with tempfile.TemporaryDirectory(prefix="aims_loadtest_") as keys:
        key_dir = Path(keys)

        if args.dry_run:
            rooms = {n: Room(None, n, args.hospital, args.cmed_hospital, timings,
                             dry_run=True, key_dir=key_dir)
                     for n in range(1, args.rooms + 1)}
            for room in rooms.values():
                await room.enrol("")
            await _drive(plan, rooms, args, timings)
            return report(timings)

        import aiohttp
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            client = Client(args.server, session)
            rooms = {}
            for n in range(1, args.rooms + 1):
                room = Room(client, n, args.hospital, args.cmed_hospital, timings,
                            dry_run=False, key_dir=key_dir,
                            cmed_key=args.cmed_key)
                await room.enrol(args.admin_key)
                rooms[n] = room
            print(f"{len(rooms)} virtual recorder(s) enrolled")
            await _drive(plan, rooms, args, timings)
        return report(timings)


async def _drive(plan, rooms, args, timings: Timings) -> None:
    started = time.perf_counter()
    running: List[asyncio.Task] = []
    for consultation in plan:
        wait = consultation.starts_at - (time.perf_counter() - started)
        if wait > 0:
            await asyncio.sleep(wait)
        room = rooms[consultation.room]
        running.append(asyncio.create_task(room.consultation(consultation, args.speed)))
        running = [task for task in running if not task.done()]
    await asyncio.gather(*running, return_exceptions=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", default="http://localhost:6060")
    parser.add_argument("--admin-key", default=os.getenv("AIMS_ADMIN_KEY", ""))
    parser.add_argument("--hospital", default="HOSP003", help="the AIMS LAB clinic code")
    parser.add_argument("--cmed-hospital", default="", help="CMED's code for it")
    parser.add_argument("--cmed-key", default="",
                        help="CMED's key. Given, every consultation is also "
                             "described and prescribed over Channel B, so the "
                             "recordings confirm and archive")
    parser.add_argument("--rooms", type=int, default=14)
    parser.add_argument("--hours", type=float, default=8.0)
    parser.add_argument("--minutes", type=float, default=15.0,
                        help="average consultation length")
    parser.add_argument("--gap", type=float, default=5.0,
                        help="average minutes between consultations in a room")
    parser.add_argument("--speed", type=float, default=60.0,
                        help="how much faster than real time (60 = a clinic day "
                             "in eight minutes)")
    parser.add_argument("--dry-run", action="store_true",
                        help="build every message, send nothing")
    parser.add_argument("--json", action="store_true", help="report as JSON")
    args = parser.parse_args(argv)
    args.cmed_hospital = args.cmed_hospital or args.hospital

    if not args.dry_run and not args.admin_key:
        parser.error("--admin-key is needed to enrol the virtual recorders "
                     "(or use --dry-run)")
    if not args.dry_run:
        print("This writes real sessions and uploads real objects. Use it on a new "
              "server or a bench, never on one carrying patients.")

    result = asyncio.run(run(args))
    print()
    print(json.dumps(result, indent=2) if args.json else as_text(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
