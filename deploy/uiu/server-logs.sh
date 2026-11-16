#!/usr/bin/env bash
# ================================================================
# Read the server's logs - every program, across every update.
#
#     ./server-logs.sh                     every program, the last hour
#     ./server-logs.sh api 2h              the API, the last two hours
#     ./server-logs.sh api 3d error        the API, three days, lines with "error"
#     ./server-logs.sh gateway today device/register
#     ./server-logs.sh all 2026-11-16      one whole day (Dhaka time)
#     ./server-logs.sh api 30m -f          the last 30 minutes, then follow live
#
# Programs: api, gateway, worker, archive-worker, monitor, backup, postgres,
# pgbouncer, redis, dbviewer - or all. Period: 30m, 2h, 3d, today, or a day.
# The time on each line is UTC (Dhaka is UTC+6).
#
# The lines come from what keep-logs.sh has kept (it is run first, so they
# are up to the second), so an update in the middle does not hide anything.
# ================================================================
set -uo pipefail

STACK="$(cd "$(dirname "$0")" && pwd)"
STATE="${AIMS_DEPLOY_STATE:-/home/aims/.aimscribe-deploy}"
LOGS="${AIMS_LOG_DIR:-$STATE/logs}"

service="${1:-all}"
period="${2:-1h}"
word="${3:-}"
follow=""
for arg in "$@"; do [ "$arg" = "-f" ] && follow=1; done
[ "$word" = "-f" ] && word=""

case "$period" in
    *m)     from=$(date -u -d "-${period%m} minutes" +%Y-%m-%dT%H:%M:%S); to="9999" ;;
    *h)     from=$(date -u -d "-${period%h} hours" +%Y-%m-%dT%H:%M:%S); to="9999" ;;
    *d)     from=$(date -u -d "-${period%d} days" +%Y-%m-%dT%H:%M:%S); to="9999" ;;
    today)  from=$(date -u -d "$(date -u -d '+6 hours' +%Y-%m-%d) 00:00 +0600" +%Y-%m-%dT%H:%M:%S)
            to="9999" ;;
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9])
            from=$(date -u -d "$period 00:00 +0600" +%Y-%m-%dT%H:%M:%S)
            to=$(date -u -d "$period 00:00 +0600 +1 day" +%Y-%m-%dT%H:%M:%S) ;;
    *)      echo "period: 30m, 2h, 3d, today or YYYY-MM-DD (got '$period')" >&2; exit 2 ;;
esac

# Bring the kept logs up to now (needs docker; without it, what is kept).
bash "$STACK/keep-logs.sh" >/dev/null 2>&1 || true

first_day=${from:0:10}
files=()
for f in "$LOGS"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].log.gz \
         "$LOGS"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].log; do
    [ -e "$f" ] || continue
    day=$(basename "$f"); day=${day:0:10}
    [[ "$day" < "$first_day" ]] || files+=("$f")
done
if [ "${#files[@]}" = 0 ]; then
    echo "No kept log for that time in $LOGS yet." >&2
    exit 1
fi

pick() {
    awk -v svc="$service" -v from="$from" -v to="$to" '
        {
          name = $1
          if (svc != "all" && name != "aims-" svc && name != svc && name != svc "-1" \
              && name != "aimscribe-" svc "-1") next
          if (match($0, /[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9:.]+Z?/)) {
            t = substr($0, RSTART, 19)
            if (t < from || t >= to) next
          }
          print
        }'
}

# Each day's file in order: the compressed ones first, then today's.
printf '%s\n' "${files[@]}" | sort | while read -r f; do zcat -f "$f"; done \
  | pick | { if [ -n "$word" ]; then grep -i -- "$word"; else cat; fi; }

if [ -n "$follow" ]; then
    echo "---- following live (Ctrl+C to stop) ----" >&2
    if [ "$service" = all ]; then
        (cd "$STACK" && docker compose logs -f --no-color --timestamps --since 1s)
    else
        (cd "$STACK" && docker compose logs -f --no-color --timestamps --since 1s "$service")
    fi
fi
