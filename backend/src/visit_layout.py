"""
Put what is already archived into its visit's folder (migration 044,
clinical/006). Run once after deploying, by scripts/backfill_visit_layout.py.

Three steps, each safe to run again:

  1. Every recording (and each patient's part of one) gets its visit key.
  2. Every cloud copy still only in copies/<clinic>/<doctor>/<day>/ is copied,
     inside the store, to visits/<patient_id>/<visit_key>/ under the same file
     name - the encryption binds the name, so the copy restores exactly as the
     original does. Nothing is downloaded, decrypted or encrypted again. The
     new object is read back (size, and the store's fingerprint when it has
     one) and only then recorded, as a new cloud_copies row pointing at the
     one it came from. The old object and its row stay.
  3. Every copy in a visit's folder is entered in the clinical database's
     visit_objects, so v_visit_folder and patient_folder() show it.

Without `apply` it only says what it would do.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import api_v2

logger = logging.getLogger(__name__)


def _store_for(kind: str):
    """The bucket a copy of this kind is kept in: as api_v2._copy_bucket."""
    ctx = api_v2.ctx
    if kind == "json" and ctx.copy_json is not None:
        return ctx.copy_json
    return ctx.copy


def _same_object(a, b) -> Optional[str]:
    """None when two stat results describe the same bytes, else why not."""
    if int(getattr(a, "size", -1)) != int(getattr(b, "size", -2)):
        return f"size {getattr(b, 'size', None)} is not {getattr(a, 'size', None)}"
    ea = (getattr(a, "etag", "") or "").strip('"').lower()
    eb = (getattr(b, "etag", "") or "").strip('"').lower()
    # A multipart object's ETag is not its MD5 and a copy may be cut differently:
    # then the size stands alone, as in api_v2._confirm_stored.
    if ea and eb and "-" not in ea and "-" not in eb and ea != eb:
        return "the store's fingerprints differ"
    return None


def _copy_in_store(store, source: str, target: str) -> None:
    """A server-side copy within one bucket. Sync; called in a thread."""
    from minio.commonconfig import CopySource
    store.client.copy_object(store.bucket, target, CopySource(store.bucket, source))


async def _part_of(repo, session: Dict[str, Any], object_key: str) -> Optional[Dict[str, Any]]:
    """The part a copy belongs to, by its file name, or None for the whole recording."""
    name = object_key.rsplit("/", 1)[-1]
    for part in await repo.parts_for(session["session_id"]):
        stem = part.get("file_stem")
        if stem and name.startswith(stem + ".v"):
            return part
    return None


async def set_visit_keys(*, apply: bool) -> Dict[str, int]:
    repo = api_v2._repo()
    sessions, parts = await repo.without_visit_key()
    done = {"sessions": 0, "parts": 0}
    by_id = {s["session_id"]: s for s in sessions}
    for session in sessions:
        key = await api_v2.visit_key_of(session)
        if key:
            done["sessions"] += 1
            if apply:
                await repo.set_visit_key(session["session_id"], key)
    for part in parts:
        session = by_id.get(part["session_id"]) or await repo.get_session(part["session_id"])
        key = await api_v2.visit_key_of(session or {"session_id": part["session_id"]}, part)
        if key:
            done["parts"] += 1
            if apply:
                await repo.set_visit_key(part["session_id"], key, part_no=part["part_no"])
    return done


async def move_copies(*, apply: bool, limit: int = 500) -> Dict[str, Any]:
    repo = api_v2._repo()
    loop = asyncio.get_running_loop()
    out: Dict[str, Any] = {"moved": 0, "would_move": 0, "skipped": [], "failed": []}
    for copy in await repo.copies_to_move(limit):
        session = await repo.get_session(copy["session_id"])
        if session is None or session.get("confirmation") in ("refused", "expired") \
                or session.get("patient_id") == "REDACTED":
            out["skipped"].append({"object_key": copy["object_key"], "why": "erased"})
            continue
        part = await _part_of(repo, session, copy["object_key"])
        key = await api_v2.visit_key_of(session, part)
        patient = (part or session).get("patient_id")
        if not key or not patient:
            out["skipped"].append({"object_key": copy["object_key"], "why": "no visit key"})
            continue
        name = copy["object_key"].rsplit("/", 1)[-1]
        target = f"visits/{patient}/{key}/{name}"
        if not apply:
            out["would_move"] += 1
            logger.info("would copy %s -> %s", copy["object_key"], target)
            continue
        store = _store_for(copy["kind"])
        try:
            await loop.run_in_executor(None, _copy_in_store, store, copy["object_key"], target)
            old = await loop.run_in_executor(None, store.client.stat_object, store.bucket,
                                             copy["object_key"])
            new = await loop.run_in_executor(None, store.client.stat_object, store.bucket,
                                             target)
        except Exception as exc:
            out["failed"].append({"object_key": copy["object_key"], "why": str(exc)[:300]})
            continue
        problem = _same_object(old, new)
        if problem is None and int(getattr(new, "size", -1)) != int(copy["bytes"]):
            problem = f"size {getattr(new, 'size', None)} is not the {copy['bytes']} recorded"
        if problem:
            # Not recorded: the old copy stays the one that counts.
            out["failed"].append({"object_key": copy["object_key"], "why": problem})
            await repo.raise_alert(alert_type="cloud_copy_unverified", severity="critical",
                                   session_id=copy["session_id"],
                                   detail={"object_key": target, "copied_from":
                                           copy["object_key"], "problem": problem})
            continue
        await repo.record_moved_copy(copy, target)
        out["moved"] += 1
    return out


async def register_copies(*, apply: bool) -> Dict[str, int]:
    """Step 3: every copy in a visit's folder, in the clinical visit_objects."""
    repo, clinical = api_v2._repo(), api_v2.ctx.clinical
    out = {"registered": 0, "already": 0}
    if clinical is None:
        return out
    for row in await repo.by_visit_copies():
        parts = row["object_key"].split("/")
        if len(parts) != 4:
            continue
        if not apply:
            out["registered"] += 1
            continue
        added = await clinical.record_visit_object(
            visit_key=parts[2], patient_id=parts[1], doctor_id=row["doctor_id"] or "unknown",
            visit_id=row["visit_id"], hospital_id=row["hospital_id"],
            session_id=row["session_id"], part_no=row.get("part_no"), kind=row["kind"],
            object_key=row["object_key"], version=row["version"], byte_length=row["bytes"])
        out["registered" if added else "already"] += 1
    return out


