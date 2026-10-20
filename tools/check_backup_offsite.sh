#!/bin/bash
# Proves the off-site backup end to end on THIS laptop: the real backup image dumps
# both local databases, encrypts them, copies them to a throwaway bucket in the local
# object store (standing in for R2), fetches the newest back and restores it into a
# scratch database. Everything it creates is removed at the end. Passwords are read
# from deploy/local/.env into variables and never printed.
#   bash tools/check_backup_offsite.sh
set -u
export MSYS_NO_PATHCONV=1
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENVF=$ROOT/deploy/local/.env
val() { grep "^$1=" "$ENVF" | head -1 | cut -d= -f2- | tr -d '\r'; }
PGU=$(val POSTGRES_SUPERUSER); PGP=$(val POSTGRES_SUPERUSER_PASSWORD)
MAK=$(val MINIO_ACCESS_KEY); MSK=$(val MINIO_SECRET_KEY)
KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
WORK=$(mktemp -d)
NET=aimscribe-v3_default
BUCKET=aims-backups-livetest

echo "== build the real backup image"
docker build -q -t aimscribe/backup:livetest "$(cygpath -w "$ROOT/deploy/uiu/backup")" >/dev/null && echo built

run() {
  docker run --rm --network "$NET" -v "$(cygpath -w "$WORK"):/backups" \
    -e PGHOST=aimscribe-v3-postgres -e PGPORT=5432 -e PGUSER="${PGU:-aims_admin}" -e PGPASSWORD="$PGP" \
    -e AIMS_BACKUP_DIR=/backups -e AIMS_BACKUP_KEY="$KEY" \
    -e AIMS_OFFSITE_BUCKET="$BUCKET" -e AIMS_OFFSITE_ENDPOINT=http://minio:9000 \
    -e AIMS_OFFSITE_ACCESS_KEY="$MAK" -e AIMS_OFFSITE_SECRET_KEY="$MSK" \
    -e AIMS_OFFSITE_PROVIDER=Minio -e AIMS_OFFSITE_REGION=us-east-1 \
    "$@"
}

echo "== create the test bucket (stands in for the R2 backup bucket)"
run --entrypoint sh aimscribe/backup:livetest -c '. /dev/null; export RCLONE_CONFIG_OFFSITE_TYPE=s3 RCLONE_CONFIG_OFFSITE_PROVIDER=Minio RCLONE_CONFIG_OFFSITE_ENDPOINT=http://minio:9000 RCLONE_CONFIG_OFFSITE_ACCESS_KEY_ID="$AIMS_OFFSITE_ACCESS_KEY" RCLONE_CONFIG_OFFSITE_SECRET_ACCESS_KEY="$AIMS_OFFSITE_SECRET_KEY" RCLONE_CONFIG_OFFSITE_REGION=us-east-1 RCLONE_CONFIG=/tmp/x.conf; rclone mkdir offsite:'"$BUCKET"' && echo bucket ready'

echo "== one real nightly run: dump, encrypt, copy off-site"
run aimscribe/backup:livetest --now 2>&1 | sed 's/^/   /'
echo "   exit: ${PIPESTATUS[0]}"
ls -la "$WORK" | sed 's/^/   /'

echo "== what the off-site bucket holds"
run aimscribe/backup:livetest list-offsite 2>&1 | sed 's/^/   /'

echo "== the local copies are lost: fetch the newest back from off-site"
rm -f "$WORK"/aims_recordings_*.sql.gz.enc
FETCHED=$(run aimscribe/backup:livetest fetch latest aims_recordings 2>&1 | tail -1)
echo "   fetched: $FETCHED"

echo "== restore the fetched copy into a scratch database and count"
run aimscribe/backup:livetest restore "$FETCHED" aims_restore_livetest 2>&1 | grep -E "Decompressed|Restored|sessions|segments|chain_entries|^ *[a-z_]+ *\|" | head -8 | sed 's/^/   /'
docker exec aimscribe-v3-postgres psql -U "${PGU:-aims_admin}" -d aims_restore_livetest -tAc "select 'sessions restored: ' || count(*) from sessions"
docker exec aimscribe-v3-postgres psql -U "${PGU:-aims_admin}" -d aims_recordings -tAc "select 'sessions live:     ' || count(*) from sessions"

echo "== clean up: scratch database, test bucket, work folder, image"
docker exec aimscribe-v3-postgres psql -U "${PGU:-aims_admin}" -d postgres -qc "DROP DATABASE IF EXISTS aims_restore_livetest" && echo "   scratch database dropped"
run --entrypoint sh aimscribe/backup:livetest -c 'export RCLONE_CONFIG_OFFSITE_TYPE=s3 RCLONE_CONFIG_OFFSITE_PROVIDER=Minio RCLONE_CONFIG_OFFSITE_ENDPOINT=http://minio:9000 RCLONE_CONFIG_OFFSITE_ACCESS_KEY_ID="$AIMS_OFFSITE_ACCESS_KEY" RCLONE_CONFIG_OFFSITE_SECRET_ACCESS_KEY="$AIMS_OFFSITE_SECRET_KEY" RCLONE_CONFIG_OFFSITE_REGION=us-east-1 RCLONE_CONFIG=/tmp/x.conf; rclone purge offsite:'"$BUCKET"' && echo "   test bucket removed"'
rm -rf "$WORK" && echo "   work folder removed"
docker rmi -f aimscribe/backup:livetest >/dev/null && echo "   test image removed"
