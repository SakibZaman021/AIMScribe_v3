"""
The live bench: the whole system running on this PC, with a real microphone.

    python tools/bench.py

It starts and keeps running:

    PostgreSQL 16          both databases, built from the migration scripts
    the AIMS LAB server    the shipping API, Channel B, and the dashboard
    object storage         a local stand-in for R2, on disk
    the archive worker     merging, the JSON, the cloud copy, in a loop

Then it tells you how to start the real recorder and CMED's page, and prints
what happens as it happens: the patient opened, each piece sealed and
committed, the receipt, CMED's message arriving, the file landing in the
archive.

Nothing here touches a real deployment. The databases, the storage and the
recorder's state all live under one bench folder, and the recorder is started
with its own state directory so an agent already enrolled on this machine is
left alone. Stop it with Ctrl+C; run it again and everything is still there.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "backend" / "archive_worker"))

from simulate import Postgres, build_databases, post_json, get_json   # noqa: E402

CLINIC, CMED_CLINIC = "HOSP003", "CMED-BENCH-01"
CMED_ORIGIN = "http://localhost:3000"
SERVER_PORT = 6000
RECORDER_PORT = 5050


# ============================================================
# Where the bench keeps everything
# ============================================================

class Bench:
    def __init__(self, home: Path):
        self.home = home
        self.db = home / "db"
        self.storage = home / "storage"
        self.archive = home / "archive"
        self.recorder_state = home / "recorder"
        self.secrets = home / "bench.json"
        for folder in (self.db, self.storage, self.archive, self.recorder_state):
            folder.mkdir(parents=True, exist_ok=True)

    def remember(self, values: Dict[str, str]) -> Dict[str, str]:
        """Keys and passwords, kept so a restart is the same bench."""
        known = self.recall()
        known.update({k: v for k, v in values.items() if v})
        self.secrets.write_text(json.dumps(known, indent=2), encoding="utf-8")
        return known

    def recall(self) -> Dict[str, str]:
        try:
            return json.loads(self.secrets.read_text(encoding="utf-8"))
        except Exception:
            return {}


# ============================================================
# Object storage, on disk
# ============================================================

class LocalStore(ThreadingHTTPServer):
    """
    A bucket, as a folder. It answers PUT, GET, HEAD and DELETE the way the
    server and the recorder expect, and keeps the objects where you can look
    at them. It is not Cloudflare: there is no signature checking here.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, root: Path):
        super().__init__(("127.0.0.1", port), _StoreHandler)
        self.root = root


class _StoreHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def _path(self) -> Optional[Path]:
        key = unquote(urlparse(self.path).path.lstrip("/").split("?")[0])
        target = (self.server.root / key).resolve()
        if self.server.root.resolve() not in target.parents:
            return None                      # never write outside the bucket
        return target

    def _is_bucket(self) -> bool:
        """A request for the bucket itself, not an object in it."""
        key = unquote(urlparse(self.path).path.strip("/")).split("?")[0]
        return "/" not in key

    def do_PUT(self):
        target = self._path()
        if target is None:
            self.send_error(400, "bad key")
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self._is_bucket():
            # Creating the bucket, not writing an object. Writing a file here
            # is what broke the first bench run: every later object under that
            # name then had a file where it needed a folder.
            target.mkdir(parents=True, exist_ok=True)
            self._ok(0, b"")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        self._ok(len(body), body)

    def do_GET(self):
        target = self._path()
        if self._is_bucket():
            self._ok(0, b"") if target and target.is_dir() else self.send_error(404)
            return
        if target is None or not target.is_file():
            self.send_error(404, "NoSuchKey")
            return
        body = target.read_bytes()
        self._ok(len(body), body)
        self.wfile.write(body)

    def do_HEAD(self):
        target = self._path()
        if self._is_bucket():
            self._ok(0, b"") if target and target.is_dir() else self.send_error(404)
            return
        if target is None or not target.is_file():
            self.send_error(404, "NoSuchKey")
            return
        self._ok(target.stat().st_size, target.read_bytes())

    def do_DELETE(self):
        target = self._path()
        if target is not None and target.is_file():
            target.unlink()
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _ok(self, length: int, body: bytes) -> None:
        import hashlib
        self.send_response(200)
        self.send_header("Content-Length", str(length) if self.command != "GET" else str(length))
        self.send_header("ETag", f'"{hashlib.md5(body).hexdigest()}"')
        self.end_headers()


