#!/bin/sh
# ================================================================
# Rebuild the database server as it was at a moment, and the monthly drill
# that proves it can be done.
#
#   docker compose run --rm backup pitr recover <name> ["2026-10-25 14:30"]
#       The base backup taken before that moment (clinic time; omitted: the
#       newest), then every archived WAL file up to it, into
#       $AIMS_BACKUP_DIR/restore/<name>. Checked by starting it, counting
#       its tables, and stopping it. The live database is never touched:
#       putting a restored copy in its place is a deliberate step, written
#       out in DISASTER_RECOVERY.md.
#
#   docker compose run --rm backup pitr drill
#       What the backup job does by itself every AIMS_DRILL_EVERY_DAYS: a
#       recovery to now from the off-site copy alone, and a restore of last
#       night's dumps, each compared with the live databases, then removed.
#       The result goes in .last_drill, which the monitor reads.
#
# Runs in the backup image: PostgreSQL 16, the same major version as the
# server, so the recovered copy can be started here.
# ================================================================
set -eu
set -o pipefail
umask 077

DIR="${AIMS_BACKUP_DIR:-/backups}"
PORT="${AIMS_RESTORE_PORT:-5499}"
WAIT="${AIMS_RESTORE_WAIT_SECONDS:-3600}"
PG_UID="${AIMS_PG_UID:-70}"
SUPER="${PGUSER:-aims_admin}"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') PITR $*"; }
die() { log "FAILED: $*"; exit 1; }

base_remote() { echo "offsite:${AIMS_OFFSITE_BUCKET}/${AIMS_BASE_PREFIX:-base}"; }
offsite_enabled() { [ -n "${AIMS_OFFSITE_BUCKET:-}" ]; }

# "2026-10-25 14:30" -> 20261025_1430, the form base backups are named in.
stamp_of() {
    echo "$1" | sed -n 's/^\([0-9]\{4\}\)-\([0-9]\{2\}\)-\([0-9]\{2\}\)[ T]\([0-9]\{2\}\):\([0-9]\{2\}\).*/\1\2\3_\4\5/p'
}

# The newest base backup named at or before the stamp, from this server or
# (AIMS_RESTORE_FROM=offsite, or none here) the bucket. Prints its name.
pick_base() {
    limit="$1"
    {
        if [ "${AIMS_RESTORE_FROM:-auto}" != offsite ]; then
            ls "$DIR/base" 2>/dev/null | grep '^base_.*\.tar\.gz\.enc$' || true
        fi
        if offsite_enabled; then
            rclone lsf "$(base_remote)" 2>/dev/null | grep '^base_.*\.tar\.gz\.enc$' || true
        fi
    } | sort -u | while read -r name; do
        # 20261025_1430 -> 202610251430: compared as a number.
        stamp=$(echo "$name" | sed -n 's/^base_\([0-9]\{8\}\)_\([0-9]\{4\}\).*/\1\2/p')
        if [ -n "$stamp" ] && [ "$stamp" -le "$(echo "$limit" | tr -d _)" ]; then
            echo "$name"
        fi
    done | tail -1
}

# The base backup, here or fetched, with its checksum checked. Prints its path.
get_base() {
    name="$1"; work="$2"
    if [ "${AIMS_RESTORE_FROM:-auto}" != offsite ] && [ -f "$DIR/base/$name" ]; then
        (cd "$DIR/base" && sha256sum -c "$name.sha256" >/dev/null 2>&1) \
            || die "$name does not match its checksum"
        echo "$DIR/base/$name"
        return
    fi
    offsite_enabled || die "$name is not on this server and no off-site bucket is set"
    rclone copyto "$(base_remote)/$name" "$work/$name" || die "could not fetch $name"
    rclone copyto "$(base_remote)/$name.sha256" "$work/$name.sha256" \
        || die "$name has no checksum off-site"
    (cd "$work" && sha256sum -c "$name.sha256" >/dev/null 2>&1) \
        || die "$name from the bucket does not match its checksum"
    echo "$work/$name"
}

psql_at() {  # psql_at <port|live> <database> <sql>
    if [ "$1" = live ]; then
        psql -X -q -At -v ON_ERROR_STOP=1 --dbname "$2" -c "$3"
    else
        psql -X -q -At -v ON_ERROR_STOP=1 -h 127.0.0.1 -p "$1" -U "$SUPER" --dbname "$2" -c "$3"
    fi
}

# Row counts of every table, one "table count" a line.
counts() {
    psql_at "$1" "$2" "
        SELECT table_name || ' ' || (xpath('/row/c/text()', query_to_xml(
                 'SELECT count(*) AS c FROM ' || quote_ident(table_name), false, true, '')))[1]::text
          FROM information_schema.tables
         WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
         ORDER BY table_name"
}

