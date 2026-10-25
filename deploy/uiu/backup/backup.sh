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
# Recovery to any minute: PostgreSQL puts each filled WAL segment in WAL_DIR
# (archive_command); this job encrypts it and sends it off-site within a
# minute, then keeps it a while in WAL_SENT for a fast restore here. With the
# nightly base backup in BASE_DIR, pitr.sh can rebuild the server as it was
# at any moment of the last AIMS_BASE_KEEP_DAYS.
WAL_DIR="$DIR/wal"
WAL_SENT="$DIR/wal-sent"
BASE_DIR="$DIR/base"
WAL_EVERY="${AIMS_WAL_SHIP_SECONDS:-60}"
WAL_LOCAL_DAYS="${AIMS_WAL_LOCAL_DAYS:-2}"
BASE_KEEP="${AIMS_BASE_KEEP_DAYS:-35}"
BASE_LOCAL="${AIMS_BASE_LOCAL_KEEP:-2}"
DRILL_EVERY_DAYS="${AIMS_DRILL_EVERY_DAYS:-30}"
PG_UID="${AIMS_PG_UID:-70}"
# A label for one backup made out of turn, such as before an update:
# aims_recordings_<stamp>_<label>.sql.gz.enc.
LABEL=""

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
    target="$DIR/${db}_${stamp}${LABEL:+_$LABEL}.sql.gz.enc"
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
    remote_size_at "$(remote)" "$1"
}

remote_size_at() {
    rclone size --json "$1/$2" 2>/dev/null | sed -n 's/.*"bytes":\([0-9]*\).*/\1/p'
}

wal_remote() { echo "offsite:${AIMS_OFFSITE_BUCKET}/${AIMS_WAL_PREFIX:-wal}"; }
base_remote() { echo "offsite:${AIMS_OFFSITE_BUCKET}/${AIMS_BASE_PREFIX:-base}"; }

