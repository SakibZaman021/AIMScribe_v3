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
    # Seconds from now until the next HH:MM, local time (TZ). Plain
    # arithmetic on the clock, because busybox date cannot parse "today
    # 02:30" - it failed silently, the wait came out negative, sleep
    # refused it, and the container restarted in a loop without ever
    # making a backup.
    target_h=$(echo "$AT" | cut -d: -f1 | sed 's/^0*//')
    target_m=$(echo "$AT" | cut -d: -f2 | sed 's/^0*//')
    now_h=$(date +%H | sed 's/^0*//')
    now_m=$(date +%M | sed 's/^0*//')
    now_s=$(date +%S | sed 's/^0*//')
    target=$(( ${target_h:-0} * 3600 + ${target_m:-0} * 60 ))
    now=$(( ${now_h:-0} * 3600 + ${now_m:-0} * 60 + ${now_s:-0} ))
    wait_for=$((target - now))
    if [ "$wait_for" -le 0 ]; then
        wait_for=$((wait_for + 86400))
    fi
    echo "$wait_for"
}

run_once() {
    failed=0
    dump_one aims_recordings || failed=1
    dump_one aims_clinical   || failed=1
    sweep_old

    if [ "$failed" -eq 0 ]; then
        log "done; $(find "$DIR" -maxdepth 1 -name 'aims_*.sql.gz*' | wc -l) backup file(s) held"
        date '+%Y-%m-%dT%H:%M:%S%z' > "$DIR/.last_success"
        return 0
    fi
    log "FINISHED WITH FAILURES - a database was not backed up tonight"
    return 1
}

# The service's entrypoint is this script, so the other jobs come in through it:
#   docker compose run --rm backup --now                      one backup, then exit
#   docker compose run --rm backup restore <file> <database>  restore.sh
case "${1:-}" in
    --now)
        mkdir -p "$DIR"
        run_once
        exit $?
        ;;
    restore|/opt/aims/restore.sh)
        shift
        exec /bin/sh /opt/aims/restore.sh "$@"
        ;;
    "")
        ;;
    *)
        echo "usage: backup.sh [--now | restore <backup file> <target database>]"
        exit 2
        ;;
esac

if ! command -v openssl >/dev/null 2>&1 && [ -n "${AIMS_BACKUP_KEY:-}" ]; then
    log "FAILED: AIMS_BACKUP_KEY is set but this image has no openssl - build the"
    log "        backup image (deploy/uiu/backup/Dockerfile) instead of using postgres:16-alpine"
    exit 1
fi

mkdir -p "$DIR"
log "started; nightly at $AT, keeping $KEEP days, encryption: $([ -n "${AIMS_BACKUP_KEY:-}" ] && echo on || echo OFF)"

while true; do
    wait_for=$(seconds_until)
    log "next backup in $((wait_for / 3600))h $(((wait_for % 3600) / 60))m"
    sleep "$wait_for"
    run_once || true
    # Past the minute, so a fast dump cannot schedule the same night twice.
    sleep 61
done
