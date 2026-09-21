"""
Start AIMScribe in Docker, ready to record.

    python bootstrap.py                 start everything and set it up
    python bootstrap.py --down          stop it, keep the data
    python bootstrap.py --reset         stop it and throw the data away
    python bootstrap.py --no-build      start without rebuilding the images

It does the parts nobody should have to do by hand:

  * makes every password and key the stack needs, once, into `.env`;
  * brings the containers up and waits until the server actually answers;
  * registers the clinic, issues CMED's key, and mints an enrolment token;
  * writes `out\\recorder\\` - the `.env` and the two public keys a doctor's
    PC needs - so installing the recorder is a copy, not a configuration
    exercise.

Nothing here is a secret worth guarding: it is a bench. The UIU server's
secrets are made the same way but kept by an administrator (../uiu).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
ENV = HERE / ".env"
OUT = HERE / "out"


PORT_TAKEN = """
Something is already answering on port {port}, and it is not this stack.

Usually it is a server that was started by hand earlier - `python tools/bench.py`,
or another copy of the backend - and left running. Docker will publish the port
anyway, both will answer to localhost, and requests will be shared between them
by accident: half of them refused, with credentials the other one never issued.

Stop it, or give this stack a port of its own with AIMS_PORT in {env}.

  Windows:  Get-NetTCPConnection -LocalPort {port} -State Listen |
                ForEach-Object {{ Get-Process -Id $_.OwningProcess }}
  Linux:    ss -ltnp | grep :{port}
