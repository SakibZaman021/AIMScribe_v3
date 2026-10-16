"""
Put right what the old previous_visit loader left in aims_clinical.

Before the previous visit carried its own five fields, every API 2 that
brought one filed it as a separate visit at midnight, under the doctor and
clinic of the API 2 that carried it. Two things followed:

  * a visit already held as a live visit was held a second time, and
  * a visit not held was filed under today's doctor, who may never have
    seen the patient then.

This script finds those rows by that exact signature - source
'previous_visit', a midnight start, and the carrying API 2's own doctor and
clinic - so a visit CMED named properly is never touched.

  duplicate     a live visit the same day says the same thing: the copy goes
  unattributed  no such live visit: kept, but its doctor and clinic become
                UNKNOWN, as the loader now files a visit CMED did not name

Nothing changes without --apply; without it the counts are only reported.
Running it twice changes nothing the second time.

    python merge_previous_visits.py                     report only
    python merge_previous_visits.py --apply             merge
    python merge_previous_visits.py --purge-test-patients --apply
                                                        also remove every
                                                        patient whose id starts
                                                        with TEST_

The database is AIMS_CLINICAL_DATABASE_URL, or --dsn. The access log is
append-only by design, so a purged test patient's past reads stay in it.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import clinical_model as cm                     # noqa: E402
from clinical_store import ClinicalStore        # noqa: E402

# The old loader's rows: midnight, and the carrying API 2's own doctor and clinic.
_OLD_COPIES = """
    SELECT e.encounter_id, e.patient_id, e.visit_date
      FROM encounters e
      JOIN intake_records i ON i.id = e.patient_information_id
     WHERE e.source = 'previous_visit'
       AND e.start_time LIKE '%T00:00:00%'
       AND e.doctor_id = i.doctor_id
       AND e.cmed_hospital_id = i.cmed_hospital_id
       AND e.doctor_id <> $1
     ORDER BY e.patient_id, e.visit_date
"""


async def run(dsn: Optional[str], *, apply: bool, purge_test_patients: bool = False,
              pool=None) -> Dict[str, Any]:
    import asyncpg

    own = pool is None
    if own:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    store = ClinicalStore(pool)
    report = {"duplicates": 0, "unattributed": 0, "kept_as_conflict": 0,
              "test_patients": 0, "applied": apply}
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                for copy in await conn.fetch(_OLD_COPIES, cm.UNKNOWN):
                    mine = await store._latest_of_encounter(conn, copy["encounter_id"])
                    same_day = await conn.fetch(
                        "SELECT encounter_id FROM encounters WHERE patient_id = $1 "
                        "AND visit_date = $2 AND encounter_id <> $3",
                        copy["patient_id"], copy["visit_date"], copy["encounter_id"])
                    duplicate = False
                    for other in same_day:
                        theirs = await store._latest_of_encounter(conn, other["encounter_id"])
                        if mine and theirs and theirs["fingerprint"] == mine["fingerprint"]:
                            duplicate = True
                            break
                    if duplicate:
                        report["duplicates"] += 1
                        if apply:
                            await conn.execute(
                                "DELETE FROM encounters WHERE encounter_id = $1",
                                copy["encounter_id"])
                        continue
                    # Unattribute. An UNKNOWN row for that day may exist already:
                    # then this copy stays as it is and is reported for review.
                    clash = await conn.fetchval("""
                        SELECT 1 FROM encounters
                         WHERE patient_id = $1 AND doctor_id = $2 AND cmed_hospital_id = $2
                           AND start_time = (SELECT start_time FROM encounters
                                              WHERE encounter_id = $3)
                           AND visit_date = $4 AND source = 'previous_visit'
                    """, copy["patient_id"], cm.UNKNOWN, copy["encounter_id"],
                        copy["visit_date"])
                    if clash:
                        report["kept_as_conflict"] += 1
                        continue
                    report["unattributed"] += 1
                    if apply:
                        await conn.execute(
                            "UPDATE encounters SET doctor_id = $2, cmed_hospital_id = $2, "
                            "hospital_id = NULL WHERE encounter_id = $1",
                            copy["encounter_id"], cm.UNKNOWN)

                if purge_test_patients:
                    patients = [r["patient_id"] for r in await conn.fetch(
                        "SELECT patient_id FROM patients WHERE patient_id LIKE 'TEST\\_%' "
                        "UNION SELECT patient_id FROM intake_records "
                        "WHERE patient_id LIKE 'TEST\\_%'")]
                    report["test_patients"] = len(patients)
                    if apply and patients:
                        await conn.execute("DELETE FROM encounters WHERE patient_id = ANY($1)",
                                           patients)
                        await conn.execute(
                            "DELETE FROM intake_records WHERE patient_id = ANY($1)", patients)
                        await conn.execute("DELETE FROM patients WHERE patient_id = ANY($1)",
                                           patients)
    finally:
        if own:
            await pool.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dsn", default=os.getenv("AIMS_CLINICAL_DATABASE_URL", ""))
    parser.add_argument("--apply", action="store_true", help="change the database")
    parser.add_argument("--purge-test-patients", action="store_true",
                        help="also remove every patient whose id starts with TEST_")
    args = parser.parse_args()
    if not args.dsn:
        print("Set AIMS_CLINICAL_DATABASE_URL or pass --dsn.", file=sys.stderr)
        return 2
    report = asyncio.run(run(args.dsn, apply=args.apply,
                             purge_test_patients=args.purge_test_patients))
    verb = "changed" if args.apply else "would change (dry run; add --apply)"
    print(f"Duplicate copies removed: {report['duplicates']}  ({verb})")
    print(f"Copies made UNKNOWN:      {report['unattributed']}  ({verb})")
    print(f"Left for review (clash):  {report['kept_as_conflict']}")
    if args.purge_test_patients:
        print(f"TEST_ patients removed:   {report['test_patients']}  ({verb})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
