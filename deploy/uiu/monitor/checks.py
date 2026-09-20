"""
What "the system is well" means, as functions that can be tested.

`SRS-SRV-08`: monitoring shall alert on disk space, failed background jobs,
silent recorders and certificate expiry, **before a doctor would notice**.
That last phrase is the whole design. Each check below answers one question
a doctor would otherwise ask first:

    "why did it stop recording?"        the archive volume is full
    "why is nothing being transcribed?" the queue stopped moving
    "why did today's clinic vanish?"    a recorder has been silent
    "why can't the PCs connect?"        the certificate expired

Nothing here talks to a database or a disk. The service (monitor.py)
gathers the facts; these functions decide what they mean, so the deciding
can be tested without a server.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

OK, WARNING, CRITICAL = "ok", "warning", "critical"
_ORDER = {OK: 0, WARNING: 1, CRITICAL: 2}


@dataclass
class Finding:
    check: str
    status: str
    message: str
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def raise_it(self) -> bool:
        return self.status != OK


def worst(findings: Iterable[Finding]) -> str:
    return max((f.status for f in findings), key=lambda s: _ORDER[s], default=OK)


# ============================================================
# Disk
# ============================================================

def disk_space(name: str, free_bytes: int, total_bytes: int, *,
               floor_gb: int) -> Finding:
    """
    Free space, judged by what is left rather than by a percentage.

    A percentage is the wrong measure here: 5% of a 96 TB array is nearly
    5 TB, which is weeks of recording, while 5% of the database volume is
    minutes. What matters is whether there is room for tonight's clinic,
    so the floor is an absolute number of gigabytes, per volume.
    """
    free_gb = free_bytes / 1024 ** 3
    used = 100 * (1 - free_bytes / total_bytes) if total_bytes else 0
    detail = {"free_gb": round(free_gb, 1), "used_percent": round(used, 1),
              "floor_gb": floor_gb}

    if free_gb < floor_gb / 2:
        return Finding(f"disk:{name}", CRITICAL,
                       f"{name} has {free_gb:.0f} GB free, under half the {floor_gb} GB "
                       f"floor - recordings will start failing", detail)
    if free_gb < floor_gb:
        return Finding(f"disk:{name}", WARNING,
                       f"{name} has {free_gb:.0f} GB free, under the {floor_gb} GB floor",
                       detail)
    return Finding(f"disk:{name}", OK, f"{name}: {free_gb:.0f} GB free", detail)


# ============================================================
# Work that stopped moving
# ============================================================

def archive_backlog(waiting: int, oldest_minutes: Optional[float], *,
                    minutes_allowed: int = 30) -> Finding:
    """
    Recordings closed but not archived. `SRS-ARC-14` expects five minutes;
    this allows longer before complaining, because a busy evening is not a
    fault - a recording sitting for half an hour is.
    """
    detail = {"waiting": waiting, "oldest_minutes": oldest_minutes}
    if not waiting or oldest_minutes is None:
        return Finding("archive:backlog", OK, "nothing waiting to be archived", detail)
    if oldest_minutes > minutes_allowed * 4:
        return Finding("archive:backlog", CRITICAL,
                       f"{waiting} recording(s) unarchived, the oldest for "
                       f"{oldest_minutes / 60:.1f} hours - the archive worker has "
                       f"stopped or cannot reach the bucket", detail)
    if oldest_minutes > minutes_allowed:
        return Finding("archive:backlog", WARNING,
                       f"{waiting} recording(s) waiting, the oldest for "
                       f"{oldest_minutes:.0f} minutes", detail)
    return Finding("archive:backlog", OK, f"{waiting} recording(s) in hand", detail)


def copies_behind(uncopied: int, oldest_hours: Optional[float]) -> Finding:
    """
    The cloud copy should be made the same night (`SRS-ARC-14`). Until it
    is, that recording exists in one place and its pieces are still held in
    the segment store waiting for it.
    """
    detail = {"uncopied": uncopied, "oldest_hours": oldest_hours}
    if not uncopied or oldest_hours is None:
        return Finding("archive:copies", OK, "every recording has its cloud copy", detail)
    if oldest_hours > 48:
        return Finding("archive:copies", CRITICAL,
                       f"{uncopied} recording(s) with no cloud copy, the oldest for "
                       f"{oldest_hours / 24:.1f} days - they exist in one place only",
                       detail)
    if oldest_hours > 24:
        return Finding("archive:copies", WARNING,
                       f"{uncopied} recording(s) still to copy, the oldest for "
                       f"{oldest_hours:.0f} hours", detail)
    return Finding("archive:copies", OK, f"{uncopied} copy(ies) due tonight", detail)


def failed_jobs(alerts: Sequence[Dict[str, Any]], *, since_hours: int = 1) -> Finding:
    """
    What the system itself raised in the last hour: a chain that did not
    verify, a copy that could not be made, an unmapped clinic.
    """
    counts: Dict[str, int] = {}
    for alert in alerts:
        counts[alert.get("alert_type", "unknown")] = \
            counts.get(alert.get("alert_type", "unknown"), 0) + 1
    detail = {"since_hours": since_hours, "by_type": counts,
              "total": sum(counts.values())}

    critical = [a for a in alerts if a.get("severity") == "critical"]
    if critical:
        kinds = ", ".join(sorted({a.get("alert_type", "?") for a in critical}))
        return Finding("jobs:alerts", CRITICAL,
                       f"{len(critical)} critical alert(s) in the last "
                       f"{since_hours}h: {kinds}", detail)
    if len(alerts) >= 10:
        return Finding("jobs:alerts", WARNING,
                       f"{len(alerts)} alerts in the last {since_hours}h", detail)
    if alerts:
        return Finding("jobs:alerts", WARNING,
                       f"{len(alerts)} alert(s) in the last {since_hours}h: "
                       f"{', '.join(sorted(counts))}", detail)
    return Finding("jobs:alerts", OK, "no alerts in the last hour", detail)


# ============================================================
# Recorders
# ============================================================

def in_clinic_hours(now: datetime, window: str) -> bool:
    """`08:00-22:00` in the clinic's own time. Outside it, silence is normal."""
    try:
        start_text, end_text = window.split("-")
        start = time.fromisoformat(start_text.strip())
        end = time.fromisoformat(end_text.strip())
    except (ValueError, AttributeError):
        return True                      # a malformed window watches all day
    current = now.time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end      # a window over midnight


