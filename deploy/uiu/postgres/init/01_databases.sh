#!/bin/sh
# ================================================================
# The two databases and the roles that reach them (SRS-DBA-20).
#
# Runs once, when the data directory is empty. Passwords come from the
# environment, never from a file in the repository.
#
#   aims_recordings   sessions, chains, receipts, the five-field notices
#   aims_clinical     everything CMED sends about a patient
#
# No application role can write to both, and neither can even connect to
# the other's database - which is the point of two databases rather than
# two schemas in one.
# ================================================================
set -eu

run() { psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$1"; }

echo "Creating the two databases and their roles"

run postgres <<SQL
-- The recordings service: owns its database and reads and writes it.
CREATE ROLE aims_recordings LOGIN PASSWORD '${AIMS_RECORDINGS_PASSWORD}';

-- The clinical side keeps its owner apart from the account that runs:
-- nothing connects as the owner, so a mistake in the application cannot
-- drop a table (SRS-DBA-15).
CREATE ROLE aims_clinical_owner NOLOGIN;
CREATE ROLE aims_clinical_writer LOGIN PASSWORD '${AIMS_CLINICAL_PASSWORD}';

-- Reads only, and only the recordings side: it says whether the system is
-- well, and never sees a patient's name.
CREATE ROLE aims_monitor LOGIN PASSWORD '${AIMS_MONITOR_PASSWORD}';

CREATE DATABASE aims_recordings OWNER aims_recordings;
CREATE DATABASE aims_clinical   OWNER aims_clinical_owner;

-- Nobody reaches a database just by existing.
REVOKE ALL ON DATABASE aims_recordings FROM PUBLIC;
REVOKE ALL ON DATABASE aims_clinical   FROM PUBLIC;

GRANT CONNECT ON DATABASE aims_recordings TO aims_recordings, aims_monitor;
GRANT CONNECT ON DATABASE aims_clinical   TO aims_clinical_writer;
SQL

# Extensions are created by the superuser, because the application roles are
# not allowed to: pgcrypto for gen_random_uuid(), pg_trgm for searching part
# of a file name (SRS-DBA-24).
for db in aims_recordings aims_clinical; do
    run "$db" <<SQL
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL
done

echo "Databases created"