# ============================================================
# The server
# ============================================================

class BenchServer:
    def __init__(self, bench: Bench, *, port: int, store_port: int,
                 recordings: str, clinical: str):
        self.bench = bench
        self.port = port
        self.store_port = store_port
        self.recordings = recordings
        self.clinical = clinical
        self.url = f"http://127.0.0.1:{port}"
        known = bench.recall()
        self.admin_key = known.get("admin_key") or f"bench-admin-{os.urandom(6).hex()}"
        self.worker_key = known.get("worker_key") or f"bench-worker-{os.urandom(6).hex()}"
        self.cmed_key = known.get("cmed_key", "")
        self.copy_key = known.get("copy_key") or base64.b64encode(os.urandom(32)).decode()
        self.ready = threading.Event()
        self._server = None
        self._thread: Optional[threading.Thread] = None

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

        # The two signing keys, kept across restarts: the recorder pins their
        # public halves, so a new key every run would mean re-enrolling.
        known = self.bench.recall()
        grant_key = _load_or_make_key(known.get("grant_private"))
        receipt_key = _load_or_make_key(known.get("receipt_private"))
        self.bench.remember({
            "admin_key": self.admin_key, "worker_key": self.worker_key,
            "copy_key": self.copy_key,
            "grant_private": _pem(grant_key), "receipt_private": _pem(receipt_key),
        })

        keys = ROOT / "recorder" / "keys"
        keys.mkdir(parents=True, exist_ok=True)
        (keys / "aimslab_grant_pub.pem").write_bytes(_public_pem(grant_key))
        (keys / "aimslab_receipt_pub.pem").write_bytes(_public_pem(receipt_key))

        @asynccontextmanager
        async def lifespan(app):
            import asyncpg
            from clinical_store import ClinicalStore
            from db_v2 import V2Repository
            from grants import GrantIssuer
            from integrity import ReceiptSigner
            from storage.minio_client import MinIOClient

            pool = await asyncpg.create_pool(self.recordings, min_size=2, max_size=10)
            clinical_pool = await asyncpg.create_pool(self.clinical, min_size=1, max_size=5)
            endpoint = f"127.0.0.1:{self.store_port}"
            buckets = {name: MinIOClient(endpoint=endpoint, access_key="bench",
                                         secret_key="benchbench", bucket=name,
                                         secure=False, region="us-east-1")
                       for name in ("segments", "copies", "clinical-json")}

            api_v2.ctx.repo = V2Repository(pool)
            api_v2.ctx.minio = buckets["segments"]
            api_v2.ctx.clinical = ClinicalStore(clinical_pool, separate=True)
            api_v2.ctx.copy = buckets["copies"]
            api_v2.ctx.copy_json = buckets["clinical-json"]
            api_v2.ctx.signer = ReceiptSigner(receipt_key)
            api_v2.ctx.grants = GrantIssuer(grant_key)
            self.ready.set()
            try:
                yield
            finally:
                await pool.close()
                await clinical_pool.close()

        app = FastAPI(title="AIMScribe (bench)", lifespan=lifespan)
        app.include_router(api_v2.router)
        app.include_router(channel_b.router)
        app.include_router(dashboard.router)

        @app.get("/health")
        async def health():
            return {"status": "ok", "bench": True}

        self._server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning",
            access_log=False))
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        for _ in range(200):
            if self.ready.is_set() and getattr(self._server, "started", False):
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("the server did not start")

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=10)

    @property
    def admin(self) -> Dict[str, str]:
        return {"X-Admin-Key": self.admin_key}

    async def prepare(self) -> None:
        """The clinic, and CMED's key - both only once."""
        status, reply = await post_json(f"{self.url}/api/v2/admin/hospital", {
            "hospital_id": CLINIC, "name": "Bench clinic", "timezone": "Asia/Dhaka",
            "cmed_hospital_id": CMED_CLINIC}, headers=self.admin)
        if status >= 300:
            raise RuntimeError(f"could not register the clinic ({status}): {reply}")

        # A key kept from last time is only any use if this database still
        # knows it - and it will not, if the databases were reset while the
        # bench's own notes survived. Ask, rather than assume: an empty body
        # with a good key is refused for a missing field, with a bad one for
        # the key itself.
        if self.cmed_key:
            status, reply = await post_json(
                f"{self.url}/api/v2/clinical/patient-information", {},
                headers={"X-CMED-Key": self.cmed_key})
            if reply.get("code") == "INVALID_KEY":
                say("the CMED key from last time is not in this database; "
                    "issuing a new one")
                self.cmed_key = ""

        if not self.cmed_key:
            status, reply = await post_json(f"{self.url}/api/v2/admin/cmed-key", {
                "label": "cmed-bench", "created_by": "bench"}, headers=self.admin)
            if status >= 300:
                raise RuntimeError(f"no CMED key ({status})")
            self.cmed_key = reply["cmed_key"]
            self.bench.remember({"cmed_key": self.cmed_key})

    async def enrolment_token(self) -> str:
        status, reply = await post_json(f"{self.url}/api/v2/admin/enrollment-token", {
            "hospital_id": CLINIC, "created_by": "bench"}, headers=self.admin)
        return reply.get("enrollment_token") or reply.get("token") or ""


