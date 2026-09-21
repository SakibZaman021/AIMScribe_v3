"""
Channel B, tried out before CMED's first real patient (`SRS-CHB-01`-`12`).

Two ways to use it, and CMED's engineers need both:

**Check a message without a server.** The same rules the live server applies,
run locally, so a payload can be fixed in a minute instead of a round trip:

    python tools/channel_b_test.py --validate my_api2.json --kind patient_information
    python tools/channel_b_test.py --sample patient_information > example.json

**Try the whole interface against a test server.** It sends every case that
matters - a good message, the same message twice, a broken one, a changed
prescription, a wrong key, an oversized body - and checks the reply code
against §6.2.4:

    python tools/channel_b_test.py --server https://test.aimscribe.example \\
        --key "$CMED_TEST_KEY" --hospital CMED-DHK-BANANI-01

Nothing here needs a recorder or a patient. The messages are made up and
the identifiers say so, which matters because a test that has to be run
against real patients never gets run.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "src"))

import clinical                                        # noqa: E402
import confirmation as conf                            # noqa: E402
from confirmation import FieldError                    # noqa: E402

MAX_BODY_BYTES = clinical.MAX_BODY_BYTES


# ============================================================
# Sample messages - what a good one looks like
# ============================================================

def sample(kind: str, *, hospital: str = "CMED-TEST-01", patient: str = "TESTP0001",
           doctor: str = "TESTDR01", start_time: Optional[str] = None) -> Dict[str, Any]:
    """A valid message of either kind, for CMED to copy and change."""
    start = start_time or datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=6))).replace(microsecond=0).isoformat()
    five = {"patient_id": patient, "doctor_id": doctor, "hospital_id": hospital,
            "start_time": start, "date": start[:10]}

    if kind == "patient_information":
        return {
            **five,
            "demographics": {"name": "Test Patient", "sex": "female",
                             "age_years": 34, "phone": "01700000000",
                             "address": "Test address"},
            "paramedic": {"recorded_at": start, "weight_kg": 58, "height_cm": 156,
                          "blood_pressure": "120/80", "pulse_bpm": 78,
                          "temperature_c": 37.1, "spo2_percent": 98},
            # Required, and null for a first visit - saying "none" explicitly
            # is what tells us the field was not simply forgotten.
            "previous_visit": None,
        }
    return {
        **five,
        "issued_at": start,
        "diagnoses": ["Test diagnosis"],
        "investigations": ["Test investigation"],
        "advice": "Test advice",
        "follow_up": (datetime.fromisoformat(start).date()
                      + timedelta(days=30)).isoformat(),
        "items": [{"drug": "Test Medicine", "dose": "5 mg", "frequency": "1+0+0",
                   "duration": "30 days", "instructions": "after food"}],
    }


# ============================================================
# Checking a message without a server
# ============================================================

@dataclass
class Verdict:
    ok: bool
    problems: List[Dict[str, str]] = field(default_factory=list)
    note: str = ""

    def text(self) -> str:
        if self.ok:
            return "This message would be accepted." + (f" {self.note}" if self.note else "")
        lines = ["This message would be refused:"]
        lines += [f"  {p['field']}: {p['problem']}" for p in self.problems]
        return "\n".join(lines)


def validate(kind: str, body: Any) -> Verdict:
    """
    Exactly what the server checks, in the same order (`clinical.py`): the
    five fields that identify the visit, then everything else.

    Unknown fields are never a problem (`SRS-CHB-11`) - CMED can send more
    than we ask for, and it is stored with the rest.
    """
    if not isinstance(body, dict):
        return Verdict(False, [{"field": "body", "problem": "must be a JSON object"}])

    size = len(json.dumps(body).encode("utf-8"))
    if size > MAX_BODY_BYTES:
        return Verdict(False, [{"field": "body",
                                "problem": f"{size} bytes; the limit is "
                                           f"{MAX_BODY_BYTES} (413 TOO_LARGE)"}])
    try:
        conf.parse_visit(body)
    except FieldError as exc:
        return Verdict(False, [{"field": exc.field, "problem": str(exc)}])

    problems = clinical.problems_with(kind, body)
    if problems:
        return Verdict(False, problems)

    extra = sorted(set(body) - _known_fields(kind))
    return Verdict(True, note=f"Extra fields kept as sent: {', '.join(extra)}."
                   if extra else "")


def _known_fields(kind: str) -> set:
    five = {"patient_id", "doctor_id", "hospital_id", "start_time", "date"}
    if kind == "patient_information":
        return five | {"demographics", "paramedic", "previous_visit"}
    return five | {"issued_at", "diagnoses", "investigations", "items", "advice",
                   "follow_up", "notes"}


# ============================================================
# Trying the interface against a server
# ============================================================

@dataclass
class Case:
    name: str
    kind: str
    expect_status: int
    expect_code: str
    why: str
    mutate: Any = None          # (body) -> body
    key: Optional[str] = None   # None: the real key


CASES: List[Case] = [
    Case("A good API 2", "patient_information", 202, "ACCEPTED",
         "stored before the reply is sent, so 202 means it is safe here"),
    Case("The same API 2 again", "patient_information", 200, "ALREADY_RECEIVED",
         "a retry after a timeout must not store the visit twice (SRS-CHB-07)",
         mutate=lambda body: body),
    Case("An API 2 with no demographics", "patient_information", 422, "SCHEMA_INVALID",
         "kept in quarantine and reported, never silently dropped",
         mutate=lambda body: {k: v for k, v in body.items() if k != "demographics"}),
    Case("An API 2 with a sex we do not know", "patient_information", 422,
         "SCHEMA_INVALID", "'female' or 'male'; anything else is reported",
         mutate=lambda body: {**body, "demographics": {**body["demographics"],
                                                       "sex": "F"}}),
    Case("An API 2 with no start_time", "patient_information", 400, "MISSING_FIELD",
         "the five fields are what match the message to a recording"),
    Case("A good prescription", "prescription", 202, "ACCEPTED",
         "one row per medicine on our side (SRS-DBA-10)"),
    Case("The prescription, changed", "prescription", 202, "ACCEPTED",
         "a new version; earlier ones are kept (SRS-CHB-08)",
         mutate=lambda body: {**body, "advice": "Changed advice"}),
    Case("A prescription with a medicine that has no name", "prescription", 422,
         "SCHEMA_INVALID", "every item needs a drug",
         mutate=lambda body: {**body, "items": [{"dose": "5 mg"}]}),
    Case("A message with the wrong key", "patient_information", 401, "INVALID_KEY",
         "the key is checked before anything else (SRS-CHB-02)",
         key="not-the-right-key"),
    Case("A message over 1 MB", "patient_information", 413, "TOO_LARGE",
         "the limit is per message (SRS-CHB-04)",
         mutate=lambda body: {**body, "notes": "x" * (MAX_BODY_BYTES + 1000)}),
]


def _mutated(case: Case, body: Dict[str, Any]) -> Dict[str, Any]:
    if case.name == "An API 2 with no start_time":
        return {k: v for k, v in body.items() if k != "start_time"}
    return case.mutate(body) if case.mutate else body


def send(server: str, kind: str, body: Dict[str, Any], key: str,
         timeout: int = 30) -> Tuple[int, Dict[str, Any]]:
    path = "patient-information" if kind == "patient_information" else "prescription"
    request = urllib.request.Request(
        f"{server.rstrip('/')}/api/v2/clinical/{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-CMED-Key": key})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def run_cases(server: str, key: str, *, hospital: str, patient: str, doctor: str,
              timeout: int = 30) -> List[Dict[str, Any]]:
    """
    Each case once, in order. The second case repeats the first message
    deliberately, so the order matters and they are not run in parallel.
    """
    start = datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=6))).replace(microsecond=0).isoformat()
    bodies = {kind: sample(kind, hospital=hospital, patient=patient, doctor=doctor,
                           start_time=start)
              for kind in ("patient_information", "prescription")}

    results = []
    for case in CASES:
        body = _mutated(case, bodies[case.kind])
        try:
            status, reply = send(server, case.kind, body, case.key or key,
                                 timeout=timeout)
            code = str(reply.get("code", ""))
            results.append({
                "case": case.name, "why": case.why,
                "expected": f"{case.expect_status} {case.expect_code}",
                "got": f"{status} {code}".strip(),
                "passed": status == case.expect_status and code == case.expect_code,
                "message": reply.get("message", ""),
                "fields": reply.get("fields", []),
            })
        except Exception as exc:
            results.append({
                "case": case.name, "why": case.why,
                "expected": f"{case.expect_status} {case.expect_code}",
                "got": f"could not reach the server: {exc}", "passed": False,
                "message": "", "fields": [],
            })
    return results


def as_text(results: List[Dict[str, Any]]) -> str:
    width = max(len(r["case"]) for r in results) + 2
    lines = [f"{'case':<{width}}{'expected':<22}{'got':<22}", "-" * (width + 44)]
    for r in results:
        lines.append(f"{r['case']:<{width}}{r['expected']:<22}{r['got']:<22}"
                     f"{'' if r['passed'] else '  <-- not as expected'}")
        if not r["passed"]:
            lines.append(f"{'':<{width}}why this matters: {r['why']}")
            for problem in r["fields"][:5]:
                lines.append(f"{'':<{width}}  {problem.get('field')}: "
                             f"{problem.get('problem')}")
    passed = sum(1 for r in results if r["passed"])
    lines += ["", f"{passed}/{len(results)} as expected",
              "Channel B: READY" if passed == len(results) else "Channel B: NOT READY"]
    return "\n".join(lines)


# ============================================================
# Command line
# ============================================================

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", help="the AIMS LAB test server")
    parser.add_argument("--key", default="", help="the key AIMS LAB issued to CMED")
    parser.add_argument("--hospital", default="CMED-TEST-01")
    parser.add_argument("--patient", default="TESTP0001")
    parser.add_argument("--doctor", default="TESTDR01")
    parser.add_argument("--validate", metavar="FILE",
                        help="check a message without sending it")
    parser.add_argument("--kind", default="patient_information",
                        choices=["patient_information", "prescription"])
    parser.add_argument("--sample", metavar="KIND", nargs="?", const="patient_information",
                        choices=["patient_information", "prescription"],
                        help="print a valid message to start from")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.sample:
        print(json.dumps(sample(args.sample, hospital=args.hospital,
                                patient=args.patient, doctor=args.doctor), indent=2))
        return 0

    if args.validate:
        try:
            body = json.loads(Path(args.validate).read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(f"No such file: {args.validate}")
            return 2
        except json.JSONDecodeError as exc:
            print(f"That file is not valid JSON: {exc}")
            return 1
        verdict = validate(args.kind, body)
        print(json.dumps({"ok": verdict.ok, "problems": verdict.problems}, indent=2)
              if args.json else verdict.text())
        return 0 if verdict.ok else 1

    if not args.server or not args.key:
        parser.error("--server and --key are needed to try the interface "
                     "(or use --validate / --sample)")

    print(f"Trying Channel B against {args.server}\n")
    results = run_cases(args.server, args.key, hospital=args.hospital,
                        patient=args.patient, doctor=args.doctor)
    print(json.dumps(results, indent=2) if args.json else as_text(results))
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
