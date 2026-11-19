"""
Put what is already archived into its visit's folder (migration 044,
clinical/006). Run once after deploying, by scripts/backfill_visit_layout.py.

Three steps, each safe to run again:

  1. Every recording (and each patient's part of one) gets its visit key.
  2. Every cloud copy not yet in its visit's folder - loose in a day's folder,
     or in the 17 Nov visits/<patient>/ layout - is copied, inside the store,
     to <hospital>/<doctor>/<day>/<patient>_<doctor>_<visit_id>/ (the
     user's layout, 18 Nov) under the same file name - the encryption binds the name, so the copy restores exactly as the
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
        named = {**session, "patient_id": patient,
                 "doctor_id": (part or session).get("doctor_id")}
        # The day's folder, then the visit's; the file keeps its name (the
        # encryption binds it).
        folder = api_v2.copy_object_key(named, copy["kind"], copy["version"], key)
        target = f"{folder.rsplit('/', 1)[0]}/{name}"
        if target == copy["object_key"]:
            continue
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
        key = api_v2.visit_folder_of(row["object_key"])
        if key is None:
            continue
        if not apply:
            out["registered"] += 1
            continue
        added = await clinical.record_visit_object(
            visit_key=key, patient_id=row["patient_id"], doctor_id=row["doctor_id"] or "unknown",
            visit_id=row["visit_id"], hospital_id=row["hospital_id"],
            session_id=row["session_id"], part_no=row.get("part_no"), kind=row["kind"],
            object_key=row["object_key"], version=row["version"], byte_length=row["bytes"])
        out["registered" if added else "already"] += 1
    return out


async def retire_old_copies(*, apply: bool, limit: int = 1000) -> Dict[str, Any]:
    """
    Remove copies that were moved on: the by-day copies (the user's decision,
    17 Nov 2026) and the visits/<patient>/ ones (18 Nov), 045.

    Only a copy whose twin in the visit's folder is recorded with the same
    bytes and fingerprint, AND that the store confirms holds the twin now with
    the old object's size (and the same ETag where the store gives one). The
    row stays, marked removed_at, and nothing reads it for a download. A copy
    the store still holds after the delete (a bucket lock) is not marked; it
    is asked for again on the next run.
    """
    repo = api_v2._repo()
    loop = asyncio.get_running_loop()
    out: Dict[str, Any] = {"removed": 0, "would_remove": 0, "kept": []}
    for row in await repo.old_copies_with_twin(limit):
        if bytes(row["sha256"]) != bytes(row["twin_sha256"])                 or int(row["bytes"]) != int(row["twin_bytes"]):
            out["kept"].append({"object_key": row["object_key"],
                                "why": "the recorded twin is not the same bytes"})
            continue
        store = _store_for(row["kind"])
        if store is None:
            out["kept"].append({"object_key": row["object_key"], "why": "no copy bucket"})
            continue
        try:
            twin = await loop.run_in_executor(None, store.client.stat_object, store.bucket,
                                              row["twin_key"])
        except Exception as exc:
            out["kept"].append({"object_key": row["object_key"],
                                "why": f"twin not in the store: {str(exc)[:200]}"})
            continue
        try:
            old = await loop.run_in_executor(None, store.client.stat_object, store.bucket,
                                             row["object_key"])
        except Exception:
            old = None                      # already gone: only the row is left to mark
        problem = _same_object(old, twin) if old is not None else (
            None if int(getattr(twin, "size", -1)) == int(row["bytes"])
            else "the twin's size is not the size recorded")
        if problem:
            out["kept"].append({"object_key": row["object_key"], "why": problem})
            continue
        if not apply:
            out["would_remove"] += 1
            logger.info("would remove %s (twin %s)", row["object_key"], row["twin_key"])
            continue
        if old is not None:
            left = await api_v2._delete_cloud_objects([{"kind": row["kind"],
                                                        "object_key": row["object_key"]}])
            if left:
                out["kept"].append({"object_key": row["object_key"],
                                    "why": "the store still holds it (a lock?)"})
                continue
        await repo.mark_copy_removed(row["id"])
        if api_v2.ctx.clinical is not None:
            # The visit's file register names only what is there.
            await api_v2.ctx.clinical.forget_object(row["object_key"])
        out["removed"] += 1
    if apply and out["removed"]:
        await repo.audit(event_type="copies.retired", actor_type="admin",
                         actor_id="visit_layout", detail={"removed": out["removed"],
                                                          "kept": len(out["kept"])})
    return out


async def backfill(*, apply: bool = False, limit: int = 500) -> Dict[str, Any]:
    """All three steps. Without `apply`, what they would do."""
    keys = await set_visit_keys(apply=apply)
    moved = await move_copies(apply=apply, limit=limit) if api_v2.ctx.copy is not None \
        else {"moved": 0, "would_move": 0, "skipped": [], "failed": ["no copy bucket configured"]}
    registered = await register_copies(apply=apply)
    return {"applied": apply, "visit_keys": keys, "copies": moved, "clinical": registered}


__all__ = ["backfill", "set_visit_keys", "move_copies", "register_copies",
           "retire_old_copies"]


# ---------------------------------------------------------------- run on the server

async def _main(apply: bool, limit: int, *, retire: bool = False) -> int:
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
        report = await (retire_old_copies(apply=apply) if retire
                        else backfill(apply=apply, limit=limit))
    finally:
        await rec.close()
        if cli is not None:
            await cli.close()
    print(json.dumps(report, indent=2, default=str))
    failed = report.get("kept") if retire else (report["copies"].get("failed") or [])
    return 1 if failed else 0


if __name__ == "__main__":                         # pragma: no cover - the server's own run
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="Put archived copies in their visit's folder.")
    parser.add_argument("--apply", action="store_true",
                        help="do it (without this, only say what would be done)")
    parser.add_argument("--limit", type=int, default=500, help="copies per run")
    parser.add_argument("--retire-old", action="store_true",
                        help="instead: remove the old by-day copies whose visit-folder twin "
                             "is in the store (045); with --apply only")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(asyncio.run(_main(args.apply, args.limit, retire=args.retire_old)))