# compare <label> <live counts file> <restored counts file> <slack %>
# Every live table is there, and each count is within the slack of the live
# one (rows written while the copy was being made, or since the dump).
compare() {
    label="$1"; live="$2"; restored="$3"; slack="$4"
    problems=""
    while read -r table n; do
        got=$(grep "^$table " "$restored" | cut -d' ' -f2)
        if [ -z "$got" ]; then
            problems="$problems $label.$table:missing"
            continue
        fi
        allowed=$(( n * slack / 100 ))
        [ "$allowed" -ge 10 ] || allowed=10
        diff=$(( n - got )); [ "$diff" -ge 0 ] || diff=$(( -diff ))
        [ "$diff" -le "$allowed" ] || problems="$problems $label.$table:$got/$n"
    done < "$live"
    echo "$problems"
}

recover() {
    name="${1:?usage: pitr recover <name> [\"YYYY-MM-DD HH:MM\"]}"
    when="${2:-}"
    case "$name" in ''|*[!a-z0-9_-]*) die "a restore name is a-z 0-9 _ -" ;; esac
    dest="$DIR/restore/$name"
    [ ! -e "$dest" ] || [ -z "$(ls -A "$dest" 2>/dev/null)" ] || die "$dest already exists"
    [ -n "${AIMS_BACKUP_KEY:-}" ] || die "AIMS_BACKUP_KEY is needed to decrypt the backups"

    if [ -n "$when" ]; then
        limit=$(stamp_of "$when")
        [ -n "$limit" ] || die "give the moment as \"YYYY-MM-DD HH:MM\" (clinic time)"
    else
        limit=99999999_9999
    fi
    base=$(pick_base "$limit")
    [ -n "$base" ] || die "no base backup from before ${when:-now}"
    log "base backup $base, then the archived WAL up to ${when:-the newest}"

    work=$(mktemp -d)
    file=$(get_base "$base" "$work")
    # The recovered server runs as postgres (uid 70): it must be able to pass
    # through to its folder, though not to list what else is here.
    mkdir -p "$dest"
    chmod 711 "$DIR" "$DIR/restore"
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:AIMS_BACKUP_KEY -in "$file" \
        | gunzip -c | tar -x -C "$dest" || { rm -rf "$work"; die "could not unpack $base"; }
    rm -rf "$work"
    rm -f "$dest/postmaster.pid"
    mkdir -p "$dest/pg_wal"
    {
        echo "# Written by pitr.sh for a recovery; this copy archives nothing."
        echo "restore_command = 'AIMS_RESTORE_FROM=${AIMS_RESTORE_FROM:-auto} /opt/aims/wal-fetch.sh %f %p'"
        echo "recovery_target_timeline = 'latest'"
        echo "recovery_target_action = 'promote'"
        if [ -n "$when" ]; then
            echo "recovery_target_time = '$(echo "$when" | cut -c1-16):00$(date +%z)'"
        fi
        echo "archive_mode = 'off'"
    } >> "$dest/postgresql.auto.conf"
    : > "$dest/recovery.signal"
    chown -R "$PG_UID:$PG_UID" "$dest"
    chmod 700 "$dest"

    # A copy in recovery must allow at least what the original did (connections,
    # workers, locks...), or PostgreSQL stops the replay. The original's values
    # are in the backup's control file: use exactly those.
    limits=$(su-exec postgres pg_controldata "$dest" | sed -n \
        -e 's/^max_connections setting: *\([0-9]*\)$/-c max_connections=\1/p' \
        -e 's/^max_worker_processes setting: *\([0-9]*\)$/-c max_worker_processes=\1/p' \
        -e 's/^max_wal_senders setting: *\([0-9]*\)$/-c max_wal_senders=\1/p' \
        -e 's/^max_prepared_xacts setting: *\([0-9]*\)$/-c max_prepared_transactions=\1/p' \
        -e 's/^max_locks_per_xact setting: *\([0-9]*\)$/-c max_locks_per_transaction=\1/p' \
        | tr '\n' ' ')

    log "replaying into $dest"
    started=$(date +%s)
    # The data folder's own pg_hba.conf (trust on the loopback): the copy
    # listens on 127.0.0.1 inside this container only.
    # shellcheck disable=SC2086
    su-exec postgres postgres -D "$dest" -p "$PORT" -c listen_addresses=127.0.0.1 \
        -c unix_socket_directories=/tmp -c hba_file="$dest/pg_hba.conf" \
        -c archive_mode=off -c shared_buffers=128MB $limits \
        > "$dest.log" 2>&1 &
    pid=$!
    ready=""
    while [ $(( $(date +%s) - started )) -lt "$WAIT" ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            tail -20 "$dest.log"
            die "the recovered copy stopped during replay (log above)"
        fi
        state=$(psql_at "$PORT" postgres "SELECT pg_is_in_recovery()" 2>/dev/null || true)
        if [ "$state" = f ]; then ready=1; break; fi
        sleep 2
    done
    [ -n "$ready" ] || { stop_copy "$dest"; die "replay did not finish in ${WAIT}s"; }
    took=$(( $(date +%s) - started ))
    log "recovered in ${took}s; it is a working database server"
    echo "$took" > "$dest.seconds"
}

