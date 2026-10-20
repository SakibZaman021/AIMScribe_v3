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
# on this machine (AIMS_BACKUP_KEY), and without it nothing is written: the
# dump is compressed and encrypted in one stream, so a plain copy of either
# database never touches a disk. Every backup gets a SHA-256 beside it
# (<file>.sha256), sent off-site with it and checked before a restore.
#
# Restoring is restore.sh, and it is tested each quarter, not assumed.
#
# Off the server (failure review A3): a backup kept only on the machine it
# protects is lost with that machine. Each encrypted backup is also copied to
# a bucket of its own (AIMS_OFFSITE_*), with its own token, checked by size
# after the upload, and marked so a night that could not reach the bucket is
# caught up the next night. Only encrypted backups ever leave the server.
# ================================================================
set -eu

# Nothing this script writes is readable by anyone but its owner - not the
# backups, not their checksums, not the markers.
umask 077

DIR="${AIMS_BACKUP_DIR:-/backups}"
KEEP="${AIMS_BACKUP_KEEP_DAYS:-30}"
AT="${AIMS_BACKUP_AT:-02:30}"
OFFSITE_KEEP="${AIMS_OFFSITE_KEEP_DAYS:-90}"
OFFSITE_TRIES="${AIMS_OFFSITE_TRIES:-3}"
OFFSITE_WAIT="${AIMS_OFFSITE_RETRY_SECONDS:-30}"

# rclone reads its remote from the environment, so no config file holds the key.
export RCLONE_CONFIG_OFFSITE_TYPE=s3
export RCLONE_CONFIG_OFFSITE_PROVIDER="${AIMS_OFFSITE_PROVIDER:-Cloudflare}"
export RCLONE_CONFIG_OFFSITE_ACCESS_KEY_ID="${AIMS_OFFSITE_ACCESS_KEY:-}"
export RCLONE_CONFIG_OFFSITE_SECRET_ACCESS_KEY="${AIMS_OFFSITE_SECRET_KEY:-}"
export RCLONE_CONFIG_OFFSITE_ENDPOINT="${AIMS_OFFSITE_ENDPOINT:-}"
export RCLONE_CONFIG_OFFSITE_REGION="${AIMS_OFFSITE_REGION:-auto}"
# A token limited to one bucket may not list buckets; do not ask.
export RCLONE_CONFIG_OFFSITE_NO_CHECK_BUCKET=true
export RCLONE_CONFIG="${RCLONE_CONFIG:-/tmp/rclone.conf}"
[ -f "$RCLONE_CONFIG" ] || : > "$RCLONE_CONFIG" 2>/dev/null || true

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') BACKUP $*"; }

private_dir() {
    # 711: nobody else can list the folder or read a backup (each is 600), but
    # the monitor - another user - can still open the two time stamps below by
    # name, which is all it reads.
    chmod 711 "$DIR" 2>/dev/null || log "could not make $DIR private (chmod 711)"
}

stamp_file() {
    # A time stamp, nothing else: readable by the monitor.
    date '+%Y-%m-%dT%H:%M:%S%z' > "$1"
    chmod 644 "$1"
}

have_key() {
    if [ -z "${AIMS_BACKUP_KEY:-}" ]; then
        log "FAILED: AIMS_BACKUP_KEY is not set. Backups hold patients' names, and an"
        log "        unencrypted one is never written. Set it in .env (openssl rand -base64 32)."
        return 1
    fi
}

checksum() {
    # <file>.sha256 in sha256sum's own format, naming the file without its
    # folder, so `sha256sum -c` works wherever the pair is put.
    (cd "$(dirname "$1")" && sha256sum "$(basename "$1")") > "$1.sha256"
}

verify() {
    # 0: the checksum matches. 1: it does not. 2: there is no checksum (a
    # backup made before checksums were written).
    [ -f "$1.sha256" ] || return 2
    (cd "$(dirname "$1")" && sha256sum -c "$(basename "$1").sha256" >/dev/null 2>&1) || return 1
}

