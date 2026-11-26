"""
Erase the recordings made while testing (23 Nov 2026).

Each recording goes through the server's own admin erase,
POST /api/v2/admin/session/{id}/erase (api_v2.admin_erase_session), one at a
time, so nothing new decides what is deleted. For each one that removes the
audio pieces in the cloud store, the archived WAVs (an erase task for the
archive worker), the encrypted cloud copies (audio and JSON), CMED's clinical
records of the visit, and the patient from the recording's row. A bare row
marked "refused" stays, without the patient, and the audit log says who
erased it. It cannot be undone.

On the server, inside the API container (it has every setting this needs):

    docker exec aims-api python /app/src/erase_test_data.py --clinic TEST
        lists what it would erase; deletes nothing
    docker exec aims-api python /app/src/erase_test_data.py --clinic TEST \\
        --erase --expect 64 --by "<your name>"
        erases exactly that list

Safeguards:
  - Without --erase nothing is deleted.
  - --erase needs --expect N, the number the list showed: if anything changed
    since (a new recording), nothing is erased.
  - --clinic takes only a clinic whose ID or name says it is a test clinic
    ("test" or "simulation"). A real clinic's recordings are erased only by
    naming each one with --session.
  - A recording still being made (open, started in the last day) is skipped.
  - One already erased is skipped.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

_TEST_WORDS = re.compile(r"test|simulation", re.IGNORECASE)


def is_test_clinic(hospital_id: str, name: Optional[str]) -> bool:
    """A clinic set up for testing says so in its ID or its name."""
    return bool(_TEST_WORDS.search(hospital_id or "") or _TEST_WORDS.search(name or ""))


_LIST = """
SELECT s.session_id, s.hospital_id, h.name AS clinic_name, s.session_date,
       s.local_start, s.opened_at, s.closed_at, s.confirmation, s.doctor_id,
       d.machine_name AS pc, s.total_duration_seconds AS seconds,
       (s.archive_relpath IS NOT NULL) AS archived, (s.copied_at IS NOT NULL) AS in_cloud
  FROM sessions s
  LEFT JOIN hospitals h ON h.hospital_id = s.hospital_id
  LEFT JOIN devices d ON d.device_id = s.device_id
 WHERE s.patient_id IS DISTINCT FROM 'REDACTED'
   AND (s.hospital_id = ANY($1::text[]) OR s.session_id = ANY($2::text[]))
 ORDER BY s.hospital_id, s.opened_at, s.session_id
"""


async def select_recordings(conn, clinics: Sequence[str],
                            sessions: Sequence[str] = ()) -> Dict[str, Any]:
    """
    What would be erased: every recording of the named test clinics and the
    named recordings, not yet erased and not being made now. A clinic that is
    not a test clinic is refused here, before anything is listed.
    """
    clinics = sorted({c.strip() for c in clinics if c and c.strip()})
    sessions = sorted({s.strip().upper() for s in sessions if s and s.strip()})
    known = {r["hospital_id"]: r["name"] for r in await conn.fetch(
        "SELECT hospital_id, name FROM hospitals WHERE hospital_id = ANY($1::text[])", clinics)}
    unknown = [c for c in clinics if c not in known]
    real = [c for c in clinics if c in known and not is_test_clinic(c, known[c])]
    if unknown or real:
        return {"refused": True, "unknown_clinics": unknown, "real_clinics": real,
                "recordings": [], "skipped": []}
    rows = [dict(r) for r in await conn.fetch(_LIST, clinics, sessions)]
    now = await conn.fetchval("SELECT now()")
    chosen, skipped = [], []
    for r in rows:
        recording = (r["closed_at"] is None and r["opened_at"] is not None
                     and (now - r["opened_at"]).total_seconds() < 24 * 3600)
        (skipped if recording else chosen).append(
            {**r, **({"why": "still being recorded"} if recording else {})})
    found = {r["session_id"] for r in rows}
    missing = [s for s in sessions if s not in found]
    return {"refused": False, "unknown_clinics": [], "real_clinics": [],
            "recordings": chosen, "skipped": skipped, "sessions_not_found": missing}


def describe(selection: Dict[str, Any]) -> str:
    """The list, as a person reads it before saying yes."""
    if selection["refused"]:
        lines = ["Nothing listed."]
        if selection["unknown_clinics"]:
            lines.append("No such clinic: " + ", ".join(selection["unknown_clinics"]))
        if selection["real_clinics"]:
            lines.append("Not a test clinic: " + ", ".join(selection["real_clinics"])
                         + ". Name its test recordings one by one with --session.")
        return "\n".join(lines)
    out = []
    by_clinic: Dict[str, List[Dict[str, Any]]] = {}
    for r in selection["recordings"]:
        by_clinic.setdefault(r["hospital_id"], []).append(r)
    for clinic, rows in by_clinic.items():
        out.append(f"\n{clinic} ({rows[0]['clinic_name'] or ''}): {len(rows)} recordings")
        out.append(f"  {'recording':<28} {'day':<10} {'start':<5} {'min':>5} "
                   f"{'PC':<18} {'doctor':<12} {'result':<12} archive cloud")
        for r in rows:
            start = r["local_start"].strftime("%H:%M") if r["local_start"] else "-"
            minutes = f"{(r['seconds'] or 0) / 60:.1f}"
            out.append(f"  {r['session_id']:<28} {str(r['session_date'] or '-'):<10} "
                       f"{start:<5} {minutes:>5} {(r['pc'] or '-')[:18]:<18} "
                       f"{(r['doctor_id'] or '-')[:12]:<12} {(r['confirmation'] or '-'):<12} "
                       f"{'yes' if r['archived'] else 'no':<7} "
                       f"{'yes' if r['in_cloud'] else 'no'}")
    for r in selection["skipped"]:
        out.append(f"  skipped {r['session_id']} ({r['hospital_id']}): {r['why']}")
    for s in selection.get("sessions_not_found") or []:
        out.append(f"  not found, or already erased: {s}")
    total = len(selection["recordings"])
    out.append(f"\nTotal to erase: {total} recordings"
               + (f" ({sum(1 for r in selection['recordings'] if r['archived'])} archived, "
                  f"{sum(1 for r in selection['recordings'] if r['in_cloud'])} in the cloud)"
                  if total else ""))
    return "\n".join(out).lstrip("\n")


async def erase(session_ids: Sequence[str], *, by: str, base_url: str, admin_key: str,
                client=None) -> Dict[str, Any]:
    """Each through the server's admin erase, one at a time; a failure stops nothing else."""
    import httpx
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(120.0))
    done, failed = [], []
    try:
        for sid in session_ids:
            try:
                answer = await client.post(
                    f"{base_url}/api/v2/admin/session/{sid}/erase",
                    params={"actor": f"erase_test_data.py ({by})"},
                    headers={"X-Admin-Key": admin_key})
            except Exception as exc:                    # the next one may still go
                failed.append({"session_id": sid, "error": type(exc).__name__})
                continue
            if answer.status_code == 200:
                done.append({"session_id": sid,
                             "objects_deleted": answer.json().get("objects_deleted")})
            else:
                failed.append({"session_id": sid, "status": answer.status_code,
                               "error": answer.text[:200]})
    finally:
        if own:
            await client.aclose()
    return {"erased": done, "failed": failed}


