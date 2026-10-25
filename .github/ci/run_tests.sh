#!/usr/bin/env bash
# ================================================================
# Every test of what runs on the server, the way GitHub runs them for each
# commit (.github/workflows/tests.yml). The server deploys a commit only when
# this passed for it (deploy/uiu/autodeploy/autodeploy.sh), so a broken
# change stops here, not on the server.
#
#     bash .github/ci/run_tests.sh
#
# Needs a PostgreSQL 16 to build throwaway databases in
# (AIMS_TEST_PG_URI, a superuser), Python 3.12, and for the backup and
# deployer tests: sh, git, flock, openssl.
# ================================================================
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
PG_URI="${AIMS_TEST_PG_URI:-postgresql://postgres:ptest@127.0.0.1:5432/postgres}"

python -m pip install -q --disable-pip-version-check \
    -r backend/requirements-api.txt -r backend/archive_worker/requirements.txt \
    pytest pytest-asyncio pyyaml requests reportlab

# The integration tests ask the pgserver package for a PostgreSQL; this one
# hands them the CI's instead of starting its own.
SHIM="$(mktemp -d)"
cat > "$SHIM/pgserver.py" <<EOF
class _Server:
    def get_uri(self):
        return "$PG_URI"

    def cleanup(self):
        pass


def get_server(path, cleanup_mode=None):
    return _Server()
EOF
export PYTHONPATH="$SHIM${PYTHONPATH:+:$PYTHONPATH}"

failed=()
suite() {  # suite <name> <folder> [pytest arguments...]
    local name=$1 dir=$2
    shift 2
    echo "::group::$name"
    if (cd "$dir" && python -m pytest -q -p no:cacheprovider "$@"); then
        echo "::endgroup::"
    else
        echo "::endgroup::"
        echo "FAILED: $name"
        failed+=("$name")
    fi
}

# The server, file by file: each file builds its own app and databases.
# Two need the transcription services and their keys, which the server's
# API does not use; test_full_pipeline is a script for a running stack.
for f in backend/tests/test_*.py; do
    case "$f" in
        *test_azure_apis.py|*test_transcription.py|*test_full_pipeline.py) continue ;;
    esac
    suite "server: $(basename "$f")" backend "tests/$(basename "$f")"
done
suite "archive worker" backend/archive_worker
suite "deployment, backups, monitor, deployer" deploy/uiu
suite "tools" tools test_channel_b_test.py test_load_test.py

if [ "${#failed[@]}" -gt 0 ]; then
    echo
    echo "${#failed[@]} suite(s) failed:"
    printf '  %s\n' "${failed[@]}"
    exit 1
fi
echo
echo "All suites passed."
