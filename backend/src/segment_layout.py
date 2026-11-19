"""
Put the pieces already in the segment store into the archive's layout
(20 Nov 2026). New recordings are given these addresses when they open
(api_v2.segment_folder); this moves the ones that arrived before, and any
recording whose visit or clinic changed after it opened:

    audio/<hospital>/<doctor>/<YYYY-MM-DD>/<patient>_<doctor>_<visit_id>/<HHMMSS>_<tail>/piece_0001.wav

Only a recording that is finished and still waiting for CMED is moved: the
PC has sent every piece, nothing is archiving it and nothing is erasing it.
For each of its pieces:

  1. the piece is copied, inside the store, to its new address;
  2. the copy is read back and its SHA-256 compared with the one taken when
     the piece arrived - any difference, and every copy made for that
     recording is removed and the recording is left as it was;
  3. every piece's address is changed in the database at once, with the
     recording's row locked: if CMED confirmed it (or it was erased) first,
     nothing is changed and the copies are removed;
  4. only then are the pieces at the old addresses deleted.

So at every moment each piece has one address the database points at, and
the bytes are there. Without `apply` it only lists what it would move.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple

import api_v2

logger = logging.getLogger(__name__)


def _copy(store, source: str, target: str) -> None:
    """A server-side copy within the segment bucket. Sync; called in a thread."""
    from minio.commonconfig import CopySource
    store.client.copy_object(store.bucket, target, CopySource(store.bucket, source))


def _sha256_of(store, key: str) -> Tuple[bytes, int]:
    """The SHA-256 and length of an object as the store returns it."""
    response = store.client.get_object(store.bucket, key)
    try:
        digest, size = hashlib.sha256(), 0
        for chunk in response.stream(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    finally:
        response.close()
        response.release_conn()
    return digest.digest(), size


def _remove(store, key: str) -> None:
    store.client.remove_object(store.bucket, key)


async def target_folder(session: Dict[str, Any]) -> Optional[str]:
    """
    Where a waiting recording belongs now: its clinic, doctor and day, and its
    visit's folder - CMED's visit_id from the recording, its grant, or CMED's
    own API 2/3; the start time when none has one, as at open.
    """
    repo = api_v2._repo()
    local = await api_v2._local_time(session["hospital_id"], session["opened_at"])
    visit = session.get("visit_id")
    if not visit and session.get("grant_jti"):
        grant = await repo.get_authorisation(session["grant_jti"])
        if grant is not None and grant.get("patient_id") == session["patient_id"]:
            visit = grant.get("visit_id") or await api_v2._cmed_visit_id(
                grant.get("cmed_hospital_id"), grant["patient_id"], grant.get("start_time"))
    if visit and not api_v2.VISIT_ID.match(str(visit)):
        visit = None
    folder = api_v2.visit_key(session["patient_id"], session["doctor_id"], visit,
                              fallback=f"{local:%Y%m%dT%H%M%S}")
    return api_v2.segment_folder(session["hospital_id"], session["doctor_id"], local,
                                 session["session_id"], folder)


async def move_waiting(*, apply: bool, limit: int = 500) -> Dict[str, Any]:
    repo = api_v2._repo()
    store = api_v2.ctx.minio
    loop = asyncio.get_running_loop()
    out: Dict[str, Any] = {"applied": apply, "recordings_moved": 0, "pieces_moved": 0,
                           "would_move": [], "skipped": [], "failed": [],
                           "old_pieces_not_deleted": []}
    for session in await repo.pieces_waiting_to_move(limit):
        sid = session["session_id"]
        pieces = [p for p in await repo.segments_for(sid) if p.get("object_deleted_at") is None]
        if session.get("segment_count") is not None \
                and int(session["pieces"]) != int(session["segment_count"]):
            out["skipped"].append({"recording": sid, "why":
                                   f"{session['pieces']} of {session['segment_count']} "
                                   "pieces arrived"})
            continue
        if any(p.get("state") != "committed" for p in pieces):
            out["skipped"].append({"recording": sid, "why": "a piece is not plainly stored"})
            continue
        folder = await target_folder(session)
        if folder is None:
            out["skipped"].append({"recording": sid, "why": "no safe folder name"})
            continue
        moves = [(int(p["seq_no"]), p["object_key"],
                  f"audio/{folder}/piece_{int(p['seq_no']):04d}.wav") for p in pieces]
        moves = [m for m in moves if m[1] != m[2]]
        if not moves and session.get("object_prefix") == folder:
            continue
        if not apply:
            out["would_move"].append({"recording": sid, "to": f"audio/{folder}/",
                                      "pieces": [{"from": old, "to": new}
                                                 for _, old, new in moves]})
            continue

        # 1-2: copy each piece and prove the copy is the piece.
        made: List[str] = []
        problem = None
        expected = {int(p["seq_no"]): (bytes(p["sha256"]), int(p["bytes"])) for p in pieces}
        for seq_no, old, new in moves:
            try:
                await loop.run_in_executor(None, _copy, store, old, new)
                made.append(new)
                digest, size = await loop.run_in_executor(None, _sha256_of, store, new)
            except Exception as exc:
                problem = f"piece {seq_no}: {str(exc)[:200]}"
                break
            if (digest, size) != expected[seq_no]:
                problem = (f"piece {seq_no}: the copy is not the piece "
                           f"({size} bytes, sha256 {digest.hex()[:12]}...)")
                break

        # 3: switch the database over, all at once, if it is still waiting.
        switched = problem is None and await repo.move_pieces(sid, moves, folder)
        if not switched:
            for key in made:
                try:
                    await loop.run_in_executor(None, _remove, store, key)
                except Exception as exc:
                    logger.warning("Could not remove the copy %s: %s", key, exc)
            if problem:
                out["failed"].append({"recording": sid, "why": problem})
                await repo.raise_alert(alert_type="segment_move_failed", severity="warning",
                                       session_id=sid, detail={"problem": problem})
            else:
                out["skipped"].append({"recording": sid, "why":
                                       "confirmed or changed during the move; left as it was"})
            continue

        # 4: the old addresses, now that nothing points at them.
        for _, old, _new in moves:
            try:
                await loop.run_in_executor(None, _remove, store, old)
            except Exception as exc:
                out["old_pieces_not_deleted"].append({"address": old, "why": str(exc)[:200]})
        await repo.audit(event_type="segments.moved", actor_type="admin",
                         actor_id="segment_layout", session_id=sid,
                         detail={"from": session.get("object_prefix"), "to": folder,
                                 "pieces": len(moves)})
        out["recordings_moved"] += 1
        out["pieces_moved"] += len(moves)
    return out


__all__ = ["move_waiting", "target_folder"]


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
    from storage.minio_client import get_minio_client

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
    api_v2.ctx.minio = get_minio_client()
    try:
        report = await move_waiting(apply=apply, limit=limit)
    finally:
        await rec.close()
        if cli is not None:
            await cli.close()
    print(json.dumps(report, indent=2, default=str))
    return 1 if report["failed"] or report["old_pieces_not_deleted"] else 0


if __name__ == "__main__":                         # pragma: no cover - the server's own run
    import argparse
    import sys
    parser = argparse.ArgumentParser(
        description="Put waiting pieces into the archive's folder layout.")
    parser.add_argument("--apply", action="store_true",
                        help="do it (without this, only list what would be moved)")
    parser.add_argument("--limit", type=int, default=500, help="recordings per run")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(asyncio.run(_main(args.apply, args.limit)))