encrypt() {  # encrypt <in> <out>
    openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:AIMS_BACKUP_KEY \
        -in "$1" -out "$2"
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

# ---------------------------------------------------------------- WAL archive

wal_dirs() {
    mkdir -p "$WAL_DIR" "$WAL_SENT" "$BASE_DIR"
    # PostgreSQL (uid 70 in its image) writes the segments, and reads the
    # shipped ones back during a recovery (wal-fetch.sh); nobody else can.
    chown "$PG_UID:$PG_UID" "$WAL_DIR" "$WAL_SENT" 2>/dev/null \
        || log "could not hand $WAL_DIR to postgres"
    chmod 700 "$WAL_DIR" "$WAL_SENT" "$BASE_DIR" 2>/dev/null || true
}

wal_status() {  # wal_status <shipped this pass> <error or empty>
    pending=$(ls "$WAL_DIR" 2>/dev/null | grep -c '\.gz$' || true)
    stuck=$(find "$WAL_DIR" -maxdepth 1 -name '*.gz' -mmin +30 2>/dev/null | wc -l | tr -d ' ')
    {
        echo "checked_at=$(date '+%Y-%m-%dT%H:%M:%S%z')"
        echo "pending=${pending:-0}"
        echo "stuck=${stuck:-0}"
        echo "offsite=$(offsite_enabled && echo yes || echo no)"
        echo "error=$2"
        if [ "$1" -gt 0 ] || [ ! -f "$DIR/.wal_status" ]; then
            echo "shipped_at=$(date '+%Y-%m-%dT%H:%M:%S%z')"
        else
            sed -n 's/^shipped_at=/shipped_at=/p' "$DIR/.wal_status"
        fi
    } > "$DIR/.wal_status.part"
    mv -f "$DIR/.wal_status.part" "$DIR/.wal_status"
    chmod 644 "$DIR/.wal_status"
}

# Every segment PostgreSQL has handed over, oldest first: encrypted, sent,
# its size checked, and only then moved out of the queue. The first failure
# stops the pass, so segments always leave in order.
ship_wal() {
    shipped=0
    error=""
    for name in $(ls "$WAL_DIR" 2>/dev/null | grep '\.gz$' | sort); do
        src="$WAL_DIR/$name"
        if offsite_enabled; then
            part="$WAL_SENT/.$name.enc.part"
            if ! encrypt "$src" "$part"; then
                rm -f "$part"; error="could not encrypt $name"; break
            fi
            size=$(wc -c < "$part" | tr -d ' ')
            if rclone copyto --retries 2 --low-level-retries 3 "$part" "$(wal_remote)/$name.enc" \
                    && [ "$(remote_size_at "$(wal_remote)" "$name.enc")" = "$size" ]; then
                rm -f "$part"
            else
                rm -f "$part"; error="could not send $name off-site"; break
            fi
        fi
        mv -f "$src" "$WAL_SENT/$name"
        shipped=$((shipped + 1))
    done
    # Shipped segments stay here a little while, for a fast restore. With no
    # bucket they are the only copy, so they stay as long as the base backups.
    days="$WAL_LOCAL_DAYS"
    offsite_enabled || days="$BASE_KEEP"
    find "$WAL_SENT" -maxdepth 1 -name '*.gz' -mtime "+$days" -exec rm -f {} + 2>/dev/null || true
    wal_status "$shipped" "$error"
    [ -z "$error" ] || { log "WAL: $error; $(sed -n 's/^pending=//p' "$DIR/.wal_status") waiting"; return 1; }
}

wal_loop() {
    while true; do
        ship_wal || true
        sleep "$WAL_EVERY"
    done
}

# ---------------------------------------------------------------- base backup

# The whole database server, as files, for recovery to a moment: pg_basebackup
# over a replication connection (postgres/hba/pg_hba.conf), compressed and
# encrypted in one stream like the dumps. The WAL it needs is the archive's.
base_backup() {
    stamp="$(date '+%Y%m%d_%H%M')"
    target="$BASE_DIR/base_${stamp}.tar.gz.enc"
    failed_flag="$target.failed"
    rm -f "$failed_flag"
    # A time limit: it waits for its last WAL segment to be archived, and a
    # wait that never ended would also stop every night after it.
    if ! { timeout "${AIMS_BASE_TIMEOUT_SECONDS:-7200}" pg_basebackup --pgdata=- --format=tar \
                --wal-method=none --checkpoint=fast --no-password || : > "$failed_flag"; } \
            | gzip -6 \
            | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
                -pass env:AIMS_BACKUP_KEY -out "$target.partial"; then
        log "FAILED to encrypt the base backup"
        rm -f "$target.partial" "$failed_flag"
        return 1
    fi
    if [ -f "$failed_flag" ]; then
        log "FAILED: pg_basebackup (is archive_mode on, and replication allowed? postgres/hba)"
        rm -f "$target.partial" "$failed_flag"
        return 1
    fi
    mv "$target.partial" "$target"
    checksum "$target"
    size=$(wc -c < "$target" | tr -d ' ')
    log "base backup -> $(basename "$target") ($((size / 1024)) KB)"

    if offsite_enabled; then
        name=$(basename "$target")
        if rclone copyto --retries 2 "$target.sha256" "$(base_remote)/$name.sha256" \
                && rclone copyto --retries 2 "$target" "$(base_remote)/$name" \
                && [ "$(remote_size_at "$(base_remote)" "$name")" = "$size" ]; then
            log "off-site: $name (size checked)"
        else
            log "FAILED: the base backup did not reach the off-site bucket"
            return 1
        fi
        # Older than the window goes - but only while a newer one is there,
        # so a run of failed nights never leaves no base backup at all.
        newer=$(rclone lsf --max-age "${BASE_KEEP}d" "$(base_remote)" 2>/dev/null \
                | grep -c '\.tar\.gz\.enc$' || true)
        if [ "${newer:-0}" -gt 0 ]; then
            rclone delete --min-age "${BASE_KEEP}d" "$(base_remote)" 2>/dev/null \
                || log "could not remove off-site base backups older than $BASE_KEEP days"
            rclone delete --min-age "$((BASE_KEEP + 1))d" "$(wal_remote)" 2>/dev/null \
                || log "could not remove off-site WAL older than $BASE_KEEP days"
        fi
    fi
    # Here, only the newest few.
    ls -t "$BASE_DIR"/base_*.tar.gz.enc 2>/dev/null | tail -n "+$((BASE_LOCAL + 1))" \
        | while read -r old; do rm -f "$old" "$old.sha256"; done
    stamp_file "$DIR/.last_base"
}

drill_due() {
    [ -f "$DIR/.last_drill" ] || return 0
    [ -n "$(find "$DIR/.last_drill" -mtime "+$DRILL_EVERY_DAYS" 2>/dev/null)" ]
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

run_once() {  # run_once [nightly]: the nightly run also takes the base backup
    have_key || return 1
    failed=0
    dump_one aims_recordings || failed=1
    dump_one aims_clinical   || failed=1
    sweep_old
    if [ "$failed" -eq 0 ]; then
        stamp_file "$DIR/.last_success"
    fi
    if [ "${1:-}" = nightly ]; then
        wal_dirs
        base_backup || failed=1
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
#   docker compose run --rm backup --now [label]              one backup of both, then exit
#                                                             (the deployer: --now predeploy-<commit>)
#   docker compose run --rm backup --base                     one base backup, then exit
#   docker compose run --rm backup --wal                      send the WAL waiting now
#   docker compose run --rm backup --offsite                  copy what is not off-site yet
#   docker compose run --rm backup list-offsite               what the off-site bucket holds
#   docker compose run --rm backup fetch latest aims_clinical bring one back for restore
#   docker compose run --rm backup restore <file> <database>  restore.sh
#   docker compose run --rm backup pitr recover <name> [time] pitr.sh: the server at a moment
#   docker compose run --rm backup pitr drill                 the monthly drill, now
case "${1:-}" in
    --now)
        case "${2:-}" in
            '') ;;
            *[!A-Za-z0-9-]*) echo "A label is letters, digits and -"; exit 2 ;;
            *) LABEL="$2" ;;
        esac
        mkdir -p "$DIR"
        private_dir
        run_once
        exit $?
        ;;
    --base)
        have_key || exit 1
        mkdir -p "$DIR"
        wal_dirs
        base_backup
        exit $?
        ;;
    --wal)
        have_key || exit 1
        wal_dirs
        ship_wal
        exit $?
        ;;
    pitr)
        shift
        have_key || exit 1
        exec /bin/sh /opt/aims/pitr.sh "$@"
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
        echo "usage: backup.sh [--now [label] | --base | --wal | --offsite | list-offsite"
        echo "                  | fetch <name|latest> [db] | restore <backup file> <target database>"
        echo "                  | pitr recover <name> [\"YYYY-MM-DD HH:MM\"] | pitr drill]"
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
wal_dirs
log "started; nightly at $AT, keeping $KEEP days, encrypted, off-site: $(offsite_enabled && echo "${AIMS_OFFSITE_BUCKET} ($OFFSITE_KEEP days)" || echo NONE)"
log "WAL sent every ${WAL_EVERY}s; base backups kept $BASE_KEEP days; a restore drill every $DRILL_EVERY_DAYS days"

# The WAL is sent all day, beside the nightly job. If it ever stops, the
# status file stops changing and the monitor says so.
wal_loop &

# A server that has never had a base backup cannot be recovered to a moment:
# take the first one now, not tonight.
[ -f "$DIR/.last_base" ] || base_backup || true

while true; do
    wait_for=$(seconds_until)
    log "next backup in $((wait_for / 3600))h $(((wait_for % 3600) / 60))m"
    sleep "$wait_for"
    run_once nightly || true
    if drill_due; then
        /bin/sh /opt/aims/pitr.sh drill || true
    fi
    # Past the minute, so a fast dump cannot schedule the same night twice.
    sleep 61
done
