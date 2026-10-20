#!/usr/bin/env python3
"""
Remove TEST_* recordings and patients from THIS laptop's local stack.

    python tools/purge_test_data.py            what would go (changes nothing)
    python tools/purge_test_data.py --apply    remove it

For the manual checks in LOCAL_MANUAL.pdf, which create patients named TEST_...
Removed: the recordings and everything hanging off them in aims_recordings, the
TEST_ patients in aims_clinical, their cloud and JSON copies in the local object
store, their folders in deploy/local/archive and the catalogue rows.

Kept on purpose: audit_log and clinical_access_log are append-only by design -
their triggers refuse deletes, so the system's history cannot be quietly
rewritten - and recordings not named TEST_.

Refuses to run against anything but the local containers (aimscribe-v3-*).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "deploy" / "local" / "archive"
PG, API = "aimscribe-v3-postgres", "aimscribe-v3-server"
LIKE = r"TEST\_%"

RECORDINGS = f"""
CREATE TEMP TABLE s AS SELECT session_id FROM sessions WHERE patient_id LIKE '{LIKE}';
SELECT 'recordings' AS what, count(*) FROM s;
DELETE FROM cloud_copies WHERE session_id IN (SELECT * FROM s);
DELETE FROM consultation_parts WHERE session_id IN (SELECT * FROM s);
DELETE FROM session_clicks WHERE session_id IN (SELECT * FROM s);
DELETE FROM segments WHERE session_id IN (SELECT * FROM s);
DELETE FROM chain_entries WHERE session_id IN (SELECT * FROM s);
DELETE FROM purge_receipts WHERE session_id IN (SELECT * FROM s);
DELETE FROM integrity_alerts WHERE session_id IN (SELECT * FROM s);
DELETE FROM used_grants WHERE session_id IN (SELECT * FROM s);
DELETE FROM session_refusals WHERE session_id IN (SELECT * FROM s);
DELETE FROM confirmation_notices WHERE patient_id LIKE '{LIKE}';
DELETE FROM grant_authorisations WHERE session_id IN (SELECT * FROM s) OR patient_id LIKE '{LIKE}';
DELETE FROM patient_recordings WHERE session_id IN (SELECT * FROM s);
UPDATE sessions SET continues_session_id = NULL
 WHERE continues_session_id IN (SELECT * FROM s);
