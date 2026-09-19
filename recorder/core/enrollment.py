"""
Device enrollment.

Every agent needs a server-issued identity before it may open a session. Without
one, a session's `hospital_id` would be whatever the client claimed - which is
precisely the flaw in v1, where the browser typed its own hospital into a text box
and the archive tree was built from it.

The flow is designed so a doctor never sees it:

    install.ps1 -EnrollmentToken <one-time token from an administrator>
        writes  %PROGRAMDATA%\\AIMScribe\\state\\enrollment.token

    first agent start
        POST /api/v2/device/enroll  { token, device_pubkey, machine info }
        <- { device_id, hospital_id }
        writes  device.json, deletes enrollment.token

    every start after that
        reads device.json

The enrollment token is single-use and short-lived; the server binds the device to
a hospital, so the agent never asserts its own tenancy. The device's Ed25519 public
key is registered at the same time, which is what lets the server verify that every
chain entry was signed by this machine.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import socket
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp

from core import crypto
from core.crypto import DeviceKey

logger = logging.getLogger(__name__)

IDENTITY_FILE = "device.json"
TOKEN_FILE = "enrollment.token"
# The credential the backend issues at enrollment, used on every later request.
# Kept out of device.json and DPAPI-wrapped, because it is a bearer token: anyone
# holding it can upload as this device.
DEVICE_TOKEN_FILE = "device.token"


class EnrollmentError(RuntimeError):
    """Enrollment failed. The agent stays unenrolled and refuses to record."""


@dataclass(frozen=True)
class DeviceIdentity:
    device_id: str
    hospital_id: str
    # Whose machine this is. Set by an administrator at enrolment and never
    # afterwards, so a consultation is attributed by the machine that recorded
    # it rather than by whatever a web page claimed.
    doctor_id: str
    enrolled_at: str
    backend_url: str
    key_fingerprint: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeviceIdentity":
        return cls(
            device_id=str(data["device_id"]),
            hospital_id=str(data["hospital_id"]),
            # Absent on devices enrolled before the doctor moved onto the
            # machine. They re-enrol; recording refuses rather than guessing.
            doctor_id=str(data.get("doctor_id", "")),
            enrolled_at=str(data.get("enrolled_at", "")),
            backend_url=str(data.get("backend_url", "")),
            key_fingerprint=str(data.get("key_fingerprint", "")),
        )


def identity_path(cfg) -> Path:
    return cfg.paths.state_dir / IDENTITY_FILE


def token_path(cfg) -> Path:
    return cfg.paths.state_dir / TOKEN_FILE


def device_token_path(cfg) -> Path:
    return cfg.paths.state_dir / DEVICE_TOKEN_FILE


def load_device_token(cfg) -> Optional[str]:
    """
    Read the DPAPI-wrapped device token issued at enrollment.

    Returns None when absent, which leaves the agent unable to talk to the
    backend - it keeps recording and spooling, and says so in the tray.
    """
    path = device_token_path(cfg)
    if not path.is_file():
        return None
    try:
        raw = crypto.unwrap_secret(
            path.read_bytes(),
            allow_plaintext=cfg.security.allow_plaintext_keystore,
        )
        return raw.decode("utf-8").strip() or None
    except Exception as exc:
        logger.error("Device token is unreadable: %s", exc)
        return None


def store_device_token(cfg, token: str) -> None:
    path = device_token_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(crypto.wrap_secret(
        token.encode("utf-8"),
        allow_plaintext=cfg.security.allow_plaintext_keystore,
    ))
    os.replace(tmp, path)


def load_identity(cfg, device_key: DeviceKey) -> Optional[DeviceIdentity]:
    """
    Read the stored identity, if this machine has one.

    The stored key fingerprint is checked against the live device key: if the key
    was regenerated (a wiped keys folder, a restored disk image), the old identity
    is no longer valid because the server still holds the previous public key.
    """
    path = identity_path(cfg)
    if not path.is_file():
        return None

    try:
        identity = DeviceIdentity.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        logger.error("Device identity at %s is unreadable: %s", path, exc)
        return None

    live = device_key.fingerprint()
    if identity.key_fingerprint and identity.key_fingerprint != live:
        logger.critical(
            "Device key has changed since enrollment (stored %s, current %s). "
            "This machine must be re-enrolled before it can record.",
            identity.key_fingerprint, live)
        return None

    if identity.backend_url and identity.backend_url != cfg.backend.base_url:
        logger.warning(
            "Enrolled against %s but configured for %s. Re-enroll if the backend moved.",
            identity.backend_url, cfg.backend.base_url)

    return identity


def read_pending_token(cfg) -> Optional[str]:
    path = token_path(cfg)
    if not path.is_file():
        return None
    try:
        token = path.read_text(encoding="utf-8-sig").strip()
        return token or None
    except OSError as exc:
        logger.error("Could not read the enrollment token: %s", exc)
        return None


def _machine_facts(cfg) -> Dict[str, Any]:
    """Inventory detail for the server's device register. No patient data."""
    try:
        machine_name = socket.gethostname()
    except Exception:
        machine_name = "unknown"
    return {
        "machine_name": machine_name,
        "os_version": f"{platform.system()} {platform.release()} ({platform.version()})",
        "app_version": cfg.app_version,
        "protocol_version": cfg.protocol_version,
        "audio": {
            "sample_rate": cfg.audio.sample_rate,
            "channels": cfg.audio.channels,
            "sample_width": cfg.audio.sample_width,
        },
    }