async def backfill(*, apply: bool = False, limit: int = 500) -> Dict[str, Any]:
    """All three steps. Without `apply`, what they would do."""
    keys = await set_visit_keys(apply=apply)
    moved = await move_copies(apply=apply, limit=limit) if api_v2.ctx.copy is not None \
        else {"moved": 0, "would_move": 0, "skipped": [], "failed": ["no copy bucket configured"]}
    registered = await register_copies(apply=apply)
    return {"applied": apply, "visit_keys": keys, "copies": moved, "clinical": registered}


__all__ = ["backfill", "set_visit_keys", "move_copies", "register_copies"]


# ---------------------------------------------------------------- run on the server

async def _main(apply: bool, limit: int) -> int:
    """With the API's own settings: run inside the API container."""
    import json
    import os
    import asyncpg
    from config import settings
    from clinical_store import ClinicalStore
    from database.postgres_async import AsyncPostgreSQLDatabase, pooler_options
    from db_v2 import V2Repository
    from main_fastapi import AsyncAppContext

    recordings = AsyncPostgreSQLDatabase(
        host=settings.postgres_host, port=settings.postgres_port,
        dbname=settings.postgres_db, user=settings.postgres_user,
        password=settings.postgres_password, sslmode=settings.postgres_sslmode)
    rec = await asyncpg.create_pool(recordings.dsn, min_size=1, max_size=2,
                                    ssl=True if settings.postgres_sslmode == "require" else None,
                                    **pooler_options())
    clinical_url = os.getenv("AIMS_CLINICAL_DATABASE_URL", "").strip()
    cli = await asyncpg.create_pool(clinical_url, min_size=1, max_size=2,
                                    **pooler_options()) if clinical_url else None
    api_v2.ctx.repo = V2Repository(rec)
    api_v2.ctx.clinical = ClinicalStore(cli, separate=True) if cli else None
    api_v2.ctx.copy = AsyncAppContext._copy_bucket()
    api_v2.ctx.copy_json = AsyncAppContext._copy_bucket(prefix="AIMS_JSON") \
        if os.getenv("AIMS_JSON_BUCKET", "").strip() else None
    try:
        report = await backfill(apply=apply, limit=limit)
    finally:
        await rec.close()
        if cli is not None:
            await cli.close()
    print(json.dumps(report, indent=2, default=str))
    failed = report["copies"].get("failed") or []
    return 1 if failed else 0


if __name__ == "__main__":                         # pragma: no cover - the server's own run
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="Put archived copies in their visit's folder.")
    parser.add_argument("--apply", action="store_true",
                        help="do it (without this, only say what would be done)")
    parser.add_argument("--limit", type=int, default=500, help="copies per run")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(asyncio.run(_main(args.apply, args.limit)))
