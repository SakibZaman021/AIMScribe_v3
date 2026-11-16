"""
Kept running (16 Nov): the recorder waits in the background for CMED's start.
Whoever ends it - Task Manager, the tray, anyone - its logon task starts it
again within two minutes; while it runs, the task starts nothing.
"""
from __future__ import annotations

from pathlib import Path

import main

HERE = Path(__file__).resolve().parent.parent


def test_the_logon_task_looks_every_two_minutes_and_starts_quietly():
    iss = (HERE / "installer.iss").read_text(encoding="utf-8")
    assert "<Repetition><Interval>PT2M</Interval>" in iss
    assert "<StopAtDurationEnd>false</StopAtDurationEnd>" in iss
    assert "<Arguments>--from-task</Arguments>" in iss and main.KEEPER_ARG == "--from-task"
    # While it runs, the look starts nothing; Windows can still end it.
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in iss
    assert "<AllowHardTerminate>true</AllowHardTerminate>" in iss
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in iss
    # Not stopped by a laptop on battery, or by Windows when idle.
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in iss
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in iss
    # At sign-in it starts at once; a clock repeats every two minutes from the
    # moment Setup registers the task - a repetition on the sign-in trigger only
    # began at the NEXT sign-in, so right after an install an ended recorder
    # stayed off (found on a real PC, 16 Nov).
    assert "<LogonTrigger><Enabled>true</Enabled></LogonTrigger>" in iss
    clock = iss[iss.index("<TimeTrigger>"):iss.index("</TimeTrigger>")]
    assert "<Repetition><Interval>PT2M</Interval>" in clock
    assert "<StartBoundary>' + GetDateTimeString(" in clock
    # Repetition before StartBoundary and Enabled, as Windows exports it.
    assert clock.index("<Repetition>") < clock.index("<StartBoundary>") < clock.index("<Enabled>")


def test_one_rule_whoever_ends_it_it_comes_back():
    source = (HERE / "main.py").read_text(encoding="utf-8")
    # No exception that keeps it stopped: an Exit, too, is undone in two minutes.
    assert "stopped_by_it" not in source and "STOP_FLAG" not in source
    # A look that finds it running ends quietly, without the "Already running" box.
    assert "if mutex is None and from_task:" in source


def test_the_same_version_everywhere_a_pc_or_it_reads_it():
    version = main.config.app_version
    iss = (HERE / "installer.iss").read_text(encoding="utf-8")
    assert f'#define AppVersion     "{version}"' in iss
    assert f"'{version}.0'" in (HERE / "version_info.txt").read_text(encoding="utf-8")
