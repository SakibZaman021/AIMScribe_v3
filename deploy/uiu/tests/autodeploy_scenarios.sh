#!/usr/bin/env bash
# ================================================================
# autodeploy.sh, run for real against a throwaway git "GitHub" and a fake
# docker and curl. Needs bash, git and flock (Linux). test_autodeploy.py
# runs it; by hand:   bash deploy/uiu/tests/autodeploy_scenarios.sh
#
# The fake docker records what it was asked to do. The fake curl answers
# the health check "not ready" while the checked-out commit holds a file
# named UNHEALTHY, and records what was sent to the alert webhook.
# ================================================================
set -euo pipefail

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/autodeploy/autodeploy.sh"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

export AIMS_DEPLOY_DIR="$WORK/server"
export AIMS_DEPLOY_STATE="$WORK/state"
export AIMS_DEPLOY_HEALTH_WAIT=1
export AIMS_DEPLOY_HEALTH_URL="http://127.0.0.1:6001/health/ready"
# Scenarios 1-6 are about the deploy itself; 7 and 8 turn the GitHub tests on.
export AIMS_DEPLOY_REQUIRE_CI=false
export AIMS_DEPLOY_GITHUB_REPO=owner/repo
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

mkdir -p "$WORK/bin"
# The fake docker fails the backup before a database change while the
# checked-out commit holds a file named BACKUP_FAILS.
cat > "$WORK/bin/docker" <<EOF
#!/usr/bin/env bash
echo "docker \$*" >> "$WORK/docker.log"
case "\$*" in *"backup --now"*) [ ! -e "$AIMS_DEPLOY_DIR/BACKUP_FAILS" ] || exit 1 ;; esac
[ "\$2" != exec ] || cat > /dev/null
EOF
# The fake curl: the health check, GitHub's answer for a commit's workflow
# runs (from $WORK/ci.json), and everything else recorded as an alert.
cat > "$WORK/bin/curl" <<EOF
#!/usr/bin/env bash
for arg in "\$@"; do last="\$arg"; done
if [ "\$last" = "$AIMS_DEPLOY_HEALTH_URL" ]; then
    [ ! -e "$AIMS_DEPLOY_DIR/UNHEALTHY" ]
    exit
fi
case "\$last" in
    *api.github.com/repos/owner/repo/actions/runs*)
        [ -f "$WORK/ci.json" ] || exit 22
        cat "$WORK/ci.json"
        exit ;;
esac
echo "\$*" >> "$WORK/alerts.log"
EOF
chmod +x "$WORK/bin/docker" "$WORK/bin/curl"
export PATH="$WORK/bin:$PATH"

fail() { echo "FAIL: $*" >&2; exit 1; }
run() { set +e; bash "$SCRIPT" > "$WORK/run.log" 2>&1; status=$?; set -e; }
head_of() { git -C "$1" rev-parse HEAD; }
commit() {   # commit <message> [file to add] [file to remove]
    [ -z "${2:-}" ] || echo x > "$WORK/dev/$2"
    [ -z "${3:-}" ] || rm "$WORK/dev/$3"
    echo "$1" >> "$WORK/dev/history"
    git -C "$WORK/dev" add -A && git -C "$WORK/dev" commit -qm "$1"
    git -C "$WORK/dev" push -q origin HEAD:main 2>/dev/null || git -C "$WORK/dev" push -qf origin HEAD:main
}

# GitHub, a developer's clone, and the server's clone.
git init -q --bare -b main "$WORK/origin.git"
git clone -q "$WORK/origin.git" "$WORK/dev" 2>/dev/null
mkdir -p "$WORK/dev/deploy/uiu/postgres"
echo "echo migrated" > "$WORK/dev/deploy/uiu/postgres/migrate.sh"
commit "first"
git clone -q "$WORK/origin.git" "$AIMS_DEPLOY_DIR"
printf 'AIMS_ALERT_WEBHOOK="https://alerts.example/hook"\n' > "$AIMS_DEPLOY_DIR/deploy/uiu/.env"

echo "1. nothing new: nothing happens"
run
[ "$status" = 0 ] || fail "exit $status"
[ ! -e "$WORK/docker.log" ] || fail "docker was called"

echo "2. a new commit is built, migrated, started and announced"
commit "second"
run
[ "$status" = 0 ] || fail "exit $status: $(cat "$WORK/run.log")"
[ "$(head_of "$AIMS_DEPLOY_DIR")" = "$(head_of "$WORK/dev")" ] || fail "not on the new commit"
grep -q "docker compose build" "$WORK/docker.log" || fail "no build"
grep -q "docker compose up -d --wait postgres" "$WORK/docker.log" || fail "postgres not started first"
grep -q "docker compose exec -T postgres sh -s" "$WORK/docker.log" || fail "migrations not run"
grep -q "docker compose up -d --remove-orphans" "$WORK/docker.log" || fail "not restarted"
grep -q "deployed .*second" "$WORK/alerts.log" || fail "no 'deployed' alert"
grep -q "https://alerts.example/hook" "$WORK/alerts.log" || fail "alert not sent to the webhook"
good=$(head_of "$AIMS_DEPLOY_DIR")