"""

CLINIC, CMED_CLINIC = "HOSP003", "CMED-LOCAL-01"


# ============================================================
# The secrets, made once
# ============================================================

def make_env() -> Dict[str, str]:
    """Write `.env` if it is not there, and return what is in it."""
    if ENV.is_file():
        values = read_env()
        if values.get("AIMS_GRANT_PRIVATE_KEY"):
            print(f"  using the settings already in {ENV.name}")
            return values

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    def pem(key) -> str:
        return key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode("utf-8").replace("\n", "\\n")

    values = {
        "POSTGRES_SUPERUSER": "aims_admin",
        "POSTGRES_SUPERUSER_PASSWORD": secrets.token_urlsafe(24),
        "AIMS_RECORDINGS_PASSWORD": secrets.token_urlsafe(24),
        "AIMS_CLINICAL_PASSWORD": secrets.token_urlsafe(24),
        "AIMS_MONITOR_PASSWORD": secrets.token_urlsafe(24),
        "REDIS_PASSWORD": secrets.token_urlsafe(24),
        "MINIO_ACCESS_KEY": "aimslocal",
        "MINIO_SECRET_KEY": secrets.token_urlsafe(24),
        "AIMS_ADMIN_KEY": secrets.token_urlsafe(24),
        "AIMS_WORKER_KEY": secrets.token_urlsafe(24),
        "AIMSCRIBE_WEBHOOK_SECRET": secrets.token_urlsafe(24),
        # Signs recording grants; its public half is pinned in the recorder.
        "AIMS_GRANT_PRIVATE_KEY": pem(Ed25519PrivateKey.generate()),
        # Signs purge receipts, which is what lets a PC delete its audio.
        "AIMS_RECEIPT_PRIVATE_KEY": pem(Ed25519PrivateKey.generate()),
        # Encrypts every cloud copy. On a real server this never leaves it.
        "AIMS_COPY_KEY": base64.b64encode(os.urandom(32)).decode(),
        # Both spellings of this machine. They are the same address, and a
        # page opened at one while only the other is trusted is refused with
        # a 403 - which the page reports as "the recorder is not running",
        # sending you to look at an agent that is running perfectly.
        "AIMS_ALLOWED_ORIGINS": "http://localhost:3000,http://127.0.0.1:3000",
        "AIMS_CMED_WEBHOOK_ENABLED": "false",
        "AIMS_PORT": "6060",
        "POSTGRES_PORT": "5433",
        "ARCHIVE_PATH": "./archive",
        "TZ": "Asia/Dhaka",
        "LOG_LEVEL": "INFO",
    }

    ENV.write_text(
        "# Written by bootstrap.py. This is a local stack, not a clinic.\n"
        "# Delete this file to start again with new keys - and then the\n"
        "# recorders enrolled against it have to enrol again.\n\n"
        + "\n".join(f"{k}={v}" for k, v in values.items()) + "\n",
        encoding="utf-8")
    print(f"  wrote {ENV.name}: every password and key, made here, once")
    return values


def this_machine() -> str:
    """
    The address other machines reach this one on.

    Upload links name a host, and that host has to mean the same thing to
    three different places: the server writing the link, a doctor's PC
    following it, and the archive worker inside Docker. `localhost` means a
    different machine to each of them, and `host.docker.internal` does not
    resolve outside Docker at all - so the address on the network is used,
    which is the same everywhere and works from a second PC as well.
    """
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))          # nothing is sent
        return probe.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        probe.close()


# Ports a browser refuses to open, whatever is listening on them. Chrome and
# Firefox both keep this list: they are ports where a crafted HTTP request
# could be read as some other protocol's command. 6000 is X11 - and it was
# version 1's port, so it is the one a browser will silently refuse while
# curl, the recorder and every test carry on working perfectly. The error a
# browser shows for it says the site may be "temporarily down", which sends
# you looking at the server.
BROWSER_BLOCKED = {
    1719, 1720, 1723, 2049, 3659, 4045, 5060, 5061, 6000, 6566, 6665, 6666,
    6667, 6668, 6669, 6697, 10080,
}


def port_is_taken(port: int) -> bool:
    """
    Something already listening on this machine's own port.

    Worth a stop. Docker publishes on every address, so a server started by
    hand earlier - a bench, an older copy - can keep 127.0.0.1 while the
    containers take the rest. Both answer to `localhost`, requests are shared
    between them by accident, and the half that reaches the wrong one is
    refused with credentials the other stack issued. That reads exactly like a
    server fault and is not one.
    """
    import socket
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        probe = socket.socket(family, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            if probe.connect_ex((address, port)) == 0:
                return True
        except OSError:
            continue
        finally:
            probe.close()
    return False


def read_env() -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not ENV.is_file():
        return values
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def update_env(changes: Dict[str, str]) -> None:
    values = read_env()
    values.update(changes)
    header = ("# Written by bootstrap.py. This is a local stack, not a clinic.\n"
              "# Delete this file to start again with new keys - and then the\n"
              "# recorders enrolled against it have to enrol again.\n\n")
    ENV.write_text(header + "\n".join(f"{k}={v}" for k, v in values.items()) + "\n",
                   encoding="utf-8")


# ============================================================
# Docker
# ============================================================

def compose(*arguments: str, check: bool = True) -> int:
    command = ["docker", "compose", *arguments]
    print(f"  $ {' '.join(command)}")
    result = subprocess.run(command, cwd=HERE)
    if check and result.returncode != 0:
        raise SystemExit(f"\n{' '.join(command)} failed. Is Docker running?")
    return result.returncode


def docker_is_running() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              timeout=30).returncode == 0
    except Exception:
        return False


# ============================================================
# Talking to the server
# ============================================================

def call(url: str, body: Optional[dict] = None,
         headers: Optional[Dict[str, str]] = None) -> Tuple[int, dict]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}
    except Exception:
        return 0, {}


def wait_for_health(url: str, *, seconds: int = 300) -> bool:
    print("  waiting for the server", end="", flush=True)
    deadline = time.time() + seconds
    while time.time() < deadline:
        status, _ = call(f"{url}/health")
        if status == 200:
            print(" - ready")
            return True
        print(".", end="", flush=True)
        time.sleep(3)
    print(" - it never answered")
    return False


def set_up_clinic(url: str, admin_key: str, values: Dict[str, str]) -> Dict[str, str]:
    """The three things an administrator does once, done here."""
    admin = {"X-Admin-Key": admin_key}

    status, reply = call(f"{url}/api/v2/admin/hospital", {
        "hospital_id": CLINIC, "name": "Local clinic", "timezone": "Asia/Dhaka",
        "cmed_hospital_id": CMED_CLINIC}, admin)
    if status >= 300:
        raise SystemExit(f"could not register the clinic: {status} {reply}")

    cmed_key = values.get("AIMS_CMED_KEY", "")
    if cmed_key:
        # A key from a previous run is no use if the database was thrown away.
        probe, reply = call(f"{url}/api/v2/clinical/patient-information", {},
                            {"X-CMED-Key": cmed_key})
        if reply.get("code") == "INVALID_KEY":
            cmed_key = ""
    if not cmed_key:
        status, reply = call(f"{url}/api/v2/admin/cmed-key",
                             {"label": "cmed-local", "created_by": "bootstrap"}, admin)
        cmed_key = reply.get("cmed_key", "")
        if not cmed_key:
            raise SystemExit(f"no CMED key was issued: {status} {reply}")
        update_env({"AIMS_CMED_KEY": cmed_key})

    status, reply = call(f"{url}/api/v2/admin/enrollment-token",
                         {"hospital_id": CLINIC, "created_by": "bootstrap"}, admin)
    token = reply.get("enrollment_token") or reply.get("token") or ""
    return {"cmed_key": cmed_key, "enrollment_token": token}


# ============================================================
# What the doctor's PC needs
# ============================================================

def write_recorder_folder(url_for_pc: str, values: Dict[str, str],
                          token: str) -> Path:
    """
    `out\\recorder\\`: the two public keys the agent pins, its `.env`, and the
    enrolment token. Copy it to the PC and the agent is configured.
    """
    from cryptography.hazmat.primitives import serialization

    folder = OUT / "recorder"
    keys = folder / "keys"
    keys.mkdir(parents=True, exist_ok=True)

    for name, variable in (("aimslab_grant_pub.pem", "AIMS_GRANT_PRIVATE_KEY"),
                           ("aimslab_receipt_pub.pem", "AIMS_RECEIPT_PRIVATE_KEY")):
        private = serialization.load_pem_private_key(
            values[variable].replace("\\n", "\n").encode(), password=None)
        (keys / name).write_bytes(private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo))

    (folder / ".env").write_text("\n".join([
        "# Written by deploy/local/bootstrap.py - the local stack.",
        "# Copy this file and the keys folder next to AIMScribe_Agent.exe.",
        f"AIMS_BACKEND_URL={url_for_pc}",
        "AIMS_ALLOWED_ORIGINS=http://localhost:3000,http://127.0.0.1:3000",
        "AIMS_ALLOWED_HOSTS=localhost:5050,127.0.0.1:5050",
        "AIMS_LOCAL_API_KEY=" + secrets.token_urlsafe(16),
        "",
    ]), encoding="utf-8")

    if token:
        (folder / "enrollment.token").write_text(token, encoding="utf-8")

    (folder / "README.txt").write_text("\n".join([
        "What to do with this folder",
        "",
        "The quick way, on the PC that has the agent:",
        "",
        "    python bootstrap.py --agent \"C:\\Path\\To\\AIMScribe_Agent\"",
        "",
        "It puts each file where the agent looks for it. By hand it is three",
        "places, and they are not the same place:",
        "",
        "1. .env             next to AIMScribe_Agent.exe.",
        "2. keys\\*.pem       into C:\\ProgramData\\AIMScribe\\keys\\ - NOT next to",
        "                    the exe. The agent reads its keys from its data",
        "                    folder, and without them it starts \"degraded\" and",
        "                    will never delete any audio from this PC.",
        "3. enrollment.token into C:\\ProgramData\\AIMScribe\\state\\.",
        "",
        "A PC that already has an agent should not have that data folder",
        "disturbed: set AIMS_DATA_DIR in .env to a folder of its own, and put",
        "keys\\ and state\\ under that instead.",
        "",
        "Then start the agent. It enrols itself once and shows a tray icon.",
        "",
        "The two keys are public halves. They carry no secret, and they are the",
        "same on every PC. The private halves stay on the server.",
        "",
        "One enrolment token is good for one PC, once.",
        "",
    ]), encoding="utf-8")
    return folder


def configure_agent(folder: Path, agent_dir: Path, data_dir: Optional[Path]) -> None:
    """
    Put `out\\recorder\\` where an agent on this machine will find it.

    Three destinations, because the agent keeps its settings next to the exe
    and its keys and enrolment in its data folder. Copying everything to one
    place leaves an agent that looks healthy, records happily, and can never
    delete a finished recording - which nobody notices until a disk fills.
    """
    if not (agent_dir / "AIMScribe_Agent.exe").is_file():
        raise SystemExit(f"No AIMScribe_Agent.exe in {agent_dir}")

    env_text = (folder / ".env").read_text(encoding="utf-8")
    if data_dir is not None:
        env_text += f"AIMS_DATA_DIR={data_dir}\n"
    (agent_dir / ".env").write_text(env_text, encoding="utf-8")

    data = data_dir or (Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
                        / "AIMScribe")
    (data / "keys").mkdir(parents=True, exist_ok=True)
    (data / "state").mkdir(parents=True, exist_ok=True)
    for pem in (folder / "keys").glob("*.pem"):
        shutil.copy2(pem, data / "keys" / pem.name)

    token = folder / "enrollment.token"
    # An agent that is already enrolled keeps its identity: handing it a new
    # token would spend a second one for nothing.
    if token.is_file() and not (data / "state" / "device.token").is_file():
        shutil.copy2(token, data / "state" / "enrollment.token")
        enrolment = "enrolment token placed"
    else:
        enrolment = "already enrolled; token not replaced"

    print(f"""
  The agent in {agent_dir} is ready:
    settings   {agent_dir / '.env'}
    keys       {data / 'keys'}
    enrolment  {enrolment}

  Start AIMScribe_Agent.exe. It shows a tray icon when it is recording-ready.""")


def write_summary(url: str, values: Dict[str, str], issued: Dict[str, str]) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "how-to-connect.txt"
    path.write_text("\n".join([
        "AIMScribe - local stack",
        "=" * 60,
        f"Server          {url}",
        f"Health          {url}/health",
        f"Dashboard       {url}/api/v2/dashboard",
        f"Storage console http://localhost:9001  "
        f"({values['MINIO_ACCESS_KEY']} / {values['MINIO_SECRET_KEY']})",
        "",
        f"Administrator key   {values['AIMS_ADMIN_KEY']}",
        f"CMED key            {issued.get('cmed_key', '')}",
        f"Enrolment token     {issued.get('enrollment_token', '')}",
        "",
        "The recorder's files are in out\\recorder\\ - see the README there.",
        "Recordings land in the archive folder named in .env (ARCHIVE_PATH).",
        "",
    ]), encoding="utf-8")
    return path


# ============================================================
# Running it
# ============================================================

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--down", action="store_true", help="stop, keep the data")
    parser.add_argument("--reset", action="store_true",
                        help="stop and throw the data away")
    parser.add_argument("--no-build", action="store_true",
                        help="do not rebuild the images")
    parser.add_argument("--cmed", action="store_true",
                        help="also run CMED's test site on :3000")
    parser.add_argument("--agent", default="",
                        help="the folder holding AIMScribe_Agent.exe on this "
                             "machine: its settings, keys and enrolment token "
                             "are put where the agent looks for them")
    parser.add_argument("--agent-data", default="",
                        help="a data folder for that agent instead of "
                             "ProgramData - for a PC that already has one")
    parser.add_argument("--host", default="",
                        help="the address doctors' PCs reach this machine on "
                             "(default: this machine's address on the network)")
    args = parser.parse_args(argv)

    if args.down or args.reset:
        compose("down", "-v") if args.reset else compose("down")
        if args.reset:
            shutil.rmtree(OUT, ignore_errors=True)
            print("Stopped. The databases and the stored objects are gone.")
        else:
            print("Stopped. Everything is still there; run this again to carry on.")
        return 0

    if not docker_is_running():
        print("Docker is not running.\n"
              "Start Docker Desktop (or `sudo systemctl start docker`) and try again.")
        return 2

    print("AIMScribe - starting the local stack\n")
    values = make_env()

    port = int(values.get("AIMS_PORT", "6060"))
    if port in BROWSER_BLOCKED:
        moved = 6060 if 6060 not in BROWSER_BLOCKED else 8060
        update_env({"AIMS_PORT": str(moved)})
        values = read_env()
        print(f"""
  Port {port} has been moved to {moved}.

  Browsers refuse to open {port} whatever is listening there - it is on the
  list of ports they will not dial - so the dashboard was unreachable from
  Chrome while curl, the recorder and the tests were all fine. Anything
  pointing at :{port} needs the new number, which is in out\\recorder\\.env
  for the agent.""")
        port = moved
    ours = subprocess.run(["docker", "compose", "ps", "-q", "api"], cwd=HERE,
                          capture_output=True, text=True).stdout.strip()
    if port_is_taken(port) and not ours:
        print(PORT_TAKEN.format(port=port, env=ENV.name))
        return 2

    host = args.host or this_machine()
    update_env({"AIMS_STORAGE_HOST": f"{host}:9000"})
    values = read_env()
    print(f"  this machine is {host} on the network; upload links will say so")

    print("\nBuilding and starting")
    profile = ["--profile", "cmed"] if args.cmed else []
    compose(*profile, "up", "-d", *([] if args.no_build else ["--build"]))

    url = f"http://localhost:{port}"
    print()
    if not wait_for_health(url):
        print("\nThe server did not come up. What it says:")
        compose("logs", "--tail", "40", "api", check=False)
        return 1

    print("\nSetting the clinic up")
    issued = set_up_clinic(url, values["AIMS_ADMIN_KEY"], read_env())
    values = read_env()
    url_for_pc = f"http://{host}:{port}"
    folder = write_recorder_folder(url_for_pc, values, issued["enrollment_token"])
    if args.agent:
        configure_agent(folder, Path(args.agent).resolve(),
                        Path(args.agent_data).resolve() if args.agent_data else None)
    summary = write_summary(url, values, issued)
    cmed_line = ("\n  CMED's site http://localhost:3000"
                 if args.cmed else "")

    print(f"""
================================================================
Running.
================================================================

  Open this    {url}
               it says which AIMScribe it is, and links to the rest

  Dashboard    {url}/api/v2/dashboard
               it asks for this key: {values['AIMS_ADMIN_KEY']}
  Storage      http://localhost:9001 (console)
               user {values['MINIO_ACCESS_KEY']}, password in .env{cmed_line}

  CMED key     {issued['cmed_key']}

  This stack is every container named aimscribe-v3-*:

    docker ps --filter label=com.aimslab.system=aimscribe-v3

  Anything named aimscribe-* without the v3 belongs to version 1 and is
  not touched by this.

For a doctor's PC, copy this folder to it:

  {folder}

and follow the README inside. Then record something.

  Everything above is also in {summary}
  Logs:   docker compose logs -f api archive-worker
  Stop:   python bootstrap.py --down
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