async def _main(args) -> int:
    """With the API's own settings: run inside the API container."""
    import asyncpg
    from config import settings
    from database.postgres_async import AsyncPostgreSQLDatabase, pooler_options

    recordings = AsyncPostgreSQLDatabase(
        host=settings.postgres_host, port=settings.postgres_port,
        dbname=settings.postgres_db, user=settings.postgres_user,
        password=settings.postgres_password, sslmode=settings.postgres_sslmode)
    pool = await asyncpg.create_pool(recordings.dsn, min_size=1, max_size=1,
                                     ssl=True if settings.postgres_sslmode == "require" else None,
                                     **pooler_options())
    try:
        async with pool.acquire() as conn:
            selection = await select_recordings(conn, args.clinic or [], args.session or [])
    finally:
        await pool.close()
    print(describe(selection))
    if selection["refused"]:
        return 2
    if not args.erase:
        print("\nNothing was erased (this was the list only). To erase exactly these, "
              f"add: --erase --expect {len(selection['recordings'])} --by \"<your name>\"")
        return 0
    if args.expect != len(selection["recordings"]):
        print(f"\nNothing was erased: the list has {len(selection['recordings'])} recordings, "
              f"--expect said {args.expect}. List again and check it.")
        return 2
    if not (args.by or "").strip():
        print("\nNothing was erased: say who is erasing with --by.")
        return 2
    key = os.getenv("AIMS_ADMIN_KEY", "").strip()
    if not key:
        print("\nNothing was erased: AIMS_ADMIN_KEY is not set here (run it in aims-api).")
        return 2
    port = os.getenv("SERVER_PORT", "6000").strip() or "6000"
    report = await erase([r["session_id"] for r in selection["recordings"]], by=args.by.strip(),
                         base_url=f"http://localhost:{port}", admin_key=key)
    print(f"\nErased: {len(report['erased'])}; failed: {len(report['failed'])}")
    for f in report["failed"]:
        print(f"  failed {f['session_id']}: {f.get('status', '')} {f['error']}")
    print("The archive worker removes the archived WAVs at its next round.")
    return 1 if report["failed"] else 0


if __name__ == "__main__":                         # pragma: no cover - the server's own run
    import argparse
    parser = argparse.ArgumentParser(
        description="List, then erase, the recordings made while testing.")
    parser.add_argument("--clinic", action="append",
                        help="a test clinic's ID (repeat for several)")
    parser.add_argument("--session", action="append",
                        help="one recording to erase, from any clinic (repeat for several)")
    parser.add_argument("--erase", action="store_true",
                        help="erase them (without this, only list them)")
    parser.add_argument("--expect", type=int, default=-1,
                        help="with --erase: the number of recordings the list showed")
    parser.add_argument("--by", default="", help="with --erase: who is erasing them")
    args = parser.parse_args()
    if not args.clinic and not args.session:
        parser.error("name at least one --clinic or --session")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(asyncio.run(_main(args)))
