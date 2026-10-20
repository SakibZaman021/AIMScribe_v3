#!/usr/bin/env python3
"""
Run every v3 server test against a real, throwaway PostgreSQL - one command.

    python tools/run_server_tests.py

Starts a PostgreSQL 16 container on port 55439, points the database tests at it,
runs the v3 test files, and removes the container afterwards, pass or fail.
Needs Docker running. Touches nothing of the local system's own databases.

The whole backend/tests folder is not run: it still holds the old v1 scripts,
which break test collection; the v3 files are listed here instead.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
# Two runs at once (e.g. two people, or two branches) each set their own.
NAME = os.getenv("AIMS_TEST_PG_NAME", "aims-servertests-db")
PORT = int(os.getenv("AIMS_TEST_PG_PORT", "55439"))
ONLY = [a for a in sys.argv[1:] if a.startswith("tests/")]
FILES = sorted(p.name for p in (BACKEND / "tests").glob("test_*.py")
               if p.name not in ("test_azure_apis.py", "test_full_pipeline.py",
                                 "test_transcription.py"))
SHIM = f'''class _Server:
    def get_uri(self):
        return "postgresql://postgres:ptest@127.0.0.1:{PORT}/postgres"
def get_server(*args, **kwargs):
    return _Server()
'''


def docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def main() -> int:
    docker("rm", "-f", NAME, check=False)
    docker("run", "-d", "--rm", "--name", NAME, "-e", "POSTGRES_PASSWORD=ptest",
           "-p", f"{PORT}:5432", "postgres:16-alpine")
    try:
        for _ in range(60):
            if docker("exec", NAME, "pg_isready", "-U", "postgres", check=False).returncode == 0:
                break
            time.sleep(1)
        with tempfile.TemporaryDirectory() as shim:
            Path(shim, "pgserver.py").write_text(SHIM, encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": shim}
            return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p",
                                   "no:cacheprovider", *(ONLY or [f"tests/{f}" for f in FILES])],
                                  cwd=BACKEND, env=env).returncode
    finally:
        docker("rm", "-f", NAME, check=False)


if __name__ == "__main__":
    sys.exit(main())
