"""
The whole system, on one machine, with nothing stubbed but the microphone
and the cloud.

    python tools/simulate.py                       # a short run, all checks
    python tools/simulate.py --rooms 6 --consultations 8 --speed 120
    python tools/simulate.py --keep                # leave the data to look at

What actually runs here is the shipping code:

  * the **real server** - `api_v2`, Channel B and the dashboard, on a real
    PostgreSQL 16 with both databases built from the migration scripts in the
    order a new UIU server applies them;
  * the **real recorder** - `Runtime` from `api/trigger_server.py`: the same
    spool, hash chain, uploader, grant checking and WebSocket contract that
    ship in `AIMScribe_Agent.exe`, driven over `ws://` exactly as CMED's page
    drives it. Only the microphone is synthetic;
  * **CMED** - API 2 and API 3 sent to the clinical endpoints with a key, as
    CMED's server sends them;
  * the **real archive worker** - merging, the JSON beside the audio, the
    lossless copy, and the deletion of the pieces, in that order.

The two things that are not real are named so nobody mistakes them:

  * the microphone is a tone generator, so a consultation can be simulated in
    seconds rather than minutes;
  * object storage is a small in-process S3 stand-in. It stores the bytes and
    answers the same calls; it is not Cloudflare and it is not Amazon.

Every check the run makes is printed with its requirement, and the exit code
is non-zero if any of them fails.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import math
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "recorder"))
sys.path.insert(0, str(ROOT / "backend" / "archive_worker"))

import load_test                                    # noqa: E402

SAMPLE_RATE, CHANNELS, SAMPLE_WIDTH = 44100, 1, 2
CLINIC, CMED_CLINIC = "HOSP003", "CMED-SIM-01"
# Where CMED's page is served from, as a browser would report it.
CMED_ORIGIN = "http://127.0.0.1:3000"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# ============================================================
# What the run proves
# ============================================================

@dataclass
class Checks:
    results: List[Tuple[bool, str, str]] = field(default_factory=list)

    def check(self, passed: bool, what: str, requirement: str = "") -> bool:
        self.results.append((bool(passed), what, requirement))
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {what}" + (f"   ({requirement})" if requirement else ""))
        return bool(passed)

    @property
    def failed(self) -> List[Tuple[bool, str, str]]:
        return [r for r in self.results if not r[0]]


# ============================================================
# Object storage, in memory (not Cloudflare, not Amazon)
# ============================================================

class FakeS3(ThreadingHTTPServer):
    """
    Enough of S3 for the system to run: PUT, GET, HEAD and DELETE.

    Signatures are not checked - this stands in for a bucket, not for a
    provider's access control. Everything else about the path the bytes take
    is real: the recorder uploads to a presigned URL, the server reads the
    object back to verify it, and the worker uploads the copy.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int):
        super().__init__(("127.0.0.1", port), _S3Handler)
        self.objects: Dict[str, bytes] = {}
        self.lock = threading.Lock()


