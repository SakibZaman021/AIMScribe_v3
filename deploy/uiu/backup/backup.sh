#!/bin/sh
# ================================================================
# Nightly backup of both databases (SRS-DAT-16, SRS-STO-06).
#
# The audio has three homes - the doctor's PC until a receipt, the archive
# array, and the cloud copy. The databases have one, which is why this
# runs every night: lose them and the recordings are still there but
# nothing knows whose they are.
#
# Backups hold patient names, so they are encrypted with a key that is set
# on this machine (AIMS_BACKUP_KEY). Without it the script still runs -
# never skipping a backup is more important - but says so every time.
#
# Restoring is restore.sh, and it is tested each quarter, not assumed.
# ================================================================
set -eu

DIR="${AIMS_BACKUP_DIR:-/backups}"
KEEP="${AIMS_BACKUP_KEEP_DAYS:-30}"
AT="${AIMS_BACKUP_AT:-02:30}"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') BACKUP $*"; }

dump_one() {
    db="$1"
    stamp="$(date '+%Y%m%d_%H%M')"
    target="$DIR/${db}_${stamp}.sql.gz"

    # Written to a .partial name and renamed, so a backup that was
    # interrupted is never mistaken for one that finished.
    if ! pg_dump --dbname="$db" --no-owner --format=plain | gzip -9 > "$target.partial"; then
        log "FAILED to dump $db"
        rm -f "$target.partial"
        return 1
    fi

    if [ -n "${AIMS_BACKUP_KEY:-}" ]; then
        openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
            -pass env:AIMS_BACKUP_KEY \
            -in "$target.partial" -out "$target.enc.partial"
        rm -f "$target.partial"
        mv "$target.enc.partial" "$target.enc"
        target="$target.enc"
    else
        mv "$target.partial" "$target"
        log "WARNING: AIMS_BACKUP_KEY is not set - $db is backed up in the clear,"
        log "         and this file holds patients' names (SRS-DAT-08)"
    fi

    size=$(wc -c < "$target")
    if [ "$size" -lt 4096 ]; then
        log "FAILED: $target is only $size bytes; keeping it for inspection"
        return 1
    fi
    log "$db -> $(basename "$target") ($((size / 1024)) KB)"
}

sweep_old() {
    # Only whole backups are ever removed, and only after KEEP days.
    find "$DIR" -maxdepth 1 -name 'aims_*.sql.gz*' -mtime "+$KEEP" -print | while read -r old; do
        case "$old" in
            *.partial) continue ;;
        esac
        log "removing $(basename "$old") (older than $KEEP days)"
        rm -f "$old"
    done
}

seconds_until() {
    # Seconds from now until the next HH:MM, local time.
    target_h=$(echo "$AT" | cut -d: -f1)
    target_m=$(echo "$AT" | cut -d: -f2)
    now=$(date +%s)
    today=$(date -d "today ${target_h}:${target_m}:00" +%s 2>/dev/null \
            || date -j -f "%H:%M:%S" "${target_h}:${target_m}:00" +%s 2>/dev/null)
    if [ -z "$today" ] || [ "$today" -le "$now" ]; then
        today=$((today + 86400))
    fi
    echo $((today - now))
}

mkdir -p "$DIR"
log "started; nightly at $AT, keeping $KEEP days, encryption: $([ -n "${AIMS_BACKUP_KEY:-}" ] && echo on || echo OFF)"

while true; do
    wait_for=$(seconds_until)
    log "next backup in $((wait_for / 3600))h $(((wait_for % 3600) / 60))m"
    sleep "$wait_for"

    failed=0
    dump_one aims_recordings || failed=1
    dump_one aims_clinical   || failed=1
    sweep_old

    if [ "$failed" -eq 0 ]; then
        log "done; $(find "$DIR" -maxdepth 1 -name 'aims_*.sql.gz*' | wc -l) backup file(s) held"
        date '+%Y-%m-%dT%H:%M:%S%z' > "$DIR/.last_success"
    else
        log "FINISHED WITH FAILURES - a database was not backed up tonight"
    fi
done
