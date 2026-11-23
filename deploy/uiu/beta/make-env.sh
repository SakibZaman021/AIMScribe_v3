#!/usr/bin/env bash
# ================================================================
# Writes deploy/uiu/.env.beta, the beta server's settings (BETA_SERVER.md),
# from the real server's .env. Run on the server, from deploy/uiu:
#
#     beta/make-env.sh
#
# Copied from .env, because beta is a copy of the same database and the same
# recorders must be able to move to it by their .env alone:
#   the database passwords, the grant and receipt signing keys, Google
#   sign-in, the owner, and the copy store with its key (so a recording moved
#   to beta still opens and downloads).
# Beta's own, made here once and kept on every later run:
#   its admin key, worker key and Redis password, its folder and address.
# Its upload bucket comes from beta/set-r2-beta.sh. Until that has run, the
# real bucket's values are used - nothing uploads to beta before a test PC
# is moved there (step 5), and set-r2-beta.sh must run before that.
# Nothing is printed back. .env is only read.
# ================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
SRC=.env
DST=.env.beta
[ -f "$SRC" ] || { echo "no $SRC here"; exit 1; }
umask 077

from() { grep -m1 -E "^$1=" "$2" 2>/dev/null || true; }
random() { openssl rand -hex 32; }

KEEP_ADMIN=$(from AIMS_BETA_ADMIN_KEY "$DST")
KEEP_WORKER=$(from AIMS_BETA_WORKER_KEY "$DST")
KEEP_REDIS=$(from REDIS_PASSWORD "$DST")
KEEP_MINIO=$(grep -E "^AIMS_BETA_MINIO_[A-Z_]+=" "$DST" 2>/dev/null || true)
HOST=$(from AIMS_PUBLIC_HOST "$SRC" | cut -d= -f2-)

{
    echo "# The beta server's settings - made by beta/make-env.sh from .env."
    echo "# Never commit this file."
    echo "AIMS_BETA_ROOT=/srv/aimscribe-beta"
    echo "AIMS_BETA_PUBLIC_HOST=${HOST}:9443"
    echo "AIMS_BETA_GATEWAY_IP=172.30.20.2"
    for name in POSTGRES_SUPERUSER POSTGRES_SUPERUSER_PASSWORD AIMS_API_DB_PASSWORD \
                AIMS_CLINICAL_PASSWORD AIMS_GRANT_PRIVATE_KEY AIMS_RECEIPT_PRIVATE_KEY \
                AIMS_GOOGLE_CLIENT_ID AIMS_GOOGLE_CLIENT_SECRET AIMS_PORTAL_OWNER_EMAIL \
                AIMS_COPY_BUCKET AIMS_COPY_ENDPOINT AIMS_COPY_ACCESS_KEY AIMS_COPY_SECRET_KEY \
                AIMS_COPY_REGION AIMS_COPY_SECURE AIMS_JSON_BUCKET AIMS_JSON_ENDPOINT \
                AIMS_JSON_ACCESS_KEY AIMS_JSON_SECRET_KEY AIMS_JSON_REGION AIMS_JSON_SECURE \
                AIMS_COPY_KEY AIMS_MIN_RECORDER_VERSION; do
        from "$name" "$SRC"
    done
    echo "${KEEP_ADMIN:-AIMS_BETA_ADMIN_KEY=$(random)}"
    echo "${KEEP_WORKER:-AIMS_BETA_WORKER_KEY=$(random)}"
    echo "${KEEP_REDIS:-REDIS_PASSWORD=$(random)}"
    if [ -n "$KEEP_MINIO" ]; then
        echo "$KEEP_MINIO"
    else
        echo "# Interim: the real bucket until beta/set-r2-beta.sh has run."
        echo "AIMS_BETA_MINIO_ENDPOINT=$(from MINIO_ENDPOINT "$SRC" | cut -d= -f2-)"
        echo "AIMS_BETA_MINIO_ACCESS_KEY=$(from MINIO_ACCESS_KEY "$SRC" | cut -d= -f2-)"
        echo "AIMS_BETA_MINIO_SECRET_KEY=$(from MINIO_SECRET_KEY "$SRC" | cut -d= -f2-)"
        echo "AIMS_BETA_MINIO_BUCKET=$(from MINIO_BUCKET "$SRC" | cut -d= -f2-)"
    fi
} > "$DST.new"
mv -f "$DST.new" "$DST"
chmod 600 "$DST"
echo "Wrote $DST ($(grep -c = "$DST") settings)."
