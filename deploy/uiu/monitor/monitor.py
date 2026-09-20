"""
The watchman on the UIU server (`SRS-SRV-08`).

Every few minutes it looks at the four things that stop a clinic, decides
what they mean (checks.py), and says so - in the log, to a webhook if one
is configured, and in a small status file the dashboard can read.

It reads the recordings database with a role that can only read, and never
touches the clinical database at all: nothing here needs a patient's name,
so nothing here can see one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import checks
from checks import Finding

logging.basicConfig(
    level=os.getenv("AIMS_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - MONITOR - %(levelname)s - %(message)s",
)
logger = logging.getLogger("monitor")

STATE = Path(os.getenv("AIMS_MONITOR_STATE", "/state"))
WATCHED = {
    # name -> where it is mounted, and how little may be left on it
    "archive": (Path("/watch/archive"), int(os.getenv("AIMS_DISK_FLOOR_GB", "500"))),
    "database": (Path("/watch/db"), int(os.getenv("AIMS_DB_FLOOR_GB", "100"))),
    "backups": (Path("/watch/backups"), int(os.getenv("AIMS_BACKUP_FLOOR_GB", "50"))),
}


# ============================================================
# Gathering the facts
# ============================================================

def disk_findings() -> List[Finding]:
    found = []
    for name, (path, floor) in WATCHED.items():
        if not path.exists():
            continue
        usage = shutil.disk_usage(path)
        found.append(checks.disk_space(name, usage.free, usage.total, floor_gb=floor))
    return found


async def database_findings(dsn: str, now: datetime) -> List[Finding]:
    import asyncpg

    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        backlog = await conn.fetchrow("""
            SELECT count(*) AS waiting,
                   EXTRACT(EPOCH FROM (now() - min(closed_at))) / 60 AS oldest_minutes
              FROM sessions
             WHERE closed_at IS NOT NULL AND archived_at IS NULL
               AND quarantine_reason IS NULL
               AND confirmation IN ('confirmed', 'legacy')
        """)
        copies = await conn.fetchrow("""
            SELECT count(*) AS uncopied,
                   EXTRACT(EPOCH FROM (now() - min(archived_at))) / 3600 AS oldest_hours
              FROM sessions
             WHERE archived_at IS NOT NULL AND copied_at IS NULL
               AND quarantine_reason IS NULL
        """)
        alerts = await conn.fetch("""
            SELECT alert_type, severity FROM integrity_alerts
             WHERE created_at > now() - interval '1 hour'
               AND resolved_at IS NULL
        """)
        devices = await conn.fetch("""
            SELECT device_id, machine_name, last_seen_at, revoked_at FROM devices
        """)
    finally:
        await conn.close()

    return [
        checks.archive_backlog(backlog["waiting"],
                               float(backlog["oldest_minutes"])
                               if backlog["oldest_minutes"] is not None else None),
        checks.copies_behind(copies["uncopied"],
                             float(copies["oldest_hours"])
                             if copies["oldest_hours"] is not None else None),
        checks.failed_jobs([dict(a) for a in alerts]),
        checks.silent_recorders([dict(d) for d in devices], now,
                                window=os.getenv("AIMS_CLINIC_HOURS", "08:00-22:00")),
    ]


def certificate_finding(host: str, now: datetime) -> Finding:
    """
    Asked from outside, the way a recorder asks: the answer that matters is
    what a doctor's PC will see, not what is in the certificate file.
    """
    if not host:
        return Finding("tls:certificate", checks.OK, "no public host configured", {})
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                not_after = datetime.strptime(
                    tls.getpeercert()["notAfter"], "%b %d %H:%M:%S %Y %Z"
                ).replace(tzinfo=timezone.utc)
        return checks.certificate(not_after, now)
    except Exception as exc:
        logger.warning("Could not read the certificate for %s: %s", host, exc)
        return checks.certificate(None, now)


# ============================================================
# Saying it
# ============================================================

def load_state() -> Dict[str, str]:
    try:
        return json.loads((STATE / "sent.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: Dict[str, str]) -> None:
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "sent.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not write the monitor's state: %s", exc)


def announce(finding: Finding, webhook: str) -> None:
    line = f"[{finding.status.upper()}] {finding.check}: {finding.message}"
    (logger.error if finding.status == checks.CRITICAL else
     logger.warning if finding.status == checks.WARNING else logger.info)(line)

    if not webhook:
        return
    body = json.dumps({"text": f"AIMScribe (UIU): {line}",
                       "check": finding.check, "status": finding.status,
                       "detail": finding.detail}).encode("utf-8")
    request = urllib.request.Request(
        webhook, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status >= 300:
                logger.warning("Alert webhook answered %s", response.status)
    except Exception as exc:
        # An alert that cannot be delivered is still in the log above.
        logger.warning("Alert webhook failed: %s", exc)


async def one_pass(dsn: str, host: str, webhook: str) -> str:
    now = datetime.now(timezone.utc)
    findings = disk_findings()
    try:
        findings += await database_findings(dsn, now)
    except Exception as exc:
        findings.append(Finding("database:reachable", checks.CRITICAL,
                                f"the recordings database cannot be read: {exc}", {}))
    findings.append(certificate_finding(host, now))

    state = load_state()
    for finding in findings:
        last = state.get(finding.check)
        last_sent = datetime.fromisoformat(last) if last else None
        if checks.should_send(finding, last_sent, now):
            announce(finding, webhook)
            if finding.raise_it:
                state[finding.check] = now.isoformat()
            else:
                state.pop(finding.check, None)
    save_state(state)

    report = checks.summary(findings)
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "status.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not write the status file: %s", exc)

    logger.info("%s - %s", report["status"].upper(),
                "; ".join(f["message"] for f in report["findings"]))
    return report["status"]


async def main() -> int:
    dsn = os.getenv("AIMS_MONITOR_DSN", "")
    host = os.getenv("AIMS_PUBLIC_HOST", "")
    webhook = os.getenv("AIMS_ALERT_WEBHOOK", "")
    every = int(os.getenv("AIMS_MONITOR_EVERY", "300"))

    if not dsn:
        logger.critical("AIMS_MONITOR_DSN is not set; nothing can be checked")
        return 1

    logger.info("Watching every %ss: disks, the archive queue, the cloud copies, "
                "recorders, and the certificate%s",
                every, " (alerts go to a webhook)" if webhook else "")
    once = "--once" in sys.argv
    while True:
        try:
            await one_pass(dsn, host, webhook)
        except Exception as exc:
            logger.error("Check failed: %s", exc, exc_info=True)
        if once:
            return 0
        await asyncio.sleep(every)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