async def enroll(cfg, device_key: DeviceKey, token: str, *, ssl_context=None) -> DeviceIdentity:
    """
    Exchange a one-time token for a device identity.

    Raises EnrollmentError on any failure; the caller leaves the agent unenrolled
    rather than proceeding with a guessed identity.
    """
    url = cfg.backend.url("/device/enroll")
    payload = {
        "enrollment_token": token,
        "device_pubkey": device_key.public_bytes_raw().hex(),
        **_machine_facts(cfg),
    }

    logger.info("Enrolling this device with %s", cfg.backend.base_url)

    timeout = aiohttp.ClientTimeout(total=cfg.backend.request_timeout)
    connector = aiohttp.TCPConnector(ssl=ssl_context) if ssl_context else None

    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.post(url, json=payload) as response:
                body = await response.text()
                if response.status >= 300:
                    raise EnrollmentError(
                        f"server rejected enrollment ({response.status}): {body[:200]}")
                data = json.loads(body)
    except EnrollmentError:
        raise
    except Exception as exc:
        raise EnrollmentError(f"could not reach the backend: {exc}") from exc

    device_id = data.get("device_id")
    hospital_id = data.get("hospital_id")
    doctor_id = data.get("doctor_id")
    device_token = data.get("device_token")
    if not device_id or not hospital_id:
        raise EnrollmentError("enrollment response did not include device_id and hospital_id")
    # A doctor is no longer required, and is only a label for the room. The
    # machine belongs to a clinic; the doctor sitting at it changes between
    # shifts and CMED names them per consultation. Requiring one here meant
    # every PC carried a name that was wrong half the day.
    if not device_token:
        raise EnrollmentError("enrollment response did not include a device token")

    # Stored before the identity file: if the process dies between the two, the
    # next start finds no identity and re-enrolls, rather than finding an identity
    # it has no credential for.
    store_device_token(cfg, str(device_token))

    identity = DeviceIdentity(
        device_id=str(device_id),
        hospital_id=str(hospital_id),
        doctor_id=str(doctor_id),
        enrolled_at=crypto.iso_utc(datetime.now(timezone.utc)),
        backend_url=cfg.backend.base_url,
        key_fingerprint=device_key.fingerprint(),
    )

    path = identity_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(identity.to_json(), encoding="utf-8")
    os.replace(tmp, path)

    # The token is single-use; leaving it on disk invites a replay attempt and
    # confuses the next start.
    try:
        token_path(cfg).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not remove the used enrollment token: %s", exc)

    logger.info("Enrolled as device %s for doctor %s at hospital %s",
                identity.device_id, identity.doctor_id, identity.hospital_id)
    return identity


async def ensure_enrolled(cfg, device_key: DeviceKey, *, ssl_context=None) -> Optional[DeviceIdentity]:
    """
    Return this machine's identity, enrolling first if a token is waiting.

    Returns None when the machine is not enrolled and cannot be, which the caller
    reports as a configuration problem - the agent then runs, shows its tray icon,
    and refuses to record.
    """
    identity = load_identity(cfg, device_key)
    if identity is not None:
        return identity

    token = read_pending_token(cfg)
    if not token:
        return None

    try:
        return await enroll(cfg, device_key, token, ssl_context=ssl_context)
    except EnrollmentError as exc:
        # The token is deliberately left in place: a backend that is merely
        # unreachable should not burn the administrator's token.
        logger.error("Enrollment failed, will retry on next start: %s", exc)
        return None


__all__ = [
    "DeviceIdentity", "EnrollmentError", "ensure_enrolled", "enroll",
    "load_identity", "read_pending_token", "identity_path", "token_path",
    "device_token_path", "load_device_token", "store_device_token",
]
