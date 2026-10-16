#!/usr/bin/env python3
"""
One test session with CMED's finished build, observed entirely from our side.

    python tools/cmed_build_session.py --patients P1,P2,P3,P4
    python tools/cmed_build_session.py --patients P1,P2,P3,P4 --only 2

CMED's code is final, so nothing here asks them to change anything. A CMED tester
works their real doctor page on a PC running our recorder, pointed at our server;
this script tells the person next to them what to click, then reads our two
databases and shows exactly what CMED sent and how our server labelled it:

    which API 2 arrived, for which patient   (does CMED send API 2 for a click
                                              made while a recording was running?)
    which clicks reached the recorder         (does CMED send API 1 again when the
                                              doctor goes back to a patient?)
    which prescriptions (3a, 3b) arrived, with whose five fields
    the files the server will write, and the rule behind each

Use four TEST patients from CMED's test data, in the doctor's waiting list. Real
patients must not be used. Read-only: it never writes to either database.

Each scenario ends with Stop on the AIMScribe control, so its recording closes
and is labelled before the next one starts. Allow about 25 minutes.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import CONTAINER, DATABASES, psql  # noqa: E402

# (title, steps, what to look at). A, B, C stand for the first three test patients;
# D is only the "next patient" that closes a recording.
SCENARIOS = [
    ("Normal consultation",
     ["Open A from the waiting list. Talk for about 30 seconds.",
      "Build A's prescription.",
      "Open B from the waiting list (A's recording must close here).",
      "Press Stop on the AIMScribe control."],
     "One file for A (R1). A second, short recording for B."),
    ("Wrong patient picked, corrected within seconds",
     ["Open A from the waiting list (the mistake).",
      "Within 10 seconds, open B (the right patient). Talk for about 70 seconds.",
      "Build B's prescription.",
      "Press Stop."],
     "One file, B (R3+R1). KEY QUESTION: does an API 2 arrive for B, and does the "
     "prescription carry B's fields?"),
    ("Deliberate click on another profile, then back",
     ["Open A. Talk for about 40 seconds.",
      "Open B's profile, look at it for 10 seconds.",
      "Go back to A's profile.",
      "Build A's prescription.",
      "Press Stop."],
     "One file, A (R2). KEY QUESTION: does going back to A send API 1 again?"),
    ("Glance in the middle of a consultation",
     ["Open A. Talk for about 90 seconds.",
      "Open B's profile for 10 seconds.",
      "Go back to A and build A's prescription.",
      "Press Stop."],
     "One file, A (R2)."),
    ("Follow-up with no prescription, then the next patient",
     ["Open A. Talk for about 70 seconds. Do NOT build a prescription.",
      "Open B from the waiting list. Talk for about 70 seconds.",
      "Build B's prescription.",
      "Press Stop."],
     "Two files: A (R4) up to B's click, then B (R1)."),
    ("Counselling limit after the prescription",
     ["Open A. Talk for about 30 seconds.",
      "Build A's prescription.",
      "Keep talking, but do not click anything, for 2 minutes 30 seconds."],
     "The recording closes by itself about 2:00-2:10 after the prescription "
     "(reason counselling_limit). No Stop needed."),
]


def rows(database: str, sql: str, container: str):
    out = psql(DATABASES[database], sql, container=container, quiet=True)
    return [line.split("|") for line in out.strip().splitlines() if line.strip()]


def table_exists(name: str, container: str) -> bool:
    return bool(rows("recordings", "SELECT 1 FROM information_schema.tables "
                     f"WHERE table_name = '{name}'", container))


def lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def observe(since: str, names: dict, container: str) -> list:
    """Everything our side received since `since`, in plain lines."""
    who = lambda pid: names.get(pid, pid)          # noqa: E731
    lines = []

    sessions = rows("recordings", f"""
        SELECT session_id, patient_id, to_char(opened_at, 'HH24:MI:SS'),
               coalesce(to_char(closed_at, 'HH24:MI:SS'), 'still open'),
               coalesce(close_reason, ''), coalesce(confirmation, '')
          FROM sessions WHERE opened_at >= {lit(since)} ORDER BY opened_at""", container)
    lines.append(f"Recordings opened: {len(sessions)}")
    ids = []
    for sid, pid, opened, closed, reason, conf in sessions:
        ids.append(sid)
        lines.append(f"  {sid}  first patient {who(pid)}  {opened} -> {closed}  "
                     f"{reason}  {conf}")

    intake = rows("clinical", f"""
        SELECT kind, patient_id, start_time, to_char(received_at, 'HH24:MI:SS')
          FROM intake_records WHERE received_at >= {lit(since)}
         ORDER BY received_at""", container)
    lines.append(f"From CMED's server (API 2 = patient_information, 3b = prescription): "
                 f"{len(intake)}")
    for kind, pid, start, at in intake:
        label = "API 2" if kind == "patient_information" else "API 3b"
        lines.append(f"  {at}  {label:6}  {who(pid)}  start_time {start}")

    if ids and table_exists("session_clicks", container):
        in_ids = ",".join(lit(i) for i in ids)
        clicks = rows("recordings", f"""
            SELECT session_id, event, patient_id, round(audio_seconds::numeric, 1),
                   coalesce(grant_jti, '')
              FROM session_clicks WHERE session_id IN ({in_ids})
             ORDER BY session_id, entry_no""", container)
        lines.append(f"Clicks and prescription signals inside recordings (API 1 / 3a): "
                     f"{len(clicks)}")
        for sid, event, pid, secs, jti in clicks:
            note = "API 1 click" if event == "patient_opened" else "API 3a"
            lines.append(f"  {sid[-6:]}  at {secs:>7}s  {note:11}  {who(pid)}"
                         f"{'' if jti or event != 'patient_opened' else '  (no grant)'}")
    if ids and table_exists("consultation_parts", container):
        in_ids = ",".join(lit(i) for i in ids)
        parts = rows("recordings", f"""
            SELECT session_id, part_no, round(start_seconds::numeric, 1),
                   coalesce(round(end_seconds::numeric, 1)::text, 'end'),
                   patient_id, rule, needs_review
              FROM consultation_parts WHERE session_id IN ({in_ids})
             ORDER BY session_id, part_no""", container)
        lines.append(f"Files the server will write: {len(parts)}")
        for sid, n, start, end, pid, rule, review in parts:
            lines.append(f"  {sid[-6:]}  file {n}: {start}s -> {end}  {who(pid)}  "
                         f"{rule}{'  NEEDS REVIEW' if review == 't' else ''}")
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--patients", required=True,
                        help="four TEST patient ids from CMED's test data: A,B,C,D")
    parser.add_argument("--only", type=int, help="run one scenario (1-6)")
    parser.add_argument("--container", default=CONTAINER)
    parser.add_argument("--report", default="", help="where to write the report")
    args = parser.parse_args(argv)

    ids = [p.strip() for p in args.patients.split(",") if p.strip()]
    if len(ids) != 4:
        parser.error("--patients needs exactly four ids")
    names = dict(zip(ids, ["A", "B", "C", "D"]))
    names = {pid: f"{letter} ({pid})" for pid, letter in names.items()}

    chosen = [args.only] if args.only else list(range(1, len(SCENARIOS) + 1))
    report = [f"CMED build session - {datetime.now():%Y-%m-%d %H:%M}",
              f"Patients: " + ", ".join(names.values()), ""]

    for number in chosen:
        title, steps, expect = SCENARIOS[number - 1]
        print(f"\n=== Scenario {number}: {title} ===")
        for i, step in enumerate(steps, 1):
            print(f"  {i}. {step}")
        input("\nPress Enter when the tester is about to do step 1... ")
        since = datetime.now(timezone.utc).isoformat()
        input("Press Enter when every step is done... ")
        if number == 6:
            print("Waiting 15 s for the recording to close and be labelled...")
            time.sleep(15)
        else:
            time.sleep(5)                       # the close and the labels land
        found = observe(since, names, args.container)
        print("\n".join(found))
        print(f"Expected: {expect}")
        verdict = input("Did it match? (y/n, or a note): ").strip()
        report += [f"Scenario {number}: {title}", *[f"  step {i}: {s}"
                   for i, s in enumerate(steps, 1)], "Observed:", *found,
                   f"Expected: {expect}", f"Tester's note: {verdict}", ""]

    out = Path(args.report or f"CMED_BUILD_SESSION_{datetime.now():%Y%m%d_%H%M}.txt")
    out.write_text("\n".join(report), encoding="utf-8")
    print(f"\nReport written to {out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