class _S3Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):            # quiet; the run has its own log
        pass

    def _key(self) -> str:
        return unquote(urlparse(self.path).path.lstrip("/").split("?")[0])

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        with self.server.lock:
            self.server.objects[self._key()] = body
        self.send_response(200)
        self.send_header("ETag", f'"{hashlib.md5(body).hexdigest()}"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        with self.server.lock:
            body = self.server.objects.get(self._key())
        if body is None:
            self.send_error(404, "NoSuchKey")
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", f'"{hashlib.md5(body).hexdigest()}"')
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        with self.server.lock:
            body = self.server.objects.get(self._key())
        if body is None:
            self.send_error(404, "NoSuchKey")
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", f'"{hashlib.md5(body).hexdigest()}"')
        self.end_headers()

    def do_DELETE(self):
        with self.server.lock:
            self.server.objects.pop(self._key(), None)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


# ============================================================
# PostgreSQL, built the way a new UIU server builds it
# ============================================================

PG_BOOT = r'''
import sys, tempfile, pgserver
data = tempfile.mkdtemp(prefix="aimssim")
server = pgserver.get_server(data, cleanup_mode="stop")
print(server.get_uri(), flush=True)
sys.stdin.readline()
'''


class Postgres:
    """
    A real PostgreSQL 16, started from the portable build.

    It runs under whichever interpreter has `pgserver` installed, which is
    usually not the one running this script, so it is started as a child
    process and told to stop when the run ends.
    """

    def __init__(self, interpreter: str):
        self.interpreter = interpreter
        self.process: Optional[subprocess.Popen] = None
        self.uri = ""

    def start(self) -> str:
        self.process = subprocess.Popen(
            [self.interpreter, "-c", PG_BOOT], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = self.process.stdout.readline().strip()
        if not line.startswith("postgres"):
            error = self.process.stderr.read()[:2000]
            raise RuntimeError(f"PostgreSQL did not start: {line} {error}")
        self.uri = line
        return line

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            with contextlib.suppress(Exception):
                self.process.stdin.write("\n")
                self.process.stdin.flush()
            with contextlib.suppress(Exception):
                self.process.wait(timeout=30)


async def build_databases(base_uri: str) -> Tuple[str, str]:
    """Both databases, from the same scripts and in the same order as deploy/uiu."""
    import asyncpg

    admin = await asyncpg.connect(base_uri)
    try:
        for name in ("aims_recordings", "aims_clinical"):
            await admin.execute(f"DROP DATABASE IF EXISTS {name}")
            await admin.execute(f"CREATE DATABASE {name}")
    finally:
        await admin.close()

    root = base_uri.rsplit("/", 1)[0]
    recordings, clinical = f"{root}/aims_recordings", f"{root}/aims_clinical"
    scripts = ROOT / "backend" / "scripts"
    baseline = (ROOT / "deploy" / "uiu" / "postgres" / "schema"
                / "00_v1_baseline.sql").read_text(encoding="utf-8")

    # The portable build has no contrib extensions; production has both.
    skips = ("CREATE EXTENSION IF NOT EXISTS pgcrypto;",
             "CREATE EXTENSION IF NOT EXISTS pg_trgm;",
             "CREATE INDEX IF NOT EXISTS idx_sessions_file_stem_trgm\n"
             "    ON sessions USING gin (file_stem gin_trgm_ops);")

    def portable(sql: str) -> str:
        for statement in skips:
            sql = sql.replace(statement, "-- (not in the portable build)")
        return sql

    conn = await asyncpg.connect(recordings)
    try:
        await conn.execute(baseline)
        for script in sorted(scripts.glob("0[0-9][0-9]_*.sql")):
            await conn.execute(portable(script.read_text(encoding="utf-8")))
    finally:
        await conn.close()

    conn = await asyncpg.connect(clinical)
    try:
        for script in sorted((scripts / "clinical").glob("001_*.sql")):
            await conn.execute(portable(script.read_text(encoding="utf-8")))
    finally:
        await conn.close()
    return recordings, clinical


# ============================================================
# The server
# ============================================================

class Server:
    """The shipping API, on a real port, against the real databases."""

    def __init__(self, *, port: int, s3_port: int, recordings: str, clinical: str,
                 workdir: Path):
        self.port = port
        self.s3_port = s3_port
        self.recordings = recordings
        self.clinical = clinical
        self.workdir = workdir
        self.url = f"http://127.0.0.1:{port}"
        self.admin_key = "sim-admin-key"
        self.worker_key = "sim-worker-key"
        self.cmed_key = "sim-cmed-key"
        self.grant_public_pem = b""
        self.receipt_public_pem = b""
        self._thread: Optional[threading.Thread] = None
        self._server = None
        self.ready = threading.Event()

    async def start(self) -> None:
        import uvicorn
        from contextlib import asynccontextmanager
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from fastapi import FastAPI

        import api_v2
        import clinical as channel_b
        import dashboard

        os.environ["AIMS_ADMIN_KEY"] = self.admin_key
        os.environ["AIMS_WORKER_KEY"] = self.worker_key

        grant_key = Ed25519PrivateKey.generate()
        self.grant_public_pem = grant_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        receipt_key = Ed25519PrivateKey.generate()
        self.receipt_public_pem = receipt_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

        @asynccontextmanager
        async def lifespan(app):
            # Everything the server owns is built on the server's own event
            # loop. A connection pool made on another loop appears to work and
            # then fails under load with "attached to a different loop" - which
            # is exactly what the first run of this simulation did.
            import asyncpg
            from clinical_store import ClinicalStore
            from db_v2 import V2Repository
            from grants import GrantIssuer
            from integrity import ReceiptSigner
            from storage.minio_client import MinIOClient

            pool = await asyncpg.create_pool(self.recordings, min_size=2, max_size=10)
            clinical_pool = await asyncpg.create_pool(self.clinical, min_size=1,
                                                      max_size=5)
            endpoint = f"127.0.0.1:{self.s3_port}"
            buckets = {name: MinIOClient(endpoint=endpoint, access_key="sim",
                                         secret_key="simsimsim", bucket=name,
                                         secure=False, region="us-east-1")
                       for name in ("aims-segments", "aims-copies", "aims-json")}

            api_v2.ctx.repo = V2Repository(pool)
            api_v2.ctx.minio = buckets["aims-segments"]
            api_v2.ctx.clinical = ClinicalStore(clinical_pool, separate=True)
            api_v2.ctx.copy = buckets["aims-copies"]
            api_v2.ctx.copy_json = buckets["aims-json"]
            api_v2.ctx.signer = ReceiptSigner(receipt_key)
            api_v2.ctx.grants = GrantIssuer(grant_key)
            self.ready.set()
            try:
                yield
            finally:
                await pool.close()
                await clinical_pool.close()

        app = FastAPI(title="AIMScribe (simulated)", lifespan=lifespan)
        app.include_router(api_v2.router)
        app.include_router(channel_b.router)
        app.include_router(dashboard.router)

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        config = uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                log_level="warning", access_log=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        for _ in range(200):
            if self.ready.is_set() and getattr(self._server, "started", False):
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("the server did not start")

    async def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=15)

    # ---- what an administrator sets up once ----

    @property
    def admin(self) -> Dict[str, str]:
        return {"X-Admin-Key": self.admin_key}

    async def prepare(self) -> None:
        """What an administrator does once, through the API they really use."""
        status, _ = await post_json(f"{self.url}/api/v2/admin/hospital", {
            "hospital_id": CLINIC, "name": "Simulated clinic",
            "timezone": "Asia/Dhaka", "cmed_hospital_id": CMED_CLINIC},
            headers=self.admin)
        if status >= 300:
            raise RuntimeError(f"the clinic could not be registered ({status})")

        # The key is returned once and stored only as a hash (SRS-CHB-02), so
        # the run keeps the one it is given.
        status, reply = await post_json(f"{self.url}/api/v2/admin/cmed-key", {
            "label": "cmed-sim", "created_by": "simulate"}, headers=self.admin)
        if status >= 300 or not reply.get("cmed_key"):
            raise RuntimeError(f"no CMED key was issued ({status}) {reply}")
        self.cmed_key = reply["cmed_key"]

    async def enrolment_token(self) -> str:
        status, reply = await post_json(f"{self.url}/api/v2/admin/enrollment-token", {
            "hospital_id": CLINIC, "created_by": "simulate"}, headers=self.admin)
        token = reply.get("enrollment_token") or reply.get("token")
        if status >= 300 or not token:
            raise RuntimeError(f"no enrolment token was issued ({status}) {reply}")
        return token


# ============================================================
# The recorder, with a synthetic microphone
# ============================================================

class ToneMicrophone:
    """
    Stands in for the sound card: the same chunks, at the same rate, without
    a room. Everything downstream - segmenting, sealing, hashing, the chain,
    the upload - is the shipping code.
    """

    def __init__(self, *, sample_rate, channels, sample_width, frames_per_buffer,
                 input_device_index=None, on_chunk=None, on_alert=None, **_):
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width
        self._on_chunk = on_chunk
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._phase = 0
        self._chunks = 0
        # A tenth of a second per chunk, delivered as fast as the simulation
        # runs rather than in real time.
        self.frames = max(1, sample_rate // 10)
        self.speed = float(os.environ.get("AIMS_SIM_SPEED", "60"))

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * self.sample_width

    @property
    def duration_seconds(self) -> float:
        return self._chunks * self.frames / self.sample_rate

    def describe_device(self) -> str:
        return "simulated tone generator"

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        return self.stats()

    def stats(self):
        from types import SimpleNamespace
        return SimpleNamespace(overruns=0, chunks=self._chunks, duration_seconds=0.0,
                               as_dict=lambda: {"overruns": 0, "chunks": self._chunks})

    def _run(self):
        period = (self.frames / self.sample_rate) / max(self.speed, 1.0)
        while not self._stop.is_set():
            chunk = bytearray()
            for _ in range(self.frames):
                self._phase += 1
                value = int(6000 * math.sin(self._phase / 28.0)
                            + 800 * math.sin(self._phase / 3.1))
                chunk += int(value).to_bytes(2, "little", signed=True) * self.channels
            self._chunks += 1
            if self._on_chunk:
                # The segmenter's queue is sized for audio arriving in real
                # time. Simulated audio arrives faster, so when it fills, this
                # waits rather than dropping a chunk - a dropped chunk here
                # would be a hole in the recording that nothing else caused.
                for attempt in range(200):
                    try:
                        self._on_chunk(bytes(chunk))
                        break
                    except Exception:
                        time.sleep(0.01)
            time.sleep(period)


class Room:
    """One consulting room: one PC, one recorder, one CMED page."""

    def __init__(self, number: int, server: Server, workdir: Path, speed: float):
        self.number = number
        self.server = server
        self.home = workdir / f"room{number:02d}"
        self.home.mkdir(parents=True, exist_ok=True)
        self.speed = speed
        self.runtime = None
        self.port = free_port()
        self._uvicorn = None
        self._thread: Optional[threading.Thread] = None
        self.ws_url = f"ws://127.0.0.1:{self.port}/ws"

    async def install(self, token: str) -> None:
        """What the installer does on a doctor's PC, then what the agent does."""
        # What the installer puts on a doctor's PC: the two public keys it
        # pins, and the single-use enrolment token (SRS-ENR-03, SRS-GRT-04).
        machine = self.home / "AIMScribe"
        keys, state = machine / "keys", machine / "state"
        for folder in (keys, state, machine / "logs"):
            folder.mkdir(parents=True, exist_ok=True)
        (keys / "aimslab_grant_pub.pem").write_bytes(self.server.grant_public_pem)
        (keys / "aimslab_receipt_pub.pem").write_bytes(self.server.receipt_public_pem)
        (state / "enrollment.token").write_text(token, encoding="utf-8")

        # The recorder keeps its state, keys and spool under PROGRAMDATA. Each
        # simulated PC gets its own, so a real enrolment on this machine is
        # never touched and the rooms cannot see each other's keys.
        os.environ["PROGRAMDATA"] = str(self.home)

        for name, value in {
            "AIMS_BACKEND_URL": self.server.url,
            "AIMS_SPOOL_DIR": str(self.home / "spool"),
            "AIMS_GRANT_PUBLIC_KEY_PATH": str(keys / "aimslab_grant_pub.pem"),
            "AIMS_ALLOW_PLAINTEXT_KEYSTORE": "true",
            "AIMS_BIND_PORT": str(self.port),
            # The recorder refuses a connection whose Host header is not on
            # its allowlist - a real defence against DNS rebinding, and one
            # this simulation has to satisfy like any other page would.
            "AIMS_ALLOWED_HOSTS": f"127.0.0.1:{self.port},localhost:{self.port}",
            # And only from CMED's own page: a connection with no Origin, or
            # one from anywhere else, is refused (SRS-IF1-03).
            "AIMS_ALLOWED_ORIGINS": CMED_ORIGIN,
            "AIMS_OVERLAY": "false",
            "AIMS_SEGMENT_MIN_SECONDS": "5",
            "AIMS_SEGMENT_MAX_SECONDS": "10",
            "AIMS_SEGMENT_GRACE_SECONDS": "2",
            "AIMS_HEARTBEAT_SECONDS": "5",
            "AIMS_SIM_SPEED": str(self.speed),
        }.items():
            os.environ[name] = value

        import config as recorder_config
        import core.session_controller as controller_module
        import uvicorn
        from api.trigger_server import Runtime, create_app

        cfg = recorder_config.Config.load()
        # The one substitution: no sound card in a simulation.
        controller_module.AudioRecorder = ToneMicrophone

        self.runtime = Runtime(cfg, log_salt=b"simulate")
        app = create_app(self.runtime)
        self._uvicorn = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="error",
            access_log=False))
        self._thread = threading.Thread(target=self._uvicorn.run, daemon=True)
        self._thread.start()
        for _ in range(150):
            if getattr(self._uvicorn, "started", False):
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError(f"room {self.number}: the recorder did not start")

    async def stop(self) -> None:
        if self._uvicorn:
            self._uvicorn.should_exit = True
        if self._thread:
            self._thread.join(timeout=15)