stop_copy() {
    su-exec postgres pg_ctl -D "$1" -m fast -w stop >/dev/null 2>&1 || true
}

show() {  # what a recovered copy holds, while it is running
    for db in aims_recordings aims_clinical; do
        echo "== $db"
        counts "$PORT" "$db" | sort -k2 -n -r | head -12
    done
    echo "== last recording opened: $(psql_at "$PORT" aims_recordings "SELECT max(opened_at) FROM sessions")"
}

stamp_drill() {  # stamp_drill <ok|failed> <seconds> <message>
    {
        echo "at=$(date '+%Y-%m-%dT%H:%M:%S%z')"
        echo "status=$1"
        echo "seconds=$2"
        echo "message=$3"
    } > "$DIR/.last_drill"
    chmod 644 "$DIR/.last_drill"
}

drill() {
    started=$(date +%s)
    name="drill_$(date '+%Y%m%d_%H%M')"
    if offsite_enabled; then
        export AIMS_RESTORE_FROM=offsite     # the bucket alone must be enough
    fi
    trap 'stop_copy "$DIR/restore/$name"; rm -rf "$DIR/restore/$name" "$DIR/restore/$name.log" "$DIR/restore/$name.seconds"' EXIT
    message=""

    # 1. To now, from the base backup and the WAL - from the bucket.
    if ( recover "$name" ) > "$DIR/.drill.log" 2>&1; then
        for db in aims_recordings aims_clinical; do
            counts live "$db" > "$DIR/.drill.live.$db"
            counts "$PORT" "$db" > "$DIR/.drill.copy.$db"
            message="$message$(compare "pitr" "$DIR/.drill.live.$db" "$DIR/.drill.copy.$db" 2)"
        done
        replay=$(cat "$DIR/restore/$name.seconds" 2>/dev/null || echo "?")
        stop_copy "$DIR/restore/$name"
    else
        message="$message pitr:$(tail -1 "$DIR/.drill.log" | cut -c1-200)"
        replay="?"
    fi

    # 2. Last night's dumps, into scratch databases on the live server.
    for db in aims_recordings aims_clinical; do
        dump=$(ls -t "$DIR"/${db}_*.sql.gz.enc 2>/dev/null | head -1)
        if [ -z "$dump" ]; then
            message="$message dump.$db:none"
            continue
        fi
        scratch="aims_drill_${db#aims_}"
        psql -X -q --dbname postgres -c "DROP DATABASE IF EXISTS $scratch" >/dev/null 2>&1 || true
        if sh /opt/aims/restore.sh "$dump" "$scratch" >> "$DIR/.drill.log" 2>&1; then
            counts live "$db" | cut -d' ' -f1 | sort > "$DIR/.drill.tables.live"
            counts live "$scratch" | cut -d' ' -f1 | sort > "$DIR/.drill.tables.dump"
            cmp -s "$DIR/.drill.tables.live" "$DIR/.drill.tables.dump" \
                || message="$message dump.$db:tables-differ"
        else
            message="$message dump.$db:restore-failed"
        fi
        psql -X -q --dbname postgres -c "DROP DATABASE IF EXISTS $scratch" >/dev/null 2>&1 || true
    done
    rm -f "$DIR"/.drill.live.* "$DIR"/.drill.copy.* "$DIR"/.drill.tables.*

    took=$(( $(date +%s) - started ))
    if [ -z "$message" ]; then
        stamp_drill ok "$took" "recovered to now from $( [ "${AIMS_RESTORE_FROM:-}" = offsite ] && echo 'the off-site copy' || echo 'this server') in ${replay}s; both dumps restored"
        log "drill passed in ${took}s"
        return 0
    fi
    stamp_drill failed "$took" "$message"
    log "DRILL FAILED:$message (details: $DIR/.drill.log)"
    return 1
}

case "${1:-}" in
    recover)
        shift
        recover "$@"
        show
        stop_copy "$DIR/restore/$1"
        # The recovery's own settings go, so the folder can become the live
        # database as it is: left in, "archive_mode = off" would silently
        # switch off the archive for good (postgresql.auto.conf wins over the
        # profile).
        auto="$DIR/restore/$1/postgresql.auto.conf"
        sed -i '/^# Written by pitr.sh for a recovery/,$d' "$auto"
        rm -f "$DIR/restore/$1/recovery.signal"
        chown "$PG_UID:$PG_UID" "$auto"
        log "stopped. The recovered data folder is $DIR/restore/$1, ready to be the live"
        log "one: deploy/uiu/DISASTER_RECOVERY.md, \"Put a recovered copy in place\"."
        ;;
    drill)
        drill
        ;;
    *)
        echo "usage: pitr.sh recover <name> [\"YYYY-MM-DD HH:MM\"] | drill"
        exit 2
        ;;
esac
