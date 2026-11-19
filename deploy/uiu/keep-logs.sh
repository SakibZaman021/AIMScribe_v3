#!/usr/bin/env bash
# ================================================================
# Keep every server program's log, so an update does not take it away.
#
# Docker keeps a program's log only as long as its container: each update
# replaces the containers, and their logs go with them. This copies what is
# new since the last time - every program, with Docker's timestamps - into
# one file per day (UTC) that outlives every update:
#
#     /home/aims/.aimscribe-deploy/logs/2026-11-16.log      today, growing
#     /home/aims/.aimscribe-deploy/logs/2026-11-15.log.gz   earlier days
#
# The deployer runs it every few minutes and once more just before an
# update replaces the containers, so their last lines are kept too.
# Read them with ./server-logs.sh. Days older than AIMS_LOG_KEEP_DAYS (180)
# go, and the oldest go first if the folder passes AIMS_LOG_KEEP_MB (5000):
# these are the programs' logs, not data - the database keeps every record.
# ================================================================
set -uo pipefail

STACK="$(cd "$(dirname "$0")" && pwd)"
STATE="${AIMS_DEPLOY_STATE:-/home/aims/.aimscribe-deploy}"
LOGS="${AIMS_LOG_DIR:-$STATE/logs}"
KEEP_DAYS="${AIMS_LOG_KEEP_DAYS:-180}"
KEEP_MB="${AIMS_LOG_KEEP_MB:-5000}"

mkdir -p "$LOGS" && chmod 700 "$LOGS" 2>/dev/null || true

now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
since=$(cat "$LOGS/.since" 2>/dev/null || true)
# The first time: what the running containers still hold.
[ -n "$since" ] || since=$(date -u -d '-3 days' +%Y-%m-%dT%H:%M:%SZ)

# Each line goes to the file of its own day, by Docker's timestamp on it.
(cd "$STACK" && docker compose logs --no-color --timestamps --since "$since" --until "$now") \
    2>/dev/null \
  | awk -v dir="$LOGS" '
      {
        if (match($0, /[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:/))
          day = substr($0, RSTART, 10)
        else if (day == "")
          day = "undated"
        file = dir "/" day ".log"
        print >> file
      }'
status=${PIPESTATUS[0]}
if [ "$status" = 0 ]; then
    echo "$now" > "$LOGS/.since"
else
    echo "keep-logs: docker compose logs failed ($status); tried again next time" >&2
fi

# Days before yesterday: compressed.
yesterday=$(date -u -d '-1 day' +%Y-%m-%d)
for f in "$LOGS"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].log; do
    [ -e "$f" ] || continue
    day=$(basename "$f" .log)
    if [[ "$day" < "$yesterday" ]]; then
        gzip -f "$f"
    fi
done

# Older than AIMS_LOG_KEEP_DAYS, then the oldest while the folder is too big.
oldest_kept=$(date -u -d "-$KEEP_DAYS days" +%Y-%m-%d)
for f in "$LOGS"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].log*; do
    [ -e "$f" ] || continue
    day=$(basename "$f"); day=${day:0:10}
    if [[ "$day" < "$oldest_kept" ]]; then
        rm -f "$f"
    fi
done
while [ "$(du -sm "$LOGS" 2>/dev/null | cut -f1)" -gt "$KEEP_MB" ]; do
    first=$(ls -1 "$LOGS"/[0-9]*.log.gz 2>/dev/null | head -n 1)
    [ -n "$first" ] || break
    rm -f "$first"
done
exit 0
