"""
Quarantine breakdown for AIMScribe.

Answers one question: of the sessions currently held back, which of the five
failure modes put them there? That decides what to build first. If they are
mostly chain-ordering failures the audio was never in danger and the fix is
upstream; if they are arrival mismatches the fix is the retry path in
SRS-REC-01..03.

    1  local_segment_unreadable      agent-side, before upload  - AUDIO AT RISK
    2  segment hash mismatch         backend, at commit         - audio intact
    3  chain entry rejected          backend, at commit         - audio intact
    4  duplicate seq_no              backend, at commit         - audio intact
    5  whole-chain verify at close   backend, at close          - audio intact

Only mode 1 endangers a recording, and mode 1 never reaches the server, so it is
visible only in the local spool - which is why this script looks in both places.

STRICTLY READ-ONLY. The database session is opened read-only and every statement
is a SELECT. Nothing is written, moved or deleted anywhere.

Credentials come from the environment, or from the backend's .env if not set.
They are never printed, and neither are patient identifiers.

    python quarantine_report.py                     everything it can reach
    python quarantine_report.py --local             just this PC's spool
    python quarantine_report.py --env PATH/TO/.env  point at a different .env
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ENV = Path(__file__).resolve().parents[1] / \
    "AIMScribe_Backend_Render-main" / "AIMScribe_Backend_Render-main" / ".env"
DEFAULT_SPOOL = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "AIMScribe" / "spool"

RULE = "-" * 78


# ----------------------------------------------------------------- helpers

def load_env(path: Path) -> dict:
    """Read KEY=VALUE pairs. Values are used, never echoed."""
    out = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def classify(reason: str) -> str:
    """Map the stored free text onto the five known triggers."""
    r = (reason or "").lower()
    if "hash mismatch on arrival" in r:
        return "2 · segment hash mismatch on arrival"
    if "chain entry rejected" in r:
        return "3 · chain entry rejected at commit"
    if "duplicate seq_no" in r:
        return "4 · duplicate seq_no, different content"
    if r:
        return "5 · whole-chain verification failed at close"
    return "unrecorded reason"


def bar(n: int, total: int, width: int = 26) -> str:
    if not total:
        return ""
    filled = int(round(width * n / total))
    return "#" * filled + "." * (width - filled)


# ------------------------------------------------------- 1 · the local spool

def report_local(spool: Path) -> None:
    print(f"\n{RULE}\n1 · LOCAL SPOOL ON THIS PC   {spool}\n{RULE}")
    if not spool.is_dir():
        print("  No spool directory here. Run this on a consulting-room PC to see\n"
              "  what that machine is holding.")
        return

    sessions = [d for d in sorted(spool.iterdir()) if (d / "journal.jsonl").is_file()]
    if not sessions:
        print("  Spool is empty - nothing is stranded on this machine.")
        return

    states: Counter = Counter()
    stuck_bytes = 0
    stuck_sessions = []

    for d in sessions:
        seg_state = {}
        for line in (d / "journal.jsonl").read_text(encoding="utf-8",
                                                    errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue                      # truncated tail is normal after a crash
            if rec.get("rec") == "state" and "seq_no" in rec:
                seg_state[rec["seq_no"]] = rec.get("state", "?")

        on_disk = sum(f.stat().st_size for f in d.glob("*.seg")) or \
                  sum(f.stat().st_size for f in d.iterdir()
                      if f.is_file() and f.name != "journal.jsonl")
        for st in seg_state.values():
            states[st] += 1

        held = {s for s in seg_state.values() if s not in ("purged",)}
        if held and held != {"receipted"}:
            stuck_bytes += on_disk
            worst = ("quarantined" if "quarantined" in held else
                     "pending" if "pending" in held else sorted(held)[0])
            age_days = (datetime.now(timezone.utc).timestamp()
                        - (d / "journal.jsonl").stat().st_mtime) / 86400
            stuck_sessions.append((d.name, worst, len(seg_state), on_disk, age_days))

    print(f"  sessions on disk        {len(sessions)}")
    print(f"  segments by state       " +
          ", ".join(f"{k}={v}" for k, v in sorted(states.items())) or "none")
    print(f"  bytes not yet purged    {human(stuck_bytes)}")

    if stuck_sessions:
        print(f"\n  {'session':28} {'worst state':13} {'segs':>5} {'size':>10} {'age':>8}")
        for sid, worst, n, size, age in sorted(stuck_sessions,
                                               key=lambda t: -t[4])[:20]:
            print(f"  {sid:28} {worst:13} {n:5d} {human(size):>10} {age:6.1f} d")
        q = [s for s in stuck_sessions if s[1] == "quarantined"]
        if q:
            print(f"\n  {len(q)} session(s) QUARANTINED locally, holding "
                  f"{human(sum(s[3] for s in q))}.")
            print("  These are mode 1 - the local file failed its own hash check, so the\n"
                  "  audio itself is suspect and it was never uploaded.")


# --------------------------------------------------------- 2 · backend alerts

def report_alerts(base_url: str, admin_key: str) -> None:
    print(f"\n{RULE}\n2 · OPEN INTEGRITY ALERTS   {base_url}\n{RULE}")
    if not base_url or not admin_key:
        print("  Skipped: set AIMS_BACKEND_URL and AIMS_ADMIN_KEY (or point --env at\n"
              "  the backend .env) to include this section.")
        return
    try:
        import urllib.request
        req = urllib.request.Request(
            base_url.rstrip("/") + "/api/v2/admin/alerts?limit=200",
            headers={"X-Admin-Key": admin_key})
        with urllib.request.urlopen(req, timeout=20) as resp:
            alerts = json.loads(resp.read().decode()).get("alerts", [])
    except Exception as exc:
        print(f"  Could not reach the backend: {exc}")
        return

    if not alerts:
        print("  No unresolved alerts. Note this endpoint returns only OPEN alerts -\n"
              "  anything already resolved will not appear here.")
        return

    by_type = Counter(a.get("alert_type", "?") for a in alerts)
    print(f"  {len(alerts)} unresolved alert(s)\n")
    for t, n in by_type.most_common():
        print(f"  {t:34} {n:5d}  {bar(n, len(alerts))}")

    print(f"\n  {'raised':20} {'type':28} {'severity':9}")
    for a in alerts[:15]:
        raised = str(a.get("raised_at", ""))[:19]
        print(f"  {raised:20} {a.get('alert_type','?'):28} {a.get('severity',''):9}")


# ------------------------------------------------------ 3 · database breakdown

def connect(env: dict):
    """Open a read-only connection. psycopg2 preferred; asyncpg accepted."""
    host = env.get("POSTGRES_HOST"); db = env.get("POSTGRES_DB")
    user = env.get("POSTGRES_USER"); pw = env.get("POSTGRES_PASSWORD")
    port = env.get("POSTGRES_PORT", "5432")
    ssl = env.get("POSTGRES_SSLMODE", "require")
    if not all([host, db, user, pw]):
        return None, "missing POSTGRES_* settings"
    try:
        import psycopg2
    except ImportError:
        return None, "psycopg2 is not installed (pip install psycopg2-binary)"
    try:
        conn = psycopg2.connect(host=host, port=port, dbname=db, user=user,
                                password=pw, sslmode=ssl, connect_timeout=20)
        conn.set_session(readonly=True, autocommit=True)   # belt and braces
        return conn, None
    except Exception as exc:
        return None, str(exc)


def q(cur, sql, args=()):
    cur.execute(sql, args)
    return cur.fetchall()


def report_db(env: dict) -> None:
    print(f"\n{RULE}\n3 · SESSIONS HELD BACK, BY REASON\n{RULE}")
    conn, err = connect(env)
    if conn is None:
        print(f"  Skipped: {err}")
        return

    with conn, conn.cursor() as cur:
        cols = {r[0] for r in q(cur, """
            SELECT column_name FROM information_schema.columns
             WHERE table_name = 'sessions'""")}
        if "quarantine_reason" not in cols:
            print("  This database has no quarantine_reason column - it predates the\n"
                  "  v2 integrity schema. Nothing to report.")
            return

        print("  sessions by status")
        rows = q(cur, "SELECT status, count(*) FROM sessions GROUP BY status "
                      "ORDER BY 2 DESC")
        total = sum(r[1] for r in rows)
        for status, n in rows:
            print(f"    {str(status):24} {n:6d}  {bar(n, total)}")

        rows = q(cur, """
            SELECT quarantine_reason, count(*)
              FROM sessions
             WHERE status = 'quarantined' OR quarantine_reason IS NOT NULL
             GROUP BY quarantine_reason""")
        if not rows:
            print("\n  No quarantined sessions. Nothing is being held back.")
            return

        buckets: Counter = Counter()
        for reason, n in rows:
            buckets[classify(reason)] += n
        held = sum(buckets.values())

        print(f"\n  {held} session(s) quarantined, by trigger\n")
        for name, n in sorted(buckets.items()):
            print(f"    {name:46} {n:5d}  {bar(n, held, 18)}")

        audio_ok = sum(n for k, n in buckets.items() if not k.startswith("1"))
        print(f"\n  Of these, {audio_ok} of {held} have INTACT audio - held back by a "
              f"chain\n  or transport defect, not by damage to the recording.")

        if "segments" in {r[0] for r in q(cur, """
                SELECT table_name FROM information_schema.tables
                 WHERE table_schema='public'""")}:
            rows = q(cur, """
                SELECT coalesce(sum(g.bytes), 0), count(*)
                  FROM segments g JOIN sessions s USING (session_id)
                 WHERE s.status = 'quarantined'""")
            by, cnt = rows[0]
            print(f"  Audio in those sessions: {cnt} segment(s), {human(float(by or 0))}.")

        print(f"\n  {'session':30} {'clinic':18} {'opened':11} {'trigger':30}")
        detail_cols = "s.session_id, s.hospital_id, s.opened_at, s.quarantine_reason"
        for sid, hosp, opened, reason in q(cur, f"""
                SELECT {detail_cols} FROM sessions s
                 WHERE s.status = 'quarantined'
                 ORDER BY s.opened_at DESC NULLS LAST LIMIT 25"""):
            when = str(opened)[:10] if opened else "-"
            print(f"  {str(sid)[:30]:30} {str(hosp)[:18]:18} {when:11} "
                  f"{classify(reason)[:30]}")

        print("\n  Reminder: mode 1 (local_segment_unreadable) never reaches this\n"
              "  database, because those segments are never uploaded. Section 1 is the\n"
              "  only place they appear, and only on the PC holding them.")
    conn.close()


# ------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only quarantine breakdown.")
    ap.add_argument("--env", type=Path, default=DEFAULT_ENV,
                    help="backend .env to read credentials from")
    ap.add_argument("--spool", type=Path, default=DEFAULT_SPOOL)
    ap.add_argument("--local", action="store_true", help="local spool only")
    args = ap.parse_args()

    env = {**load_env(args.env), **{k: v for k, v in os.environ.items()
                                    if k.startswith(("POSTGRES_", "AIMS_"))}}

    print(f"AIMScribe quarantine report   {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("read-only: no writes, no deletions, no credentials printed")

    report_local(args.spool)
    if not args.local:
        report_alerts(env.get("AIMS_BACKEND_URL",
                              "https://aimscribe-backend-render.onrender.com"),
                      env.get("AIMS_ADMIN_KEY", ""))
        report_db(env)

    print(f"\n{RULE}\nWhat the answer decides\n{RULE}")
    print("  mostly 3 or 5  ->  chain ordering is the bug. Fix delivery order and")
    print("                     retry of pause/resume entries; recovery is secondary.")
    print("  mostly 2       ->  uploads are being truncated. SRS-REC-01..03 (re-verify")
    print("                     locally, re-upload to a fresh key) is the fix.")
    print("  mostly 1       ->  local media is failing. Check disks and antivirus")
    print("                     before writing any recovery code.")
    print("  any of them    ->  SRS-REC-07 still applies: in modes 2-5 the")
    print("                     conversation is intact and should reach the server.\n")


if __name__ == "__main__":
    main()