dump_one() {
    db="$1"
    stamp="$(date '+%Y%m%d_%H%M')"
    target="$DIR/${db}_${stamp}.sql.gz.enc"
    failed_flag="$target.failed"
    rm -f "$failed_flag"

    # One stream: dump, compress, encrypt. Written to a .partial name and
    # renamed, so a backup that was interrupted is never mistaken for one
    # that finished. pg_dump's own failure is caught by the flag, because a
    # pipeline only reports its last command.
    if ! { pg_dump --dbname="$db" --no-owner --format=plain || : > "$failed_flag"; } \
            | gzip -9 \
            | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
                -pass env:AIMS_BACKUP_KEY -out "$target.partial"; then
        log "FAILED to encrypt the dump of $db"
        rm -f "$target.partial" "$failed_flag"
        return 1
    fi
    if [ -f "$failed_flag" ]; then
        log "FAILED to dump $db"
        rm -f "$target.partial" "$failed_flag"
        return 1
    fi
    mv "$target.partial" "$target"
    checksum "$target"

    size=$(wc -c < "$target")
    if [ "$size" -lt 4096 ]; then
        log "FAILED: $target is only $size bytes; keeping it for inspection"
        return 1
    fi
    log "$db -> $(basename "$target") ($((size / 1024)) KB, sha256 written)"
}

offsite_enabled() {
    [ -n "${AIMS_OFFSITE_BUCKET:-}" ]
}

remote() {
    echo "offsite:${AIMS_OFFSITE_BUCKET}/${AIMS_OFFSITE_PREFIX:-backups}"
}

remote_size() {
    # Bytes of one object off-site, or nothing if it is not there.
    rclone size --json "$(remote)/$1" 2>/dev/null | sed -n 's/.*"bytes":\([0-9]*\).*/\1/p'
}

push_one() {
    file="$1"
    name=$(basename "$file")
    size=$(wc -c < "$file" | tr -d ' ')
    # A backup from before checksums were written gets one now, from the file
    # as it is on this disk, so the off-site copy can be checked when fetched.
    [ -f "$file.sha256" ] || checksum "$file"
    try=1
    while [ "$try" -le "$OFFSITE_TRIES" ]; do
        if rclone copyto --retries 1 --low-level-retries 3 "$file.sha256" "$(remote)/$name.sha256" \
                && rclone copyto --retries 1 --low-level-retries 3 "$file" "$(remote)/$name"; then
            there=$(remote_size "$name")
            if [ "$there" = "$size" ]; then
                date '+%Y-%m-%dT%H:%M:%S%z' > "$file.offsite"
                log "off-site: $name ($((size / 1024)) KB, size checked)"
                return 0
            fi
            log "off-site copy of $name holds ${there:-nothing}, expected $size bytes"
        else
            log "off-site upload of $name failed (attempt $try of $OFFSITE_TRIES)"
        fi
        [ "$try" -lt "$OFFSITE_TRIES" ] && sleep $((OFFSITE_WAIT * try))
        try=$((try + 1))
    done
    return 1
}

offsite_push() {
    if ! offsite_enabled; then
        log "WARNING: no off-site copy is configured (AIMS_OFFSITE_BUCKET) - these"
        log "         backups exist only on this server and are lost with it"
        return 0
    fi
    if [ -z "${AIMS_BACKUP_KEY:-}" ]; then
        log "FAILED: off-site copy refused - the backups are not encrypted, and they"
        log "        hold patients' names. Set AIMS_BACKUP_KEY."
        return 1
    fi
    command -v rclone >/dev/null 2>&1 || {
        log "FAILED: rclone is missing - build the backup image (deploy/uiu/backup/Dockerfile)"
        return 1
    }
    failed=0
    # Every encrypted backup not yet off-site, oldest first: a night that could
    # not reach the bucket is caught up by the next one.
    for file in $(ls -tr "$DIR"/aims_*.sql.gz.enc 2>/dev/null); do
        [ -f "$file.offsite" ] && continue
        push_one "$file" || { failed=1; log "FAILED: $(basename "$file") is not off-site yet"; }
    done
    # Off-site copies are kept longer than the local ones. Only whole backups.
    rclone delete --min-age "${OFFSITE_KEEP}d" --include 'aims_*.sql.gz.enc*' "$(remote)" \
        || log "could not remove off-site backups older than $OFFSITE_KEEP days"
    if [ "$failed" -eq 0 ]; then
        stamp_file "$DIR/.last_offsite"
        return 0
    fi
    return 1
}

