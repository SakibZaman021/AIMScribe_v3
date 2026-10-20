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


def test_admin_and_the_dashboard_answer_only_the_lab_and_fail_closed():
    denied = _block(CADDYFILE, "@lab_only_denied")
    assert "/api/v2/admin/*" in denied and "/api/v2/dashboard/*" in denied
    # Unset means loopback only - nobody outside - never "everyone".
    assert "not remote_ip {$AIMS_ADMIN_ALLOW:127.0.0.1/32}" in denied
    assert "respond 403" in _block(CADDYFILE, "handle @lab_only_denied")
    assert SERVICES["gateway"]["environment"]["AIMS_ADMIN_ALLOW"] \
        == "${AIMS_ADMIN_ALLOW:-127.0.0.1/32}"


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
    schema = (HERE / "postgres" / "init" / "02_schema.sh").read_text(encoding="utf-8")
    assert "for script in /aims/scripts/0[0-9][0-9]_*.sql; do" in schema
    integration = (HERE.parent.parent / "backend" / "tests"
                   / "test_db_integration.py").read_text(encoding="utf-8")
    assert 'glob("0[0-9][0-9]_*.sql")' in integration
    assert "SET ROLE aims_recordings" in schema


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
