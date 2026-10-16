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
    ("clinical/001_aims_clinical.sql", "clinical", "table encounters", _table("encounters")),
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
        logger.info("Database schema up to date (migrations up to 015 present)")
    return problems


__all__ = ["REQUIRED", "STATE", "at_startup", "describe", "missing", "strict"]
