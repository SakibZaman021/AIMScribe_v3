"""
Generate the development key pairs the agent needs to start.

In production these keys belong to two different systems and the agent only ever
holds the public halves:

    cmed_grant_*      CMED's server signs recording grants; the agent verifies.
    aimslab_receipt_* The AIMS LAB server signs purge receipts; the agent verifies.

For local development one machine plays all three roles, so this script makes both
pairs, installs the public keys where the agent looks for them, and keeps the
private keys in a dev-only folder.

    python scripts/dev_keys.py

Never copy dev private keys to a clinical machine.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from config import config, data_dir
from core.crypto import DeviceKey

DEV_PRIVATE_DIR = Path(__file__).resolve().parent / "dev-keys"


def make_pair(name: str, public_target: Path) -> None:
    private = Ed25519PrivateKey.generate()

    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    DEV_PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    private_path = DEV_PRIVATE_DIR / f"{name}_private.pem"
    private_path.write_bytes(private_pem)

    public_target.parent.mkdir(parents=True, exist_ok=True)
    public_target.write_bytes(public_pem)

    print(f"  {name}")
    print(f"    private -> {private_path}")
    print(f"    public  -> {public_target}")


def write_dev_identity() -> None:
    """
    Stand in for a server-issued device identity.

    Real enrollment exchanges an administrator's one-time token for a device_id
    and a hospital_id bound by the backend. There is no backend in development,
    so this writes the same file the agent would otherwise receive.
    """
    import json

    path = config.paths.state_dir / "device.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    device_key = DeviceKey.load_or_create(
        config.security.device_key_path,
        allow_plaintext=config.security.allow_plaintext_keystore,
    )

    identity = {
        "device_id": "DEV-LOCAL-0001",
        "hospital_id": "HOSP001",
        "enrolled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "backend_url": config.backend.base_url,
        "key_fingerprint": device_key.fingerprint(),
    }
    path.write_text(json.dumps(identity, indent=2, sort_keys=True), encoding="utf-8")

    # The backend issues this at enrollment and the agent sends it as
    # X-Device-Token. There is no backend in development, so write a placeholder
    # to keep the code path identical.
    from core.enrollment import store_device_token
    store_device_token(config, "dev-local-device-token")

    print("  dev device identity")
    print(f"    device_id   -> {identity['device_id']} (hospital {identity['hospital_id']})")
    print(f"    written to  -> {path}")
    print(f"    token       -> {config.paths.state_dir / 'device.token'}")


def main() -> int:
    print("Generating development key pairs\n")

    make_pair("cmed_grant", config.security.grant_public_key_path)
    make_pair("aimslab_receipt", config.security.receipt_public_key_path)
    write_dev_identity()

    print(f"\nAgent state directory: {data_dir()}")
    print("\nThe agent will now pass its configuration check.")
    print("Mint a grant with:  python scripts/dev_make_grant.py --patient P12345")
    return 0


if __name__ == "__main__":
    sys.exit(main())