def _load_or_make_key(pem: Optional[str]):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if pem:
        with contextlib.suppress(Exception):
            return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    return Ed25519PrivateKey.generate()


def _pem(key) -> str:
    from cryptography.hazmat.primitives import serialization
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode("utf-8")


def _public_pem(key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


# ============================================================
# What the recorder and CMED need in order to join in
# ============================================================

def write_recorder_env(bench: Bench, server: BenchServer, token: str) -> Path:
    """
    The `.env` the installer would write on a doctor's PC, and a batch file
    that starts the agent with the bench's own state folder - so whatever is
    already enrolled on this machine is left untouched.
    """
    state = bench.recorder_state / "state"
    keys = bench.recorder_state / "keys"
    for folder in (state, keys, bench.recorder_state / "logs"):
        folder.mkdir(parents=True, exist_ok=True)
    for name in ("aimslab_grant_pub.pem", "aimslab_receipt_pub.pem"):
        shutil.copyfile(ROOT / "recorder" / "keys" / name, keys / name)

    identity = state / "device.json"
    if token and not identity.exists():
        (state / "enrollment.token").write_text(token, encoding="utf-8")

    env = ROOT / "recorder" / ".env"
    env.write_text("\n".join([
        "# Written by tools/bench.py. This is a bench, not a clinic.",
        "#",
        "# The agent keeps its keys, enrolment and spool here rather than in",
        "# ProgramData, so an agent already enrolled on this machine is left",
        "# exactly as it is.",
        f"AIMS_DATA_DIR={bench.recorder_state}",
        f"AIMS_BACKEND_URL={server.url}",
        f"AIMS_BIND_PORT={RECORDER_PORT}",
        f"AIMS_ALLOWED_ORIGINS={CMED_ORIGIN},http://127.0.0.1:3000",
        f"AIMS_ALLOWED_HOSTS=localhost:{RECORDER_PORT},127.0.0.1:{RECORDER_PORT}",
        "AIMS_ALLOW_PLAINTEXT_KEYSTORE=true",
        "AIMS_LOCAL_API_KEY=bench-local-key",
        "# Shorter pieces than a clinic would use, so you see one sealed in half",
        "# a minute rather than in one (SRS-SPL-02 allows 30-60 s).",
        "AIMS_SEGMENT_MIN_SECONDS=20",
        "AIMS_SEGMENT_MAX_SECONDS=30",
        "AIMS_SEGMENT_GRACE_SECONDS=5",
        "",
    ]), encoding="utf-8")

    starter = ROOT / "start-bench-recorder.bat"
    starter.write_text("\r\n".join([
        "@echo off",
        "rem The AIMScribe agent, against the local bench. Everything it needs",
        "rem is in recorder\\.env, including where it keeps its own state - so",
        "rem an agent already enrolled on this machine is left alone.",
        f'cd /d "{ROOT / "recorder"}"',
        "python main.py",
        "pause",
        "",
    ]), encoding="utf-8")
    return starter


def write_cmed_env(server: BenchServer) -> Path:
    env = ROOT / "cmed-web" / ".env.local"
    env.write_text("\n".join([
        "# Written by tools/bench.py.",
        f"AIMS_CMED_KEY={server.cmed_key}",
        f"AIMS_SERVER_URL={server.url}",
        f"NEXT_PUBLIC_RECORDER_WS=ws://localhost:{RECORDER_PORT}/ws",
        f"NEXT_PUBLIC_BACKEND_URL={server.url}",
        f"NEXT_PUBLIC_HOSPITAL_ID={CLINIC}",
        "",
    ]), encoding="utf-8")
    return env


# ============================================================
# Watching it work
# ============================================================

class Watcher:
    """
    Prints what the system does, as it does it, by reading the audit log and
    the sessions table - the same places the dashboard reads.
    """

    def __init__(self, recordings_uri: str, archive: Path):
        self.uri = recordings_uri
        self.archive = archive
        self.seen_audit = 0
        self.seen_segments: Dict[str, int] = {}
        self.seen_files: set = set()
        self.states: Dict[str, str] = {}

    async def poll(self) -> None:
        import asyncpg

        conn = await asyncpg.connect(self.uri)
        try:
            for row in await conn.fetch(
                    "SELECT id, event_type, session_id, detail FROM audit_log "
                    " WHERE id > $1 ORDER BY id", self.seen_audit):
                self.seen_audit = row["id"]
                self._say_audit(row)

            for row in await conn.fetch("""
                SELECT session_id, patient_id, doctor_id, confirmation, status,
                       segment_count, file_stem
                  FROM sessions ORDER BY opened_at DESC LIMIT 20
            """):
                sid = row["session_id"]
                count = row["segment_count"] or 0
                if count > self.seen_segments.get(sid, 0):
                    self.seen_segments[sid] = count
                    say(f"piece {count} of {short(sid)} committed, verified and "
                        f"receipted - the PC may delete its copy")
                state = f'{row["confirmation"]}/{row["status"]}'
                if self.states.get(sid) != state:
                    self.states[sid] = state
                    say(f"{short(sid)} for patient {row['patient_id']}: "
                        f"{row['confirmation']} ({row['status']})")
        finally:
            await conn.close()

        for wav in sorted(self.archive.rglob("*.wav")):
            if wav in self.seen_files:
                continue
            self.seen_files.add(wav)
            size = wav.stat().st_size / 1024 ** 2
            say(f"ARCHIVED  {wav.name}  ({size:.1f} MB)")
            say(f"          {wav.parent}")
            if wav.with_suffix(".json").is_file():
                say(f"          and its clinical JSON beside it")

    def _say_audit(self, row) -> None:
        detail = row["detail"]
        if isinstance(detail, str):
            with contextlib.suppress(Exception):
                detail = json.loads(detail)
        detail = detail if isinstance(detail, dict) else {}
        sid = short(row["session_id"] or "")
        words = {
            "session.opened": "recording started",
            "session.confirmed": "confirmed by CMED's API 2",
            "session.closed": "consultation closed",
            "session.archived": f"archived at UIU "
                                f"({detail.get('receipts_issued', 0)} receipts)",
            "session.copied": "lossless cloud copy verified; pieces deleted",
            "session.refused": "PATIENT DID NOT CONSENT - erased everywhere",
            "session.quarantined": f"QUARANTINED: {detail.get('reason', '')}",
            "session.expired_unconfirmed": "erased: 24 hours with no API 2",
            "session.json_rewritten": "clinical JSON written again",
        }
        if row["event_type"] in words:
            say(f"{sid}: {words[row['event_type']]}")


def short(session_id: str) -> str:
    return session_id[-6:] if session_id else "?"


def say(line: str) -> None:
    print(f"  {datetime.now().strftime('%H:%M:%S')}  {line}", flush=True)


# ============================================================
# The archive worker, in the background
# ============================================================

def start_worker(server: BenchServer, archive: Path) -> threading.Thread:
    for name, value in {
        "AIMS_BACKEND_URL": server.url,
        "AIMS_WORKER_KEY": server.worker_key,
        "AIMS_ARCHIVE_ROOT": str(archive),
        "AIMS_COPY_KEY": server.copy_key,
        "AIMS_POLL_SECONDS": "10",
        "AIMS_DISK_HEADROOM_BYTES": str(200 * 1024 ** 2),
    }.items():
        os.environ[name] = value

    import worker as worker_module

    worker = worker_module.ArchiveWorker(worker_module.Settings())

    def run():
        while True:
            try:
                worker.drain_once()
            except Exception as exc:
                say(f"archive worker: {exc}")
            time.sleep(10)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


# ============================================================
# Running the bench
# ============================================================

def free_port(preferred: int) -> int:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]


