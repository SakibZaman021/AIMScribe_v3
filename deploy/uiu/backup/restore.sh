#!/bin/sh
# ================================================================
# Restore one database from a backup (SRS-STO-06).
#
#   docker compose run --rm backup /opt/aims/restore.sh \
#       /backups/aims_recordings_20260920_0230.sql.gz.enc  aims_restore_test
#
# It restores into the database you name, and refuses to write into
# aims_recordings or aims_clinical: a restore is checked first and put
# back deliberately, never by running a script at speed.
#
# The quarterly drill (AT-74, SRS-ARC-07) is: restore the latest backup
# into aims_restore_test, count the sessions, and drop it again.
# ================================================================
set -eu

FILE="${1:?usage: restore.sh <backup file> <target database>}"
TARGET="${2:?usage: restore.sh <backup file> <target database>}"

case "$TARGET" in
    aims_recordings|aims_clinical)
        echo "Refusing to restore over the live database $TARGET."
        echo "Restore into a new database, check it, then swap it in by hand."
        exit 2
        ;;
esac

[ -f "$FILE" ] || { echo "No such backup: $FILE"; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
plain="$work/dump.sql"

case "$FILE" in
    *.enc)
        [ -n "${AIMS_BACKUP_KEY:-}" ] || { echo "AIMS_BACKUP_KEY is needed for $FILE"; exit 1; }
        openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:AIMS_BACKUP_KEY \
            -in "$FILE" | gunzip > "$plain"
        ;;
    *.gz)
        gunzip < "$FILE" > "$plain"
        ;;
    *)
        cp "$FILE" "$plain"
        ;;
esac

lines=$(wc -l < "$plain")
echo "Decompressed $(basename "$FILE"): $lines lines"
[ "$lines" -gt 10 ] || { echo "That is too small to be a real backup."; exit 1; }

psql -v ON_ERROR_STOP=1 --dbname postgres -c "CREATE DATABASE $TARGET"
psql -v ON_ERROR_STOP=1 --dbname "$TARGET" -f "$plain" > "$work/restore.log" 2>&1 || {
    echo "Restore failed; last lines:"; tail -20 "$work/restore.log"; exit 1;
}

echo "Restored into $TARGET. What it holds:"
psql --dbname "$TARGET" -c "
    SELECT table_name, (xpath('/row/c/text()',
           query_to_xml('SELECT count(*) AS c FROM ' || quote_ident(table_name),
                        false, true, '')))[1]::text::bigint AS rows
      FROM information_schema.tables
     WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
     ORDER BY rows DESC NULLS LAST LIMIT 12;"

echo
echo "When the drill is done:  psql -c 'DROP DATABASE $TARGET'"
