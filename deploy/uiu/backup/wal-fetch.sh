#!/bin/sh
# ================================================================
# restore_command for a point-in-time recovery (pitr.sh): bring back one
# archived WAL file.
#
#     restore_command = '/opt/aims/wal-fetch.sh %f %p'
#
# Looks on this server first - the segments not yet shipped, then the ones
# kept after shipping - and then in the off-site bucket, where each is
# encrypted. AIMS_RESTORE_FROM=offsite skips this server: the drill uses it,
# to prove the off-site copy alone is enough.
#
# Exit 0: the file is at %p. Anything else: there is no such file, which is
# how PostgreSQL learns it has reached the end of the archive.
# ================================================================
set -u
set -o pipefail
name="$1"
dest="$2"
DIR="${AIMS_BACKUP_DIR:-/backups}"
SEGMENT_BYTES="${AIMS_WAL_SEGMENT_BYTES:-16777216}"

case "$name" in
    */*|*..*|'') exit 2 ;;
esac

# A segment (24 hex digits) is always exactly one segment long; a history or
# backup-label file is whatever it is. Anything else is damaged, and handing
# PostgreSQL a damaged segment is worse than saying there is none.
whole() {
    case "$name" in
        [0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F])
            [ "$(wc -c < "$1" | tr -d ' ')" = "$SEGMENT_BYTES" ] ;;
        *) [ -s "$1" ] ;;
    esac
}

if [ "${AIMS_RESTORE_FROM:-auto}" != offsite ]; then
    for folder in "$DIR/wal" "$DIR/wal-sent"; do
        if [ -f "$folder/$name.gz" ]; then
            if gunzip -c "$folder/$name.gz" > "$dest.part" && whole "$dest.part"; then
                mv -f "$dest.part" "$dest"
                exit 0
            fi
            rm -f "$dest.part"
        fi
    done
fi

[ -n "${AIMS_OFFSITE_BUCKET:-}" ] || exit 1
remote="offsite:${AIMS_OFFSITE_BUCKET}/${AIMS_WAL_PREFIX:-wal}/$name.gz.enc"
# rclone needs no config file: the remote is in the environment. One it
# cannot read (this runs as postgres) would only produce a warning.
if RCLONE_CONFIG=/tmp/rclone-restore.conf rclone cat "$remote" 2>/dev/null \
        | openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:AIMS_BACKUP_KEY 2>/dev/null \
        | gunzip -c > "$dest.part" 2>/dev/null \
        && whole "$dest.part"; then
    mv -f "$dest.part" "$dest"
    exit 0
fi
rm -f "$dest.part"
exit 1
