"""
The release record, written by BUILD.bat as its last step.

Every installer handed out must trace to one commit, or a month later nobody can
say what a clinic is running. Beside dist\\AIMScribeSetup.exe this writes:

    dist\\AIMScribeSetup-<version>.exe   the same file, named by its version
    dist\\RELEASE.txt                    version, commit, SHA-256, size, server,
                                        signature, and what to check before
                                        it goes to a PC

A build from code that is not committed says so in RELEASE.txt, in capitals:
such an installer cannot be traced and is not a release.

    python scripts\\release_record.py dist\\AIMScribeSetup.exe <backend> <signature status>
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True,
                              timeout=20).stdout.strip()
    except Exception:
        return ""


def main(argv) -> int:
    setup = Path(argv[1])
    backend = argv[2] if len(argv) > 2 else ""
    signature = argv[3] if len(argv) > 3 else "unknown"
    from config import APP_VERSION
    data = setup.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    commit = _git("rev-parse", "HEAD") or "unknown"
    # Anything under recorder/ not committed means this installer is not that commit.
    dirty = _git("status", "--porcelain", "--untracked-files=normal", "--", ".")
    versioned = setup.with_name(f"AIMScribeSetup-{APP_VERSION}.exe")
    shutil.copyfile(setup, versioned)
    lines = [
        f"AIMScribe recorder {APP_VERSION}",
        "=" * 40,
        f"Installer:   {versioned.name}  (also {setup.name})",
        f"SHA-256:     {sha}",
        f"Size:        {len(data):,} bytes",
        f"Built:       {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
        f"Commit:      {commit}",
        f"Source:      {'UNCOMMITTED CHANGES - NOT A TRACEABLE RELEASE' if dirty else 'clean (exactly this commit)'}",
        f"Server:      {backend or 'not given'}",
        f"Signature:   {signature}",
        "",
        "Before it goes to a PC",
        "- Compare the SHA-256 above with the file you are about to run.",
        "- Unsigned: test PCs only (Windows shows a SmartScreen warning).",
        "- Install over the previous version; settings and recordings are kept.",
        "- Check on one PC (INSTALL_FOR_IT.md): the tray icon is there; in Task",
        "  Scheduler, \"AIMScribe Agent\" repeats every 2 minutes; ended in Task",
        "  Manager, it is back within 2 minutes; one CMED test visit records.",
    ]
    if dirty:
        lines += ["", "Not committed when built:"] + ["  " + l for l in dirty.splitlines()[:30]]
    (setup.parent / "RELEASE.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:9]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
