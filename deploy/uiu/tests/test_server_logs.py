"""
The programs' logs kept across updates (keep-logs.sh) and read back
(server-logs.sh), run for real with a stand-in docker that answers
`docker compose logs` with lines we choose and records how it was asked.
Needs bash, awk, gzip and GNU date; elsewhere it is skipped.
"""
from __future__ import annotations

import gzip
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
NEEDS = ("bash", "awk", "gzip", "date")
pytestmark = pytest.mark.skipif(not all(shutil.which(t) for t in NEEDS),
                                reason="needs bash, awk, gzip and GNU date")


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


def _env(tmp_path: Path, lines: list) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (tmp_path / "lines.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{(tmp_path / "docker.log").as_posix()}"\n'
        f'case "$*" in *"compose logs"*) cat "{(tmp_path / "lines.txt").as_posix()}" ;; esac\n',
        encoding="utf-8", newline="\n")
    docker.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir.as_posix()}{os.pathsep}{env['PATH']}"
    env["AIMS_DEPLOY_STATE"] = (tmp_path / "state").as_posix()
    return env


def _run(script: str, env: dict, *args: str) -> subprocess.CompletedProcess:
    # The bash found on PATH (Git's on Windows), not Windows' own WSL launcher.
    return subprocess.run([shutil.which("bash"), (HERE / script).as_posix(), *args], env=env,
                          capture_output=True, text=True, timeout=60)


def test_the_logs_outlive_an_update_and_are_read_back(tmp_path):
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=3)
    lines = [
        f"aims-api  | {_stamp(old)} INFO started three days ago",
        f"aims-gateway  | {_stamp(old)} POST /api/v2/device/register 403",
        f"aims-api  | {_stamp(now - timedelta(minutes=30))} WARNING Registration refused: UNKNOWN_CLINIC",
        f"aims-worker  | {_stamp(now - timedelta(minutes=20))} INFO transcribed",
        f"aims-api  | {_stamp(now - timedelta(minutes=5))} INFO ready",
    ]
    env = _env(tmp_path, lines)
    first = _run("keep-logs.sh", env)
    assert first.returncode == 0, first.stderr
    logs = tmp_path / "state" / "logs"
    today = now.strftime("%Y-%m-%d")
    # Each line in the file of its own day; a day before yesterday compressed.
    kept_today = (logs / f"{today}.log").read_text(encoding="utf-8")
    assert "UNKNOWN_CLINIC" in kept_today and "transcribed" in kept_today
    assert "three days ago" not in kept_today
    with gzip.open(logs / f"{old:%Y-%m-%d}.log.gz", "rt", encoding="utf-8") as f:
        assert "three days ago" in f.read()
    # The next time starts where this one ended.
    since = (logs / ".since").read_text(encoding="utf-8").strip()
    asked = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    assert "--timestamps" in asked[0] and "--until" in asked[0]
    (tmp_path / "lines.txt").write_text(
        f"aims-api  | {_stamp(datetime.now(timezone.utc))} INFO after the update\n",
        encoding="utf-8")
    assert _run("keep-logs.sh", env).returncode == 0
    asked = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    assert f"--since {since}" in asked[-1]
    assert "after the update" in (logs / f"{today}.log").read_text(encoding="utf-8")

    # Read back: one program, a period, a word.
    (tmp_path / "lines.txt").write_text("", encoding="utf-8")
    api = _run("server-logs.sh", env, "api", "2h")
    assert api.returncode == 0, api.stderr
    assert "UNKNOWN_CLINIC" in api.stdout and "ready" in api.stdout
    assert "transcribed" not in api.stdout and "three days ago" not in api.stdout
    found = _run("server-logs.sh", env, "all", "4d", "device/register")
    assert found.stdout.count("\n") == 1 and "403" in found.stdout
    assert _run("server-logs.sh", env, "api", "soon").returncode == 2


def test_old_days_go_and_the_folder_stays_small(tmp_path):
    env = _env(tmp_path, [])
    logs = tmp_path / "state" / "logs"
    logs.mkdir(parents=True)
    ancient = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%Y-%m-%d")
    recent = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
    for day in (ancient, recent):
        with gzip.open(logs / f"{day}.log.gz", "wt", encoding="utf-8") as f:
            f.write("x\n")
    assert _run("keep-logs.sh", env).returncode == 0
    assert not (logs / f"{ancient}.log.gz").exists()          # past AIMS_LOG_KEEP_DAYS
    assert (logs / f"{recent}.log.gz").exists()