# ============================================================
# CMED: the page on the PC, and the server behind it
# ============================================================

class Cmed:
    """
    What CMED does for one consultation: the five fields to the recorder over
    the WebSocket, and API 2 and API 3 to the AIMS LAB server with its key.
    """

    def __init__(self, room: "Room", server: Server, timings: "Timings"):
        self.room = room
        self.server = server
        self.timings = timings
        self.socket = None
        self.counter = 0

    async def connect(self):
        import websockets
        self.socket = await websockets.connect(
            self.room.ws_url, open_timeout=20, ping_interval=None,
            origin=CMED_ORIGIN)
        return self

    async def close(self):
        if self.socket:
            await self.socket.close()

    async def command(self, name: str, **payload) -> Dict[str, Any]:
        """One command, one reply, matched by request_id (§6.1.4)."""
        self.counter += 1
        request_id = f"sim-{self.room.number}-{self.counter}"
        started = time.perf_counter()
        await self.socket.send(json.dumps({"command": name, "request_id": request_id,
                                           **payload}))
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(self.socket.recv(), timeout=40)
            message = json.loads(raw)
            if message.get("request_id") == request_id:
                self.timings.record(name, time.perf_counter() - started)
                return message
        raise TimeoutError(f"{name} was not answered")

    async def status(self) -> Dict[str, Any]:
        reply = await self.command("status")
        return reply.get("data") or {}

    async def channel_b(self, kind: str, body: Dict[str, Any]) -> Tuple[int, Dict]:
        path = "patient-information" if kind == "patient_information" else "prescription"
        started = time.perf_counter()
        status, reply = await post_json(
            f"{self.server.url}/api/v2/clinical/{path}", body,
            headers={"X-CMED-Key": self.server.cmed_key})
        self.timings.record(f"channel_b:{kind}", time.perf_counter() - started)
        return status, reply


