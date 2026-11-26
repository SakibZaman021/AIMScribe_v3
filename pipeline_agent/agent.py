"""
AIMScribe pipeline agent (053, 27 Nov 2026).

Runs on the lab's GPU machine (role "lab": Bengali transcript, then NER) or
on the quality service (role "metrics": compare NER with CMED's prescription).
It holds the machine's two private keys and does everything security needs,
so the model side is one plain command that reads files and writes files:

    lab       {command} --audio <in.wav> --out <dir>
              writes <dir>/transcript.json, <dir>/ner.json and <dir>/model.json
    metrics   {command} --inputs <in.json> --out <dir>
              writes <dir>/metrics.json and <dir>/model.json

    model.json  {"model": "...", "model_version": "...", "weights_sha256": "<64 hex>"}

Or, for a model that already runs as an HTTP service on this machine
(--model-url instead of --command; loopback only, so audio never leaves it):

    lab       POST <url>/v1/transcribe   multipart field "audio" (the WAV)  -> transcript JSON
              POST <url>/v1/ner          {"language", "text", "segments"}   -> NER JSON
    metrics   POST <url>/v1/compare      the inputs JSON                    -> metrics JSON
    both      GET  <url>/v1/model                                           -> model.json
    (the paths can be changed with --transcribe-path, --ner-path, --compare-path,
    --model-path; a bearer token for the service, if it wants one, is read from
    the environment variable named by --model-token-env)

What it does for every job:
  * claims jobs with requests signed by this machine's Ed25519 key;
  * lab: downloads the audio sealed to this machine's X25519 key, opens it,
    checks its SHA-256 against the job's ticket, and runs the command;
  * keeps the job's lease while the command runs;
  * posts each result with the ticket and the audio hash it computed;
  * deletes the audio and every working file when the job ends, whatever
    happens; reports a failure so the server retries it later.

    python agent.py keygen --keys /etc/aimscribe-agent
    python agent.py run --server https://aimscribe.iriic.uiu.ac.bd \\
        --machine <id from enrolment> --keys /etc/aimscribe-agent --role lab \\
        --command "python /opt/rahat/run.py" --parallel 2

Needs Python 3.9+, `cryptography` and `requests`.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit
import ipaddress

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_seal  # noqa: E402

SIGNATURE_DOMAIN = b"AIMS-PIPELINE-SIG-V1\n"
PREFIX = "/api/v2/pipeline"
LEASE_EVERY = 600                     # seconds; the server's lease is 30 minutes
COMMAND_TIMEOUT = 3 * 3600
log = logging.getLogger("aimscribe-agent")


# ---------------------------------------------------------------- keys

def keygen(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    sign, box = folder / "sign.key", folder / "box.key"
    if sign.exists() or box.exists():
        sys.exit(f"Keys already exist in {folder}; refusing to overwrite them.")
    signing, receiving = Ed25519PrivateKey.generate(), X25519PrivateKey.generate()
    for path, key in ((sign, signing), (box, receiving)):
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
        os.chmod(path, 0o600)
    raw = (serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    print("Give these two public keys to the AIMScribe administrator for enrolment.")
    print("The private keys stay in", folder, "and never leave this machine.")
    print("sign_pubkey:", signing.public_key().public_bytes(*raw).hex())
    print("box_pubkey: ", receiving.public_key().public_bytes(*raw).hex())


def _load(path: Path):
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


# ---------------------------------------------------------------- the server

class Server:
    def __init__(self, base: str, machine: str, signing: Ed25519PrivateKey,
                 verify_tls: bool = True) -> None:
        self.base = base.rstrip("/")
        self.machine = machine
        self.signing = signing
        self.http = requests.Session()
        self.verify = verify_tls

    def _headers(self, method: str, url: str, body: bytes) -> Dict[str, str]:
        parts = urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        stamp, nonce = str(int(time.time())), secrets.token_urlsafe(24)
        message = b"".join([SIGNATURE_DOMAIN, method.upper().encode(), b"\n", path.encode(),
                            b"\n", stamp.encode(), b"\n", nonce.encode(), b"\n",
                            hashlib.sha256(body).hexdigest().encode()])
        signature = base64.urlsafe_b64encode(self.signing.sign(message)).rstrip(b"=").decode()
        return {"X-Pipeline-Machine": self.machine, "X-Pipeline-Timestamp": stamp,
                "X-Pipeline-Nonce": nonce, "X-Pipeline-Signature": signature}

    def call(self, method: str, path: str, payload: Any = None, *, stream: bool = False,
             headers: Optional[Dict[str, str]] = None, timeout: float = 60):
        url = f"{self.base}{PREFIX}{path}"
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False,
                                                       separators=(",", ":")).encode("utf-8")
        all_headers = {**self._headers(method, url, body), **(headers or {})}
        if payload is not None:
            all_headers["Content-Type"] = "application/json"
        response = self.http.request(method, url, data=body or None, headers=all_headers,
                                     stream=stream, timeout=timeout, verify=self.verify)
        if response.status_code >= 400:
            detail = response.text[:300]
            response.close()
            raise RuntimeError(f"{method} {path}: {response.status_code} {detail}")
        return response


def _split(command: str):
    """The command line as arguments; on Windows, quoted paths lose their quotes."""
    if os.name != "nt":
        return shlex.split(command)
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] == '"' else t
            for t in shlex.split(command, posix=False)]


# ---------------------------------------------------------------- one job

class ModelService:
    """A model already running as an HTTP service on this machine (loopback only)."""

    def __init__(self, url: str, *, transcribe_path: str = "/v1/transcribe",
                 ner_path: str = "/v1/ner", compare_path: str = "/v1/compare",
                 model_path: str = "/v1/model", token: Optional[str] = None,
                 allow_remote: bool = False) -> None:
        host = urlsplit(url).hostname or ""
        if not allow_remote and not _loopback(host):
            raise SystemExit(f"--model-url must be on this machine (127.0.0.1, localhost, ::1), "
                             f"not {host!r}: the audio must not leave it")
        self.url = url.rstrip("/")
        self.paths = {"transcribe": transcribe_path, "ner": ner_path,
                      "compare": compare_path, "model": model_path}
        self.http = requests.Session()
        self.http.trust_env = False                      # never through a proxy
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    def _answer(self, response: requests.Response, what: str) -> Dict[str, Any]:
        if response.status_code >= 400:
            raise RuntimeError(f"the model service refused {what}: "
                               f"{response.status_code} {response.text[:300]}")
        try:
            return response.json()
        except ValueError:
            raise RuntimeError(f"the model service's {what} answer is not JSON") from None

    def model(self) -> Dict[str, Any]:
        return self._answer(self.http.get(self.url + self.paths["model"], headers=self.headers,
                                          timeout=30), "model")

    def transcribe(self, wav: Path) -> Dict[str, Any]:
        with open(wav, "rb") as handle:
            return self._answer(self.http.post(
                self.url + self.paths["transcribe"], headers=self.headers,
                files={"audio": ("audio.wav", handle, "audio/wav")},
                data={"language": "bn"}, timeout=COMMAND_TIMEOUT), "transcribe")

    def ner(self, transcript: Dict[str, Any]) -> Dict[str, Any]:
        return self._answer(self.http.post(
            self.url + self.paths["ner"], headers=self.headers, timeout=COMMAND_TIMEOUT,
            json={"language": transcript.get("language", "bn"), "text": transcript.get("text", ""),
                  "segments": transcript.get("segments", [])}), "ner")

    def compare(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        return self._answer(self.http.post(
            self.url + self.paths["compare"], headers=self.headers, json=inputs,
            timeout=COMMAND_TIMEOUT), "compare")


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Job:
    def __init__(self, server: Server, job: Dict[str, Any], role: str, command,
                 work: Path, box: Optional[X25519PrivateKey]) -> None:
        # `command`: a command line (str), or a ModelService for --model-url.
        self.server, self.job, self.role, self.command, self.box = server, job, role, command, box
        self.id, self.ticket = job["job_id"], job["ticket"]
        self.dir = Path(tempfile.mkdtemp(prefix=f"{self.id}_", dir=work))
        self.done = threading.Event()

    def _keep_lease(self) -> None:
        while not self.done.wait(LEASE_EVERY):
            try:
                self.server.call("POST", f"/jobs/{self.id}/lease", {"ticket": self.ticket})
            except Exception as exc:
                log.warning("Lease of %s not extended: %s", self.id, exc)

    def run(self) -> None:
        threading.Thread(target=self._keep_lease, daemon=True).start()
        try:
            if self.role == "lab":
                self._lab()
            else:
                self._metrics()
            log.info("Job %s done", self.id)
        except Exception as exc:
            log.error("Job %s failed: %s", self.id, exc)
            try:
                self.server.call("POST", f"/jobs/{self.id}/fail",
                                 {"ticket": self.ticket, "reason": str(exc)[:480]})
            except Exception as report:
                log.error("Could not report the failure of %s: %s", self.id, report)
        finally:
            self.done.set()
            shutil.rmtree(self.dir, ignore_errors=True)     # no audio is ever kept

    def _run_command(self, *args: str) -> Dict[str, Any]:
        out = self.dir / "out"
        out.mkdir()
        argv = _split(self.command) + list(args) + ["--out", str(out)]
        finished = subprocess.run(argv, cwd=self.dir, timeout=COMMAND_TIMEOUT,
                                  capture_output=True, text=True)
        if finished.returncode != 0:
            raise RuntimeError(f"command exited {finished.returncode}: "
                               f"{(finished.stderr or '')[-300:]}")
        model = json.loads((out / "model.json").read_text(encoding="utf-8"))
        return {"out": out, "model": model}

    def _post(self, kind: str, data: Dict[str, Any], model: Dict[str, Any],
              audio_sha256: Optional[str] = None) -> None:
        payload = {"ticket": self.ticket, "model": model["model"],
                   "model_version": model["model_version"], "data": data}
        if model.get("weights_sha256"):
            payload["weights_sha256"] = model["weights_sha256"]
        if audio_sha256:
            payload["audio_sha256"] = audio_sha256
        self.server.call("POST", f"/jobs/{self.id}/results/{kind}", payload)

    def _lab(self) -> None:
        wav = self.dir / "audio.wav"
        response = self.server.call("GET", f"/jobs/{self.id}/audio", stream=True,
                                    headers={"X-Pipeline-Ticket": self.ticket}, timeout=600)
        digest = hashlib.sha256()
        try:
            size, chunks = pipeline_seal.open_sealed(response.raw.read, self.box, self.id)
            with open(wav, "wb") as handle:
                for chunk in chunks:
                    digest.update(chunk)
                    handle.write(chunk)
        finally:
            response.close()
        if digest.hexdigest() != self.job["audio_sha256"]:
            raise RuntimeError("the audio is not the archived recording (hash mismatch)")
        if isinstance(self.command, ModelService):
            service = self.command
            model = service.model()
            transcript = service.transcribe(wav)
            wav.unlink()
            self._post("transcript", transcript, model, audio_sha256=digest.hexdigest())
            self._post("ner", service.ner(transcript), model, audio_sha256=digest.hexdigest())
            return
        ran = self._run_command("--audio", str(wav))
        wav.unlink()                                       # not needed past this point
        for kind in ("transcript", "ner"):
            data = json.loads((ran["out"] / f"{kind}.json").read_text(encoding="utf-8"))
            self._post(kind, data, ran["model"], audio_sha256=digest.hexdigest())

    def _metrics(self) -> None:
        inputs = self.server.call("GET", f"/jobs/{self.id}/inputs",
                                  headers={"X-Pipeline-Ticket": self.ticket}).json()
        if isinstance(self.command, ModelService):
            service = self.command
            self._post("metrics", service.compare(inputs), service.model())
            return
        path = self.dir / "inputs.json"
        path.write_text(json.dumps(inputs, ensure_ascii=False), encoding="utf-8")
        ran = self._run_command("--inputs", str(path))
        data = json.loads((ran["out"] / "metrics.json").read_text(encoding="utf-8"))
        self._post("metrics", data, ran["model"])


# ---------------------------------------------------------------- the loop

def run(args: argparse.Namespace) -> None:
    keys = Path(args.keys)
    server = Server(args.server, args.machine, _load(keys / "sign.key"),
                    verify_tls=not args.insecure)
    box = _load(keys / "box.key") if args.role == "lab" else None
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    target = args.command
    if args.model_url:
        token = os.getenv(args.model_token_env) if args.model_token_env else None
        target = ModelService(args.model_url, transcribe_path=args.transcribe_path,
                              ner_path=args.ner_path, compare_path=args.compare_path,
                              model_path=args.model_path, token=token,
                              allow_remote=args.allow_remote_model)
    busy: Dict[str, threading.Thread] = {}
    pause = 2.0
    log.info("Agent started: role %s, up to %s job(s) at once", args.role, args.parallel)
    while True:
        busy = {k: t for k, t in busy.items() if t.is_alive()}
        free = args.parallel - len(busy)
        if free <= 0:
            time.sleep(1)
            continue
        try:
            jobs = server.call("POST", "/claim", {"max": free, "wait": 20},
                               timeout=40).json().get("jobs", [])
            pause = 2.0
        except Exception as exc:
            log.warning("Server not reached (%s); trying again in %.0f s", exc, pause)
            time.sleep(pause)
            pause = min(pause * 2, 60.0)
            continue
        for job in jobs:
            worker = Job(server, job, args.role, target, work, box)
            thread = threading.Thread(target=worker.run, name=job["job_id"], daemon=True)
            busy[job["job_id"]] = thread
            thread.start()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="AIMScribe pipeline agent")
    sub = parser.add_subparsers(dest="action", required=True)
    k = sub.add_parser("keygen", help="make this machine's two key pairs")
    k.add_argument("--keys", required=True)
    r = sub.add_parser("run", help="claim and work jobs")
    r.add_argument("--server", required=True)
    r.add_argument("--machine", required=True)
    r.add_argument("--keys", required=True)
    r.add_argument("--role", choices=("lab", "metrics"), required=True)
    how = r.add_mutually_exclusive_group(required=True)
    how.add_argument("--command", help="the model as a command that reads and writes files")
    how.add_argument("--model-url", help="the model as an HTTP service on this machine")
    r.add_argument("--transcribe-path", default="/v1/transcribe")
    r.add_argument("--ner-path", default="/v1/ner")
    r.add_argument("--compare-path", default="/v1/compare")
    r.add_argument("--model-path", default="/v1/model")
    r.add_argument("--model-token-env", help="environment variable holding the service's token")
    r.add_argument("--allow-remote-model", action="store_true",
                   help="allow a model URL off this machine (not recommended)")
    r.add_argument("--parallel", type=int, default=2)
    r.add_argument("--work", default=str(Path(tempfile.gettempdir()) / "aimscribe-agent"))
    r.add_argument("--insecure", action="store_true", help="tests only: skip TLS checks")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.action == "keygen":
        keygen(Path(args.keys))
    else:
        run(args)


if __name__ == "__main__":
    main()