fetch() {
    # Bring one backup back from off-site, for restore.sh. "latest" is the
    # newest for that database.
    wanted="${1:?usage: fetch <file name | latest> [database]}"
    db="${2:-aims_recordings}"
    offsite_enabled || { echo "No off-site copy is configured."; exit 1; }
    if [ "$wanted" = latest ]; then
        wanted=$(rclone lsf --include "${db}_*.sql.gz.enc" "$(remote)" \
                 | grep '\.sql\.gz\.enc$' | sort | tail -1)
        [ -n "$wanted" ] || { echo "No off-site backup of $db."; exit 1; }
    fi
    case "$wanted" in
        */*|*..*) echo "Not a backup name: $wanted"; exit 2 ;;
    esac
    rclone copyto "$(remote)/$wanted" "$DIR/$wanted"
    rm -f "$DIR/$wanted.sha256"
    rclone copyto "$(remote)/$wanted.sha256" "$DIR/$wanted.sha256" 2>/dev/null || true
    if verify "$DIR/$wanted"; then
        echo "checksum ok" >&2
    else
        case $? in
            2) echo "WARNING: $wanted has no checksum off-site (made before checksums" >&2
               echo "         were written); it cannot be checked before the restore." >&2 ;;
            *) echo "REFUSED: $wanted does not match its checksum - it was damaged or" >&2
               echo "         changed after it was made. It has been removed." >&2
               rm -f "$DIR/$wanted" "$DIR/$wanted.sha256"
               exit 1 ;;
        esac
    fi
    echo "$DIR/$wanted"
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
    have_key || return 1
    failed=0
    dump_one aims_recordings || failed=1
    dump_one aims_clinical   || failed=1
    sweep_old
    if [ "$failed" -eq 0 ]; then
        stamp_file "$DIR/.last_success"
    fi
    offsite_push || failed=1

    if [ "$failed" -eq 0 ]; then
        held=$(ls "$DIR"/aims_*.sql.gz "$DIR"/aims_*.sql.gz.enc 2>/dev/null | wc -l | tr -d ' ')
        log "done; $held backup file(s) held"
        return 0
    fi
    log "FINISHED WITH FAILURES - a database was not backed up tonight"
    return 1
}

# The service's entrypoint is this script, so the other jobs come in through it:
#   docker compose run --rm backup --now                      one backup, then exit
#   docker compose run --rm backup --offsite                  copy what is not off-site yet
#   docker compose run --rm backup list-offsite               what the off-site bucket holds
#   docker compose run --rm backup fetch latest aims_clinical bring one back for restore
#   docker compose run --rm backup restore <file> <database>  restore.sh
case "${1:-}" in
    --now)
        mkdir -p "$DIR"
        private_dir
        run_once
        exit $?
        ;;
    --offsite)
        mkdir -p "$DIR"
        offsite_push
        exit $?
        ;;
    list-offsite)
        offsite_enabled || { echo "No off-site copy is configured."; exit 1; }
        rclone lsl "$(remote)"
        exit $?
        ;;
    fetch)
        shift
        fetch "$@"
        exit $?
        ;;
    restore|/opt/aims/restore.sh)
        shift
        exec /bin/sh /opt/aims/restore.sh "$@"
        ;;
    "")
        ;;
    *)
        echo "usage: backup.sh [--now | --offsite | list-offsite | fetch <name|latest> [db]"
        echo "                  | restore <backup file> <target database>]"
        exit 2
        ;;
esac

have_key || exit 1
if ! command -v openssl >/dev/null 2>&1; then
    log "FAILED: this image has no openssl - build the backup image"
    log "        (deploy/uiu/backup/Dockerfile) instead of using postgres:16-alpine"
    exit 1
fi

mkdir -p "$DIR"
private_dir
log "started; nightly at $AT, keeping $KEEP days, encrypted, off-site: $(offsite_enabled && echo "${AIMS_OFFSITE_BUCKET} ($OFFSITE_KEEP days)" || echo NONE)"

while true; do
    wait_for=$(seconds_until)
    log "next backup in $((wait_for / 3600))h $(((wait_for % 3600) / 60))m"
    sleep "$wait_for"
    run_once || true
    # Past the minute, so a fast dump cannot schedule the same night twice.
    sleep 61
done