async def post_json(url: str, body: Dict[str, Any],
                    headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict]:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})})
    loop = asyncio.get_running_loop()

    def send():
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw or b"{}")
            except ValueError:
                return exc.code, {}

    return await loop.run_in_executor(None, send)


async def get_json(url: str, headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict]:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers=headers or {})
    loop = asyncio.get_running_loop()

    def send():
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, {}

    return await loop.run_in_executor(None, send)


@dataclass
class Timings:
    by_call: Dict[str, List[float]] = field(default_factory=dict)

    def record(self, call: str, seconds: float) -> None:
        self.by_call.setdefault(call, []).append(seconds)

    def report(self) -> List[Tuple[str, int, float, float, float]]:
        rows = []
        for call, values in sorted(self.by_call.items()):
            ordered = sorted(values)
            p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
            rows.append((call, len(values), statistics.median(ordered), p95,
                         max(ordered)))
        return rows


# ============================================================
# One consultation, from opening the patient to the prescription
# ============================================================

@dataclass
class Consultation:
    room: int
    patient_id: str
    doctor_id: str
    start_time: str
    date: str
    session_id: str = ""
    start_code: str = ""
    refused: bool = False
    api2_code: str = ""
    api3_code: str = ""
    gate_code: str = ""
    segments: int = 0


def demographics(patient_id: str) -> Dict[str, Any]:
    return {"name": f"Simulated Patient {patient_id[-3:]}", "sex": "female",
            "age_years": 34, "phone": "01700000000"}


async def one_consultation(cmed: Cmed, number: int, *, minutes: float,
                           refuse: bool = False) -> Consultation:
    room = cmed.room.number
    started = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=6)))
    visit = Consultation(
        room=room,
        patient_id=f"SIM{room:02d}{number:03d}",
        doctor_id=f"DRSIM{room:02d}",
        start_time=started.replace(microsecond=0).isoformat(),
        date=started.date().isoformat())

    # 1. CMED's page: the five fields (API 1).
    reply = await cmed.command("start", trigger={
        "patient_id": visit.patient_id, "doctor_id": visit.doctor_id,
        "hospital_id": CMED_CLINIC, "start_time": visit.start_time,
        "date": visit.date})
    visit.start_code = reply.get("code", "")
    if visit.start_code not in ("RECORDING_STARTED", "RECORDING_PROVISIONAL"):
        return visit

    # 2. CMED's server: API 2, which is what confirms the recording (§5.6).
    status, body = await cmed.channel_b("patient_information", {
        "patient_id": visit.patient_id, "doctor_id": visit.doctor_id,
        "hospital_id": CMED_CLINIC, "start_time": visit.start_time,
        "date": visit.date, "demographics": demographics(visit.patient_id),
        "paramedic": {"blood_pressure": "120/80", "pulse_bpm": 78,
                      "temperature_c": 36.8},
        "previous_visit": None})
    visit.api2_code = body.get("code", str(status))

    # 3. The consultation itself. Audio arrives at the simulated speed.
    await asyncio.sleep(max(0.6, minutes * 60 / cmed.room.speed))
    state = await cmed.status()
    visit.session_id = state.get("session_id") or ""
    visit.segments = int(state.get("segment_count") or 0)

    if refuse:
        # The patient said no: Stop on the recorder, "Patient did not consent".
        await cmed.command("stop", reason="patient_did_not_consent")
        visit.refused = True
        return visit

    # 4. The prescription is built: the gate opens (API 3, part one).
    gate = await cmed.command("prescription_built", patient_id=visit.patient_id,
                              session_id=visit.session_id)
    visit.gate_code = gate.get("code", "")

    # 5. And the prescription itself (API 3, part two).
    status, body = await cmed.channel_b("prescription", {
        "patient_id": visit.patient_id, "doctor_id": visit.doctor_id,
        "hospital_id": CMED_CLINIC, "start_time": visit.start_time,
        "date": visit.date, "issued_at": datetime.now(timezone.utc).isoformat(),
        "diagnoses": ["Simulated diagnosis"], "investigations": ["ECG"],
        "advice": "Simulated advice",
        "items": [{"drug": "Amlodipine", "dose": "5 mg", "frequency": "1+0+0",
                   "duration": "30 days"}]})
    visit.api3_code = body.get("code", str(status))

    # 6. The doctor closes it.
    await cmed.command("stop", reason="prescription_built")
    return visit