def silent_recorders(devices: Sequence[Dict[str, Any]], now: datetime, *,
                     window: str = "08:00-22:00",
                     silent_hours: int = 4) -> Finding:
    """
    An enrolled PC that has said nothing all clinic day. Usually it is
    switched off, and sometimes it is recording into a buffer nobody is
    draining - which is the case worth catching early.

    Devices revoked, or never yet seen, are not counted: the first is
    deliberate and the second is a laptop not yet delivered.
    """
    if not in_clinic_hours(now, window):
        return Finding("recorders:silent", OK, "outside clinic hours", {})

    cutoff = now - timedelta(hours=silent_hours)
    silent = [d for d in devices
              if d.get("revoked_at") is None
              and d.get("last_seen_at") is not None
              and d["last_seen_at"] < cutoff]
    detail = {"silent": [d.get("machine_name") or str(d.get("device_id")) for d in silent],
              "silent_hours": silent_hours,
              "enrolled": len([d for d in devices if d.get("revoked_at") is None])}

    if not silent:
        return Finding("recorders:silent", OK, "every enrolled PC has been heard from",
                       detail)
    names = ", ".join(detail["silent"][:5])
    status = CRITICAL if len(silent) >= max(3, detail["enrolled"] // 2) else WARNING
    return Finding("recorders:silent", status,
                   f"{len(silent)} recorder(s) silent for over {silent_hours}h "
                   f"during clinic hours: {names}", detail)


# ============================================================
# The certificate
# ============================================================

def certificate(not_after: Optional[datetime], now: datetime) -> Finding:
    """
    Every recorder refuses to talk to a server whose certificate has
    expired, so this is the one failure that stops the whole fleet at once
    and with no warning at all.
    """
    if not_after is None:
        return Finding("tls:certificate", CRITICAL,
                       "the certificate could not be read - the gateway may be down", {})
    days = (not_after - now).total_seconds() / 86400
    detail = {"expires": not_after.isoformat(), "days_left": round(days, 1)}
    if days <= 0:
        return Finding("tls:certificate", CRITICAL,
                       "the certificate has expired; no recorder can connect", detail)
    if days < 7:
        return Finding("tls:certificate", CRITICAL,
                       f"the certificate expires in {days:.1f} days and has not "
                       f"renewed itself", detail)
    if days < 21:
        return Finding("tls:certificate", WARNING,
                       f"the certificate expires in {days:.0f} days", detail)
    return Finding("tls:certificate", OK, f"certificate good for {days:.0f} days", detail)


# ============================================================
# Saying it once
# ============================================================

def should_send(finding: Finding, last_sent: Optional[datetime], now: datetime, *,
                repeat_hours: int = 6) -> bool:
    """
    An alert that repeats every five minutes is an alert nobody reads.
    Each finding is sent when it first goes wrong, then at most once every
    few hours while it stays wrong, and once more when it clears.
    """
    if not finding.raise_it:
        return last_sent is not None          # the all-clear, once
    if last_sent is None:
        return True
    return now - last_sent >= timedelta(hours=repeat_hours)


def summary(findings: Sequence[Finding]) -> Dict[str, Any]:
    """What the dashboard reads, and what a person sees in the log."""
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": worst(findings),
        "findings": [{"check": f.check, "status": f.status, "message": f.message,
                      **({"detail": f.detail} if f.detail else {})}
                     for f in findings],
    }


__all__ = ["CRITICAL", "Finding", "OK", "WARNING", "archive_backlog", "certificate",
           "copies_behind", "disk_space", "failed_jobs", "in_clinic_hours",
           "should_send", "silent_recorders", "summary", "worst"]