async def run(args) -> int:
    # This window is meant to be watched, so nothing waits in a buffer.
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(line_buffering=True)

    bench = Bench(Path(args.home).expanduser())
    postgres = Postgres(args.pg_python, data_dir=str(bench.db))
    store = None
    server = None

    print("AIMScribe bench")
    print(f"  everything lives in {bench.home}\n")

    try:
        print("Starting")
        base = postgres.start()
        recordings, clinical = await build_databases(base, fresh=args.reset)
        print("  [ok] PostgreSQL 16, both databases")

        if args.reset:
            # Emptying the databases throws away the devices table, so the
            # agent's identity means nothing any more. Clearing it here is
            # what lets it enrol again on its next start; without this it
            # says DEVICE_NOT_ENROLLED for ever and the cause is not obvious.
            shutil.rmtree(bench.recorder_state, ignore_errors=True)
            bench.recorder_state.mkdir(parents=True, exist_ok=True)
            print("  [ok] the bench recorder will enrol again")

        store_port = free_port(9000)
        store = LocalStore(store_port, bench.storage)
        threading.Thread(target=store.serve_forever, daemon=True).start()
        print(f"  [ok] object storage on {store_port}, files under "
              f"{bench.storage.name}\\")

        server = BenchServer(bench, port=free_port(SERVER_PORT),
                             store_port=store_port, recordings=recordings,
                             clinical=clinical)
        await server.start()
        await server.prepare()
        print(f"  [ok] the AIMS LAB server on {server.url}")

        start_worker(server, bench.archive)
        print("  [ok] the archive worker, every 10 seconds")

        token = await server.enrolment_token()
        starter = write_recorder_env(bench, server, token)
        cmed_env = write_cmed_env(server)
        print("  [ok] the recorder and CMED page are configured\n")

        print("=" * 68)
        print("NOW DO THIS")
        print("=" * 68)
        print(f"""
1. Start the recorder - a second terminal, or double-click the file:

       {starter}

   It enrols itself, shows a tray icon, and captures from your microphone.

2. Start CMED's page - a third terminal:

       cd {ROOT / "cmed-web"}
       npm run dev

   Then open  {CMED_ORIGIN}

3. On that page, enter a patient and press "Open patient".
   Speak. Watch this window: each piece is sealed, uploaded, verified and
   receipted, and your PC deletes its copy as soon as the server has it.

4. Press "Prescription built", then Stop on the AIMScribe window.
   Within about 15 seconds the recording is merged into:

       {bench.archive}

   Open the WAV and you will hear yourself; the JSON beside it is what
   CMED sent about the visit.

5. The dashboard, with the administrator key below:

       {server.url}/api/v2/dashboard

   Administrator key : {server.admin_key}
   CMED key          : {server.cmed_key}

   To try the refusal path, press Stop and choose
   "Patient did not consent" - the recording is deleted everywhere.
""")
        print("=" * 68)
        print("Watching. Ctrl+C to stop.\n")

        watcher = Watcher(recordings, bench.archive)
        complained = ""
        while True:
            try:
                await watcher.poll()
                complained = ""
            except Exception as exc:
                # Say it once rather than every two seconds - but say it. A
                # window that shows nothing because it is broken looks exactly
                # like a window that shows nothing because nothing happened.
                if str(exc) != complained:
                    complained = str(exc)
                    say(f"(cannot read the database just now: {exc})")
            await asyncio.sleep(2)

    except KeyboardInterrupt:
        print("\nStopping.")
        return 0
    finally:
        if server:
            server.stop()
        if store:
            with contextlib.suppress(Exception):
                store.shutdown()
        postgres.stop()
        print(f"Everything is still in {bench.home} - run this again to carry on.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", default=str(Path(
        os.environ.get("LOCALAPPDATA", Path.home())) / "aimscribe-bench"),
        help="where the bench keeps its databases, storage and archive")
    parser.add_argument("--reset", action="store_true",
                        help="start from empty databases")
    parser.add_argument("--pg-python",
                        default=os.environ.get(
                            "AIMS_PG_PYTHON",
                            r"C:\Users\USER\AppData\Local\aimspg\Scripts\python"),
                        help="an interpreter with the pgserver package installed")
    args = parser.parse_args(argv)

    if not Path(args.pg_python).exists() and shutil.which(args.pg_python) is None:
        print(f"No interpreter with pgserver at {args.pg_python}.")
        return 2
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
