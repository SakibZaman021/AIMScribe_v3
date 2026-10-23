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
#   3. check out the new commit, build the images (the running ones keep
#      serving meanwhile), apply new migrations (postgres/migrate.sh),
#      restart what changed, and wait for /health/ready
#   4. healthy -> done, alert "deployed"; not healthy -> check out the
#      previous commit, rebuild, restart, alert "rolled back"
#
# A commit that failed is not tried again: the next commit is. Migrations
# only go forward - a rollback runs the previous code on the newer schema,
# which is why a migration only ever adds (see migrate.sh).
#
# The whole script is one function, called on the last line: bash has read
# all of it before step 3 replaces this file on disk.
# ================================================================
set -euo pipefail

REPO="${AIMS_DEPLOY_DIR:-/home/aims/AIMScribe_v3}"
BRANCH="${AIMS_DEPLOY_BRANCH:-main}"
STATE="${AIMS_DEPLOY_STATE:-/home/aims/.aimscribe-deploy}"
HEALTH_URL="${AIMS_DEPLOY_HEALTH_URL:-http://127.0.0.1:6001/health/ready}"
HEALTH_WAIT="${AIMS_DEPLOY_HEALTH_WAIT:-300}"
REQUIRE_SIGNED="${AIMS_DEPLOY_REQUIRE_SIGNED:-false}"
STACK="$REPO/deploy/uiu"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# To the monitor's alert webhook, read from the stack's .env by name.
notify() {
    local webhook text
    webhook=$(sed -n 's/^AIMS_ALERT_WEBHOOK=//p' "$STACK/.env" 2>/dev/null \
              | tail -n 1 | tr -d "\"'\r")
    [ -n "$webhook" ] || return 0
    text=$(printf 'AIMScribe deploy: %s' "$1" | tr -d '"\\' | tr '\n' ' ')
    curl -fsS -m 15 -o /dev/null -H 'Content-Type: application/json' \
         -d "{\"text\": \"$text\", \"check\": \"deploy\", \"status\": \"$2\"}" \
         "$webhook" || log "the alert webhook did not answer"
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

# release <commit>: that commit, built, migrated, running and healthy.
release() {
    git -C "$REPO" -c advice.detachedHead=false checkout --quiet --detach "$1" || return 1
    compose build --quiet || return 1
    compose up -d --wait postgres || return 1
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

    log "deploying $short: $subject"
    if release "$target"; then
        rm -f "$STATE/failed"
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $target $subject" >> "$STATE/deployed.log"
        docker image prune -f >/dev/null 2>&1 || true
        log "deployed $short"
        notify "deployed $short: $subject" ok
        exit 0
    fi

    log "deploying $short failed; going back to ${current:0:7}"
    echo "$target" > "$STATE/failed"
    if release "$current"; then
        notify "$short ($subject) did not come up healthy and was rolled back; the server runs ${current:0:7}" critical
    else
        notify "$short failed and the rollback to ${current:0:7} is not healthy either; look at the server now" critical
    fi
    exit 1
}

main "$@"
