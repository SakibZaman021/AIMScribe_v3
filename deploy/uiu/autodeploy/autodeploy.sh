#!/usr/bin/env bash
# ================================================================
# Deploy whatever is new on GitHub main - safely, or not at all.
#
# Run every two minutes by aimscribe-autodeploy.timer, as the aims user.
# The server pulls; GitHub is never given a way into the server, so the
# public repository holds no key that could reach it.
#
# One run:
#   1. fetch main; nothing new -> stop (this is almost every run)
#   2. refuse, with an alert, when
#        - main was rewritten instead of added to (a force-push),
#        - a tracked file on the server was edited by hand,
#        - AIMS_DEPLOY_REQUIRE_SIGNED=true and the commit is not signed
#          by a key in git's allowed signers
#   3. wait for the commit's tests on GitHub (the "tests" workflow): still
#      running -> try again next run; failed -> not deployed, alert; passed
#      -> on. AIMS_DEPLOY_REQUIRE_CI=false turns this off, and a file
#      $STATE/ci-skip-<commit> lets one commit through by hand.
#   4. check out the new commit, build the images (the running ones keep
#      serving meanwhile); when it changes the database (backend/scripts or
#      deploy/uiu/postgres), take a labelled backup of both databases first
#      - no backup, no deploy; then apply the new migrations
#      (postgres/migrate.sh), restart what changed, and wait for
#      /health/ready
#   5. healthy -> done, alert "deployed"; not healthy -> check out the
#      previous commit, rebuild, restart, alert "rolled back"
#
# Alerts go to the webhook and, when set (set-alerts.sh), by email.
#
# A commit that failed is not tried again: the next commit is. Migrations
# only go forward - a rollback runs the previous code on the newer schema,
# which is why a migration only ever adds (see migrate.sh, and the check in
# tests/test_deployment.py that refuses one that drops).
#
# The whole script is one function, called on the last line: bash has read
# all of it before step 4 replaces this file on disk.
# ================================================================
set -euo pipefail

REPO="${AIMS_DEPLOY_DIR:-/home/aims/AIMScribe_v3}"
BRANCH="${AIMS_DEPLOY_BRANCH:-main}"
STATE="${AIMS_DEPLOY_STATE:-/home/aims/.aimscribe-deploy}"
HEALTH_URL="${AIMS_DEPLOY_HEALTH_URL:-http://127.0.0.1:6001/health/ready}"
HEALTH_WAIT="${AIMS_DEPLOY_HEALTH_WAIT:-300}"
REQUIRE_SIGNED="${AIMS_DEPLOY_REQUIRE_SIGNED:-false}"
REQUIRE_CI="${AIMS_DEPLOY_REQUIRE_CI:-true}"
CI_WORKFLOW="${AIMS_DEPLOY_CI_WORKFLOW:-tests}"
CI_ALERT_MINUTES="${AIMS_DEPLOY_CI_ALERT_MINUTES:-60}"
GITHUB_API="${AIMS_DEPLOY_GITHUB_API:-https://api.github.com}"
STACK="$REPO/deploy/uiu"
# What, changed, means the database changes with this commit.
SCHEMA_PATHS=(backend/scripts deploy/uiu/postgres)

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

setting() {  # one value from the stack's .env, by name
    sed -n "s/^$1=//p" "$STACK/.env" 2>/dev/null | tail -n 1 | tr -d "\"'\r"
}

# To the monitor's alert webhook and, when set, by email - both read from the
# stack's .env by name, so no key is ever in this file or the repository.
notify() {
    local webhook text key to
    text=$(printf 'AIMScribe deploy: %s' "$1" | tr -d '"\\' | tr '\n' ' ')
    webhook=$(setting AIMS_ALERT_WEBHOOK)
    if [ -n "$webhook" ]; then
        curl -fsS -m 15 -o /dev/null -H 'Content-Type: application/json' \
             -d "{\"text\": \"$text\", \"check\": \"deploy\", \"status\": \"$2\"}" \
             "$webhook" || log "the alert webhook did not answer"
    fi
    key=$(setting AIMS_RESEND_API_KEY)
    to=$(setting AIMS_ALERT_EMAIL)
    if [ -n "$key" ] && [ -n "$to" ]; then
        curl -fsS -m 20 -o /dev/null https://api.resend.com/emails \
             -H "Authorization: Bearer $key" -H 'Content-Type: application/json' \
             -d "{\"from\": \"AIMScribe server <onboarding@resend.dev>\", \"to\": [\"$to\"], \"subject\": \"[AIMScribe] deploy $2: ${text:18:120}\", \"text\": \"$text\"}" \
             || log "the alert email was not accepted"
    fi
}

compose() { (cd "$STACK" && docker compose "$@"); }

healthy() {
    local deadline=$((SECONDS + HEALTH_WAIT))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if curl -fsS -m 5 -o /dev/null "$HEALTH_URL"; then
            return 0
        fi
        sleep 5
    done
    return 1
}

# owner/name of the repository on GitHub, from the server's origin.
github_repo() {
    if [ -n "${AIMS_DEPLOY_GITHUB_REPO:-}" ]; then
        echo "$AIMS_DEPLOY_GITHUB_REPO"
        return
    fi
    git -C "$REPO" remote get-url origin \
        | sed -n 's#.*github\.com[:/]\([^/]*/[^/]*\)$#\1#p' | sed 's#\.git$##'
}

