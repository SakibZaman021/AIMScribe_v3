"""
Report the live database's schema state before running a migration.

Read-only. Never writes, never migrates. Run it before 002_v2_integrity.sql so a
deploy does not discover the answer for you.

    # credentials come from the same env vars the app uses
    set POSTGRES_HOST=ep-xxxx.neon.tech
    set POSTGRES_DB=aimscribe_db
    set POSTGRES_USER=...
    set POSTGRES_PASSWORD=...
    set POSTGRES_SSLMODE=require
    python scripts/check_schema.py

Or point it at a full URL:

    python scripts/check_schema.py "postgresql://user:pass@host/db?sslmode=require"
"""
from __future__ import annotations

import asyncio
import os
import sys

try:
    import asyncpg
except ImportError:
    sys.exit("asyncpg is not installed.  pip install asyncpg")


V2_TABLES = [
    "hospitals", "devices", "enrollment_tokens", "chain_entries",
    "segments", "purge_receipts", "audit_log", "integrity_alerts",
    "used_grants", "api_keys",
]

V1_TABLES = [
    "sessions", "clips", "transcripts", "ner_results",
    "doctor_reviews", "prescription_data", "patients",
]


def dsn_from_env() -> str:
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "aimscribe_db")
    user = os.getenv("POSTGRES_USER", "aimscribe_user")
    password = os.getenv("POSTGRES_PASSWORD", "")
    sslmode = os.getenv("POSTGRES_SSLMODE", "prefer")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}?sslmode={sslmode}"


async def main() -> int:
    dsn = sys.argv[1] if len(sys.argv) > 1 else dsn_from_env()

    # Show where we are connecting without printing the password.
    safe = dsn
    if "@" in dsn and "//" in dsn:
        head, tail = dsn.split("//", 1)
        creds, rest = tail.split("@", 1)
        user = creds.split(":", 1)[0]
        safe = f"{head}//{user}:***@{rest}"
    print(f"Connecting to {safe}\n")

    try:
        conn = await asyncpg.connect(dsn, timeout=20)
    except Exception as exc:
        print(f"Could not connect: {exc}")
        return 2

    try:
        version = await conn.fetchval("SELECT version()")
        print(version.split(",")[0])

        session_id_type = await conn.fetchval("""
            SELECT data_type FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'sessions'
              AND column_name = 'session_id'
        """)

        present = {
            row["table_name"]
            for row in await conn.fetch("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
            """)
        }

        v2_columns = await conn.fetchval("""
            SELECT count(*) FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'sessions'
              AND column_name IN ('device_id','chain_head_hash','archive_relpath',
                                  'consent_obtained','session_date')
        """)

        print("\n--- v1 tables ---")
        for name in V1_TABLES:
            mark = "present" if name in present else "MISSING"
            count = ""
            if name in present:
                try:
                    count = f"  ({await conn.fetchval(f'SELECT count(*) FROM {name}')} rows)"
                except Exception:
                    count = ""
            print(f"  {name:<20} {mark}{count}")

        print("\n--- v2 tables (created by 002_v2_integrity.sql) ---")
        v2_found = [name for name in V2_TABLES if name in present]
        for name in V2_TABLES:
            print(f"  {name:<20} {'present' if name in present else 'not yet'}")

        print("\n--- verdict ---")
        print(f"  sessions.session_id type : {session_id_type}")
        print(f"  v2 columns on sessions   : {v2_columns}/5")
        print(f"  v2 tables present        : {len(v2_found)}/{len(V2_TABLES)}")
        print()

        if session_id_type == "uuid":
            print("  ACTION: 001_session_id_varchar.sql has NOT been applied.")
            print("          Protocol 2 agents mint 26-character ULIDs, which will")
            print("          not fit a uuid column. Apply 001 first, then 002.")
        elif session_id_type in ("character varying", "text"):
            print("  OK: session_id is already a string type, so 002 applies cleanly.")
        else:
            print(f"  UNEXPECTED: session_id is {session_id_type!r}. Stop and investigate.")

        if len(v2_found) == len(V2_TABLES) and v2_columns == 5:
            print("  002 appears to have been applied already; re-running is a no-op.")
        elif v2_found:
            print(f"  PARTIAL: {len(v2_found)} v2 table(s) exist. 002 is written to be")
            print("           idempotent (IF NOT EXISTS throughout), so re-running is safe,")
            print("           but check why it stopped part-way.")

        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
