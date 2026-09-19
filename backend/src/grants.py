"""
Recording grants, issued by this server (SRS 3.2 §5).

The CMED page sends five plain fields to the recorder; the recorder forwards
them here; this server checks them against its own register and returns a
short-lived, single-use, Ed25519-signed JWT. The recorder verifies it against a
public key pinned at installation. CMED holds no key and signs nothing
(SRS-GRT-01, SRS-GRT-09).
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Any, Dict, Optional, Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

logger = logging.getLogger(__name__)

ISSUER = "aimslab"
AUDIENCE = "aimscribe-recorder"
LIFETIME_SECONDS = 60           # SRS §5.2


def new_jti() -> str:
    return secrets.token_urlsafe(18)


class GrantIssuer:
    """Holds the grant signing key. It never leaves this server."""

    def __init__(self, private_key: Ed25519PrivateKey, *, issuer: str = ISSUER,
                 audience: str = AUDIENCE, lifetime: int = LIFETIME_SECONDS):
        self._key = private_key
        self.issuer = issuer
        self.audience = audience
        self.lifetime = lifetime

    @classmethod
    def from_env(cls, var: str = "AIMS_GRANT_PRIVATE_KEY") -> Optional["GrantIssuer"]:
        """
        Load from a PEM in an environment variable.

        Returns None when unset. Recordings then cannot be authorised, which is
        the safe failure direction: the recorder refuses rather than records
        without permission (SRS-GRT-06).
        """
        pem = os.getenv(var, "").strip()
        if not pem:
            logger.critical("%s is not set - recording grants cannot be issued", var)
            return None
        try:
            key = serialization.load_pem_private_key(
                pem.replace("\\n", "\n").encode("utf-8"), password=None)
        except Exception as exc:
            logger.critical("Could not load the grant signing key: %s", exc)
            return None
        if not isinstance(key, Ed25519PrivateKey):
            logger.critical("Grant signing key is not Ed25519")
            return None
        return cls(key)

    def public_pem(self) -> str:
        return self._key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")

    def issue(self, *, jti: str, patient_ref: str, doctor_id: str, hospital_id: str,
              cmed_hospital_id: str, start_time: str, visit_date: str,
              confirmation: str) -> Tuple[str, Dict[str, Any]]:
        """
        Sign one grant. `hospital_id` is this PC's clinic, never CMED's value:
        the recorder files the recording under it (SRS-INV-01).
        """
        import jwt

        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": self.audience,
            "sub": doctor_id,
            "iat": now,
            "exp": now + self.lifetime,
            "jti": jti,
            "patient_ref": patient_ref,
            "hospital_id": hospital_id,
            "cmed_hospital_id": cmed_hospital_id,
            "start_time": start_time,
            "date": visit_date,
            "confirmation": confirmation,
        }
        return jwt.encode(claims, self._key, algorithm="EdDSA"), claims


__all__ = ["GrantIssuer", "new_jti", "ISSUER", "AUDIENCE", "LIFETIME_SECONDS"]
