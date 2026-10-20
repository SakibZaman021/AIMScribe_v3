#!/bin/sh
# ================================================================
# Fill in the settings and the user list from the environment.
#
# The passwords are written to a file inside the container and nowhere
# else: not into the image, not into the repository, and not into a
# volume that survives the container.
# ================================================================
set -eu

CONF=/tmp/pgbouncer.ini
USERS=/etc/pgbouncer/userlist.txt

sed \
    -e "s/DB_HOST_PLACEHOLDER/${DB_HOST:-postgres}/g" \
    -e "s/DB_PORT_PLACEHOLDER/${DB_PORT:-5432}/g" \
    -e "s/POOL_MODE_PLACEHOLDER/${POOL_MODE:-transaction}/g" \
    -e "s/MAX_CLIENT_CONN_PLACEHOLDER/${MAX_CLIENT_CONN:-500}/g" \
    -e "s/DEFAULT_POOL_SIZE_PLACEHOLDER/${DEFAULT_POOL_SIZE:-25}/g" \
    /etc/pgbouncer/pgbouncer.ini > "$CONF"
sed -i "s|auth_file = /etc/pgbouncer/userlist.txt|auth_file = $USERS|" "$CONF"

umask 077
{
    # aims_api, not aims_recordings: the owner of the tables has no login
    # (018_v3_least_privilege.sql), so it cannot come through here at all.
    printf '"aims_api" "%s"\n' "${AIMS_API_DB_PASSWORD:?}"
    printf '"aims_clinical_writer" "%s"\n' "${AIMS_CLINICAL_PASSWORD:?}"
    printf '"aims_monitor" "%s"\n' "${AIMS_MONITOR_PASSWORD:?}"
} > "$USERS"

echo "PgBouncer: ${POOL_MODE:-transaction} pooling to ${DB_HOST:-postgres}, 3 roles"
exec pgbouncer "$CONF"
