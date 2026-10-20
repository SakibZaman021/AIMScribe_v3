"""
Is the recorder really talking to the AIMS LAB server?

Clinic PCs commonly run antivirus that inspects HTTPS: it installs its own root
certificate in Windows and re-signs every site's certificate with it. Windows
then trusts the interception, and so would the recorder - handing its device
token and its upload links to the inspecting software.

Two checks, both made on a separate handshake before the server is trusted and
again every few minutes:

  pins       AIMS_TLS_PINS: base64 SHA-256 hashes of the server's public key
             (SPKI), the current key and a spare. When set, any other key is
             refused: nothing is sent.
  issuer     the certificate's issuer names a known HTTPS-inspection product.
             Reported, and refused when pins are set (they would not match).

`spki_pin(der)` is how a pin is computed, the same way as
`openssl x509 -pubkey | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64`.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import ssl
import time
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

RECHECK_SECONDS = 600.0

# Names that appear in the issuer of certificates re-signed by HTTPS inspection.
INSPECTION_ISSUERS = ("kaspersky", "eset", "avast", "avg ", "bitdefender", "norton",
                      "symantec web", "sophos", "fortinet", "fortigate", "zscaler",
                      "mcafee web", "trend micro", "palo alto", "cisco umbrella",
                      "netskope", "forcepoint", "watchguard", "barracuda", "quick heal",
                      "k7 computing", "check point", "untangle", "sonicwall", "f-secure",
                      "drweb", "dr.web", "comodo internet security")


@dataclass
class TlsVerdict:
    ok: bool
    problem: str = ""
    issuer: str = ""


def spki_pin(der_cert: bytes) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    key = x509.load_der_x509_certificate(der_cert).public_key()
    spki = key.public_bytes(serialization.Encoding.DER,
                            serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")


def _issuer(der_cert: bytes) -> str:
    from cryptography import x509
    try:
        return x509.load_der_x509_certificate(der_cert).issuer.rfc4514_string()
    except Exception:
        return ""


def judge(der_cert: bytes, pins: Iterable[str]) -> TlsVerdict:
    """The verdict on one server certificate."""
    pins = {p.strip() for p in pins if p and p.strip()}
    issuer = _issuer(der_cert)
    inspected = any(name in issuer.lower() for name in INSPECTION_ISSUERS)
    if pins:
        if spki_pin(der_cert) not in pins:
            return TlsVerdict(False, "TLS interception detected: the server's key is not "
                                     "the pinned one" + (f" (issued by {issuer})" if issuer else ""),
                              issuer)
        return TlsVerdict(True, "", issuer)
    if inspected:
        return TlsVerdict(True, f"TLS interception detected: the connection is inspected by "
                                f"'{issuer}'. Exclude the AIMS LAB server from HTTPS scanning.",
                          issuer)
    return TlsVerdict(True, "", issuer)


async def peer_certificate(url: str, context: ssl.SSLContext,
                           timeout: float = 10.0) -> Optional[bytes]:
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port or 443
    if not host:
        return None
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=context, server_hostname=host), timeout)
    try:
        ssl_object = writer.get_extra_info("ssl_object")
        return ssl_object.getpeercert(binary_form=True) if ssl_object else None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


class TlsGuard:
    """Checks the server before it is trusted, and again every few minutes."""

    def __init__(self, url: str, context: Optional[ssl.SSLContext], pins: Tuple[str, ...]):
        self.url, self.context, self.pins = url, context, tuple(pins)
        self.checked_at = 0.0
        self.verdict: Optional[TlsVerdict] = None

    @property
    def problem(self) -> str:
        return self.verdict.problem if self.verdict else ""

    async def allowed(self) -> bool:
        """False only when pins are set and the server's key does not match."""
        if self.context is None:                 # plain http (a test server)
            return True
        if self.verdict is not None and time.monotonic() - self.checked_at < RECHECK_SECONDS:
            return self.verdict.ok
        try:
            der = await peer_certificate(self.url, self.context)
        except Exception as exc:
            # Unreachable is an outage, not an interception: the request itself
            # will fail and be retried. Do not cache it.
            logger.debug("TLS check could not connect: %s", exc)
            return True
        if der is None:
            return True
        verdict = judge(der, self.pins)
        if verdict.problem and (self.verdict is None or self.verdict.problem != verdict.problem):
            logger.critical("%s", verdict.problem)
        self.verdict, self.checked_at = verdict, time.monotonic()
        return verdict.ok


__all__ = ["TlsGuard", "TlsVerdict", "judge", "spki_pin", "peer_certificate",
           "INSPECTION_ISSUERS"]
