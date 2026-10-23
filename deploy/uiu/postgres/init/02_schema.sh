#!/bin/sh
# ================================================================
# The schema of a new server: the v1 tables the later scripts build on,
# then every migration through postgres/migrate.sh - the same runner the
# automatic deployer uses on every update, so a new server and an updated
# one are made the same way, and each file applied is recorded in the
# aims_migrations ledger from the start.
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

echo "Applying the v1 baseline"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname aims_recordings <<'SQL'
SET ROLE aims_recordings;
\i /aims/baseline/00_v1_baseline.sql
RESET ROLE;
-- The monitor reads whatever the owner adds later, too.
ALTER DEFAULT PRIVILEGES FOR ROLE aims_recordings IN SCHEMA public
    GRANT SELECT ON TABLES TO aims_monitor;
SQL

echo "Applying every migration"
AIMS_MIGRATIONS_FRESH=1 sh /aims/migrate.sh

echo "Schema applied. Nothing else touches these databases by hand:"
echo "a chain or a constraint is fixed in the system's own code, never in psql."