# ============================================================
# The archive worker, doing what it does at UIU
# ============================================================

class Archiver:
    """The shipping worker, pointed at this run's server and archive."""

    def __init__(self, server: Server, archive: Path, copy_key: bytes):
        self.archive = archive
        self.archive.mkdir(parents=True, exist_ok=True)
        for name, value in {
            "AIMS_BACKEND_URL": server.url,
            "AIMS_WORKER_KEY": server.worker_key,
            "AIMS_ARCHIVE_ROOT": str(archive),
            "AIMS_COPY_KEY": base64.b64encode(copy_key).decode(),
            "AIMS_COPY_BATCH": "5",
            "AIMS_BATCH_SIZE": "10",
            "AIMS_DISK_HEADROOM_BYTES": str(50 * 1024 ** 2),
        }.items():
            os.environ[name] = value

        import worker as worker_module
        self.module = worker_module
        self.worker = worker_module.ArchiveWorker(worker_module.Settings())

    async def run_until_quiet(self, *, passes: int = 12) -> int:
        """
        Work until nothing moves. Each pass archives what is closed and copies
        what is archived, exactly as it does on the real machine.
        """
        loop = asyncio.get_running_loop()
        done = 0
        for _ in range(passes):
            moved = await loop.run_in_executor(None, self.worker.drain_once)
            done += moved
            if moved == 0:
                break
            await asyncio.sleep(0.2)
        return done


async def wait_until_delivered(recordings_uri: str, visits: List["Consultation"], *,
                               seconds: int = 120) -> float:
    """
    Wait until every consultation has reached the server: closed, or - for the
    patient who said no - erased.

    Neither is instant. The recorder deletes its own copy at once and then
    reports, retrying until the server acknowledges, so "it is gone" is a
    promise the system keeps in seconds, not in the same breath.
    """
    kept = {v.session_id for v in visits if not v.refused and v.session_id}
    refused = {v.session_id for v in visits if v.refused and v.session_id}
    started = time.perf_counter()

    while time.perf_counter() - started < seconds:
        async with asyncpg_connection(recordings_uri) as conn:
            closed = {r["session_id"] for r in await conn.fetch(
                "SELECT session_id FROM sessions WHERE closed_at IS NOT NULL")}
            erased = {r["session_id"] for r in await conn.fetch(
                "SELECT session_id FROM sessions WHERE confirmation = 'refused'")}
        if kept <= closed and refused <= erased:
            break
        await asyncio.sleep(1)
    return time.perf_counter() - started


# ============================================================
# What has to be true when it is over
# ============================================================

