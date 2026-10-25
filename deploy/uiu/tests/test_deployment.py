"""
The rules the UIU stack has to keep, checked against the files themselves.

A compose file is configuration, and configuration is where safety quietly
disappears: someone publishes a database port to debug something on a
Friday and it stays published. These tests read docker-compose.yml and the
files beside it and hold them to what the SRS requires - one way in, no
service reachable from outside the machine, the three kinds of disk
separate, every secret from the environment.

    cd deploy/uiu && python -m pytest -q
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

HERE = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((HERE / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]
ENV_EXAMPLE = (HERE / ".env.example").read_text(encoding="utf-8")
CADDYFILE = (HERE / "Caddyfile").read_text(encoding="utf-8")


# ============================================================
# One way in (SRS-TOP-02, SRS-SRV-07)
# ============================================================

def test_only_the_gateway_is_reachable_from_outside():
    published = {name: service.get("ports")
                 for name, service in SERVICES.items() if service.get("ports")}
    assert set(published) == {"gateway"}, (
        f"these publish ports to the machine: {sorted(published)}. Only the "
        f"gateway may (SRS-TOP-02).")


def test_the_database_viewer_is_reached_only_through_the_portal():
    viewer = SERVICES["dbviewer"]
    assert "ports" not in viewer and viewer["networks"] == ["internal"]
    assert "handle_path /db/*" in CADDYFILE and "reverse_proxy dbviewer:8080" in CADDYFILE


def test_the_gateway_opens_only_80_and_443():
    """To the world: 80 and 443. Anything else is bound to the machine's loopback."""
    public = {p.split(":")[0] for p in SERVICES["gateway"]["ports"]
              if not p.startswith("127.0.0.1:")}
    assert public == {"80", "443"}
    assert [p for p in SERVICES["gateway"]["ports"] if p.startswith("127.0.0.1:")] \
        == ["127.0.0.1:6001:6001"]


def _networks(name):
    networks = SERVICES[name]["networks"]
    return sorted(networks) if isinstance(networks, (list, dict)) else networks


def test_the_database_and_redis_are_on_the_internal_network_only():
    for name in ("postgres", "pgbouncer", "redis"):
        assert _networks(name) == ["internal"], name


def test_the_services_that_reach_r2_or_azure_have_a_way_out_and_no_way_in():
    """
    An internal network has no route out, so with `internal` alone the API could
    not reach the segment bucket and the archive worker could not fetch a piece
    or upload a copy. They get `egress` - and publish nothing on it.
    """
    for name in ("api", "worker", "archive-worker"):
        assert _networks(name) == ["egress", "internal"], name
        assert not SERVICES[name].get("ports"), name
    assert COMPOSE["networks"]["egress"].get("internal") is not True


def test_the_gateways_fixed_address_is_outside_the_dynamic_range():
    """Otherwise a container that starts first can take it, and the gateway cannot start."""
    import ipaddress
    config = COMPOSE["networks"]["internal"]["ipam"]["config"][0]
    dynamic = ipaddress.ip_network(config["ip_range"].split(":-")[1].rstrip("}"))
    gateway = ipaddress.ip_address(
        SERVICES["gateway"]["networks"]["internal"]["ipv4_address"].split(":-")[1].rstrip("}"))
    assert gateway not in dynamic


def test_the_internal_network_has_no_way_out():
    """A compromised worker cannot open a connection from the internal side."""
    assert COMPOSE["networks"]["internal"]["internal"] is True


def test_the_api_believes_x_forwarded_for_from_the_gateway_alone():
    gateway_ip = SERVICES["gateway"]["networks"]["internal"]["ipv4_address"]
    assert SERVICES["api"]["environment"]["AIMS_TRUSTED_PROXIES"] == gateway_ip
    subnet = COMPOSE["networks"]["internal"]["ipam"]["config"][0]["subnet"]
    assert "AIMS_INTERNAL_SUBNET" in subnet and "AIMS_GATEWAY_IP" in gateway_ip


def test_the_gateway_answers_only_for_the_configured_host():
    assert "{$AIMS_PUBLIC_HOST}" in CADDYFILE
    assert "reverse_proxy api:6000" in CADDYFILE


