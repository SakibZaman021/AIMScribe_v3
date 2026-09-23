"""
Follow one recording through the system, or list what the server holds.

    python tools/trace_recording.py                     today's recordings
    python tools/trace_recording.py --all               everything it holds
    python tools/trace_recording.py 9889                one patient, in detail

The detailed view answers the question that matters when a recording seems
to have gone missing: where did it stop? Every recording passes through the
same gates, and it is visible in the database at each one:

    opened      a session row exists
    pieces      segments, state = committed, audio in the bucket
    closed      closed_at, and a name (file_stem)
    confirmed   CMED described the visit, so it may enter the dataset
    archived    the worker joined it, wrote the JSON, filed it on disk
    copied      an encrypted copy verified in the copy bucket
    purged      receipts issued, so the doctor's PC may delete its audio

A recording that stops at "closed" was never described by CMED, and after
24 hours it is erased entirely (SRS-CNF-07) - which is not a fault, but it
is not obvious either unless somebody looks.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV = HERE.parent / "deploy" / "local" / ".env"
CONTAINER = "aimscribe-v3-postgres"


def settings() -> dict:
    values = {}
    if ENV.is_file():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def query(sql: str, *, table: bool = True, expanded: bool = False) -> str:
    values = settings()
    password = values.get("POSTGRES_SUPERUSER_PASSWORD", "")
    user = values.get("POSTGRES_SUPERUSER", "aims_admin")
    if not password:
        raise SystemExit(f"No database password found in {ENV}. Is the stack set up?")

    command = ["docker", "exec", "-e", f"PGPASSWORD={password}", CONTAINER,
               "psql", "-U", user, "-d", "aims_recordings"]
    if expanded:
        command.append("-x")           # one field per line, with its name
    command += ["-c" if table else "-tAc", sql]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or
                         "The database did not answer. Is the stack running?")
    return result.stdout.rstrip()


LIST_SQL = """
SELECT to_char(s.opened_at AT TIME ZONE COALESCE(h.timezone, 'UTC'),
               'MM-DD HH24:MI')                       AS opened,
       s.patient_id, s.doctor_id, s.hospital_id,
       round(s.total_duration_seconds)                AS secs,
       s.segment_count                                AS pieces,
       s.confirmation,
       CASE WHEN s.archived_at IS NOT NULL THEN 'yes' ELSE 'no' END AS archived,
       CASE WHEN c.session_id IS NOT NULL THEN 'yes' ELSE 'no' END  AS copied
  FROM sessions s
  LEFT JOIN hospitals h ON h.hospital_id = s.hospital_id
  LEFT JOIN (SELECT DISTINCT session_id FROM cloud_copies
              WHERE kind = 'audio' AND verified_at IS NOT NULL) c
         ON c.session_id = s.session_id
 WHERE s.closed_at IS NOT NULL {window}
 ORDER BY s.opened_at DESC
 LIMIT 40
"""


def show_list(all_days: bool) -> None:
    window = "" if all_days else "AND s.session_date >= CURRENT_DATE - 1"
    print(query(LIST_SQL.format(window=window)))
    print("\nconfirmation: confirmed = CMED described the visit; unconfirmed = "
          "not yet;\n              expired = never described, and erased after 24 hours;"
          "\n              refused = the patient did not consent, erased at once.")


DETAIL_SQL = """
SELECT s.session_id, s.file_stem, s.patient_id, s.doctor_id, s.hospital_id,
       s.status, s.confirmation,
       s.opened_at, s.closed_at, s.archived_at,
       round(s.total_duration_seconds) AS duration_seconds,
       s.segment_count, s.archive_relpath, s.archive_bytes,
       s.quarantine_reason,
       (SELECT count(*) FROM segments g WHERE g.session_id = s.session_id) AS segment_rows,
       (SELECT count(*) FROM segments g WHERE g.session_id = s.session_id
         AND g.state = 'committed') AS pieces_in_the_bucket,
       (SELECT count(*) FROM chain_entries e WHERE e.session_id = s.session_id) AS chain_entries,
       (SELECT count(*) FROM purge_receipts r WHERE r.session_id = s.session_id) AS purge_receipts,
       (SELECT count(*) FROM cloud_copies c WHERE c.session_id = s.session_id
         AND c.verified_at IS NOT NULL) AS copies_verified
  FROM sessions s
 WHERE s.patient_id = '{who}' OR s.file_stem LIKE '{who}\\_%' OR s.session_id = '{who}'
 ORDER BY s.opened_at DESC
"""


def show_detail(who: str) -> None:
    who = who.replace("'", "''")
    print(query(DETAIL_SQL.format(who=who) + " LIMIT 3", expanded=True)
          or "nothing found")

    rows = query(f"""
        SELECT s.session_id, s.confirmation, s.archived_at IS NOT NULL,
               s.archive_relpath
          FROM sessions s
         WHERE s.patient_id = '{who}' OR s.file_stem LIKE '{who}\\_%'
         ORDER BY s.opened_at DESC LIMIT 1
    """, table=False)
    if not rows.strip():
        print(f"\nNo recording found for '{who}'.")
        return

    session_id, confirmation, archived, relpath = (rows.split("|") + ["", "", "", ""])[:4]
    print("\nWhere it got to:")
    gates = [
        ("opened", True),
        ("pieces stored", True),
        ("closed", True),
        ("confirmed by CMED", confirmation == "confirmed"),
        ("archived", archived == "t"),
    ]
    for name, done in gates:
        print(f"  [{'x' if done else ' '}] {name}")

    if confirmation == "expired":
        print("\n  It stopped at 'confirmed'. CMED never described this visit, so after\n"
              "  24 hours the audio was erased and the identifiers redacted\n"
              "  (SRS-CNF-07). Nothing can bring it back.")
    elif confirmation == "unconfirmed":
        print("\n  It is waiting for CMED. If CMED's message never arrives, the audio\n"
              "  is erased 24 hours after it was recorded. Check Channel B now:\n"
              "      python tools/preflight.py --cmed-key <key>")
    elif archived == "t" and relpath:
        root = os.environ.get("ARCHIVE_PATH") or str(
            HERE.parent / "deploy" / "local" / "archive")
        print(f"\n  On disk: {Path(root) / relpath}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("who", nargs="?", default="",
                        help="a patient id, a file name, or a session id")
    parser.add_argument("--all", action="store_true", help="every day, not just today")
    args = parser.parse_args(argv)

    if args.who:
        show_detail(args.who)
    else:
        show_list(args.all)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