DELETE FROM sessions WHERE session_id IN (SELECT * FROM s);
"""

CLINICAL = f"""
SELECT 'clinical patients' AS what, count(*) FROM patients WHERE patient_id LIKE '{LIKE}';
DELETE FROM female_details WHERE patient_id LIKE '{LIKE}';
DELETE FROM male_details WHERE patient_id LIKE '{LIKE}';
DELETE FROM encounters WHERE patient_id LIKE '{LIKE}';
DELETE FROM intake_records WHERE patient_id LIKE '{LIKE}';
DELETE FROM patients WHERE patient_id LIKE '{LIKE}';
"""

OBJECTS = r'''
import os, sys
from minio import Minio
from minio.deleteobjects import DeleteObject
sids = set(filter(None, open("/tmp/purge_sids.txt").read().split(",")))
apply = sys.argv[1:] == ["--apply"]
c = Minio(os.environ["MINIO_ENDPOINT"], access_key=os.environ["MINIO_ACCESS_KEY"],
          secret_key=os.environ["MINIO_SECRET_KEY"],
          secure=os.environ.get("MINIO_SECURE", "").lower() in ("1", "true"))
total = 0
for b in sorted({os.environ["MINIO_BUCKET"], os.environ["AIMS_JSON_BUCKET"],
                 os.environ["AIMS_COPY_BUCKET"]}):
    if not c.bucket_exists(b):
        continue
    keys = [o.object_name for o in c.list_objects(b, recursive=True)
            if "/TEST_" in o.object_name or any(s in o.object_name for s in sids)]
    total += len(keys)
    if apply and keys:
        list(c.remove_objects(b, [DeleteObject(k) for k in keys]))
print(total)
'''


def docker(*args, stdin=None):
    done = subprocess.run(["docker", *args], input=stdin, capture_output=True, text=True,
                          env={**os.environ, "MSYS_NO_PATHCONV": "1"})
    if done.returncode != 0:
        sys.exit(f"docker {' '.join(args[:3])} failed: {(done.stderr or done.stdout).strip()}")
    return done.stdout


def owner(db: str) -> str:
    return docker("exec", PG, "psql", "-U", "aims_admin", "-d", db, "-tAc",
                  "select tableowner from pg_tables where tablename='patients'").strip()


def run_sql(db: str, sql: str, apply: bool) -> str:
    script = f"BEGIN;\nSET ROLE {owner(db)};\n{sql}\n{'COMMIT' if apply else 'ROLLBACK'};\n"
    return docker("exec", "-i", PG, "psql", "-U", "aims_admin", "-d", db, "-v",
                  "ON_ERROR_STOP=1", "-tA", "-q", "-f", "-", stdin=script)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="remove it (default: show only)")
    args = ap.parse_args()

    names = docker("ps", "--format", "{{.Names}}").split()
    if PG not in names or API not in names:
        sys.exit("The local stack is not running (aimscribe-v3-postgres / -server).")

    sids = [l for l in docker("exec", PG, "psql", "-U", "aims_admin", "-d", "aims_recordings",
                              "-tAc", f"select session_id from sessions where patient_id "
                                      f"like '{LIKE}'").split() if l]
    folders = [p for p in ARCHIVE.glob("*/*/*/TEST_*") if p.is_dir()]

    with tempfile.TemporaryDirectory() as tmp:
        ids = Path(tmp, "purge_sids.txt")
        ids.write_text(",".join(sids))
        code = Path(tmp, "purge_objects.py")
        code.write_text(OBJECTS)
        docker("cp", str(ids), f"{API}:/tmp/purge_sids.txt")
        docker("cp", str(code), f"{API}:/tmp/purge_objects.py")
        objects = docker("exec", API, "python", "/tmp/purge_objects.py",
                         *(["--apply"] if args.apply else [])).strip()
        docker("exec", "-u", "root", API, "rm", "-f", "/tmp/purge_sids.txt",
               "/tmp/purge_objects.py")

    def count(output: str) -> str:
        first = next((line for line in output.splitlines() if "|" in line), "|0")
        return first.rsplit("|", 1)[1].strip()

    rec = count(run_sql("aims_recordings", RECORDINGS, args.apply))
    cli = count(run_sql("aims_clinical", CLINICAL, args.apply))
    print(f"recordings          {rec}")
    print(f"clinical patients   {cli}")
    print(f"storage objects     {objects}")
    print(f"archive folders     {len(folders)}")

    if args.apply:
        for f in folders:
            shutil.rmtree(f)
        catalogue = ARCHIVE / "catalogue.sqlite3"
        if catalogue.exists() and sids:
            db = sqlite3.connect(catalogue)
            marks = ",".join("?" * len(sids))
            for table in ("archived_parts", "archived_pauses", "archived_clips",
                          "archived_sessions"):
                try:
                    db.execute(f"DELETE FROM {table} WHERE session_id IN ({marks})", sids)
                except sqlite3.OperationalError:
                    pass
            db.commit()
            db.close()
        sys.path.insert(0, str(ROOT / "backend" / "archive_worker"))
        import archive                                        # noqa: E402
        for day in {f.parent for f in folders}:
            if any(child.is_dir() for child in day.iterdir()):
                archive.update_day_index(day)
            else:
                shutil.rmtree(day)
        print("removed. audit_log and clinical_access_log are kept: they are append-only.")
    else:
        print("nothing changed - run again with --apply to remove it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