echo "3. an unhealthy commit is rolled back, announced, and not retried"
commit "broken" UNHEALTHY
bad=$(head_of "$WORK/dev")
run
[ "$status" = 1 ] || fail "exit $status"
[ "$(head_of "$AIMS_DEPLOY_DIR")" = "$good" ] || fail "not rolled back"
grep -q "rolled back" "$WORK/alerts.log" || fail "no rollback alert"
[ "$(cat "$AIMS_DEPLOY_STATE/failed")" = "$bad" ] || fail "failure not remembered"
: > "$WORK/docker.log"
run
[ "$status" = 0 ] && [ ! -s "$WORK/docker.log" ] || fail "the failed commit was tried again"

echo "4. the next commit is deployed"
commit "fixed" "" UNHEALTHY
run
[ "$status" = 0 ] || fail "exit $status"
[ "$(head_of "$AIMS_DEPLOY_DIR")" = "$(head_of "$WORK/dev")" ] || fail "fix not deployed"
[ ! -e "$AIMS_DEPLOY_STATE/failed" ] || fail "failure not cleared"

echo "5. a file edited by hand on the server stops the deploy"
echo "hand edit" >> "$AIMS_DEPLOY_DIR/history"
commit "after the edit"
here=$(head_of "$AIMS_DEPLOY_DIR")
run
[ "$status" = 1 ] || fail "exit $status"
[ "$(head_of "$AIMS_DEPLOY_DIR")" = "$here" ] || fail "deployed over a hand edit"
grep -q "edited by hand" "$WORK/run.log" || fail "reason not given"
git -C "$AIMS_DEPLOY_DIR" checkout -q -- history

echo "6. a rewritten main (force-push) is refused"
git -C "$WORK/dev" reset -q --hard HEAD~2
commit "rewritten history"
run
[ "$status" = 1 ] || fail "exit $status"
[ "$(head_of "$AIMS_DEPLOY_DIR")" = "$here" ] || fail "deployed a rewritten main"
grep -q "rewritten" "$WORK/alerts.log" || fail "no alert for the rewrite"
# Back to a main the server can follow.
git -C "$WORK/dev" reset -q --hard "$here"
git -C "$WORK/dev" push -qf origin HEAD:main
rm -f "$AIMS_DEPLOY_STATE/failed"

echo "7. only a commit whose tests passed on GitHub is deployed"
export AIMS_DEPLOY_REQUIRE_CI=true
ci() {  # ci <status> <conclusion>: GitHub's answer for the newest commit
    printf '{"workflow_runs": [{"name": "tests", "run_number": 3, "run_attempt": 1, "status": "%s", "conclusion": "%s", "head_sha": "%s"}]}' \
        "$1" "$2" "$(head_of "$WORK/dev")" > "$WORK/ci.json"
}
commit "needs its tests"
here=$(head_of "$AIMS_DEPLOY_DIR")
rm -f "$WORK/ci.json"                                   # no run on GitHub yet
run
[ "$status" = 0 ] && [ "$(head_of "$AIMS_DEPLOY_DIR")" = "$here" ] || fail "deployed before its tests"
ci in_progress null
run
[ "$status" = 0 ] && [ "$(head_of "$AIMS_DEPLOY_DIR")" = "$here" ] || fail "deployed while its tests ran"
grep -q "waiting for the tests" "$WORK/run.log" || fail "not said it waits"
ci completed failure
run
[ "$status" = 1 ] && [ "$(head_of "$AIMS_DEPLOY_DIR")" = "$here" ] || fail "deployed with failing tests"
grep -q "tests failed on GitHub" "$WORK/alerts.log" || fail "no alert for failing tests"
ci completed success                                    # re-run on GitHub, now green
run
[ "$status" = 0 ] || fail "exit $status: $(cat "$WORK/run.log")"
[ "$(head_of "$AIMS_DEPLOY_DIR")" = "$(head_of "$WORK/dev")" ] || fail "a green re-run not deployed"

echo "8. a commit that changes the database is backed up first; no backup, no deploy"
: > "$WORK/docker.log"
commit "no database change" "notes.txt"
ci completed success
run
[ "$status" = 0 ] || fail "exit $status"
! grep -q "backup --now" "$WORK/docker.log" || fail "backed up for a change that touches no database"
mkdir -p "$WORK/dev/backend/scripts"
commit "a migration" "backend/scripts/031_new.sql"
ci completed success
run
[ "$status" = 0 ] || fail "exit $status: $(cat "$WORK/run.log")"
grep -q "compose run --rm -T backup --now predeploy-" "$WORK/docker.log" || fail "no backup before the migration"
grep -q "databases backed up first" "$WORK/alerts.log" || fail "the backup is not in the announcement"
good=$(head_of "$AIMS_DEPLOY_DIR")
mkdir -p "$WORK/dev/backend/scripts"
echo x > "$WORK/dev/BACKUP_FAILS"
commit "another migration, and the backup fails" "backend/scripts/032_new.sql"
ci completed success
run
[ "$status" = 1 ] || fail "exit $status"
[ "$(head_of "$AIMS_DEPLOY_DIR")" = "$good" ] || fail "deployed without its backup"
grep -q "the backup before it failed" "$WORK/alerts.log" || fail "the reason is not in the alert"

echo "All autodeploy scenarios passed."