def test_the_gateway_keeps_credentials_out_of_its_log():
    for header in ("X-Device-Token", "X-Cmed-Key", "X-Worker-Key", "X-Admin-Key",
                   "Authorization"):
        assert f"request>headers>{header} delete" in CADDYFILE


def _block(text, opener):
    start = text.index(opener)
    depth, i = 0, text.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[start:i + 1]
        i += 1


def test_the_internet_never_reaches_v1_the_docs_or_the_workers_routes():
    never = _block(CADDYFILE, "@never path")
    for path in ("/api/v1/*", "/docs", "/redoc", "/openapi.json",
                 "/api/v2/archive/*", "/api/v2/maintenance/*"):
        assert f" {path}" in never, path
    assert "respond 404" in _block(CADDYFILE, "handle @never")


def test_admin_the_dashboard_and_the_viewer_need_a_google_sign_in():
    portal = [line for line in CADDYFILE.split("\n") if line.strip().startswith("@portal path")][0]
    for path in ("/api/v2/admin/*", "/api/v2/dashboard/*", "/db/*", "/dashboard/*"):
        assert path in portal
    door = _block(CADDYFILE, "forward_auth @portal api:6000")
    assert "uri /auth/check" in door and "copy_headers X-Portal-Email X-Portal-Role" in door
    assert "basic_auth" not in CADDYFILE
    # An address allow-list can still be added on top.
    assert "respond 403" in _block(CADDYFILE, "handle @lab_only_denied")


def test_researchers_open_the_databases_with_read_only_logins_only():
    viewer = SERVICES["dbviewer"]
    plugins = " ".join(viewer["volumes"])
    assert "portal-readonly.php" in plugins
    plugin = (HERE / "adminer" / "portal-readonly.php").read_text(encoding="utf-8")
    assert '"aims_monitor"' in plugin and '"aims_clinical_reader"' in plugin
    # A researcher: their own login only, the name handed on by the gateway.
    assert "Use your own database login" in plugin and "HTTP_X_PORTAL_DB_USER" in plugin
    assert "copy_headers X-Portal-Email X-Portal-Role X-Portal-Db-User" in CADDYFILE
    migrate = (HERE / "postgres" / "migrate.sh").read_text(encoding="utf-8")
    assert "roles/researcher_logins_recordings.sql" in migrate
    assert "roles/researcher_logins_clinical.sql" in migrate
    # The superuser's password never reaches the viewer.
    assert "POSTGRES_SUPERUSER_PASSWORD" not in str(viewer.get("environment"))


def test_the_client_address_the_api_sees_is_the_gateways_not_the_clients_claim():
    assert "header_up X-Forwarded-For {remote_host}" in CADDYFILE


def test_cmeds_token_and_fhir_routes_are_not_blocked():
    never = _block(CADDYFILE, "@never path")
    denied = _block(CADDYFILE, "@lab_only_denied")
    for path in ("/api/v2/oauth", "/fhir", "/.well-known", "/api/v2/clinical"):
        assert path not in never and path not in denied, path


def test_the_lab_door_serves_admin_and_dashboard_only():
    door = _block(CADDYFILE, "http://:6001")
    assert "@lab path /api/v2/admin /api/v2/admin/* /api/v2/dashboard " in door
    # /health/ready too: the deployer asks it from the droplet itself.
    assert "/health /health/*" in door
    assert "respond 404" in door


def test_an_update_does_not_drop_a_consultation():
    """AT-30: the gateway holds the request while the API restarts."""
    assert "lb_try_duration" in CADDYFILE
    assert "health_uri /health" in CADDYFILE


# ============================================================
# The three kinds of disk (SRS-SRV-03)
# ============================================================

def test_each_kind_of_disk_is_its_own_mount():
    assert "${AIMS_DB_PATH" in str(SERVICES["postgres"]["volumes"])
    assert "${AIMS_ARCHIVE_PATH" in str(SERVICES["archive-worker"]["volumes"])
    assert "${AIMS_BACKUP_PATH" in str(SERVICES["backup"]["volumes"])


def test_the_archive_worker_joins_recordings_on_the_working_disk():
    """Not on the archive array, and not in the container's own layer."""
    tmpfs = " ".join(SERVICES["archive-worker"]["tmpfs"])
    assert tmpfs.startswith("/tmp:size=")
    assert SERVICES["archive-worker"]["read_only"] is True


