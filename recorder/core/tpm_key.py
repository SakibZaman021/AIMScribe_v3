"""
This PC's identity key, held inside its TPM security chip.

The software key (crypto.DeviceKey) is wrapped with machine-scope DPAPI, which
stops the file working on another computer - but any program on this PC, as
any user, can unwrap it and carry the raw key away, then act as this laptop
from anywhere. A key created inside the TPM never exists outside the chip:
programs on this PC can ask the chip to sign, but nothing - not an
administrator, not malware - can copy the key off the machine. Revoking the
laptop then ends everything that key could do.

TPM 2.0 chips sign ECDSA P-256 (not Ed25519), so this key is P-256. The server
accepts both kinds, telling them apart by the public key's length: 32 bytes is
Ed25519, 65 bytes (0x04 || X || Y) is P-256. Signatures are the raw 64-byte
r || s that Windows returns.

The key is a machine key, so every Windows user of a shared clinic laptop is
the same laptop. Creating a machine key needs an administrator, so the
installer makes it (main.py --provision-device-key) and grants the Users group
the right to use it - never to export it. Windows' own TPM provider, the
"Microsoft Platform Crypto Provider", does the work; nothing is installed.
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
import sys
import threading
from ctypes import wintypes
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

logger = logging.getLogger(__name__)

KEY_NAME = "AIMScribe Device Key"
PROVIDER = "Microsoft Platform Crypto Provider"

NCRYPT_MACHINE_KEY_FLAG = 0x20
NCRYPT_SILENT_FLAG = 0x40
NTE_BAD_KEYSET = 0x80090016
NTE_EXISTS = 0x8009000F
DACL_SECURITY_INFORMATION = 0x4
BCRYPT_ECDSA_PUBLIC_P256_MAGIC = 0x31534345      # "ECS1"

# Only SYSTEM and administrators control the key; Users may use it to sign
# (read), which is what the agent - running as whoever is signed in - needs.
KEY_SDDL = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GR;;;BU)"


class TpmUnavailable(RuntimeError):
    """No TPM 2.0, or Windows' TPM provider cannot be used on this PC."""


class TpmKeyMissing(RuntimeError):
    """The TPM works, but the device key has not been created yet."""


def _check(status: int, what: str) -> None:
    if status != 0:
        raise OSError(f"{what} failed: 0x{status & 0xFFFFFFFF:08X}")


def _ncrypt():
    if sys.platform != "win32":
        raise TpmUnavailable("the TPM key is only available on Windows")
    try:
        return ctypes.WinDLL("ncrypt.dll")
    except OSError as exc:
        raise TpmUnavailable(f"ncrypt.dll could not be loaded: {exc}")


def _open_provider(nc) -> wintypes.HANDLE:
    provider = ctypes.c_void_p()
    status = nc.NCryptOpenStorageProvider(ctypes.byref(provider),
                                          ctypes.c_wchar_p(PROVIDER), 0)
    if status != 0:
        raise TpmUnavailable(
            f"this PC's TPM cannot be used (0x{status & 0xFFFFFFFF:08X}). AIMScribe needs "
            "a TPM 2.0 security chip, switched on in the PC's firmware settings.")
    return provider


