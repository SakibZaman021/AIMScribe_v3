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
    # name -> where it is mounted, the setting for how little may be left on
    # it, and the default when that is unset. The default is capped at a share
    # of the volume (checks.effective_floor_gb), so one written for the UIU
    # array does not alarm for ever on a 320 GB droplet.
    "archive": (Path("/watch/archive"), "AIMS_DISK_FLOOR_GB", 500),
    "database": (Path("/watch/db"), "AIMS_DB_FLOOR_GB", 100),
    "backups": (Path("/watch/backups"), "AIMS_BACKUP_FLOOR_GB", 50),
}


# ============================================================
# Gathering the facts
# ============================================================

def disk_findings() -> List[Finding]:
    found = []
    for name, (path, setting, default_gb) in WATCHED.items():
        if not path.exists():
            continue
        usage = shutil.disk_usage(path)
        floor = checks.effective_floor_gb(os.getenv(setting), default_gb, usage.total)
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
               -- only a recording CMED confirmed is copied (025)
               AND confirmation = 'confirmed'
        """)
        alerts = await conn.fetch("""
            SELECT alert_type, severity FROM integrity_alerts
             WHERE raised_at > now() - interval '1 hour'
               AND resolved_at IS NULL
        """)
        devices = await conn.fetch("""
            SELECT device_id, machine_name, last_seen_at, revoked_at FROM devices
        """)
        archiver = await conn.fetchrow("""
            SELECT archived_count, last_archived_time, failed_count, last_failed_wal,
                   last_failed_time
              FROM pg_stat_archiver
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
        checks.wal_archiver(dict(archiver) if archiver else None, now),
    ]


def _stamp(path: Path) -> Optional[datetime]:
    """The time the backup job wrote into one of its marker files."""
    try:
        return datetime.strptime(path.read_text(encoding="utf-8").strip(),
                                 "%Y-%m-%dT%H:%M:%S%z")
    except Exception:
        return None


def _fields(path: Path) -> Optional[Dict[str, str]]:
    """A key=value status file the backup job writes (.wal_status, .last_drill)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    return {k: v for k, _, v in (line.partition("=") for line in lines) if k}


def _moment(value: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


def backup_findings(now: datetime) -> List[Finding]:
    folder = WATCHED["backups"][0]
    found = checks.backups(_stamp(folder / ".last_success"), _stamp(folder / ".last_offsite"),
                           bool(os.getenv("AIMS_OFFSITE_BUCKET", "").strip()), now)
    wal = _fields(folder / ".wal_status")
    found.append(checks.wal_shipping(wal, _moment((wal or {}).get("checked_at")),
                                     _moment((wal or {}).get("shipped_at")), now))
    found.append(checks.base_backup(_stamp(folder / ".last_base"), now))
    drill = _fields(folder / ".last_drill")
    found.append(checks.restore_drill(drill, _moment((drill or {}).get("at")), now,
                                      every_days=int(os.getenv("AIMS_DRILL_EVERY_DAYS", "30"))))
    return found


def schema_finding(url: str) -> Finding:
    """Ask the API's /health, the way Docker does, and read its schema answer."""
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return checks.schema(json.loads(response.read()))
    except Exception as exc:
        logger.warning("Could not read %s: %s", url, exc)
        return checks.schema(None)


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


class Mailer:
    """
    Alerts by email, through an HTTPS mail API (Resend): DigitalOcean closes
    the SMTP ports, so plain mail cannot leave the droplet. Configured by
    AIMS_ALERT_EMAIL and AIMS_RESEND_API_KEY (set with set-alerts.sh); unset,
    it does nothing and the log and webhook still carry every alert.
    """

    URL = "https://api.resend.com/emails"

    def __init__(self) -> None:
        self.to = [a.strip() for a in os.getenv("AIMS_ALERT_EMAIL", "").split(",") if a.strip()]
        self.key = os.getenv("AIMS_RESEND_API_KEY", "").strip()
        self.sender = os.getenv("AIMS_ALERT_FROM", "").strip() or \
            "AIMScribe server <onboarding@resend.dev>"

    @property
    def ready(self) -> bool:
        return bool(self.to and self.key)

    def send(self, subject: str, text: str) -> bool:
        if not self.ready:
            return False
        body = json.dumps({"from": self.sender, "to": self.to,
                           "subject": f"[AIMScribe] {subject}"[:200],
                           "text": text}).encode("utf-8")
        request = urllib.request.Request(self.URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
            "User-Agent": "aimscribe-monitor"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status < 300
        except urllib.error.HTTPError as exc:
            logger.warning("Alert email refused: %s %s", exc.code,
                           exc.read()[:200].decode("utf-8", "replace"))
        except Exception as exc:
            # An alert that cannot be delivered is still in the log.
            logger.warning("Alert email failed: %s", exc)
        return False


def email_text(host: str, lines: List[str], report: Dict[str, Any]) -> str:
    """One email for one pass: what changed, then the whole picture."""
    out = [f"From the monitor on {host or 'the AIMScribe server'}, "
           f"{datetime.now().astimezone():%Y-%m-%d %H:%M %Z}.", ""]
    out += lines
    out += ["", "Everything checked just now:"]
    out += [f"  {f['status'].upper():8} {f['check']}: {f['message']}" for f in report["findings"]]
    out += ["", "A problem is repeated every few hours while it lasts, and an all-clear is",
            "sent once when it is fixed. deploy/uiu/DISASTER_RECOVERY.md says what to do."]
    return "\n".join(out)


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


WEEK = 7 * 24 * 3600


async def one_pass(dsn: str, host: str, webhook: str, mailer: Optional[Mailer] = None) -> str:
    now = datetime.now(timezone.utc)
    mailer = mailer or Mailer()
    findings = disk_findings()
    try:
        findings += await database_findings(dsn, now)
    except Exception as exc:
        findings.append(Finding("database:reachable", checks.CRITICAL,
                                f"the recordings database cannot be read: {exc}", {}))
    findings.append(certificate_finding(host, now))
    findings += backup_findings(now)
    findings.append(schema_finding(os.getenv("AIMS_API_HEALTH_URL", "http://api:6000/health")))

    state = load_state()
    news: List[Finding] = []
    for finding in findings:
        last = state.get(finding.check)
        last_sent = datetime.fromisoformat(last) if last else None
        if checks.should_send(finding, last_sent, now):
            announce(finding, webhook)
            news.append(finding)
            if finding.raise_it:
                state[finding.check] = now.isoformat()
            else:
                state.pop(finding.check, None)

    report = checks.summary(findings)

    # By email: one message for everything this pass found, worst first; and
    # once a week a note that all is well, so a quiet inbox is never mistaken
    # for a healthy server when it is really a monitor that stopped.
    if mailer.ready and news:
        news.sort(key=lambda f: {"critical": 0, "warning": 1}.get(f.status, 2))
        first = news[0]
        subject = (f"{first.status.upper()}: {first.message}" if first.raise_it
                   else f"resolved: {first.check}")
        mailer.send(subject, email_text(host, [
            ("PROBLEM  " if f.raise_it else "RESOLVED ") + f"{f.check}: {f.message}"
            for f in news], report))
    weekly = state.get("_weekly")
    if mailer.ready and (not weekly or
                         (now - datetime.fromisoformat(weekly)).total_seconds() >= WEEK):
        if mailer.send(f"weekly report: {report['status'].upper()}", email_text(
                host, ["The weekly report. Nothing needs doing unless a line below says so."],
                report)):
            state["_weekly"] = now.isoformat()
    save_state(state)
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