def test_the_monitor_can_only_look_at_the_volumes():
    for mount in SERVICES["monitor"]["volumes"]:
        if mount.startswith("${"):
            assert mount.endswith(":ro"), mount


def test_the_archive_is_the_only_volume_the_worker_can_write():
    writable = [v for v in SERVICES["archive-worker"]["volumes"] if not v.endswith(":ro")]
    assert writable == ["${AIMS_ARCHIVE_PATH:?set AIMS_ARCHIVE_PATH in .env}:/archive"]


# ============================================================
# Secrets
# ============================================================

def test_no_secret_is_written_into_the_compose_file():
    """Every credential comes from the environment; none has a default."""
    text = (HERE / "docker-compose.yml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if re.search(r"(PASSWORD|_KEY|SECRET):", line) and "${" not in line:
            pytest.fail(f"a credential is written into the compose file: {line.strip()}")


def test_every_password_in_the_example_is_empty():
    for line in ENV_EXAMPLE.splitlines():
        if re.match(r"^[A-Z0-9_]*(PASSWORD|_KEY|SECRET)[A-Z0-9_]*=", line):
            name, _, value = line.partition("=")
            assert value == "", f"{name} has a value in .env.example"


def test_the_stack_refuses_to_start_without_the_settings_that_matter():
    """`${VAR:?}` stops the stack rather than starting it wrongly configured."""
    text = (HERE / "docker-compose.yml").read_text(encoding="utf-8")
    for required in ("AIMS_PUBLIC_HOST", "POSTGRES_SUPERUSER_PASSWORD",
                     "AIMS_API_DB_PASSWORD", "AIMS_CLINICAL_PASSWORD",
                     "AIMS_ARCHIVE_PATH", "AIMS_DB_PATH", "AIMS_WORKER_KEY",
                     "AIMS_ADMIN_KEY", "AIMS_GRANT_PRIVATE_KEY", "AIMS_RECEIPT_PRIVATE_KEY",
                     "AIMS_BACKUP_KEY", "MINIO_ENDPOINT"):
        assert f"${{{required}:?" in text, required


def test_no_service_reads_the_whole_env_file():
    """Each service is given the variables it uses, and nothing else."""
    for name, service in SERVICES.items():
        assert "env_file" not in service, name


def test_the_api_gets_none_of_the_secrets_it_does_not_use():
    api = str(SERVICES["api"]["environment"])
    for secret in ("AIMS_COPY_KEY", "AIMS_BACKUP_KEY", "POSTGRES_SUPERUSER_PASSWORD",
                   "AIMS_MONITOR_PASSWORD", "AIMS_OFFSITE_SECRET_KEY",
                   "AIMS_OFFSITE_ACCESS_KEY", "AZURE_NER_API_KEY",
                   "AZURE_TRANSCRIBE_API_KEY"):
        assert secret not in api, secret


def test_the_transcription_worker_gets_no_signing_or_admin_key():
    worker = str(SERVICES["worker"]["environment"])
    for secret in ("AIMS_GRANT_PRIVATE_KEY", "AIMS_RECEIPT_PRIVATE_KEY", "AIMS_ADMIN_KEY",
                   "AIMS_WORKER_KEY", "AIMS_COPY_KEY", "AIMS_COPY_SECRET_KEY",
                   "AIMS_BACKUP_KEY", "POSTGRES_SUPERUSER_PASSWORD"):
        assert secret not in worker, secret


def test_upload_links_are_never_signed_for_localhost():
    """
    MINIO_EXTERNAL_ENDPOINT unset fell back to the server's default,
    localhost:9000, and every link a doctor's PC was given pointed at itself.
    """
    assert SERVICES["api"]["environment"]["MINIO_EXTERNAL_ENDPOINT"] \
        == "${MINIO_EXTERNAL_ENDPOINT:-}"


def test_the_copy_key_is_only_on_the_machine_that_makes_copies():
    """SRS-DAT-08: the API and the database never see it."""
    for name, service in SERVICES.items():
        environment = service.get("environment") or {}
        if name != "archive-worker" and isinstance(environment, dict):
            assert "AIMS_COPY_KEY" not in environment, name
    assert "AIMS_COPY_KEY" in SERVICES["archive-worker"]["environment"]


def test_the_api_never_receives_the_monitor_or_superuser_passwords():
    api = SERVICES["api"]["environment"]
    assert "POSTGRES_SUPERUSER_PASSWORD" not in str(api)
    assert "AIMS_MONITOR_PASSWORD" not in str(api)


# ============================================================
# Two databases, two roles (SRS-DBA-20)
# ============================================================

def test_the_api_reaches_each_database_as_a_different_role():
    api = SERVICES["api"]["environment"]
    # Not aims_recordings: that role owns the tables and could switch off the
    # audit log's triggers. aims_api reads and writes rows only (018).
    assert api["POSTGRES_USER"] == "aims_api"
    assert SERVICES["worker"]["environment"]["POSTGRES_USER"] == "aims_api"
    assert "aims_clinical_writer" in api["AIMS_CLINICAL_DATABASE_URL"]
    assert "aims_clinical" in api["AIMS_CLINICAL_DATABASE_URL"].rsplit("/", 1)[-1]


def test_the_monitor_reads_the_recordings_database_only():
    dsn = SERVICES["monitor"]["environment"]["AIMS_MONITOR_DSN"]
    assert dsn.startswith("postgresql://aims_monitor:")
    assert dsn.endswith("/aims_recordings")


def test_the_pooler_trap_is_handled_everywhere_it_applies():
    """
    Transaction pooling plus a driver-side statement cache fails only under
    load, so every service that goes through PgBouncer must say so.
    """
    for name in ("api", "worker"):
        environment = SERVICES[name]["environment"]
        assert environment["POSTGRES_HOST"] == "pgbouncer"
        assert environment["AIMS_DB_POOLER"] == "transaction"
    ini = (HERE / "pgbouncer" / "pgbouncer.ini").read_text(encoding="utf-8")
    assert "pool_mode = POOL_MODE_PLACEHOLDER" in ini


def test_the_schema_is_applied_in_the_order_the_tests_prove():
    """
    The init script applies every migration by the same glob, in the same
    file-name order, as test_db_integration.py - not from a list kept by hand,
    which is how a new migration would be one a new server never got.
    """
    runner = (HERE / "postgres" / "migrate.sh").read_text(encoding="utf-8")
    assert "for script in /aims/scripts/0[0-9][0-9]_*.sql; do" in runner
    integration = (HERE.parent.parent / "backend" / "tests"
                   / "test_db_integration.py").read_text(encoding="utf-8")
    assert 'glob("0[0-9][0-9]_*.sql")' in integration
    assert "apply aims_recordings aims_recordings" in runner
    assert 'echo "SET ROLE $2;"' in runner


def test_a_new_server_and_an_update_build_the_schema_the_same_way():
    """One runner: 02_schema.sh calls it on a new server, the deployer on an update."""
    schema = (HERE / "postgres" / "init" / "02_schema.sh").read_text(encoding="utf-8")
    assert "AIMS_MIGRATIONS_FRESH=1 sh /aims/migrate.sh" in schema
    mounts = SERVICES["postgres"]["volumes"]
    assert "./postgres/migrate.sh:/aims/migrate.sh:ro" in mounts
    deployer = (HERE / "autodeploy" / "autodeploy.sh").read_text(encoding="utf-8")
    assert 'exec -T postgres sh -s < "$STACK/postgres/migrate.sh"' in deployer


def test_a_migration_is_applied_once_and_never_guessed():
    runner = (HERE / "postgres" / "migrate.sh").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS aims_migrations" in runner
    assert "not applied again" in runner                    # an edited file is reported
    assert "has tables but no migration ledger" in runner  # an old server is adopted, not re-run
    assert "set -eu" in runner


def test_the_deployer_runs_as_aims_and_holds_no_key_to_github():
    service = (HERE / "autodeploy" / "aimscribe-autodeploy.service").read_text(encoding="utf-8")
    assert "User=aims" in service and "User=root" not in service
    deployer = (HERE / "autodeploy" / "autodeploy.sh").read_text(encoding="utf-8")
    assert "git push" not in deployer and "GITHUB_TOKEN" not in deployer
    assert "merge-base --is-ancestor" in deployer          # a force-push is refused
    assert "status --porcelain" in deployer                # so is a hand-edited server


def test_shell_scripts_reach_linux_without_carriage_returns():
    attributes = (HERE.parent.parent / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in attributes


def test_the_owner_of_the_tables_cannot_log_in_and_the_api_role_is_not_it():
    init = (HERE / "postgres" / "init" / "01_databases.sh").read_text(encoding="utf-8")
    assert "CREATE ROLE aims_recordings NOLOGIN;" in init
    assert "CREATE ROLE aims_api LOGIN PASSWORD '${AIMS_API_DB_PASSWORD}';" in init
    pooler = (HERE / "pgbouncer" / "entrypoint.sh").read_text(encoding="utf-8")
    assert '"aims_api"' in pooler and '"aims_recordings"' not in pooler
    migration = (HERE.parent.parent / "backend" / "scripts"
                 / "018_v3_least_privilege.sql").read_text(encoding="utf-8")
    assert "REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM aims_api;" in migration
    granted = migration.split("'GRANT SELECT, INSERT, UPDATE, DELETE ON public.%I TO aims_api'")
    assert len(granted) == 2                            # rows only: no TRUNCATE, no TRIGGER
    assert "TRUNCATE ON" not in migration.replace("REVOKE UPDATE, DELETE, TRUNCATE ON audit_log", "")


def test_backups_are_never_written_without_their_key():
    assert SERVICES["backup"]["environment"]["AIMS_BACKUP_KEY"] \
        == "${AIMS_BACKUP_KEY:?set AIMS_BACKUP_KEY in .env}"


def test_a_new_server_starts_from_the_baseline_the_tests_use():
    baseline = HERE / "postgres" / "schema" / "00_v1_baseline.sql"
    assert baseline.exists()
    integration = (HERE.parent.parent / "backend" / "tests"
                   / "test_db_integration.py").read_text(encoding="utf-8")
    assert "00_v1_baseline.sql" in integration


# ============================================================
# Keeping running (SRS-NFR-01, SRS-SRV-08)
# ============================================================

def test_everything_comes_back_after_a_power_cut():
    for name, service in SERVICES.items():
        assert service.get("restart") == "unless-stopped", name


def test_the_services_a_recorder_depends_on_are_checked_not_assumed():
    for name in ("postgres", "pgbouncer", "redis", "api"):
        assert "healthcheck" in SERVICES[name], name


def test_the_api_waits_for_what_it_needs():
    assert set(SERVICES["api"]["depends_on"]) == {"pgbouncer", "redis"}
    for condition in SERVICES["api"]["depends_on"].values():
        assert condition["condition"] == "service_healthy"


def test_no_container_may_gain_privileges():
    for name, service in SERVICES.items():
        assert "no-new-privileges:true" in service.get("security_opt", []), name


def test_logs_cannot_fill_the_disk_that_holds_the_recordings():
    for name, service in SERVICES.items():
        options = service.get("logging", {}).get("options", {})
        assert options.get("max-size"), name
        assert options.get("max-file"), name


def test_a_download_is_decrypted_by_the_worker_on_the_internal_network_only():
    api = SERVICES["api"]["environment"]
    worker = SERVICES["archive-worker"]
    assert api["AIMS_DECRYPT_URL"] == "http://archive-worker:7070"
    assert str(worker["environment"]["AIMS_DECRYPT_PORT"]) == "7070"
    # The key stays with the worker; the API never has it.
    assert "AIMS_COPY_KEY" in worker["environment"] and "AIMS_COPY_KEY" not in api
    assert "internal" in worker["networks"] and not worker.get("ports")
    dockerfile = (HERE.parent.parent / "backend" / "archive_worker" / "Dockerfile").read_text()
    assert "serve.py" in dockerfile and "EXPOSE" not in dockerfile.replace("No EXPOSE", "")


# ============================================================
# Migrations only add (autodeploy.sh: a rollback runs the old code on the
# new schema, and nothing a migration destroys comes back without a restore)
# ============================================================

SCRIPTS = HERE.parent.parent / "backend" / "scripts"

# Migrations that destroy data on purpose, each with why. Adding one here is a
# decision someone has to make in review - with a backup taken first, which
# the deployer does for every database change.
DESTRUCTIVE_APPROVED = {
    "022_v3_drop_v1_tables.sql": "the v1 pipeline's tables, empty since v3; dropped once",
    "024_v3_readable_devices.sql": "one-time nonces past their window; never patient data",
}

_DESTROYS = re.compile(
    r"^\s*(DROP\s+(TABLE|SCHEMA|DATABASE)\b|TRUNCATE\b|DELETE\s+FROM\b"
    r"|ALTER\s+TABLE\b[^;]*\bDROP\s+COLUMN\b)", re.IGNORECASE | re.DOTALL)


def _statements(sql: str):
    """The statements of a script, comments removed; inside DO blocks too."""
    sql = re.sub(r"--[^\n]*", "", sql)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    for part in re.split(r";|\$\$", sql):
        if part.strip():
            yield part


def test_no_migration_destroys_data_unless_it_was_decided():
    offenders = []
    for path in sorted(SCRIPTS.rglob("*.sql")):
        if path.name in DESTRUCTIVE_APPROVED:
            continue
        for statement in _statements(path.read_text(encoding="utf-8")):
            # A statement may follow BEGIN or THEN on the same line in a DO block.
            for line in statement.splitlines():
                if _DESTROYS.match(re.sub(r"^\s*(BEGIN|THEN|ELSE)\s+", "", line,
                                          flags=re.IGNORECASE)):
                    offenders.append(f"{path.relative_to(SCRIPTS)}: {line.strip()[:80]}")
    assert not offenders, (
        "these migrations drop or delete data: " + "; ".join(offenders) +
        ". A migration only adds. If this one really must destroy, add it to "
        "DESTRUCTIVE_APPROVED with the reason, in review.")


def test_the_destroy_check_catches_what_it_should():
    bad = ["DROP TABLE sessions", "truncate audit_log", "DELETE FROM patients WHERE 1=1",
           "ALTER TABLE sessions DROP COLUMN patient_id"]
    fine = ["DROP TRIGGER IF EXISTS t ON x", "CREATE TRIGGER t BEFORE TRUNCATE ON audit_log",
            "ALTER TABLE x DROP CONSTRAINT y", "DROP VIEW IF EXISTS v", "DROP FUNCTION f()"]
    assert all(_DESTROYS.match(s) for s in bad)
    assert not any(_DESTROYS.match(s) for s in fine)


def test_the_data_guard_is_applied_last_to_both_databases():
    """
    scripts/guard/data_guard.sql makes PostgreSQL itself refuse a drop, a
    truncate or a mass delete. migrate.sh applies it on every update, after
    the migrations, to both databases (tested against PostgreSQL in
    backend/tests/test_data_guard.py).
    """
    runner = (HERE / "postgres" / "migrate.sh").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in runner.splitlines() if not ln.strip().startswith("#")]
    guard = [i for i, ln in enumerate(lines) if "/aims/scripts/guard/data_guard.sql" in ln]
    assert len(guard) == 2
    assert any("aims_recordings" in lines[i] for i in guard)
    assert any("aims_clinical" in lines[i] for i in guard)
    last_migration = max(i for i, ln in enumerate(lines) if ln.startswith("apply "))
    assert min(guard) > last_migration


def test_only_a_decided_migration_switches_the_guard_off():
    """
    The guard's switch (aims.allow_destructive) is for the migrations decided
    in review, and only those - otherwise a migration could simply turn it on.
    """
    switched = sorted(p.name for p in SCRIPTS.rglob("*.sql")
                      if p.parent.name != "guard"
                      and re.search(r"aims\.allow_destructive\s*=\s*'on'",
                                    "\n".join(_statements(p.read_text(encoding="utf-8")))))
    assert set(switched) <= set(DESTRUCTIVE_APPROVED), switched


# Nothing that deploys may destroy where the data lives: the database folder,
# the archive and the backups are bind mounts under /srv/aimscribe, which
# Docker never removes by itself - only a command like these can.
_LIVE = r"(aims_recordings|aims_clinical)\b"
_WIPES = re.compile(
    r"docker\s+(compose\b[^\n]*?\s)?down\b[^\n]*(\s-v\b|--volumes)"
    r"|docker\s+volume\s+(rm|prune)|system\s+prune[^\n]*--volumes"
    r"|rm\s+-[a-z]*r[a-z]*f?\s+[^\n]*(/srv|AIMS_DB_PATH|AIMS_BACKUP_PATH|AIMS_ARCHIVE_PATH)"
    r"|dropdb\s+[^\n]*" + _LIVE + r"|DROP\s+DATABASE\s+(IF\s+EXISTS\s+)?" + _LIVE,
    re.IGNORECASE)


def test_no_deploy_script_can_wipe_the_data():
    scripts = [p for p in HERE.rglob("*.sh") if "tests" not in p.parts]
    scripts += [HERE / "docker-compose.yml"]
    offenders = []
    for path in scripts:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.lstrip().startswith("#") and _WIPES.search(line):
                offenders.append(f"{path.relative_to(HERE)}:{n}: {line.strip()[:80]}")
    assert not offenders, offenders


def test_the_wipe_check_catches_what_it_should():
    bad = ["docker compose down -v", "docker compose -p x down --volumes",
           "docker volume rm aimscribe_db", "docker system prune -a --volumes",
           "rm -rf /srv/aimscribe/db", "rm -rf \"$AIMS_DB_PATH\"", "dropdb aims_recordings",
           "DROP DATABASE IF EXISTS aims_clinical"]
    # The restore drill drops its own scratch databases; that is fine.
    fine = ["docker compose down", "docker compose up -d", "rm -f \"$tmp\"",
            "rm -rf \"$work\"", "docker image prune -f",
            "psql -c \"DROP DATABASE IF EXISTS $scratch\"", "DROP DATABASE aims_check"]
    assert all(_WIPES.search(s) for s in bad)
    assert not any(_WIPES.search(s) for s in fine)


# ============================================================
# Recovery to any minute: every profile archives, where the backup job looks
# ============================================================

@pytest.mark.parametrize("profile", ["postgresql.conf", "postgresql.16gb.conf",
                                     "postgresql.4gb.conf"])
def test_every_postgres_profile_archives_its_wal_for_the_backup_job(profile):
    conf = (HERE / "postgres" / profile).read_text(encoding="utf-8")
    settings = dict(re.findall(r"^\s*(\w+)\s*=\s*(.+?)\s*(?:#.*)?$", conf, re.MULTILINE))
    assert settings["wal_level"] == "replica"
    assert settings["archive_mode"] == "on"
    assert "/wal/%f.gz" in settings["archive_command"] and ".part" in settings["archive_command"]
    assert int(settings["archive_timeout"]) <= 600          # at most ten minutes lost
    assert settings["hba_file"] == "'/etc/postgresql/hba/pg_hba.conf'"


def test_postgres_gets_its_access_rules_and_the_wal_folder_the_backup_job_reads():
    pg = " ".join(SERVICES["postgres"]["volumes"])
    backup = " ".join(SERVICES["backup"]["volumes"])
    assert "./postgres/hba:/etc/postgresql/hba:ro" in pg
    assert "${AIMS_BACKUP_PATH:?set AIMS_BACKUP_PATH in .env}/wal:/wal" in pg
    assert "${AIMS_BACKUP_PATH:?set AIMS_BACKUP_PATH in .env}:/backups" in backup
    hba = (HERE / "postgres" / "hba" / "pg_hba.conf").read_text(encoding="utf-8")
    rules = [line.split() for line in hba.splitlines() if line and not line.startswith("#")]
    # Replication from the network only with a password, never trust.
    assert ["host", "replication", "all", "samenet", "scram-sha-256"] in rules
    assert not any(r[0] == "host" and r[-1] == "trust" and r[3] not in ("127.0.0.1/32", "::1/128")
                   for r in rules)


def test_the_api_is_given_the_erase_switches():
    env = SERVICES["api"]["environment"]
    for name in ("AIMS_ERASE_PAUSED", "AIMS_ERASE_BREAKER_SHARE", "AIMS_ERASE_BREAKER_MIN"):
        assert name in env, f"{name} is read by the API but never passed to it"
