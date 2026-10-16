"""
Do the recorder's public keys belong to this server's private keys?

The installer ships two public keys (recorder/keys). The server signs with the
matching private keys (AIMS_GRANT_PRIVATE_KEY, AIMS_RECEIPT_PRIVATE_KEY). If
the halves do not belong together, every recording grant fails the recorder's
signature check, and every purge receipt is refused, so laptops never free their
disks. Run this before building the installer and before enrolling any PC:

    python tools/check_key_pair.py deploy/uiu/.env
    python tools/check_key_pair.py deploy/uiu/.env --keys "C:/Program Files/AIMScribe/keys"
    python tools/check_key_pair.py --from-env          (on the server, inside the API container)
    python tools/check_key_pair.py --show --keys recorder/keys
                                    (where there is no private key: the laptop
                                     that builds the installer, or a clinic PC)

It prints MATCH or MISMATCH per key and a short fingerprint of each public key,
which is public information. --show prints only the fingerprints, so the
server's answer and the installer's keys can be compared without a private key
ever leaving the server. It never prints a private key, or any part of one.
Exit status: 0 all match, 1 a mismatch, 2 a key is missing or unreadable.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import Dict, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

REPO = Path(__file__).resolve().parent.parent
PAIRS = {
    "grant": ("AIMS_GRANT_PRIVATE_KEY", "aimslab_grant_pub.pem"),
    "receipt": ("AIMS_RECEIPT_PRIVATE_KEY", "aimslab_receipt_pub.pem"),
}


def read_env_file(path: Path) -> Dict[str, str]:
    """KEY=value lines, as docker compose reads them. Values are not echoed."""
    values: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def raw_public(key) -> bytes:
    return key.public_bytes(encoding=serialization.Encoding.Raw,
                            format=serialization.PublicFormat.Raw)


def fingerprint(public: bytes) -> str:
    return hashlib.sha256(public).hexdigest()[:16]


def server_public(pem: Optional[str]) -> Optional[bytes]:
    if not pem:
        return None
    key = serialization.load_pem_private_key(
        pem.replace("\\n", "\n").encode("utf-8"), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("not an Ed25519 private key")
    return raw_public(key.public_key())


def shipped_public(path: Path) -> Optional[bytes]:
    if not path.is_file():
        return None
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("not an Ed25519 public key")
    return raw_public(key)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("env_file", nargs="?", type=Path,
                        help="the server's .env (deploy/uiu/.env on the droplet)")
    parser.add_argument("--from-env", action="store_true",
                        help="read the private keys from this process's environment")
    parser.add_argument("--keys", type=Path, default=REPO / "recorder" / "keys",
                        help="folder holding the public keys the installer ships")
    parser.add_argument("--show", action="store_true",
                        help="only print the fingerprints of the public keys in --keys")
    args = parser.parse_args(argv)

    if args.show:
        print(f"public keys in {args.keys}")
        worst = 0
        for name, (_, filename) in PAIRS.items():
            try:
                shipped = shipped_public(args.keys / filename)
            except Exception as exc:
                print(f"  {name:8} UNREADABLE  {filename} ({type(exc).__name__})")
                worst = 2
                continue
            if shipped is None:
                print(f"  {name:8} MISSING     {filename}")
                worst = 2
            else:
                print(f"  {name:8} {fingerprint(shipped)}")
        return worst

    if args.from_env:
        source = dict(os.environ)
    elif args.env_file:
        source = read_env_file(args.env_file)
    else:
        parser.error("give the server's .env file, or --from-env")

    print(f"public keys from {args.keys}")
    worst = 0
    for name, (variable, filename) in PAIRS.items():
        try:
            server = server_public(source.get(variable))
        except Exception as exc:
            print(f"  {name:8} UNREADABLE  {variable} could not be loaded ({type(exc).__name__})")
            worst = max(worst, 2)
            continue
        try:
            shipped = shipped_public(args.keys / filename)
        except Exception as exc:
            print(f"  {name:8} UNREADABLE  {filename} ({type(exc).__name__})")
            worst = max(worst, 2)
            continue
        if server is None or shipped is None:
            missing = variable if server is None else filename
            print(f"  {name:8} MISSING     {missing}")
            worst = max(worst, 2)
            continue
        if server == shipped:
            print(f"  {name:8} MATCH       {fingerprint(shipped)}")
        else:
            print(f"  {name:8} MISMATCH    server {fingerprint(server)}  "
                  f"installer {fingerprint(shipped)}")
            worst = max(worst, 1)

    if worst == 1:
        print("\nDo not build the installer or enrol any PC: recordings would fail their "
              "grant check.\nPut the public halves of the server's own keys in "
              "recorder/keys\n(openssl pkey -in grant.pem -pubout, and the same for the "
              "receipt key),\nthen rebuild the installer and run this again.")
    return worst


if __name__ == "__main__":
    sys.exit(main())
