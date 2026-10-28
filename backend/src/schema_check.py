"""
Is every migration this server needs applied to the database it is talking to?

Nothing checked this. With 013 missing every reassign answered 500 (seen live);
with 015 missing every click marker fails and that recording stops uploading.
Nothing was lost - recorders keep their audio and retry - but nothing arrived
either, and no one was told.

Each migration is recognised by what it leaves behind - a table, a column, a
value its CHECK accepts - not by a version table, so the check works on every
database already deployed, and also catches a migration that stopped half-way.
The catalogue is read through pg_catalog, which every role may read, so the
check needs no extra grants.

Used three ways:
    at start-up             logs each missing migration, raises schema_missing,
                            and refuses to start if AIMS_SCHEMA_STRICT=true
    GET /health             "schema": "ok" or the missing files; status degraded
    the monitor             reads /health and alerts like any other check
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _table(name: str) -> str:
    return f"SELECT to_regclass('public.{name}') IS NOT NULL"


def _column(table: str, column: str) -> str:
    return (f"SELECT EXISTS (SELECT 1 FROM pg_attribute "
            f"WHERE attrelid = to_regclass('public.{table}') "
            f"AND attname = '{column}' AND NOT attisdropped)")


def _check_allows(table: str, column: str, value: str) -> str:
    # No CHECK on the column at all accepts every value; a CHECK must name it.
    return (f"SELECT NOT EXISTS (SELECT 1 FROM pg_constraint "
            f"WHERE conrelid = to_regclass('public.{table}') AND contype = 'c' "
            f"AND pg_get_constraintdef(oid) LIKE '%{column}%' "
            f"AND pg_get_constraintdef(oid) NOT LIKE '%''{value}''%')"
            f" AND to_regclass('public.{table}') IS NOT NULL")


# (migration file, database, what it leaves behind, query that is true when present)
REQUIRED: List[Tuple[str, str, str, str]] = [
    ("002_v2_integrity.sql", "recordings", "table chain_entries", _table("chain_entries")),
    ("002_v2_integrity.sql", "recordings", "table segments", _table("segments")),
    ("003_readable_names.sql", "recordings", "column segments.clip_name",
     _column("segments", "clip_name")),
    ("004_transit_storage.sql", "recordings", "column segments.object_deleted_at",
     _column("segments", "object_deleted_at")),
    ("006_doctor_from_device.sql", "recordings", "column devices.doctor_id",
     _column("devices", "doctor_id")),
    ("007_readable_object_keys.sql", "recordings", "column sessions.object_prefix",
     _column("sessions", "object_prefix")),
    ("008_doctors.sql", "recordings", "table doctors", _table("doctors")),
    ("009_close_reason.sql", "recordings", "column sessions.close_reason",
     _column("sessions", "close_reason")),
    ("010_v3_confirmation_channel_b.sql", "recordings", "table confirmation_notices",
     _table("confirmation_notices")),
    ("010_v3_confirmation_channel_b.sql", "recordings", "table grant_authorisations",
     _table("grant_authorisations")),
    ("010_v3_confirmation_channel_b.sql", "recordings", "column sessions.confirmation",
     _column("sessions", "confirmation")),
    ("011_v3_file_names.sql", "recordings", "column sessions.file_stem",
     _column("sessions", "file_stem")),
    ("012_v3_cloud_copy.sql", "recordings", "table cloud_copies", _table("cloud_copies")),
    ("013_v3_reassign.sql", "recordings", "chain entry type 'reassign' accepted",
     _check_allows("chain_entries", "entry_type", "reassign")),
    ("014_v3_device_health.sql", "recordings", "table device_health_log",
     _table("device_health_log")),
    ("015_v3_consultation_parts.sql", "recordings", "table consultation_parts",
     _table("consultation_parts")),
    ("015_v3_consultation_parts.sql", "recordings", "table session_clicks",
     _table("session_clicks")),
    ("015_v3_consultation_parts.sql", "recordings", "column sessions.parts_count",
     _column("sessions", "parts_count")),
    ("015_v3_consultation_parts.sql", "recordings", "chain entry type 'marker' accepted",
     _check_allows("chain_entries", "entry_type", "marker")),
    ("016_v3_device_state.sql", "recordings", "column devices.held_count",
     _column("devices", "held_count")),
    ("016_v3_device_state.sql", "recordings", "column devices.disk_encrypted",
     _column("devices", "disk_encrypted")),
    ("016_v3_device_state.sql", "recordings", "column sessions.continues_session_id",
     _column("sessions", "continues_session_id")),
    ("017_v3_cmed_clients.sql", "recordings", "table cmed_clients", _table("cmed_clients")),
    ("017_v3_cmed_clients.sql", "recordings", "table cmed_access_tokens",
     _table("cmed_access_tokens")),
    ("017_v3_cmed_clients.sql", "recordings", "column api_keys.cmed_hospital_ids",
     _column("api_keys", "cmed_hospital_ids")),
    # Applied 017 as a login other than the owner, and 018 never re-run: the API
    # cannot read the tables that prove who CMED is, so every token is refused.
    ("018_v3_least_privilege.sql", "recordings", "the API's rights on cmed_access_tokens",
     "SELECT to_regclass('public.cmed_access_tokens') IS NOT NULL AND "
     "has_table_privilege(current_user, 'public.cmed_access_tokens', 'INSERT')"),
    ("019_v3_integrity.sql", "recordings", "column devices.signs_requests",
     _column("devices", "signs_requests")),
    ("019_v3_integrity.sql", "recordings", "table device_nonces", _table("device_nonces")),
    ("019_v3_integrity.sql", "recordings", "table erase_tasks", _table("erase_tasks")),
    ("019_v3_integrity.sql", "recordings", "column sessions.late_alerted_at",
     _column("sessions", "late_alerted_at")),
    ("019_v3_integrity.sql", "recordings", "column session_refusals.signed",
     _column("session_refusals", "signed")),
    ("020_v3_cmed_hardening.sql", "recordings", "column cmed_access_tokens.kid",
     _column("cmed_access_tokens", "kid")),
    ("020_v3_cmed_hardening.sql", "recordings", "column cmed_clients.identifier_systems",
     _column("cmed_clients", "identifier_systems")),
    ("020_v3_cmed_hardening.sql", "recordings", "table fhir_messages", _table("fhir_messages")),
    # Without it a PC whose key lives in its TPM (65 bytes) cannot enrol at all.
    ("021_v3_tpm_device_keys.sql", "recordings", "devices accept a TPM (P-256) key",
     "SELECT coalesce((SELECT pg_get_constraintdef(oid) LIKE '%65%' FROM pg_constraint "
     "WHERE conname = 'devices_pubkey_len'), true)"),
    # Not harmful if missed, but it says the migrations stopped short of 022.
    ("022_v3_drop_v1_tables.sql", "recordings", "the v1 transcription tables are gone",
     "SELECT to_regclass('public.ner_results') IS NULL "
     "AND to_regclass('public.used_grants') IS NULL"),
    ("023_v3_audio_by_day_and_hospital.sql", "recordings", "view v_audio_daily",
     _table("v_audio_daily")),
    ("023_v3_audio_by_day_and_hospital.sql", "recordings", "view v_audio_problems",
     _table("v_audio_problems")),
    ("024_v3_readable_devices.sql", "recordings", "view v_devices", _table("v_devices")),
    ("024_v3_readable_devices.sql", "recordings", "each nonce belongs to a known PC",
     "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'device_nonces_device_fk')"),
    # Without it nothing unconfirmed is ever erased, and no PC can register itself.
    ("025_v3_confirmed_only_and_self_registration.sql", "recordings",
     "column sessions.erase_after", _column("sessions", "erase_after")),
    ("025_v3_confirmed_only_and_self_registration.sql", "recordings",
     "column devices.approval", _column("devices", "approval")),
    # Without it nobody can sign in to the portal with Google.
    ("026_v3_portal_people.sql", "recordings", "table portal_people",
     _table("portal_people")),
    # Without it no recording can be downloaded from the portal.
    ("027_v3_portal_downloads.sql", "recordings", "table portal_downloads",
     _table("portal_downloads")),
    # Without it no researcher can be given their own database login.
    ("028_v3_researcher_db_logins.sql", "recordings", "column portal_people.db_user",
     _column("portal_people", "db_user")),
    # Without it nothing can be downloaded in batches.
    ("029_v3_portal_batches.sql", "recordings", "table portal_batches",
     _table("portal_batches")),
    ("029_v3_portal_batches.sql", "recordings", "table portal_batch_files",
     _table("portal_batch_files")),
    # Without it an admin cannot release a clinic the erase breaker stopped.
    ("030_v3_erase_breaker.sql", "recordings", "table erase_releases",
     _table("erase_releases")),
    # Without it a recording cannot carry CMED's visit_id, and the heartbeat
    # cannot tell a recorder its prescription has arrived (interface 4).
    ("031_v3_visit_id.sql", "recordings", "column sessions.prescription_received_at",
     _column("sessions", "prescription_received_at")),
    # Without it the dashboard cannot show which microphone each PC records with.
    ("032_v3_microphone.sql", "recordings", "column devices.microphone",
     _column("devices", "microphone")),
    # Without it recorders cannot be told CMED's EHR addresses.
    ("033_v3_cmed_connections.sql", "recordings", "table cmed_trusted_origins",
     _table("cmed_trusted_origins")),
    ("clinical/001_aims_clinical.sql", "clinical", "table encounters", _table("encounters")),
    # Without it the doctor's findings in API 3 cannot be stored.
    ("clinical/003_consultation_findings.sql", "clinical", "table consultation_findings",
     _table("consultation_findings")),
    # Without it interface 4 (/api/v4/clinical) cannot file a visit.
    ("clinical/004_patient_visit.sql", "clinical", "table patient_visit",
     _table("patient_visit")),
    # Without it interface 4 cannot file a message: it loads the sections as rows.
    ("clinical/005_visit_sections.sql", "clinical", "table visit_drug_order",
     _table("visit_drug_order")),
    ("clinical/001_aims_clinical.sql", "clinical", "table intake_records",
     _table("intake_records")),
    ("clinical/001_aims_clinical.sql", "clinical", "table prescriptions",
     _table("prescriptions")),
]


async def missing(recordings_pool, clinical_pool=None) -> List[Dict[str, Any]]:
    """
    Every migration not (fully) applied, with what is absent. Empty when all is
    well. A database that cannot be read is reported as such, not as missing.
    """
    found: Dict[str, Dict[str, Any]] = {}
    pools = {"recordings": recordings_pool, "clinical": clinical_pool or recordings_pool}
    for name, pool in pools.items():
        checks = [r for r in REQUIRED if r[1] == name]
        if pool is None or not checks:
            continue
        async with pool.acquire() as conn:
            for migration, database, what, query in checks:
                if not await conn.fetchval(query):
                    entry = found.setdefault(migration, {"migration": migration,
                                                         "database": database,
                                                         "missing": []})
                    entry["missing"].append(what)
    return sorted(found.values(), key=lambda m: m["migration"])


def describe(problems: List[Dict[str, Any]]) -> str:
    """'ok', or the files to apply, for /health."""
    if not problems:
        return "ok"
    return "missing " + ", ".join(p["migration"] for p in problems)


def strict() -> bool:
    return os.getenv("AIMS_SCHEMA_STRICT", "").strip().lower() in ("1", "true", "yes")


class SchemaState:
    """
    The last answer, reused by /health. Checked again every minute while
    something is missing - so applying the migration clears it without a
    restart - and every ten minutes once all is well.
    """
    RECHECK_WHEN_MISSING = 60.0
    RECHECK_WHEN_OK = 600.0

    def __init__(self) -> None:
        self.problems: Optional[List[Dict[str, Any]]] = None
        self.error: Optional[str] = None
        self.at = 0.0
        self._lock = asyncio.Lock()

    async def check(self, recordings_pool, clinical_pool=None, *, force: bool = False):
        due = self.RECHECK_WHEN_OK if self.problems == [] else self.RECHECK_WHEN_MISSING
        if not force and self.problems is not None and time.monotonic() - self.at < due:
            return self.problems
        async with self._lock:
            try:
                self.problems = await missing(recordings_pool, clinical_pool)
                self.error = None
            except Exception as exc:
                logger.warning("Schema check could not read the database: %s", exc)
                self.error = str(exc)
                self.problems = self.problems if self.problems is not None else []
            self.at = time.monotonic()
            return self.problems

    def summary(self) -> str:
        if self.problems is None:
            return "not checked"
        if self.error:
            return "unreadable"
        return describe(self.problems)


STATE = SchemaState()


async def at_startup(recordings_pool, clinical_pool=None, repo=None) -> List[Dict[str, Any]]:
    """Check once, say so loudly, and stop the server if strict mode asks for it."""
    problems = await STATE.check(recordings_pool, clinical_pool, force=True)
    for p in problems:
        logger.critical(
            "DATABASE NOT UP TO DATE: %s is not applied to the %s database (missing: %s). "
            "Apply it from backend/scripts, then this clears by itself within a minute.",
            p["migration"], p["database"], "; ".join(p["missing"]))
    if problems and repo is not None:
        try:
            await repo.raise_alert(alert_type="schema_missing", severity="critical",
                                   detail={"migrations": [p["migration"] for p in problems],
                                           "missing": problems})
        except Exception as exc:          # the alerts table may be what is missing
            logger.warning("Could not record the schema alert: %s", exc)
    if problems and strict():
        raise RuntimeError("AIMS_SCHEMA_STRICT is set and the database is missing "
                           + ", ".join(p["migration"] for p in problems))
    if not problems:
        logger.info("Database schema up to date (migrations up to 030 present)")
    await _warn_if_owner(recordings_pool)
    return problems


async def _warn_if_owner(pool) -> None:
    """
    The API should log in as aims_api (migration 018), which may add to the
    audit log but never change it. Logged in as the tables' owner, it could
    switch the audit log's protection off - say so at every start.
    """
    try:
        async with pool.acquire() as conn:
            owner = await conn.fetchval("""
                SELECT pg_get_userbyid(relowner) = current_user
                    OR (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)
                  FROM pg_class WHERE oid = to_regclass('public.audit_log')""")
    except Exception as exc:
        logger.warning("Could not check which database login this server uses: %s", exc)
        return
    if owner:
        logger.critical("The API is logged in to the recordings database as the owner of "
                        "its tables (or a superuser). The audit log is not protected from "
                        "this server: connect as aims_api (migration 018).")


__all__ = ["REQUIRED", "STATE", "at_startup", "describe", "missing", "strict"]
