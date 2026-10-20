#!/bin/sh
# ================================================================
# The schema, applied exactly as a deployment applies it: the v1 tables
# the later scripts build on, then every migration in order, then the
# clinical database.
#
# The same order is exercised on a real PostgreSQL by
# backend/tests/test_db_integration.py, so what a new server gets here is
# what the tests run against.
#
# Each set of tables is created by the role that owns it: the scripts run
# under SET ROLE, not as the superuser, so ownership ends up right without
# a chain of ALTER TABLE ... OWNER TO afterwards.
# ================================================================
set -eu

run() { psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$1"; }

echo "Applying the recordings schema"
# Every migration in backend/scripts, in file-name order - the glob, not a
# list kept by hand, so a new migration can never be one a new server misses.
# The same files, in the same order, are what test_db_integration.py applies.
{
    echo "SET ROLE aims_recordings;"
    printf '%s\n' '\i /aims/baseline/00_v1_baseline.sql'
    for script in /aims/scripts/0[0-9][0-9]_*.sql; do
        printf '%s %s\n' '\i' "$script"
    done
    echo "RESET ROLE;"
    # The monitor reads what exists now, and whatever is added later.
    echo "GRANT SELECT ON ALL TABLES IN SCHEMA public TO aims_monitor;"
    echo "ALTER DEFAULT PRIVILEGES FOR ROLE aims_recordings IN SCHEMA public"
    echo "    GRANT SELECT ON TABLES TO aims_monitor;"
} | run aims_recordings

echo "Applying the clinical schema"
run aims_clinical <<'SQL'
SET ROLE aims_clinical_owner;
\i /aims/scripts/clinical/001_aims_clinical.sql
RESET ROLE;
\i /aims/scripts/clinical/002_roles.sql
SQL

echo "Schema applied. Nothing else touches these databases by hand:"
echo "a chain or a constraint is fixed in the system's own code, never in psql."
