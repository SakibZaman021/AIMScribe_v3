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
run aims_recordings <<'SQL'
SET ROLE aims_recordings;
\i /aims/baseline/00_v1_baseline.sql
\i /aims/scripts/002_v2_integrity.sql
\i /aims/scripts/003_readable_names.sql
\i /aims/scripts/004_transit_storage.sql
\i /aims/scripts/005_wall_clock_times.sql
\i /aims/scripts/006_doctor_from_device.sql
\i /aims/scripts/007_readable_object_keys.sql
\i /aims/scripts/008_doctors.sql
\i /aims/scripts/009_close_reason.sql
\i /aims/scripts/010_v3_confirmation_channel_b.sql
\i /aims/scripts/011_v3_file_names.sql
\i /aims/scripts/012_v3_cloud_copy.sql
RESET ROLE;
-- The monitor reads what exists now, and whatever is added later.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO aims_monitor;
ALTER DEFAULT PRIVILEGES FOR ROLE aims_recordings IN SCHEMA public
    GRANT SELECT ON TABLES TO aims_monitor;
SQL

echo "Applying the clinical schema"
run aims_clinical <<'SQL'
SET ROLE aims_clinical_owner;
\i /aims/scripts/clinical/001_aims_clinical.sql
RESET ROLE;
\i /aims/scripts/clinical/002_roles.sql
SQL

echo "Schema applied. Nothing else touches these databases by hand:"
echo "a chain or a constraint is fixed in the system's own code, never in psql."
