#!/usr/bin/env bash
# ================================================================
# Puts the beta server's own R2 upload bucket into .env.beta - never into
# the real server's .env. Run it yourself on the server:
#
#     cd ~/AIMScribe_v3/deploy/uiu && beta/set-r2-beta.sh
#
# Asks for the bucket name, the S3 endpoint and that bucket's token. The
# secret is typed without being shown; nothing is printed back. Then:
#
#     docker compose -p aimsbeta -f docker-compose.beta.yml --env-file .env.beta up -d api
# ================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
DST=.env.beta
[ -f "$DST" ] || { echo "Run beta/make-env.sh first."; exit 1; }
umask 077

ask() {   # ask <prompt> <variable> [secret]
    local value=""
    while [ -z "$value" ]; do
        if [ "${3:-}" = secret ]; then read -r -s -p "$1: " value; echo
        else read -r -p "$1: " value; fi
        value="$(printf '%s' "$value" | tr -d '[:space:]')"
    done
    printf -v "$2" '%s' "$value"
}

echo "The beta server's R2 bucket. Paste each value and press Enter."
ask "Bucket name (for example aimscribe-beta)" BUCKET
ask "S3 endpoint (with or without https://)" ENDPOINT
ENDPOINT="${ENDPOINT#https://}"; ENDPOINT="${ENDPOINT#http://}"; ENDPOINT="${ENDPOINT%%/*}"
case "$ENDPOINT" in
    *.r2.cloudflarestorage.com) ;;
    *) echo "That does not end in .r2.cloudflarestorage.com - run the script again."; exit 1 ;;
esac
REAL_BUCKET=$(grep -m1 -E '^MINIO_BUCKET=' .env | cut -d= -f2- || true)
if [ "$BUCKET" = "$REAL_BUCKET" ]; then
    echo "That is the real server's bucket. Beta needs its own - run the script again."
    exit 1
fi
ask "Access Key ID (that bucket's token)" KEY_ID
ask "Secret Access Key (hidden)" SECRET secret

grep -v -E '^(AIMS_BETA_MINIO_[A-Z_]+=|# Interim: the real bucket)' "$DST" > "$DST.new"
{
    echo "AIMS_BETA_MINIO_ENDPOINT=$ENDPOINT"
    echo "AIMS_BETA_MINIO_BUCKET=$BUCKET"
    echo "AIMS_BETA_MINIO_ACCESS_KEY=$KEY_ID"
    echo "AIMS_BETA_MINIO_SECRET_KEY=$SECRET"
} >> "$DST.new"
mv -f "$DST.new" "$DST"
chmod 600 "$DST"
echo "Saved. Restart beta's API to use it (the command is at the top of this script)."
