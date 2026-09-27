#!/usr/bin/env python3
"""
Read the two databases without a login form.

    python tools/db.py tables                  every table in both, with real counts
    python tools/db.py sessions                the most recent recordings
    python tools/db.py patient P0012345        one patient, both databases
    python tools/db.py sql "select ..."        anything, --db recordings|clinical
    python tools/db.py analyze                 refresh the planner's row estimates

Why this exists: Adminer runs inside a container, so `localhost` in its Server
box is Adminer itself, not PostgreSQL - the connection is refused and the page
comes back with nothing. The server name is `postgres`. Nothing here can be
typed wrong, because there is nothing to type: it goes through the database
container directly, and works whether or not the port is published.

Read-only by default. `sql` refuses anything that is not a SELECT unless you
pass --write, because this is the tool people reach for on a live server.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

CONTAINER = "aimscribe-v3-postgres"
SUPERUSER = "aims_admin"
DATABASES = {"recordings": "aims_recordings", "clinical": "aims_clinical"}

# Anything that can change data or schema. Checked as whole words, so a column
# called `updated_at` or a patient named Update does not trip it.
WRITES = ("insert", "update", "delete", "drop", "truncate", "alter", "create",
          "grant", "revoke", "copy", "vacuum", "reindex", "refresh")


def psql(database: str, sql: str, *, container: str, quiet: bool = False) -> str:
    """Run one statement in the database container and return stdout."""
    if not shutil.which("docker"):
        sys.exit("docker is not on PATH. Start Docker Desktop and try again.")

    command = ["docker", "exec", "-i", container, "psql",
               "-U", SUPERUSER, "-d", database, "-v", "ON_ERROR_STOP=1"]
    if quiet:
        command += ["-tA"]
    command += ["-c", sql]

    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0:
        message = (done.stderr or done.stdout).strip()
        if "No such container" in message:
            sys.exit(f"The database container ({container}) is not running.\n"
                     f"Start it with:  cd deploy/local && docker compose up -d postgres")
        sys.exit(message)
    return done.stdout


def _counts(database: str, container: str) -> list:
    """Every table with a **real** count, not the planner's estimate."""
    names = psql(database, """
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r' AND n.nspname = 'public' ORDER BY c.relname;
    """, container=container, quiet=True).split()
    if not names:
        return []

    # One statement rather than one per table: on a slow disk the difference
    # between 30 round trips and one is the difference between useful and not.
    union = " UNION ALL ".join(
        f"SELECT {_literal(name)} AS tbl, count(*) AS rows FROM {_ident(name)}"
        for name in names)
    rows = psql(database, f"SELECT * FROM ({union}) t ORDER BY rows DESC, tbl;",
                container=container, quiet=True).strip().splitlines()
    return [line.split("|") for line in rows if "|" in line]


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def cmd_tables(args) -> None:
    for label, database in DATABASES.items():
        print(f"\n=== {database} ===")
        rows = _counts(database, args.container)
        if not rows:
            print("  no tables")
            continue
        width = max(len(t) for t, _ in rows)
        total = 0
        for table, count in rows:
            total += int(count)
            marker = "" if int(count) else "   (empty)"
            print(f"  {table:<{width}}  {int(count):>9,}{marker}")
        print(f"  {'':<{width}}  {'-' * 9}")
        print(f"  {'total':<{width}}  {total:>9,}")
    print()


def cmd_sessions(args) -> None:
    print(psql("aims_recordings", f"""
        SELECT left(session_id, 10) AS session, hospital_id, doctor_id,
               patient_id AS patient, close_reason,
               to_char(opened_at, 'YYYY-MM-DD HH24:MI') AS opened,
               round(total_duration_seconds)::int AS secs,
               confirmation, archive_relpath IS NOT NULL AS archived
        FROM sessions ORDER BY opened_at DESC LIMIT {int(args.limit)};
    """, container=args.container))


def cmd_patient(args) -> None:
    who = _literal(args.patient_id)
    print("--- recordings ---")
    print(psql("aims_recordings", f"""
        SELECT left(session_id, 10) AS session, hospital_id, doctor_id,
               to_char(opened_at, 'YYYY-MM-DD HH24:MI') AS opened,
               round(total_duration_seconds)::int AS secs, confirmation
        FROM sessions WHERE patient_id = {who} ORDER BY opened_at DESC;
    """, container=args.container))

    print("--- clinical ---")
    print(psql("aims_clinical", f"""
        SELECT e.encounter_id, e.hospital_id, e.doctor_id, e.source,
               to_char(e.start_time, 'YYYY-MM-DD HH24:MI') AS started,
               (SELECT count(*) FROM prescriptions p
                WHERE p.encounter_id = e.encounter_id) AS scripts
        FROM encounters e WHERE e.patient_id = {who} ORDER BY e.start_time DESC;
    """, container=args.container))


def cmd_sql(args) -> None:
    statement = args.statement.strip()
    lowered = statement.lower()
    if not args.write:
        first = lowered.lstrip("( \t\n").split(None, 1)
        head = first[0] if first else ""
        if head not in ("select", "with", "table", "explain", "show"):
            sys.exit(f"'{head or statement[:20]}' is not a read. Pass --write if you mean it.")
        words = set(lowered.replace("(", " ").replace(",", " ").split())
        clashes = sorted(words & set(WRITES))
        if clashes:
            sys.exit(f"Refusing: the statement contains {', '.join(clashes)}. "
                     f"Pass --write if you mean it.")
    print(psql(DATABASES[args.db], statement, container=args.container))


def cmd_analyze(args) -> None:
    """
    Refresh row estimates.

    A crash or a restart clears the statistics counters, and then anything
    reading pg_stat_user_tables reports zero rows for tables that are full -
    which reads exactly like an empty database and is not one.
    """
    for database in DATABASES.values():
        psql(database, "ANALYZE;", container=args.container)
        print(f"analyzed {database}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--container", default=CONTAINER,
                        help=f"database container (default {CONTAINER})")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("tables", help="every table in both databases, with real counts"
                   ).set_defaults(func=cmd_tables)

    recent = sub.add_parser("sessions", help="the most recent recordings")
    recent.add_argument("--limit", type=int, default=20)
    recent.set_defaults(func=cmd_sessions)

    one = sub.add_parser("patient", help="one patient across both databases")
    one.add_argument("patient_id")
    one.set_defaults(func=cmd_patient)

    query = sub.add_parser("sql", help="run a statement")
    query.add_argument("statement")
    query.add_argument("--db", choices=sorted(DATABASES), default="recordings")
    query.add_argument("--write", action="store_true",
                       help="allow a statement that changes data")
    query.set_defaults(func=cmd_sql)

    sub.add_parser("analyze", help="refresh row estimates after a crash or restart"
                   ).set_defaults(func=cmd_analyze)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