# The newest run of the tests workflow for one commit: success, failure or
# pending (not started, running, or GitHub not answering - all mean wait).
ci_state() {
    local repo answer
    repo=$(github_repo)
    [ -n "$repo" ] || { echo pending; return; }
    answer=$(curl -fsS -m 20 -H 'Accept: application/vnd.github+json' \
             "$GITHUB_API/repos/$repo/actions/runs?head_sha=$1&per_page=30" 2>/dev/null) \
        || { echo pending; return; }
    printf '%s' "$answer" | python3 -c '
import json, sys
try:
    runs = [r for r in json.load(sys.stdin).get("workflow_runs", []) if r.get("name") == sys.argv[1]]
except ValueError:
    runs = []
if not runs:
    print("pending")
else:
    run = max(runs, key=lambda r: (r.get("run_number", 0), r.get("run_attempt", 0)))
    if run.get("status") != "completed":
        print("pending")
    else:
        print("success" if run.get("conclusion") == "success" else "failure")
' "$CI_WORKFLOW"
}

# release <commit> [backup label]: that commit, built, migrated, running and
# healthy. With a label, both databases are backed up before the migrations,
# and a failed backup stops the release.
BACKUP_FAILED=""
release() {
    git -C "$REPO" -c advice.detachedHead=false checkout --quiet --detach "$1" || return 1
    compose build --quiet || return 1
    compose up -d --wait postgres || return 1
    if [ -n "${2:-}" ]; then
        log "backing up both databases before the database changes ($2)"
        compose run --rm -T backup --now "$2" < /dev/null || { BACKUP_FAILED=1; return 1; }
    fi
    compose exec -T postgres sh -s < "$STACK/postgres/migrate.sh" || return 1
    compose up -d --remove-orphans || return 1
    healthy
}

main() {
    mkdir -p "$STATE"
    exec 9>"$STATE/lock"
    flock -n 9 || exit 0

    git -C "$REPO" fetch --quiet origin "$BRANCH"
    local current target short subject
    current=$(git -C "$REPO" rev-parse HEAD)
    target=$(git -C "$REPO" rev-parse "origin/$BRANCH")
    [ "$current" != "$target" ] || exit 0
    # Tried and rolled back already: wait for the next commit.
    [ "$(cat "$STATE/failed" 2>/dev/null || true)" != "$target" ] || exit 0

    short=${target:0:7}
    subject=$(git -C "$REPO" log -1 --format=%s "$target")

    refuse() {
        log "not deploying $short: $1"
        echo "$target" > "$STATE/failed"
        notify "not deploying $short ($subject): $1" critical
        exit 1
    }

    git -C "$REPO" merge-base --is-ancestor "$current" "$target" \
        || refuse "main was rewritten rather than added to; check why, then deploy it by hand"
    [ -z "$(git -C "$REPO" status --porcelain --untracked-files=no)" ] \
        || refuse "files on the server were edited by hand (git status shows them)"
    if [ "$REQUIRE_SIGNED" = true ]; then
        git -C "$REPO" verify-commit "$target" >/dev/null 2>&1 \
            || refuse "the commit is not signed by an allowed key"
    fi

    # Only what passed its tests on GitHub. A failure is not remembered as
    # "failed": a re-run on GitHub that passes is deployed on the next run.
    if [ "$REQUIRE_CI" = true ] && [ ! -e "$STATE/ci-skip-$target" ]; then
        local ci waited
        ci=$(ci_state "$target")
        case "$ci" in
            success)
                log "tests passed on GitHub for $short" ;;
            failure)
                if [ ! -e "$STATE/ci-failed-$target" ]; then
                    touch "$STATE/ci-failed-$target"
                    notify "not deploying $short ($subject): its tests failed on GitHub" critical
                fi
                log "not deploying $short: its tests failed on GitHub"
                exit 1 ;;
            *)
                [ -e "$STATE/ci-wait-$target" ] || date +%s > "$STATE/ci-wait-$target"
                waited=$(( ($(date +%s) - $(cat "$STATE/ci-wait-$target")) / 60 ))
                if [ "$waited" -ge "$CI_ALERT_MINUTES" ] && [ ! -e "$STATE/ci-wait-$target.told" ]; then
                    touch "$STATE/ci-wait-$target.told"
                    notify "$short ($subject) has waited $waited min for its tests on GitHub; not deployed yet" warning
                fi
                log "waiting for the tests on GitHub for $short"
                exit 0 ;;
        esac
    fi

    local label=""
    if ! git -C "$REPO" diff --quiet "$current" "$target" -- "${SCHEMA_PATHS[@]}"; then
        label="predeploy-$short"
    fi

    log "deploying $short: $subject"
    if release "$target" "$label"; then
        rm -f "$STATE/failed" "$STATE"/ci-wait-* "$STATE"/ci-failed-* "$STATE"/ci-skip-*
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $target $subject" >> "$STATE/deployed.log"
        docker image prune -f >/dev/null 2>&1 || true
        log "deployed $short"
        notify "deployed $short: $subject${label:+ (databases backed up first)}" ok
        exit 0
    fi

    log "deploying $short failed; going back to ${current:0:7}"
    echo "$target" > "$STATE/failed"
    local why="did not come up healthy"
    [ -z "$BACKUP_FAILED" ] || why="changes the database, and the backup before it failed"
    if release "$current"; then
        notify "$short ($subject) $why, so it was rolled back; the server runs ${current:0:7}" critical
    else
        notify "$short failed ($why) and going back to ${current:0:7} is not healthy either; look at the server now" critical
    fi
    exit 1
}

main "$@"