class TpmDeviceKey:
    """
    The same surface as crypto.DeviceKey: sign(), public_bytes_raw(),
    public_key(), public_pem(), fingerprint(). Signing is serialised: one
    handle, one chip.
    """

    kind = "p256-tpm"

    def __init__(self, nc, provider, key, public_point: bytes, *, name: str):
        self._nc = nc
        self._provider = provider
        self._key = key
        self._point = public_point
        self._lock = threading.Lock()
        self.name = name

    # ---- lifecycle ----

    @classmethod
    def open(cls, *, name: str = KEY_NAME, machine: bool = True) -> "TpmDeviceKey":
        """Open the existing key. TpmKeyMissing when it has not been made."""
        nc = _ncrypt()
        provider = _open_provider(nc)
        key = ctypes.c_void_p()
        flags = NCRYPT_SILENT_FLAG | (NCRYPT_MACHINE_KEY_FLAG if machine else 0)
        status = nc.NCryptOpenKey(provider, ctypes.byref(key), ctypes.c_wchar_p(name),
                                  0, flags)
        if status != 0:
            nc.NCryptFreeObject(provider)
            if (status & 0xFFFFFFFF) == NTE_BAD_KEYSET:
                raise TpmKeyMissing(f"no TPM key named {name!r} on this PC")
            raise OSError(f"opening the TPM key failed: 0x{status & 0xFFFFFFFF:08X}")
        return cls(nc, provider, key, _export_point(nc, key), name=name)

    @classmethod
    def create(cls, *, name: str = KEY_NAME, machine: bool = True,
               users_may_sign: bool = True) -> "TpmDeviceKey":
        """
        Make the key inside the chip. Never overwrites: an existing key is this
        PC's enrolled identity, and replacing it would orphan the enrolment.
        """
        nc = _ncrypt()
        provider = _open_provider(nc)
        key = ctypes.c_void_p()
        flags = NCRYPT_SILENT_FLAG | (NCRYPT_MACHINE_KEY_FLAG if machine else 0)
        status = nc.NCryptCreatePersistedKey(
            provider, ctypes.byref(key), ctypes.c_wchar_p("ECDSA_P256"),
            ctypes.c_wchar_p(name), 0, flags)
        if (status & 0xFFFFFFFF) == NTE_EXISTS:
            nc.NCryptFreeObject(provider)
            return cls.open(name=name, machine=machine)
        if status != 0:
            nc.NCryptFreeObject(provider)
            raise OSError(f"creating the TPM key failed: 0x{status & 0xFFFFFFFF:08X}"
                          + (" (run as administrator)" if machine else ""))
        try:
            if machine and users_may_sign:
                _set_security(nc, key, KEY_SDDL)
            _check(nc.NCryptFinalizeKey(key, NCRYPT_SILENT_FLAG), "NCryptFinalizeKey")
        except Exception:
            nc.NCryptDeleteKey(key, 0)
            nc.NCryptFreeObject(provider)
            raise
        logger.info("Created the device key inside this PC's TPM (%s)", name)
        return cls(nc, provider, key, _export_point(nc, key), name=name)

    def delete(self) -> None:
        """Tests only: a real device key is never deleted by the agent."""
        with self._lock:
            if self._key:
                self._nc.NCryptDeleteKey(self._key, 0)      # also frees the handle
                self._key = None
            if self._provider:
                self._nc.NCryptFreeObject(self._provider)
                self._provider = None

    # ---- use ----

    def sign(self, message: bytes) -> bytes:
        digest = hashlib.sha256(message).digest()
        out = ctypes.create_string_buffer(64)
        size = wintypes.DWORD(0)
        with self._lock:
            status = self._nc.NCryptSignHash(
                self._key, None, digest, len(digest), out, 64, ctypes.byref(size),
                NCRYPT_SILENT_FLAG)
        _check(status, "NCryptSignHash")
        if size.value != 64:
            raise OSError(f"the TPM returned a {size.value}-byte signature, not 64")
        return out.raw

    def public_bytes_raw(self) -> bytes:
        return self._point

    def public_key(self) -> ec.EllipticCurvePublicKey:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), self._point)

    def public_pem(self) -> bytes:
        return self.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo)

    def fingerprint(self) -> str:
        return hashlib.sha256(self._point).hexdigest()[:16]


def _export_point(nc, key) -> bytes:
    """The public half, as 0x04 || X || Y. Exporting the private half is impossible."""
    blob_type = ctypes.c_wchar_p("ECCPUBLICBLOB")
    size = wintypes.DWORD(0)
    _check(nc.NCryptExportKey(key, None, blob_type, None, None, 0, ctypes.byref(size), 0),
           "NCryptExportKey (size)")
    buf = ctypes.create_string_buffer(size.value)
    _check(nc.NCryptExportKey(key, None, blob_type, None, buf, size, ctypes.byref(size), 0),
           "NCryptExportKey")
    raw = buf.raw[:size.value]
    magic = int.from_bytes(raw[0:4], "little")
    length = int.from_bytes(raw[4:8], "little")
    if magic != BCRYPT_ECDSA_PUBLIC_P256_MAGIC or length != 32 or len(raw) != 8 + 64:
        raise OSError("the TPM key is not an ECDSA P-256 key")
    return b"\x04" + raw[8:72]


def _set_security(nc, key, sddl: str) -> None:
    advapi = ctypes.WinDLL("advapi32.dll")
    kernel = ctypes.WinDLL("kernel32.dll")
    sd = ctypes.c_void_p()
    sd_size = wintypes.ULONG(0)
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            ctypes.c_wchar_p(sddl), 1, ctypes.byref(sd), ctypes.byref(sd_size)):
        raise OSError("the key's access list could not be built")
    try:
        _check(nc.NCryptSetProperty(key, ctypes.c_wchar_p("Security Descr"), sd,
                                    sd_size, DACL_SECURITY_INFORMATION | NCRYPT_SILENT_FLAG),
               "setting who may use the TPM key")
    finally:
        kernel.LocalFree(sd)


def verify(public_point: bytes, message: bytes, signature: bytes) -> bool:
    """Check a P-256 r || s signature the way the server does."""
    from cryptography.exceptions import InvalidSignature
    if len(signature) != 64:
        return False
    der = encode_dss_signature(int.from_bytes(signature[:32], "big"),
                               int.from_bytes(signature[32:], "big"))
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_point).verify(
            der, message, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False


def available() -> Optional[str]:
    """None when this PC's TPM can hold the key; otherwise why not."""
    try:
        nc = _ncrypt()
        nc.NCryptFreeObject(_open_provider(nc))
        return None
    except TpmUnavailable as exc:
        return str(exc)
