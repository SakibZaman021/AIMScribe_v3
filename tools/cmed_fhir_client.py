"""
CMED's side of Channel B, in Python: per-hospital keys, tokens, FHIR R4.

A working reference for CMED's engineers and the tool AIMS LAB uses to test.
The TypeScript twin is cmed-web/src/lib/cmed-fhir.ts; both produce messages the
server reads back into exactly the body that was meant (checked by
backend/tests/test_cmed_auth_fhir.py).

    # 1. CMED, once per hospital: make the key pair. The private key stays on
    #    CMED's server; the public JWK printed is what AIMS LAB registers.
    python tools/cmed_fhir_client.py keygen --out keys/cmed-hosp003

    # 2. AIMS LAB registers it (the admin key comes from an environment variable,
    #    never the command line).
    python tools/cmed_fhir_client.py register --server https://aims.example \\
        --client-id cmed-hosp003 --hospital CMED-HOSP003 \\
        --jwk keys/cmed-hosp003.public.json [--cidr 203.0.113.0/24]

    # 3. CMED: prove it works - a token, then one FHIR message.
    python tools/cmed_fhir_client.py token --server https://aims.example \\
        --client-id cmed-hosp003 --key keys/cmed-hosp003.private.pem --kid <kid>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SYSTEMS = {
    "Patient": os.getenv("AIMS_FHIR_SYSTEM_PATIENT", "urn:aimscribe:cmed:patient"),
    "Practitioner": os.getenv("AIMS_FHIR_SYSTEM_PRACTITIONER",
                              "urn:aimscribe:cmed:practitioner"),
    "Organization": os.getenv("AIMS_FHIR_SYSTEM_ORGANIZATION", "urn:aimscribe:cmed:hospital"),
}
LOINC = "http://loinc.org"
EVENT = {"patient_information": "patient-information", "prescription": "prescription"}
TOKEN_PATH = "/api/v2/oauth/token"
# SMART v2: create message Bundles (API 2, API 3b); read and search Encounters.
SCOPES = "system/Bundle.c system/Encounter.rs"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


# ------------------------------------------------------------------ keys and tokens

def keygen(out: Path) -> Tuple[Path, Dict[str, Any]]:
    """An ES384 key pair. The private PEM is written owner-only."""
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP384R1())
    pem = private.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(private.public_key()))
    out.parent.mkdir(parents=True, exist_ok=True)
    key_file = out.with_suffix(".private.pem")
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    out.with_suffix(".public.json").write_text(json.dumps(public, indent=2), encoding="utf-8")
    return key_file, public


def kid_of(public_jwk: Dict[str, Any]) -> str:
    """The key's id, as AIMS LAB's server computes it (RFC 7638 thumbprint)."""
    import base64
    import hashlib
    members = {"EC": ("crv", "kty", "x", "y"), "RSA": ("e", "kty", "n")}[public_jwk["kty"]]
    canonical = json.dumps({m: public_jwk[m] for m in members}, separators=(",", ":"),
                           sort_keys=True)
    return base64.urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest())         .decode().rstrip("=")


def assertion(private_pem: bytes, kid: str, client_id: str, audience: str) -> str:
    import jwt
    now = int(time.time())
    return jwt.encode({"iss": client_id, "sub": client_id, "aud": audience, "iat": now,
                       "exp": now + 240, "jti": str(uuid.uuid4())},
                      private_pem, algorithm="ES384", headers={"kid": kid, "typ": "JWT"})


def _call(url: str, data: Optional[bytes], headers: Dict[str, str],
          timeout: int = 30) -> Tuple[int, Dict[str, Any]]:
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def get_token(server: str, client_id: str, kid: str, private_pem: bytes, *,
              audience: Optional[str] = None,
              signed: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
    server = server.rstrip("/")
    signed = signed or assertion(private_pem, kid, client_id, audience or server + TOKEN_PATH)
    form = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "scope": SCOPES,
        "client_assertion_type": ASSERTION_TYPE, "client_assertion": signed}).encode()
    return _call(server + TOKEN_PATH, form,
                 {"Content-Type": "application/x-www-form-urlencoded"})


class TokenCache:
    """
    One hospital's token, reused until 30 s before it expires - not one per
    message. Thread-safe enough for an outbox worker: a race costs one extra
    token, never a failure.
    """

    def __init__(self, server: str, client_id: str, kid: str, private_pem: bytes, *,
                 audience: Optional[str] = None):
        self.args = (server, client_id, kid, private_pem)
        self.audience = audience
        self._token: Optional[str] = None
        self._until = 0.0

    def get(self) -> str:
        if self._token and time.time() < self._until:
            return self._token
        status, reply = get_token(*self.args, audience=self.audience)
        if status != 200 or not reply.get("access_token"):
            raise RuntimeError(f"token refused ({status}): {reply.get('error')} "
                               f"{reply.get('error_description', '')}".strip())
        self._token = reply["access_token"]
        self._until = time.time() + int(reply.get("expires_in", 300)) - 30
        return self._token

    def forget(self) -> None:
        self._token, self._until = None, 0.0


RETRYABLE = {429, 500, 502, 503, 504}


def deliver(server: str, tokens: TokenCache, message: Dict[str, Any], *,
            attempts: int = 6, base_delay: float = 2.0, max_delay: float = 300.0,
            sleep=time.sleep) -> Tuple[int, Dict[str, Any]]:
    """
    How CMED's outbox should send one message: reuse the token; on 401 get a
    new one once; on 429/5xx or no connection wait (Retry-After if given, else
    exponential backoff with full jitter, so a clinic's backlog does not retry
    in lock-step) and try again. 4xx other than 401/429 is final: fix the
    message, do not resend it.
    """
    import random
    renewed = False
    status, reply = 0, {}
    for n in range(attempts):
        try:
            status, reply, headers = _send(server, tokens.get(), message)
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            status, reply, headers = 0, {"error": str(exc)}, {}
        if status == 401 and not renewed:
            tokens.forget()
            renewed = True
            continue
        if status and status not in RETRYABLE:
            return status, reply
        wait = None
        retry_after = headers.get("Retry-After") if headers else None
        if retry_after and retry_after.isdigit():
            wait = min(float(retry_after), max_delay)
        if wait is None:
            wait = random.uniform(0, min(max_delay, base_delay * 2 ** n))
        if n + 1 < attempts:
            sleep(wait)
    return status, reply


def _send(server: str, token: str, message: Dict[str, Any], timeout: int = 30):
    req = urllib.request.Request(server.rstrip("/") + "/fhir/r4/$process-message",
                                 data=json.dumps(message).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/fhir+json",
                                          "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except ValueError:
            body = {}
        return exc.code, body, dict(exc.headers or {})


def send_fhir(server: str, token: Optional[str], message: Dict[str, Any],
              timeout: int = 30) -> Tuple[int, Dict[str, Any]]:
    headers = {"Content-Type": "application/fhir+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return _call(server.rstrip("/") + "/fhir/r4/$process-message",
                 json.dumps(message).encode("utf-8"), headers, timeout)


def reply_code(reply: Dict[str, Any]) -> Tuple[str, str]:
    """(code, message) from a FHIR reply: a response message or an OperationOutcome."""
    if reply.get("resourceType") == "OperationOutcome":
        issue = (reply.get("issue") or [{}])[0]
        return (((issue.get("details") or {}).get("coding") or [{}])[0].get("code", ""),
                (issue.get("details") or {}).get("text", ""))
    resources = [e.get("resource", {}) for e in reply.get("entry", [])]
    params = next((r for r in resources if r.get("resourceType") == "Parameters"), {})
    oo = next((r for r in resources if r.get("resourceType") == "OperationOutcome"), {})
    code = next((p.get("valueCode") for p in params.get("parameter", [])
                 if p.get("name") == "code"), "")
    text = ((oo.get("issue") or [{}])[0].get("details") or {}).get("text", "")
    return code or "", text


# ------------------------------------------------------------------ body -> FHIR

def _id() -> str:
    return f"urn:uuid:{uuid.uuid4()}"


def _ref(url: str) -> Dict[str, str]:
    return {"reference": url}


def _ident(kind: str, value: str) -> List[Dict[str, str]]:
    return [{"system": SYSTEMS[kind], "value": value}]


def _split(text: Any) -> Optional[Tuple[float, str]]:
    m = re.match(r"^\s*([\d.]+)\s*(.*)$", text or "") if isinstance(text, str) else None
    if not m:
        return None
    value = float(m.group(1))
    return (int(value) if value.is_integer() else value), m.group(2)


def _prescription(p: Dict[str, Any], encounter: str, patient: str,
                  issued_at: Optional[str] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in p.get("items") or []:
        dosage: Dict[str, Any] = {}
        dose = _split(item.get("dose"))
        if dose:
            dosage["doseAndRate"] = [{"doseQuantity": {"value": dose[0],
                                                       **({"unit": dose[1]} if dose[1] else {})}}]
        timing: Dict[str, Any] = {}
        if item.get("frequency"):
            timing["code"] = {"text": item["frequency"]}
        days = _split(item.get("duration"))
        if days:
            timing["repeat"] = {"boundsDuration": {"value": days[0],
                                                   **({"unit": days[1]} if days[1] else {})}}
        if timing:
            dosage["timing"] = timing
        if item.get("instructions"):
            dosage["patientInstruction"] = item["instructions"]
        mr = {"resourceType": "MedicationRequest", "status": "active", "intent": "order",
              "subject": _ref(patient), "encounter": _ref(encounter),
              "medicationCodeableConcept": {"text": item.get("drug", "")}}
        if issued_at:
            mr["authoredOn"] = issued_at
        if dosage:
            mr["dosageInstruction"] = [dosage]
        out.append(mr)
    for d in p.get("diagnoses") or []:
        out.append({"resourceType": "Condition", "subject": _ref(patient),
                    "encounter": _ref(encounter),
                    "code": {"text": d if isinstance(d, str) else d.get("text") or d.get("name")}})
    for t in p.get("investigations") or []:
        out.append({"resourceType": "ServiceRequest", "status": "active", "intent": "order",
                    "subject": _ref(patient), "encounter": _ref(encounter),
                    "code": {"text": t if isinstance(t, str) else t.get("text") or t.get("name")}})
    # The doctor's findings: one Observation each, its category naming the section.
    for f in p.get("findings") or []:
        obs: Dict[str, Any] = {
            "resourceType": "Observation", "status": "final",
            "category": [{"coding": [{"system": "urn:aimscribe:fhir:finding",
                                      "code": f["section"].replace("_", "-")}]}],
            "code": {"text": f["item"]}, "subject": _ref(patient),
            "encounter": _ref(encounter)}
        if f.get("value") is not None:
            obs["valueString"] = f["value"]
        detail = dict(f.get("detail") or {})
        note = detail.pop("note", None)
        if detail:
            obs["component"] = [{"code": {"text": k}, "valueString": str(v)}
                                for k, v in detail.items()]
        if note:
            obs["note"] = [{"text": note}]
        out.append(obs)
    if p.get("advice") or p.get("follow_up") or p.get("notes"):
        plan: Dict[str, Any] = {"resourceType": "CarePlan", "status": "active",
                                "intent": "plan", "subject": _ref(patient),
                                "encounter": _ref(encounter)}
        if p.get("advice"):
            plan["description"] = p["advice"]
        if p.get("follow_up"):
            plan["period"] = {"start": p["follow_up"]}
        if p.get("notes"):
            plan["note"] = [{"text": p["notes"]}]
        out.append(plan)
    return out


def _vital(code: str, value: Any, unit: str, when: Optional[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    obs = {"resourceType": "Observation", "status": "final",
           "category": [{"coding": [{"code": "vital-signs"}]}],
           "code": {"coding": [{"system": LOINC, "code": code}]},
           "valueQuantity": {"value": value, "unit": unit,
                             "system": "http://unitsofmeasure.org", "code": unit}}
    if when:
        obs["effectiveDateTime"] = when
    return obs


def to_fhir(kind: str, b: Dict[str, Any]) -> Dict[str, Any]:
    """An API 2 or API 3b body as a FHIR R4 message Bundle."""
    enc, pat, doc, org = _id(), _id(), _id(), _id()
    res: List[Tuple[str, Dict[str, Any]]] = [
        (_id(), {"resourceType": "MessageHeader", "id": str(uuid.uuid4()),
                 "eventCoding": {"system": "urn:aimscribe:fhir:event", "code": EVENT[kind]},
                 "source": {"name": "CMED", "endpoint": "urn:cmed:server"},
                 "focus": [_ref(enc)]}),
        (enc, {"resourceType": "Encounter", "status": "in-progress",
               "class": {"code": "AMB"}, "subject": _ref(pat),
               "participant": [{"individual": _ref(doc)}],
               "serviceProvider": _ref(org), "period": {"start": b.get("start_time")}}),
    ]
    d = b.get("demographics") or {}
    # Sex and date of birth only: name, phone and address are not needed.
    patient: Dict[str, Any] = {"resourceType": "Patient",
                               "identifier": _ident("Patient", b.get("patient_id"))}
    if d.get("sex") in ("female", "male"):
        patient["gender"] = d["sex"]
    if d.get("date_of_birth"):
        patient["birthDate"] = d["date_of_birth"]
    res += [(pat, patient),
            (doc, {"resourceType": "Practitioner",
                   "identifier": _ident("Practitioner", b.get("doctor_id"))}),
            (org, {"resourceType": "Organization",
                   "identifier": _ident("Organization", b.get("hospital_id"))})]
    if kind == "prescription":
        res += [(_id(), r) for r in _prescription(b, enc, pat, b.get("issued_at"))]
    else:
        p = b.get("paramedic") or {}
        when = p.get("recorded_at")
        for v in (_vital("29463-7", p.get("weight_kg"), "kg", when),
                  _vital("8302-2", p.get("height_cm"), "cm", when),
                  _vital("8867-4", p.get("pulse_bpm"), "/min", when),
                  _vital("8310-5", p.get("temperature_c"), "Cel", when),
                  _vital("59408-5", p.get("spo2_percent"), "%", when),
                  _vital("30525-0", d.get("age_years"), "a", None)):
            if v:
                res.append((_id(), {**v, "subject": _ref(pat), "encounter": _ref(enc)}))
        bp = re.match(r"^(\d+)/(\d+)$", str(p.get("blood_pressure") or ""))
        if bp:
            res.append((_id(), {
                "resourceType": "Observation", "status": "final", "subject": _ref(pat),
                "encounter": _ref(enc),
                "code": {"coding": [{"system": LOINC, "code": "85354-9"}]},
                **({"effectiveDateTime": when} if when else {}),
                "component": [
                    {"code": {"coding": [{"system": LOINC, "code": "8480-6"}]},
                     "valueQuantity": {"value": int(bp.group(1)), "unit": "mm[Hg]"}},
                    {"code": {"coding": [{"system": LOINC, "code": "8462-4"}]},
                     "valueQuantity": {"value": int(bp.group(2)), "unit": "mm[Hg]"}}]}))
        if p.get("notes"):
            res.append((_id(), {"resourceType": "Observation", "status": "final",
                                "subject": _ref(pat), "encounter": _ref(enc),
                                "code": {"text": "paramedic notes"},
                                "note": [{"text": p["notes"]}]}))
        pv = b.get("previous_visit")
        if isinstance(pv, dict):
            prev = _id()
            prev_doc, prev_org = doc, org
            if pv.get("doctor_id") and pv["doctor_id"] != b.get("doctor_id"):
                prev_doc = _id()
                res.append((prev_doc, {"resourceType": "Practitioner",
                                       "identifier": _ident("Practitioner", pv["doctor_id"])}))
            if pv.get("hospital_id") and pv["hospital_id"] != b.get("hospital_id"):
                prev_org = _id()
                res.append((prev_org, {"resourceType": "Organization",
                                       "identifier": _ident("Organization",
                                                            pv["hospital_id"])}))
            res.append((prev, {"resourceType": "Encounter", "status": "finished",
                               "class": {"code": "AMB"}, "subject": _ref(pat),
                               "participant": [{"individual": _ref(prev_doc)}],
                               "serviceProvider": _ref(prev_org),
                               "period": {"start": pv.get("start_time") or pv.get("date")}}))
            res += [(_id(), r) for r in _prescription(pv.get("prescription") or {}, prev,
                                                      pat)]
    return {"resourceType": "Bundle", "id": str(uuid.uuid4()), "type": "message",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "entry": [{"fullUrl": u, "resource": r} for u, r in res]}


# ------------------------------------------------------------------ command line

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    kd = sub.add_parser("kid", help="print the kid of a public key (.public.json)")
    kd.add_argument("jwk", type=Path)
    k = sub.add_parser("keygen", help="make one hospital's key pair")
    k.add_argument("--out", required=True, type=Path, help="path prefix for the two files")
    r = sub.add_parser("register", help="AIMS LAB: register a hospital's public key")
    r.add_argument("--server", required=True)
    r.add_argument("--client-id", required=True)
    r.add_argument("--hospital", required=True, action="append",
                   help="CMED hospital id; normally given once")
    r.add_argument("--jwk", required=True, type=Path)
    r.add_argument("--label", default="")
    r.add_argument("--cidr", action="append", help="address range CMED calls from")
    r.add_argument("--days", type=int, default=365)
    r.add_argument("--admin-key-env", default="AIMS_ADMIN_KEY",
                   help="environment variable holding the admin key")
    t = sub.add_parser("token", help="CMED: get a token, to prove the setup")
    t.add_argument("--server", required=True)
    t.add_argument("--client-id", required=True)
    t.add_argument("--kid", required=True)
    t.add_argument("--key", required=True, type=Path)
    t.add_argument("--audience", help="if the server's public URL differs from --server")
    args = parser.parse_args(argv)

    if args.command == "keygen":
        key_file, public = keygen(args.out)
        print(f"Private key (stays on CMED's server, never sent): {key_file}")
        print(f"Public key for AIMS LAB to register: {args.out.with_suffix('.public.json')}")
        print(f"Your kid (AIMS LAB reads the same value back to you): {kid_of(public)}")
        return 0
    if args.command == "kid":
        print(kid_of(json.loads(args.jwk.read_text(encoding="utf-8"))))
        return 0
    if args.command == "register":
        admin = os.getenv(args.admin_key_env, "")
        if not admin:
            parser.error(f"set {args.admin_key_env} to the admin key first")
        body = {"client_id": args.client_id, "label": args.label or args.client_id,
                "cmed_hospital_ids": args.hospital, "allowed_cidrs": args.cidr,
                "expires_in_days": args.days, "created_by": os.getenv("USERNAME", "admin"),
                "jwk": json.loads(args.jwk.read_text(encoding="utf-8")), "alg": "ES384"}
        status, reply = _call(args.server.rstrip("/") + "/api/v2/admin/cmed-client",
                              json.dumps(body).encode(), {"Content-Type": "application/json",
                                                          "X-Admin-Key": admin})
        print(status, json.dumps(reply, indent=2))
        return 0 if status == 200 else 1
    status, reply = get_token(args.server, args.client_id, args.kid, args.key.read_bytes(),
                              audience=args.audience)
    shown = {k: v for k, v in reply.items() if k != "access_token"}
    print(status, json.dumps(shown, indent=2),
          "\n(token received; not printed)" if reply.get("access_token") else "")
    return 0 if status == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
