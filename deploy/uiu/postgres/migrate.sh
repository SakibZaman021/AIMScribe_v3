#!/bin/sh
# ================================================================
# Apply the migrations this server does not have yet - and only those.
#
# Runs inside the postgres container, as the superuser. From deploy/uiu:
#
#     docker compose exec -T postgres sh -s < postgres/migrate.sh
#
# The automatic deployer (autodeploy/autodeploy.sh) runs it on every
# update, and 02_schema.sh runs it to build a new server, so there is one
# way a schema is made.
#
# Each database keeps a ledger, aims_migrations: every file applied and the
# SHA-256 of what was applied. A file in the ledger is never applied again.
# Re-running an old migration on a database with data in it is not safe in
# general: 013 sets the chain's entry types as they were before 015 widened
# them, and today's rows break that. So a migration, once applied, is fixed;
# a change is a new file. A file whose contents differ from what the ledger
# recorded is reported, not re-applied.
#
# A server built before the ledger is adopted once, by naming the last
# migration it has (the /health detail lists what is missing):
#
#     docker compose exec -T -e AIMS_MIGRATIONS_ADOPT_THROUGH=022 \
#         postgres sh -s < postgres/migrate.sh
#
# records 001-022 as applied without running them, then applies 023 on.
# Without that, a database that has tables and no ledger is refused rather
# than guessed at.
#
# The first failure stops everything: the failed file is not recorded and
# nothing after it runs. Each migration carries its own transaction.
# ================================================================
set -eu

# psql_in <database> [psql options...]. Every query passes -c or a here-document:
# when this file is piped in with `sh -s`, a psql reading stdin would read the
# rest of this script.
psql_in() {
    db=$1
    shift
    psql -X -q -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:?}" --dbname "$db" "$@"
}

ADOPT="${AIMS_MIGRATIONS_ADOPT_THROUGH:-}"
FRESH="${AIMS_MIGRATIONS_FRESH:-}"
applied=0
current=0
changed=0

case "$ADOPT" in
    ''|[0-9][0-9][0-9]) ;;
    *) echo "AIMS_MIGRATIONS_ADOPT_THROUGH must be three digits, such as 022" >&2; exit 2 ;;
esac

# prepare <database> <a table every built database has>
prepare() {
    has_ledger=$(psql_in "$1" -At -c "SELECT to_regclass('public.aims_migrations') IS NOT NULL")
    built=$(psql_in "$1" -At -c "SELECT to_regclass('public.$2') IS NOT NULL")
    if [ "$has_ledger" = f ] && [ "$built" = t ] && [ -z "$ADOPT" ] && [ -z "$FRESH" ]; then
        echo "$1 has tables but no migration ledger: it was built before the ledger." >&2
        echo "Adopt it once, naming the last migration it has (see the header of this file)." >&2
        exit 3
    fi
    psql_in "$1" <<'SQL'
CREATE TABLE IF NOT EXISTS aims_migrations (
    file_name  text PRIMARY KEY,
    sha256     text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    adopted    boolean NOT NULL DEFAULT false
);
COMMENT ON TABLE aims_migrations IS
    'Which migration files this database has, and when each was applied (deploy/uiu/postgres/migrate.sh).';
SQL
    # Adoption covers a database that had no ledger until now, and only that.
    if [ "$has_ledger" = f ] && [ "$built" = t ]; then adopting=yes; else adopting=; fi
}

record() {
    psql_in "$1" -c "INSERT INTO aims_migrations (file_name, sha256, adopted)
                     VALUES ('$2', '$3', $4) ON CONFLICT (file_name) DO NOTHING"
}

# apply <database> <role, or empty for the superuser> <file>
apply() {
    name=$(basename "$3")
    sum=$(sha256sum "$3" | cut -d ' ' -f 1)
    have=$(psql_in "$1" -At -c "SELECT sha256 FROM aims_migrations WHERE file_name = '$name'")
    if [ -n "$have" ]; then
        current=$((current + 1))
        if [ "$have" != "$sum" ]; then
            changed=$((changed + 1))
            echo "  $1: $name differs from what was applied; not applied again" \
                 "(a change to a migration belongs in a new file)" >&2
        fi
        return
    fi
    number=$(printf '%s' "$name" | cut -c 1-3)
    if [ -n "$adopting" ] && [ -n "$ADOPT" ] && [ "$number" -le "$ADOPT" ]; then
        record "$1" "$name" "$sum" true
        echo "  $1: $name adopted (already on this server)"
        current=$((current + 1))
        return
    fi
    echo "  $1: applying $name"
    {
        if [ -n "$2" ]; then echo "SET ROLE $2;"; fi
        cat "$3"
        echo
        echo "RESET ROLE;"
        echo "INSERT INTO aims_migrations (file_name, sha256) VALUES ('$name', '$sum');"
    } | psql_in "$1"
    applied=$((applied + 1))
}

prepare aims_recordings sessions
for script in /aims/scripts/0[0-9][0-9]_*.sql; do
    apply aims_recordings aims_recordings "$script"
done
# The monitor reads whatever exists, as on a new server.
psql_in aims_recordings -c "GRANT SELECT ON ALL TABLES IN SCHEMA public TO aims_monitor;"

# The clinical database's two scripts belong to its first build.
prepare aims_clinical encounters
apply aims_clinical aims_clinical_owner /aims/scripts/clinical/001_aims_clinical.sql
apply aims_clinical "" /aims/scripts/clinical/002_roles.sql
# The doctor's findings with each prescription (API 3).
apply aims_clinical aims_clinical_owner /aims/scripts/clinical/003_consultation_findings.sql
# Interface 4: one patient row, one row per visit by CMED's visit_id.
apply aims_clinical aims_clinical_owner /aims/scripts/clinical/004_patient_visit.sql

# The clinical database's read-only login, for the database viewer: its
# password comes from .env (AIMS_CLINICAL_READER_PASSWORD), never a file.
if [ -n "${AIMS_CLINICAL_READER_PASSWORD:-}" ]; then
    psql_in aims_clinical -v pw="$AIMS_CLINICAL_READER_PASSWORD" <<'SQL'
ALTER ROLE aims_clinical_reader LOGIN PASSWORD :'pw';
SQL
fi

# Researchers' own read-only logins (the portal's People panel): the roles,
# the read-only grants on both databases - again on every update, so a new
# table is readable too - and the two functions the server calls to make
# and remove a login. As the superuser; safe to repeat.
psql_in aims_recordings -f /aims/scripts/roles/researcher_logins_recordings.sql
psql_in aims_clinical -f /aims/scripts/roles/researcher_logins_clinical.sql

# The data guard (scripts/guard/data_guard.sql): no drop, no truncate, no
# delete of more than 5,000 rows in one statement, in either database, unless
# a migration decided in review allows it. Last, so it covers every table the
# migrations above made; again on every update; safe to repeat.
psql_in aims_recordings -f /aims/scripts/guard/data_guard.sql
psql_in aims_clinical -f /aims/scripts/guard/data_guard.sql

# The access rules (hba/pg_hba.conf, a mounted folder) may have changed with
# this update: read them again now. A setting that needs a restart, such as
# archive_mode, takes effect when the update recreates the container.
psql_in postgres -At -c "SELECT pg_reload_conf()" >/dev/null

echo "Migrations: $applied applied, $current already in place, $changed changed since applied."
