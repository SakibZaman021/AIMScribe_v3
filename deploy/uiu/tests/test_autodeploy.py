"""
The automatic deployer, run for real: autodeploy_scenarios.sh drives
autodeploy.sh against a throwaway git remote with a fake docker and curl,
through a normal deploy, a rollback, a hand-edited server and a force-push.
Linux only (it needs flock); elsewhere it is skipped.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


@pytest.mark.skipif(not all(shutil.which(t) for t in ("bash", "git", "flock")),
                    reason="needs bash, git and flock")
def test_the_deployer_deploys_rolls_back_and_refuses():
    result = subprocess.run(["bash", str(HERE / "autodeploy_scenarios.sh")],
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "All autodeploy scenarios passed." in result.stdout