async def verify(checks: Checks, *, server: Server, visits: List[Consultation],
                 archive: Path, s3: FakeS3, copy_key: bytes,
                 clinical_uri: str, recordings_uri: str) -> None:

    kept = [v for v in visits if not v.refused and v.start_code in
            ("RECORDING_STARTED", "RECORDING_PROVISIONAL")]
    refused = [v for v in visits if v.refused]

    print("\nThe consultation, end to end")
    checks.check(all(v.start_code in ("RECORDING_STARTED", "RECORDING_PROVISIONAL")
                     for v in visits),
                 f"all {len(visits)} consultations started on five fields",
                 "SRS-GRT-07, Appendix A")
    checks.check(all(v.api2_code == "ACCEPTED" for v in visits),
                 "CMED's API 2 was accepted for every consultation", "SRS-CHB-05")
    checks.check(all(v.gate_code in ("GATE_ARMED", "GATE_ALREADY_ARMED") for v in kept),
                 "the gate opened on prescription_built", "SRS §7.7")
    checks.check(all(v.api3_code == "ACCEPTED" for v in kept),
                 "every prescription was stored", "SRS-CRI-03")
    checks.check(all(v.segments > 0 for v in visits),
                 "audio was captured and sealed in every room", "SRS-REC-01")

    async with asyncpg_connection(recordings_uri) as conn:
        rows = {r["session_id"]: dict(r) for r in await conn.fetch("""
            SELECT session_id, patient_id, doctor_id, hospital_id, confirmation,
                   status, file_stem, archived_at, copied_at, segments_deleted_at,
                   archive_relpath, archive_sha256, close_reason
              FROM sessions
        """)}
        receipts = await conn.fetchval("SELECT count(*) FROM purge_receipts")
        pieces_left = await conn.fetchval(
            "SELECT count(*) FROM segments WHERE object_deleted_at IS NULL")
        audit = {r["event_type"]: r["n"] for r in await conn.fetch(
            "SELECT event_type, count(*) AS n FROM audit_log GROUP BY event_type")}
        notices = await conn.fetchval("SELECT count(*) FROM confirmation_notices")
        copies = await conn.fetchval("SELECT count(*) FROM cloud_copies")

    print("\nConfirmation against CMED (§5.6)")
    confirmed = [s for s in rows.values() if s["confirmation"] == "confirmed"]
    checks.check(len(confirmed) >= len(kept),
                 f"{len(confirmed)} recordings confirmed by CMED's API 2",
                 "SRS-CNF-03")
    # A refusal deletes the notice with everything else about that visit,
    # so the ones that remain are the consultations that were kept.
    checks.check(notices >= len(kept),
                 f"{notices} five-field notices held on the recordings side, "
                 f"{len(refused)} deleted with their refusal",
                 "SRS-DBA-20, SRS-CNS-04")

    print("\nThe patient who said no (§7.8a)")
    for visit in refused:
        session = rows.get(visit.session_id, {})
        checks.check(session.get("confirmation") == "refused",
                     f"{visit.patient_id}: the recording is marked refused",
                     "SRS-CNS-03")
        checks.check(session.get("patient_id") == "REDACTED",
                     f"{visit.patient_id}: no patient identifier is left on it",
                     "SRS-CNS-06")
        async with asyncpg_connection(clinical_uri) as conn:
            left = await conn.fetchval(
                "SELECT count(*) FROM intake_records WHERE patient_id = $1",
                visit.patient_id)
        checks.check(left == 0,
                     f"{visit.patient_id}: what CMED sent about them is gone too",
                     "SRS-CNS-04")

    print("\nThe archive at UIU (§7.10)")
    archived = [s for s in rows.values() if s["archived_at"]]
    checks.check(len(archived) >= len(kept),
                 f"{len(archived)} recordings merged into the archive", "SRS-ARC-02")
    checks.check(receipts > 0, f"{receipts} receipts signed", "SRS-REC-15")

    named, with_json, chain_ok = 0, 0, 0
    for session in archived:
        stem = session["file_stem"] or ""
        parts = stem.split("_")
        if len(parts) == 6 and parts[2] == CLINIC and len(parts[5]) == 8:
            named += 1
        wav = archive / (session["archive_relpath"] or "")
        if wav.is_file():
            if wav.with_suffix(".json").is_file():
                with_json += 1
            digest = hashlib.sha256(wav.read_bytes()).digest()
            if session["archive_sha256"] and bytes(session["archive_sha256"]) == digest:
                chain_ok += 1

    checks.check(named == len(archived),
                 "every recording is named Patient_Doctor_Clinic_start_end_date",
                 "SRS-SES-05")
    checks.check(with_json == len(archived),
                 "every recording has its clinical JSON beside it", "SRS-CRI-05")
    checks.check(chain_ok == len(archived),
                 "every archived file hashes to what the server recorded",
                 "SRS-ARC-03")

    # The JSON is the portable copy: it must carry both halves, and the
    # prescription that arrived over Channel B.
    sample = next((archive / (s["archive_relpath"] or "") for s in archived
                   if (archive / (s["archive_relpath"] or "")).is_file()), None)
    if sample is not None:
        document = json.loads(sample.with_suffix(".json").read_text(encoding="utf-8"))
        checks.check(document.get("patient", {}).get("full_name", "").startswith(
            "Simulated Patient"), "the JSON carries what CMED sent", "§8.8")
        checks.check(bool(document.get("recording", {}).get("audio_sha256")),
                     "the JSON carries the recording's fingerprint", "§8.8")
        checks.check(bool((document.get("prescription") or {}).get("items")),
                     "the JSON carries the prescription", "SRS-ARC-13")

    print("\nThe cloud copy (SRS-ARC-08..13)")
    copied = [s for s in rows.values() if s["copied_at"]]
    checks.check(len(copied) >= len(archived),
                 f"{len(copied)} recordings have a verified cloud copy", "SRS-ARC-09")
    checks.check(copies >= 2 * len(copied),
                 f"{copies} objects recorded in the copy store (audio and JSON)",
                 "SRS-ARC-12")
    checks.check(pieces_left == 0,
                 "the pieces were deleted - and only after the copy was verified",
                 "SRS-ARC-09 step 7")

    # A copy is only worth keeping if it comes back. Take one, decrypt it,
    # decode it, and compare the samples with the archived recording (AT-74).
    import cloudcopy
    with s3.lock:
        sealed_keys = [k for k in s3.objects if k.endswith("flac.enc")]
    if sealed_keys and sample is not None:
        restored = archive / "_restore_test"
        restored.mkdir(exist_ok=True)
        blob = s3.objects[sorted(sealed_keys)[0]]
        (restored / "copy.flac.enc").write_bytes(blob)
        flac = cloudcopy.decrypt_file(restored / "copy.flac.enc",
                                      restored / "copy.flac", copy_key)
        matching = [w for w in archive.rglob("*.wav")
                    if cloudcopy.audio_fingerprint(w) == cloudcopy.audio_fingerprint(flac)]
        checks.check(bool(matching),
                     "a copy, brought back from the store, is the recording exactly",
                     "AT-74, SRS-ARC-11")

    print("\nWhat the two databases hold (SRS-DBA-20)")
    async with asyncpg_connection(recordings_uri) as conn:
        leaked = await conn.fetchval("""
            SELECT count(*) FROM sessions
             WHERE patient_id ILIKE '%Simulated Patient%'
                OR doctor_id ILIKE '%Simulated Patient%'
        """)
        columns = {r["column_name"] for r in await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'confirmation_notices'")}
    checks.check(leaked == 0 and "body" not in columns,
                 "no patient name, and no clinical body, in the recordings database",
                 "SRS-DBA-20")

    async with asyncpg_connection(clinical_uri) as conn:
        patients = await conn.fetchval("SELECT count(*) FROM patients")
        medicines = await conn.fetchval("SELECT count(*) FROM prescription_items")
        linked = await conn.fetchval(
            "SELECT count(*) FROM encounters WHERE file_stem IS NOT NULL")
    checks.check(patients >= 1 and medicines >= len(kept),
                 f"the clinical database holds {patients} patients and "
                 f"{medicines} prescribed medicines", "SRS-DBA-10")
    checks.check(linked >= len(archived),
                 f"{linked} visits are linked to their recording by file name",
                 "SRS-DBA-21")

    print("\nThe audit trail")
    checks.check(audit.get("session.archived", 0) >= len(archived),
                 "every archiving is in the audit log", "SRS-AUD-01")
    checks.check(audit.get("session.copied", 0) >= len(copied),
                 "every cloud copy is in the audit log", "SRS-ARC-09")
    if refused:
        checks.check(audit.get("session.refused", 0) >= len(refused),
                     "every refusal is in the audit log", "SRS-CNS-05")

    print("\nThe dashboard (§8.9)")
    status, summary = await get_json(f"{server.url}/api/v2/dashboard/summary",
                                     {"X-Admin-Key": server.admin_key})
    checks.check(status == 200, "the dashboard answers", "SRS-DSH-01")
    if status == 200:
        volume = summary["volume"]
        checks.check(volume["total_recordings"] >= len(kept),
                     f"it counts {volume['total_recordings']} recordings, "
                     f"{volume['total_hours']} hours", "SRS-DSH-01")
        checks.check(summary["copies"]["recordings_without_a_cloud_copy"] == 0,
                     "it agrees that everything has a cloud copy", "SRS-DSH-09")
        checks.check(summary["recorders"]["enrolled"] >= 1,
                     f"it sees {summary['recorders']['enrolled']} recorder(s)",
                     "SRS-DSH-04")
        checks.check("Simulated Patient" not in json.dumps(summary),
                     "and it shows no patient name", "SRS-DSH-06")

    status, unauthorised = await get_json(f"{server.url}/api/v2/dashboard/summary")
    checks.check(status in (401, 403),
                 "the dashboard refuses a request with no administrator key",
                 "SRS-IF2-02")


@contextlib.asynccontextmanager
async def asyncpg_connection(uri: str):
    import asyncpg
    conn = await asyncpg.connect(uri)
    try:
        yield conn
    finally:
        await conn.close()


async def run_load(server: Server, args) -> Dict[str, Any]:
    """
    A clinic day of protocol traffic, from the load tool that ships for `AT-29`.

    It builds real signed chains and uploads real objects, but runs no
    recorders: nothing here competes with the server for this machine, which
    is what makes the reply times worth reading.
    """
    from types import SimpleNamespace

    settings = SimpleNamespace(
        server=server.url, admin_key=server.admin_key, hospital=CLINIC,
        cmed_hospital=CMED_CLINIC, rooms=args.load_rooms, hours=args.load_hours,
        minutes=15.0, gap=5.0, speed=args.load_speed, dry_run=False, json=False)
    return await load_test.run(settings)


# ============================================================
# The run
# ============================================================

async def simulate(args) -> int:
    checks = Checks()
    timings = Timings()
    workdir = Path(tempfile.mkdtemp(prefix="aimscribe_sim_"))
    copy_key = os.urandom(32)
    postgres = Postgres(args.pg_python)
    s3 = server = None
    rooms: List[Room] = []

    print(f"AIMScribe end-to-end simulation")
    print(f"  {args.rooms} room(s), {args.consultations} consultation(s) each, "
          f"{args.speed}x speed")
    print(f"  working in {workdir}\n")

    try:
        print("Starting the parts")
        base_uri = postgres.start()
        recordings, clinical_uri = await build_databases(base_uri)
        print("  [ok] PostgreSQL 16, both databases, built from the migration scripts")

        s3_port = free_port()
        s3 = FakeS3(s3_port)
        threading.Thread(target=s3.serve_forever, daemon=True).start()
        print(f"  [ok] object storage stand-in on {s3_port}")

        server = Server(port=free_port(), s3_port=s3_port, recordings=recordings,
                        clinical=clinical_uri, workdir=workdir)
        await server.start()
        await server.prepare()
        print(f"  [ok] the AIMS LAB server on {server.url}")

        for number in range(1, args.rooms + 1):
            room = Room(number, server, workdir, args.speed)
            await room.install(await server.enrolment_token())
            rooms.append(room)
        print(f"  [ok] {len(rooms)} recorder(s) installed and enrolled")

        archiver = Archiver(server, workdir / "archive", copy_key)
        print("  [ok] the archive worker\n")

        # ---- the clinic day ----
        print("Running consultations")
        started_at = time.perf_counter()

        async def run_room(room: Room) -> List[Consultation]:
            cmed = await Cmed(room, server, timings).connect()
            made = []
            try:
                for number in range(1, args.consultations + 1):
                    # One patient in the run says no, on the first room.
                    refuse = (room.number == 1 and number == args.consultations
                              and args.consultations > 1)
                    made.append(await one_consultation(
                        cmed, number, minutes=args.minutes, refuse=refuse))
            finally:
                await cmed.close()
            return made

        results = await asyncio.gather(*(run_room(room) for room in rooms))
        visits = [visit for room_visits in results for visit in room_visits]
        elapsed = time.perf_counter() - started_at
        print(f"  {len(visits)} consultations in {elapsed:.1f}s of real time "
              f"({len(visits) * args.minutes:.0f} simulated minutes)\n")

        # Everything the recorders still hold has to reach the server before
        # the archive worker can do its part. A recorder delivers when it can
        # and retries until acknowledged, so this waits for the work to settle
        # rather than assuming a fixed pause is enough.
        print("Draining the recorders, then archiving")
        settled = await wait_until_delivered(recordings, visits, seconds=120)
        for room in rooms:
            await room.stop()
        await asyncio.sleep(1)
        print(f"  the recorders delivered everything in {settled:.0f}s")
        moved = await archiver.run_until_quiet()
        print(f"  the worker archived and copied in {moved} step(s)\n")

        print("Checking what happened")
        await verify(checks, server=server, visits=visits, archive=archiver.archive,
                     s3=s3, copy_key=copy_key, clinical_uri=clinical_uri,
                     recordings_uri=recordings)

        # ---- timings ----
        print("\nHow long each call took (seconds)")
        print(f"  {'call':<34}{'count':>7}{'median':>10}{'p95':>10}{'max':>10}")
        for call, count, median, p95, worst in timings.report():
            print(f"  {call:<34}{count:>7}{median:>10.3f}{p95:>10.3f}{worst:>10.3f}")

        grant = timings.by_call.get("start", [])
        if grant:
            ordered = sorted(grant)
            p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
            if args.rooms <= 3:
                checks.check(p95 < 2.0,
                             f"a patient is opened and recording starts in {p95:.2f}s "
                             f"at the 95th percentile", "§9.1")
            else:
                # Every recorder, the server, the storage stand-in and the
                # worker are in one interpreter on one machine here, so above a
                # few rooms these figures measure this laptop, not the server.
                # The load phase below is the measurement that means something.
                print(f"  ({args.rooms} rooms in one process: {p95:.2f}s at the 95th "
                      f"percentile measures the harness, so it is not asserted)")

        # ---- the load phase: many rooms, no recorders in this process ----
        if args.load_rooms:
            print(f"\nLoad: {args.load_rooms} rooms driving the server directly")
            result = await run_load(server, args)
            print(load_test.as_text(result))
            checks.check(result["pieces_accepted"] > 0 and result["pieces_lost"] == 0
                         and not result["failures"],
                         f"under load, {result['pieces_accepted']} pieces accepted "
                         f"and none lost", "AT-29")
            for call, figures in result["calls"].items():
                if figures["within_target"] is not None:
                    checks.check(
                        figures["within_target"],
                        f"under load, {call} answers in {figures['p95']:.2f}s at the "
                        f"95th percentile (target {figures['target']}s)", "§9.1")

    finally:
        with contextlib.suppress(Exception):
            for room in rooms:
                await room.stop()
        if server:
            with contextlib.suppress(Exception):
                await server.stop()
        if s3:
            with contextlib.suppress(Exception):
                s3.shutdown()
        postgres.stop()
        if args.keep:
            print(f"\nLeft for inspection: {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    print("\n" + "=" * 64)
    passed = len(checks.results) - len(checks.failed)
    print(f"{passed}/{len(checks.results)} checks passed")
    for _, what, requirement in checks.failed:
        print(f"  FAILED: {what}   ({requirement})")
    print("SIMULATION PASSED" if not checks.failed else "SIMULATION FAILED")
    return 0 if not checks.failed else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rooms", type=int, default=2)
    parser.add_argument("--consultations", type=int, default=3,
                        help="consultations per room")
    parser.add_argument("--minutes", type=float, default=3.0,
                        help="simulated length of each consultation")
    parser.add_argument("--speed", type=float, default=60.0,
                        help="how much faster than real time the audio runs")
    parser.add_argument("--keep", action="store_true",
                        help="keep the databases, archive and spools afterwards")
    parser.add_argument("--load-rooms", type=int, default=0,
                        help="after the checks, drive the server with this many "
                             "rooms of protocol traffic and report its reply times "
                             "(AT-29). No recorders run in this process, so the "
                             "figures are the server's, not the harness's.")
    parser.add_argument("--load-hours", type=float, default=8.0,
                        help="how long a clinic day the load phase simulates")
    parser.add_argument("--load-speed", type=float, default=240.0,
                        help="how much faster than real time the load phase runs")
    parser.add_argument("--pg-python",
                        default=os.environ.get(
                            "AIMS_PG_PYTHON",
                            r"C:\Users\USER\AppData\Local\aimspg\Scripts\python"),
                        help="an interpreter with the pgserver package installed")
    args = parser.parse_args(argv)

    if not Path(args.pg_python).exists() and shutil.which(args.pg_python) is None:
        print(f"No interpreter with pgserver at {args.pg_python}.\n"
              f"Install one:  python -m venv C:\\aimspg && "
              f"C:\\aimspg\\Scripts\\pip install pgserver asyncpg\n"
              f"then pass --pg-python, or set AIMS_PG_PYTHON.")
        return 2

    return asyncio.run(simulate(args))


if __name__ == "__main__":
    raise SystemExit(main())
