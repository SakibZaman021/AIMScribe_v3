"""
Mint a development recording grant.

Stands in for the endpoint CMED's server will expose. In production the doctor is
already authenticated when this is issued, and the claims come from that session -
which is the entire point: the browser cannot choose its own doctor_id.

    python scripts/dev_make_grant.py --patient P12345 --doctor DR001 --hospital HOSP001
"""
from __future__ import annotations

import argparse
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jwt

from config import config

DEV_PRIVATE_KEY = Path(__file__).resolve().parent / "dev-keys" / "cmed_grant_private.pem"


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint a development recording grant")
    parser.add_argument("--patient", required=True, help="Patient reference, e.g. P12345")
    parser.add_argument("--doctor", default="DR001")
    parser.add_argument("--hospital", default="HOSP001")
    parser.add_argument("--doctor-name", default="Dr Dev")
    parser.add_argument("--ttl", type=int, default=60, help="Lifetime in seconds")
    args = parser.parse_args()

    if not DEV_PRIVATE_KEY.is_file():
        print(f"Missing {DEV_PRIVATE_KEY}\nRun: python scripts/dev_keys.py", file=sys.stderr)
        return 1

    now = int(time.time())
    claims = {
        "iss": config.security.grant_issuer,
        "aud": config.security.grant_audience,
        "sub": args.doctor,
        "doctor_name": args.doctor_name,
        "hospital_id": args.hospital,
        "patient_ref": args.patient,
        # Consent is a hard precondition. The agent refuses a grant without it, and
        # the database has a CHECK constraint that refuses the session too.
        "consent_obtained": True,
        "consent_method": "verbal_at_reception",
        "iat": now,
        "exp": now + args.ttl,
        "jti": secrets.token_urlsafe(16),
    }

    token = jwt.encode(claims, DEV_PRIVATE_KEY.read_bytes(), algorithm="EdDSA")
    print(token)
    print(f"\nValid for {args.ttl}s. Single use - the agent rejects a replayed jti.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
