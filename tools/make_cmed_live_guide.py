#!/usr/bin/env python3
"""
Build the CMED connection guide for the live server - the address, why each
part of it exists, and the three FHIR messages in full.

    python tools/make_cmed_live_guide.py --host <the server's public name> [--out NAME.pdf]

The host is an argument, never written here: the guide is rebuilt when the
server moves to its permanent name. Every example message in it is built by
the reference client (tools/cmed_fhir_client.py) and then read back by the
server's own FHIR reader (backend/src/fhir.py) and validator
(backend/src/clinical.py) while the PDF is made - a message the server would
refuse cannot reach the page. The fingerprint example is recomputed with the
server's function. Sources for the rules: CMED_DATA_CONTRACT.md.
"""
import argparse
import itertools
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "backend" / "src"))

from reportlab.lib.units import mm
from reportlab.platypus import (CondPageBreak, HRFlowable, PageBreak, Paragraph,
                                Preformatted, Spacer)

import cmed_doc_style as st
from cmed_doc_style import BAD, BODY, CODE, GOOD, H1, H2, H3, NOTE, SUB, build, part, tbl

import cmed_fhir_client as client

# ------------------------------------------------------------------ the examples

_n = itertools.count(1)
client._id = lambda: "urn:uuid:00000000-0000-4000-8000-%012d" % next(_n)
client.time.strftime = lambda fmt, t=None: "2026-09-13T04:14:40Z"
_ids = itertools.count(1)
client.uuid.uuid4 = lambda: "b0000000-0000-4000-8000-%012d" % next(_ids)

FIVE = {"patient_id": "P0012345", "doctor_id": "DR0042", "hospital_id": "HOSP003",
        "start_time": "2026-09-13T10:14:32+06:00", "date": "2026-09-13"}

OLD_PRESCRIPTION = {
    "items": [{"drug": "Amlodipine 5 mg", "dose": "1 tablet", "frequency": "once daily",
               "duration": "30 days", "instructions": "in the morning"}],
    "diagnoses": ["Essential hypertension"], "investigations": ["Serum creatinine"],
    "advice": "Reduce salt", "follow_up": "2026-07-02", "notes": None,
    "findings": [
        {"section": "chief_complaint", "item": "Headache", "value": None,
         "detail": {"duration": "2 weeks"}},
        {"section": "on_examination", "item": "oedema", "value": "absent", "detail": {}}]}

# API 2: what the paramedic collects - no name, phone or address.
API2 = {**FIVE,
        "demographics": {"sex": "female", "age_years": 34, "date_of_birth": "1992-03-11"},
        "paramedic": {"recorded_at": "2026-09-13T10:02:10+06:00", "weight_kg": 58.4,
                      "height_cm": 157, "blood_pressure": "120/80", "pulse_bpm": 78,
                      "temperature_c": 36.8, "spo2_percent": 98},
        "previous_visit": {"doctor_id": "DR0007", "hospital_id": "HOSP003",
                           "start_time": "2026-06-02T09:40:05+06:00", "date": "2026-06-02",
                           "prescription": OLD_PRESCRIPTION}}

# API 3: the whole consultation, from the chief complaint to the advice.
FINDINGS = [
    {"section": "chief_complaint", "item": "Fever", "value": None,
     "detail": {"severity": "moderate", "duration": "3 days"}},
    {"section": "chief_complaint", "item": "Frequent urination", "value": None,
     "detail": {"duration": "1 month"}},
    {"section": "history", "item": "family", "value": "Diabetes (mother)", "detail": {}},
    {"section": "history", "item": "personal", "value": "Non-smoker", "detail": {}},
    {"section": "allergy", "item": "medicine", "value": "Penicillin", "detail": {}},
    {"section": "allergy", "item": "dust", "value": "yes", "detail": {}},
    {"section": "on_examination", "item": "anaemia", "value": "absent", "detail": {}},
    {"section": "on_examination", "item": "jaundice", "value": "absent", "detail": {}},
    {"section": "on_examination", "item": "dehydration", "value": "mild", "detail": {}},
    {"section": "systemic_examination", "item": "heart", "value": "S1 S2 normal",
     "detail": {"note": "no murmur"}},
    {"section": "systemic_examination", "item": "lungs", "value": "clear", "detail": {}},
    {"section": "gynae_obstetric", "item": "obstetric", "value": "G2 P2", "detail": {}},
    {"section": "gynae_obstetric", "item": "menstrual", "value": "regular", "detail": {}},
]
API3 = {**FIVE, "issued_at": "2026-09-13T10:26:55+06:00",
        "items": [{"drug": "Metformin 500 mg", "dose": "1 tablet",
                   "frequency": "twice daily", "duration": "30 days",
                   "instructions": "after meals"}],
        "diagnoses": ["Type 2 diabetes mellitus"], "investigations": ["HbA1c"],
        "advice": "Walk 30 minutes a day", "follow_up": "2026-10-13", "notes": None,
        "findings": FINDINGS}


def checked(kind, body, *, drop_previous=False, warned=False):
    """The reference client's Bundle, proven readable and valid by the server."""
    import clinical
    import fhir
    sent = dict(body)
    if drop_previous:
        sent["previous_visit"] = None
    bundle = client.to_fhir(kind, sent)
    got_kind, got = fhir.to_body(fhir.Bundle(bundle))
    assert got_kind == kind, (got_kind, kind)
    for key in FIVE:
        assert got[key] == FIVE[key], (key, got[key])
    if kind == "patient_information":
        # API 2 carries no name, phone or address, and none of the doctor's findings.
        assert set(got["demographics"]) <= {"sex", "date_of_birth", "age_years"}
        assert "findings" not in got
    problems = clinical.problems_with(kind, got)
    assert warned or not problems, problems      # the clean examples carry no warnings
    if kind == "prescription":
        assert [i["drug"] for i in got["items"]] == [i["drug"] for i in body["items"]]
        assert got["follow_up"] == body["follow_up"] and got["advice"] == body["advice"]
        assert got.get("findings", []) == body.get("findings", []), "findings round trip"
    elif drop_previous:
        assert got["previous_visit"] is None
    else:
        pv = got["previous_visit"]
        assert (pv["doctor_id"], pv["start_time"]) == ("DR0007", "2026-06-02T09:40:05+06:00")
        assert pv["prescription"]["items"][0]["drug"] == "Amlodipine 5 mg"
        assert pv["prescription"]["findings"] == OLD_PRESCRIPTION["findings"]
        assert got["paramedic"]["blood_pressure"] == "120/80"
    return bundle, got


def reply_example(request_bundle, extra):
    import fhir
    reply = fhir._response_message(request_bundle, 200, "ACCEPTED", None, extra)
    return reply


def fingerprint_example():
    from clinical_model import fingerprint_of
    value = fingerprint_of(items=[{"drug": "Paracetamol", "dose": "500 mg",
                                   "frequency": "1+1+1", "duration": "5 days"}],
                           diagnoses=["Fever"], investigations=["CBC"],
                           advice="Rest and fluids", follow_up="2026-10-08", notes=None)
    assert value == "f1e7109404fd5d4010c358f0b3afdec79699955299a089eaf2c830fb60ec4806", value
    return value


def pretty(obj, indent=0, width=96):
    """JSON with anything short kept on one line, so a message fits a page or two."""
    flat = json.dumps(obj, ensure_ascii=False, separators=(", ", ": "))
    if len(flat) + indent <= width or not isinstance(obj, (dict, list)) or not obj:
        return flat
    pad, nl = " " * (indent + 2), chr(10)
    if isinstance(obj, dict):
        inner = [f"{pad}{json.dumps(k)}: {pretty(v, indent + 2, width)}" for k, v in obj.items()]
        opening, closing = "{", "}"
    else:
        inner = [pad + pretty(v, indent + 2, width) for v in obj]
        opening, closing = "[", "]"
    return opening + nl + ("," + nl).join(inner) + nl + " " * indent + closing


def code(text):
    return Preformatted(text, CODE)


def mono(text):
    return f"<font face='Courier'>{text}</font>"


# ------------------------------------------------------------------ the document

def story(host, permanent):
    base = f"https://{host}"
    S = []
    api2_first, _ = checked("patient_information", API2, drop_previous=True)
    api2_returning, _ = checked("patient_information", API2)
    api3, _ = checked("prescription", API3)
    reply = reply_example(api3, {"record_id": "41", "version": 1,
                                 "received_at": "2026-09-13T04:27:01.512+00:00"})
    reply2 = reply_example(api2_returning, {
        "record_id": "40", "received_at": "2026-09-13T04:14:41.120+00:00",
        "previous_visit": {"status": "STORED", "version": 1, "record_id": "39"}})
    fp = fingerprint_example()
    warned_bundle, warned_body = checked("patient_information", {
        **API2, "previous_visit": None,
        "paramedic": {**API2["paramedic"], "temperature_c": 98.6}}, drop_previous=True,
        warned=True)
    import clinical
    warnings = clinical.problems_with("patient_information", warned_body)
    assert [w["field"] for w in warnings] == ["paramedic.temperature_c"], warnings
    reply_warned = reply_example(warned_bundle, {
        "record_id": "42", "received_at": "2026-09-13T04:14:41.120+00:00",
        "previous_visit": {"status": "NOT_SENT"}, "warnings": warnings})

    # ---------------------------------------------------------------- title
    S.append(Paragraph("CMED &rarr; AIMScribe &mdash; Connecting to the Live Server", H1))
    S.append(Paragraph("<b>For CMED's server and software teams</b> &middot; the address, why "
                       "each part exists, and the three FHIR messages in full &middot; AIMS LAB, "
                       "United International University", SUB))
    S.append(HRFlowable(width="100%", thickness=1, color=st.RULE, spaceAfter=8))
    S.append(tbl([["The address to configure"],
                  [f"<font face='Courier' size='12'><b>{base}</b></font>"],
                  [("<b>Keep it in one configuration value</b> (for example "
                    f"{mono('AIMS_SERVER_URL')}), never compiled in. This is the server's "
                    "<b>temporary</b> public name while the university sets up its own: it will "
                    f"become <b>{mono('https://' + permanent)}</b>. When it does, CMED changes "
                    "that one value &mdash; every address below, and the token audience, follow "
                    "from it.") if host != permanent else
                   ("This is the AIMScribe server's permanent address, under <b>United "
                    "International University's own domain</b>. It is live: the health check "
                    "in section 2 answers 200.<br/><b>Keep it in one configuration value</b> "
                    "(for example "
                    f"{mono('AIMS_SERVER_URL')}), never compiled in: every address below, and "
                    "the token audience, follow from it.")]],
                 [156 * mm], bold_first=False, highlight=[1]))
    S.append(Spacer(1, 5))
    S.append(tbl([["What this guide covers", "Section"],
                  ["Why there is an API gateway, and what it does for CMED", "1"],
                  ["Every address CMED calls, in full", "2"],
                  ["What CMED sends us before the first message", "3"],
                  ["Getting a token", "4"],
                  ["The message envelope and the five fields", "5"],
                  ["<b>Current patient information</b> (API 2)", "6"],
                  ["<b>Old prescription</b> (the previous visit, inside API 2)", "7"],
                  ["<b>Current prescription</b> (API 3)", "8"],
                  ["<b>The doctor's findings</b> (in API 3)", "9"],
                  ["Replies and what to do about each", "10"],
                  ["Testing, and the go-live checklist", "11"]],
                 [136 * mm, 20 * mm], highlight=[6, 7, 8, 9]))
    S.append(Spacer(1, 5))
    S.append(Paragraph("Every example message in this guide was built by our reference client "
                       "and read back by the live server's own FHIR reader while this document "
                       "was produced: they are exactly what the server accepts.", GOOD))

    # ---------------------------------------------------------------- quick start
    S.append(PageBreak())
    S.append(part("START HERE", "The whole integration on one page",
                  "Read this first; the sections after it are the detail"))
    S.append(Paragraph("What happens, and when", H2))
    S.append(tbl([["When", "Who sends what, to where", "Section"],
                  ["<b>Once per hospital</b>", "CMED makes a key pair, emails AIMS LAB the "
                   "public half and the hospital's code; receives a client_id and kid back",
                   "3, 4"],
                  ["<b>About every 5 minutes</b>", "CMED's server signs a short note with the "
                   f"private key and swaps it for a 5-minute token at "
                   f"{mono('/api/v2/oauth/token')}", "4"],
                  ["<b>1. Doctor opens a patient</b>", "<b>API 1</b> &mdash; the doctor's "
                   "browser tells <b>AIMScribe.exe</b> on the same PC to start recording. "
                   "On the PC only; never to our server", "2"],
                  ["", "<b>API 2</b> &mdash; CMED's server sends us <b>what the paramedic "
                   "collected</b> (sex, date of birth or age, health screening) and, for a "
                   "returning patient, <b>the previous prescription if we do not hold it "
                   "yet</b>", "6, 7"],
                  ["<b>2. Doctor builds the prescription</b>", "<b>API 3</b> &mdash; the "
                   "browser tells AIMScribe.exe <i>prescription built</i>: the recording is now "
                   "<b>armed</b>, and the next patient's API 1 ends it and starts a new one", "2"],
                  ["", "<b>API 3</b> &mdash; CMED's server sends us <b>the whole "
                   "consultation</b>: chief complaints, history, allergies, O/E, S/E, G/H and "
                   "O/H, investigations, diagnosis, medicines, advice, next visit", "8, 9"],
                  ["<b>Every reply</b>", "200 ACCEPTED (sometimes with warnings): remove the "
                   "message from the outbox. Anything else: keep it and retry", "10"]],
                 [38 * mm, 100 * mm, 18 * mm], highlight=[4, 6]))
    S.append(Spacer(1, 6))
    S.append(Paragraph("What goes where", H2))
    S.append(Paragraph("Every piece of the clinical sheet, the message it travels in, and the "
                       "FHIR resource that carries it.", BODY))
    S.append(Spacer(1, 3))
    S.append(tbl([["On CMED's screen", "Message", "FHIR resource"],
                  ["Sex, date of birth", "API 2", "Patient (gender, birthDate)"],
                  ["Age", "API 2", "Observation (LOINC 30525-0)"],
                  ["Health screening: BP, pulse, temperature, weight, height, SpO<sub>2</sub> "
                   "(BMI follows from weight and height)", "API 2",
                   "Observation, one per measurement (LOINC)"],
                  ["<b>Previous prescription</b> &mdash; only if we do not hold it (7.4)",
                   "<b>API 2</b>", "a second Encounter (the earlier visit) with its own "
                                   "findings, medicines, diagnoses, tests and advice"],
                  ["Chief complaints (severity, duration)", "<b>API 3</b>",
                   "Observation, category chief-complaint"],
                  ["Family, personal, past, drug history", "<b>API 3</b>",
                   "Observation, category history"],
                  ["Allergies: food, medicine, pollen, dust, mites", "<b>API 3</b>",
                   "Observation, category allergy (or AllergyIntolerance)"],
                  ["O/E: anaemia, jaundice, dehydration, oedema, cyanosis, lymph nodes",
                   "<b>API 3</b>", "Observation, category on-examination"],
                  ["S/E: heart, lungs, abdomen", "<b>API 3</b>",
                   "Observation, category systemic-examination"],
                  ["G/H and O/H (women)", "<b>API 3</b>", "Observation, category gynae-obstetric"],
                  ["Investigations", "<b>API 3</b>", "ServiceRequest, one per test"],
                  ["Diagnosis", "<b>API 3</b>", "Condition, one per diagnosis"],
                  ["Medicines (name, dose, frequency, duration, instruction)", "<b>API 3</b>",
                   "MedicationRequest, one per medicine"],
                  ["Advice, next consultation date, notes", "<b>API 3</b>",
                   "CarePlan (description, period.start, note)"],
                  ["Name, phone, address", "<b>not sent</b>",
                   "not needed; removed if sent"]],
                 [70 * mm, 22 * mm, 64 * mm], highlight=[4], req=[15]))
    S.append(Spacer(1, 5))
    S.append(Paragraph("<b>Only five fields can make us refuse a message</b> &mdash; patient, "
                       "doctor, hospital, start time, date (section 5 says why they are in API 2 "
                       "and API 3). Any other value is accepted however it is written; what we "
                       "had to interpret comes back as a warning.", GOOD))

    # ---------------------------------------------------------------- 1 gateway
    S.append(PageBreak())
    S.append(part("SECTION 1", "Why an API gateway",
                  "One public door, so everything behind it can stay private"))
    S.append(Paragraph("1 &middot; What the gateway is", H2))
    S.append(Paragraph(f"{mono(base)} is the server's <b>API gateway</b>: the only part of "
                       "the AIMScribe server that can be reached from the internet. Behind it run "
                       "the application server, two PostgreSQL databases, a cache, the archive "
                       "worker and the backups &mdash; none of which has a public address. Every "
                       "request CMED makes arrives at the gateway, which checks it and passes it "
                       "on over a private network.", BODY))
    S.append(Spacer(1, 4))
    S.append(tbl([["Why it is needed", "What it means for CMED"],
                  ["<b>Encryption (HTTPS)</b>. Patient names, measurements and prescriptions "
                   "cross the internet. The gateway holds the server's certificate and accepts "
                   "TLS 1.2 or 1.3 only; plain HTTP is redirected",
                   "Always use <b>https://</b>. No VPN and no extra encryption needed"],
                  ["<b>A name, not an IP address</b>. A certificate is issued for a name. The "
                   "machine behind the name can be replaced or moved without CMED noticing",
                   "Configure the name once; it survives a server move"],
                  ["<b>One door, two ports</b>. Only 443 (HTTPS) and 80 (redirect to 443) are "
                   "open. The databases are never reachable from outside",
                   "CMED's firewall needs outbound 443 only. Nothing inbound, ever"],
                  ["<b>Checks before the application</b>. A 1 MB body limit, rate limits, and "
                   "an optional address allow-list for each hospital's client",
                   "An oversized or flooding request is stopped at the door, not inside"],
                  ["<b>Routing</b>. One name serves the FHIR API, the token endpoint, the "
                   "health check, and the research portal; each goes to the right service",
                   "CMED uses only the paths in section 2"],
                  ["<b>Updates without errors</b>. When the server is updated, the gateway "
                   "holds a request for a few seconds instead of failing it",
                   "An update rarely shows as an error; the outbox covers the rest"]],
                 [86 * mm, 70 * mm]))
    S.append(Spacer(1, 4))
    if host != permanent:
        S.append(Paragraph("<b>Why the address will change once.</b> The current name "
                           f"({mono(host)}) is a temporary public name that points at the "
                           "server, so integration can start now. The permanent name will be "
                           f"{mono(permanent)}, under the university's own domain. Nothing "
                           "else changes: same paths, same keys, same messages.", NOTE))
    else:
        S.append(Paragraph("<b>Why a university name.</b> The certificate that encrypts every "
                           "connection is issued for this name; the machine behind it can be "
                           "moved or replaced without CMED changing anything; and it shows the "
                           "system is operated by UIU. The paths, keys and messages in this "
                           "guide do not depend on it.", NOTE))

    # ---------------------------------------------------------------- 2 addresses
    S.append(Paragraph("2 &middot; Every address CMED calls", H2))
    S.append(tbl([["#", "Method and full address", "Auth", "Purpose"],
                  ["1", f"GET {mono(base + '/health')}", "none",
                   "Is the server up? <b>200</b> with <font face='Courier'>{\"status\":\"healthy\", "
                   "...}</font>"],
                  ["2", f"POST {mono(base + '/api/v2/oauth/token')}", "signed assertion",
                   "<b>A 5-minute access token</b> for one hospital (section 4)"],
                  ["3", f"POST {mono(base + '/fhir/r4/$process-message')}", "Bearer",
                   "<b>API 2 and API 3</b>: patient information and prescriptions (6&ndash;8)"],
                  ["4", f"GET {mono(base + '/fhir/r4/Encounter?patient.identifier=&lt;id&gt;')}",
                   "Bearer", "The visits we already hold for a patient (section 7)"],
                  ["5", f"GET {mono(base + '/fhir/r4/Encounter/&lt;id&gt;')}", "Bearer",
                   "One visit, by the id the search gave"],
                  ["6", f"GET {mono(base + '/fhir/r4/metadata')}", "none",
                   "The CapabilityStatement"],
                  ["7", f"GET {mono(base + '/fhir/r4/.well-known/smart-configuration')}", "none",
                   "SMART discovery: the token endpoint and the scopes"],
                  ["8", f"GET {mono(base + '/fhir/r4/MessageDefinition/aimscribe-patient-information')}"
                        " (and <font face='Courier'>aimscribe-prescription</font>)", "none",
                   "The two message definitions"]],
                 [7 * mm, 83 * mm, 20 * mm, 46 * mm], req=[2, 3]))
    S.append(Spacer(1, 3))
    S.append(Paragraph("<b>Two calls do all the work:</b> 2 (token) and 3 (messages). "
                       "Content type for 3 is <font face='Courier'>application/fhir+json</font>, "
                       "UTF-8, at most 1 MB. There is no DELETE anywhere, and nothing clinical is "
                       "ever sent back to CMED.", BODY))
    S.append(Spacer(1, 3))
    S.append(tbl([["On the doctor's PC (not through the gateway)", ""],
                  [mono("ws://127.0.0.1:5050/ws"), "CMED's web page, in the doctor's browser, "
                   "talks to AIMScribe.exe on the same PC. It never leaves the PC, so it needs "
                   "no internet address; it does need CMED's page address on the recorder's "
                   "allow-list (section 3)."],
                  ["<b>start</b> (API 1)", "The patient is opened: the recorder starts, carrying "
                   "the five fields of section 5."],
                  ["<b>prescription_built</b> (API 3, the PC's part)",
                   "The prescription is built: the recording is <b>armed</b>. The next "
                   "patient's <b>start</b> then ends it and begins a new recording. Before it is "
                   "armed, another <b>start</b> does not end it &mdash; it is marked inside the "
                   "same recording (a doctor stepping between patients), and the server sorts "
                   "out whose part was whose."]],
                 [44 * mm, 112 * mm]))
    S.append(Spacer(1, 3))
    S.append(Paragraph("At <b>Build Prescription</b>, CMED does two things at once: the page "
                       "sends <b>prescription_built</b> to AIMScribe.exe, and CMED's server sends "
                       "the prescription (API 3) to our server.", BODY))

    # ---------------------------------------------------------------- 3 before
    S.append(PageBreak())
    S.append(Paragraph("3 &middot; What CMED sends us before the first message", H2))
    S.append(tbl([["#", "What", "Why"],
                  ["<b>1</b>", "<b>The CMED code of each hospital</b> (the "
                               "<font face='Courier'>hospital_id</font> CMED will send), for all "
                               "seven clinics",
                   "Each clinic is registered under it; a message for an unregistered code "
                   "is refused"],
                  ["<b>2</b>", "<b>One public key per hospital</b>, as a JWK (ES384 "
                               "recommended; ES256, or RS384 with 2048+ bits, also accepted). "
                               "<b>Never the private key</b>",
                   "Each hospital has its own identity. We register the key and send back its "
                   "<font face='Courier'>client_id</font> and <font face='Courier'>kid</font>"],
                  ["<b>3</b>", "<b>The public IP address(es) of CMED's server</b> that will call "
                               "us", "Optional but recommended: a stolen token then cannot be "
                                     "used from anywhere else"],
                  ["<b>4</b>", "<b>Every web address CMED's page is served from</b> (production, "
                               "staging), exactly, e.g. "
                               "<font face='Courier'>https://ehr.example.com</font>",
                   "The recorder on each PC accepts commands only from these pages. "
                   "<b>Without it the recorder never starts</b>"],
                  ["<b>5</b>", "Confirmation that CMED's server clock is on NTP",
                   "Assertions are checked within &plusmn;60 seconds"]],
                 [8 * mm, 78 * mm, 70 * mm], req=[1, 2, 4]))
    S.append(Spacer(1, 4))
    S.append(Paragraph("Send items 1&ndash;4 to AIMS LAB by email. Keys are public by design; "
                       "there is nothing secret in them. The private keys stay in CMED's secret "
                       "store.", BODY))

    # ---------------------------------------------------------------- 4 token
    S.append(Paragraph("4 &middot; Getting a token", H2))
    S.append(Paragraph("HL7 SMART Backend Services (OAuth 2.0 client credentials with a signed "
                       "JWT, RFC 7523) &mdash; the standard way one health system's server proves "
                       "who it is to another's. <b>There is no password and no shared secret.</b> "
                       "It happens at three different moments:", BODY))
    S.append(Spacer(1, 3))
    S.append(tbl([["When", "Who does what", "Result"],
                  ["<b>Once per hospital</b><br/>(setup, by email)",
                   "1. CMED makes a key pair for the hospital and keeps the <b>private</b> half "
                   "in its server's secret store.<br/>2. CMED emails AIMS LAB the <b>public</b> "
                   "half (a JWK) and the hospital's code.<br/>3. AIMS LAB registers it and "
                   "emails back a <font face='Courier'>client_id</font> and a "
                   "<font face='Courier'>kid</font>.",
                   "CMED's server holds: private key, client_id, kid. Valid up to a year; we "
                   "renew it before then"],
                  ["<b>About every 5 minutes</b><br/>(automatic)",
                   "4. CMED's server writes a small JWT (the <b>assertion</b>) saying \"I am "
                   "client_id, for this server, until 5 minutes from now\" and <b>signs it "
                   "with the private key</b>.<br/>5. It POSTs the assertion to our token "
                   "address.<br/>6. We check the signature with the public key we hold.",
                   "An <b>access token</b>, good for 5 minutes, for that hospital only"],
                  ["<b>Every message</b>",
                   "7. CMED's server sends API 2 or API 3 with the header "
                   "<font face='Courier'>Authorization: Bearer &lt;access token&gt;</font>.",
                   "200 ACCEPTED (section 10)"]],
                 [32 * mm, 88 * mm, 36 * mm], highlight=[2]))
    S.append(Spacer(1, 3))
    S.append(Paragraph("<b>Why this way:</b> the private key never leaves CMED, so nothing that "
                       "crosses the internet or sits in our database can be used to pretend to be "
                       "CMED. A stolen access token dies within 5 minutes and works for one "
                       "hospital only. CMED keeps one token per hospital in memory and asks for a "
                       "new one about 30 seconds before it expires &mdash; never one per "
                       "message. The reference implementation is "
                       "<font face='Courier'>tools/cmed_fhir_client.py</font> "
                       "(<font face='Courier'>TokenCache</font>).", GOOD))
    S.append(Paragraph("The exact request", H3))
    S.append(code(
        "Assertion (JWT), header:  {\"alg\": \"ES384\", \"kid\": \"<kid we sent you>\", \"typ\": \"JWT\"}\n"
        "                  claims:  iss = sub = <client_id we sent you>\n"
        f"                           aud = {base}/api/v2/oauth/token\n"
        "                           jti = <fresh random, 8-256 chars, used once>\n"
        "                           exp = now + at most 300 s,  iat = now\n"
        "\n"
        f"POST {base}/api/v2/oauth/token\n"
        "Content-Type: application/x-www-form-urlencoded\n"
        "\n"
        "grant_type=client_credentials\n"
        "&client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer\n"
        "&client_assertion=<the signed JWT>\n"
        "&scope=system/Bundle.c system/Encounter.rs\n"
        "\n"
        "200  {\"access_token\": \"...\", \"token_type\": \"bearer\", \"expires_in\": 300,\n"
        "      \"scope\": \"system/Bundle.c system/Encounter.rs\", \"hospital_ids\": [\"HOSP003\"]}"))
    S.append(Paragraph("<b>The audience is the address.</b> "
                       f"{mono('aud')} must be exactly <font face='Courier'>{base}"
                       "/api/v2/oauth/token</font>; build it from the configured address so it "
                       "follows any change of address. Reuse a token until about 30 "
                       "seconds before it expires. Every call then carries "
                       "<font face='Courier'>Authorization: Bearer &lt;token&gt;</font>.", NOTE))

    # ---------------------------------------------------------------- 5 envelope
    S.append(PageBreak())
    S.append(Paragraph("5 &middot; The message envelope and the five fields", H2))
    S.append(Paragraph("Each message is a FHIR R4 <b>Bundle</b> of type <b>message</b>. The "
                       "first entry is a <b>MessageHeader</b> naming the event; its "
                       "<font face='Courier'>focus</font> points at <b>this visit's "
                       "Encounter</b>. Resources refer to each other by their "
                       "<font face='Courier'>fullUrl</font> inside the Bundle.", BODY))
    S.append(Spacer(1, 3))
    S.append(tbl([["Event (MessageHeader.eventCoding.code)", "Which", "Sent when"],
                  [mono("patient-information"), "<b>API 2</b>: what the paramedic "
                                                "collected, and the previous prescription if "
                                                "we do not hold it",
                   "the moment the doctor opens the patient"],
                  [mono("prescription"), "<b>API 3</b>: the whole consultation &mdash; "
                                         "findings and prescription",
                   "the doctor builds (or corrects) the prescription"]],
                 [56 * mm, 56 * mm, 44 * mm]))
    S.append(Spacer(1, 3))
    S.append(Paragraph("eventCoding.system is <font face='Courier'>urn:aimscribe:fhir:event"
                       "</font>.", BODY))
    S.append(Paragraph("The five fields &mdash; identical in every message about one visit", H3))
    S.append(tbl([["Field", "Where in FHIR", "Rule"],
                  [mono("patient_id"), "Encounter.subject &rarr; Patient.identifier",
                   "letters, digits, _ and -, up to 64"],
                  [mono("doctor_id"), "Encounter.participant[0].individual &rarr; "
                                      "Practitioner.identifier", "same"],
                  [mono("hospital_id"), "Encounter.serviceProvider &rarr; "
                                        "Organization.identifier",
                   "same; <b>must be a hospital this token speaks for</b>"],
                  [mono("start_time"), "Encounter.period.start",
                   "<b>with its offset</b>, e.g. 2026-09-13T10:14:32+06:00. The moment the "
                   "patient was opened. <b>Never changes</b>: API 3 carries the same value"],
                  [mono("date"), "derived from period.start", "the clinic's local date"]],
                 [24 * mm, 66 * mm, 66 * mm], req=[4]))
    S.append(Spacer(1, 3))
    S.append(tbl([["Why the five fields are in API 2 and API 3 too"],
                  ["API 1 starts the <b>audio</b> on the doctor's PC, and the recording reaches "
                   "our server labelled with these five values. API 2 and API 3 come "
                   "separately, from <b>CMED's server</b>, minutes or hours apart. The five "
                   "values are the <b>only link</b> between them: they tell us which recording "
                   "a patient's screening and prescription belong to. They are a label, not "
                   "extra data &mdash; and without them, a prescription could not be joined to "
                   "its consultation, and a recording could not be confirmed (we keep only "
                   "recordings CMED's server confirms). So each message about one visit "
                   "carries <b>exactly the same five values API 1 carried</b>."]],
                 [156 * mm], bold_first=False, highlight=[1]))
    S.append(Spacer(1, 4))
    S.append(tbl([["Only these five fields can refuse a message"],
                  ["They tie the message to its recording, and they come from CMED's code, not "
                   "from a person typing. <b>Every other value is accepted however it is "
                   "written</b>: read the way clinics write it (Bangla digits, units, "
                   "day-first dates, \"7 days\"), or, if it cannot be read for certain, kept "
                   "exactly as sent and named in a <b>warning</b> in the reply. A refused "
                   "message would leave its recording unconfirmed &mdash; so nothing but these "
                   "five ever refuses one."]],
                 [156 * mm], bold_first=False, highlight=[1]))
    S.append(Spacer(1, 4))
    S.append(Paragraph("Identifier systems: "
                       "<font face='Courier'>urn:aimscribe:cmed:patient</font>, "
                       "<font face='Courier'>urn:aimscribe:cmed:practitioner</font>, "
                       "<font face='Courier'>urn:aimscribe:cmed:hospital</font> &mdash; or CMED's "
                       "own, if you tell us them and we register them. Any spelling of the same "
                       "moment matches: 04:14:32Z and 10:14:32+06:00 are one visit.", BODY))

    # ---------------------------------------------------------------- 6 API 2
    S.append(PageBreak())
    S.append(part("SECTION 6", "Patient information (API 2)",
                  "What the paramedic collected, sent the moment the patient is opened"))
    S.append(Paragraph("API 2 is sent when the recording starts, before the doctor has written "
                       "anything. So it carries only <b>what the paramedic collected</b>: sex, "
                       "date of birth or age, and the health screening. <b>Not the name, phone "
                       "or address</b>: they are not needed, and if sent they are removed before "
                       "anything is stored. Everything the doctor records comes later, in "
                       "API 3.", BODY))
    S.append(Paragraph("6 &middot; Field by field", H2))
    S.append(tbl([["What", "FHIR", "Stored range (anything else: kept as sent, warned)"],
                  ["Sex", "Patient.gender", "female, male; other values stored as unknown"],
                  ["Date of birth", "Patient.birthDate", "a date"],
                  ["Age", "Observation, LOINC 30525-0, valueQuantity unit a (mo, d read too)",
                   "0 &ndash; 130 years"],
                  ["Weight", "Observation, LOINC 29463-7, kg (g and [lb_av] converted)",
                   "0.5 &ndash; 499 kg"],
                  ["Height", "Observation, LOINC 8302-2, cm (m, mm, [in_i] converted)",
                   "20 &ndash; 299 cm"],
                  ["Blood pressure", "Observation, LOINC 85354-9 with components 8480-6 "
                                     "(systolic) and 8462-4 (diastolic), mm[Hg]",
                   "30&ndash;300 / 10&ndash;250"],
                  ["Pulse", "Observation, LOINC 8867-4, /min", "10 &ndash; 300"],
                  ["Temperature", "Observation, LOINC 8310-5, Cel or [degF]; a value of "
                                  "86&ndash;113 can only be &deg;F and is converted",
                   "25 &ndash; 45 &deg;C"],
                  ["SpO<sub>2</sub>", "Observation, LOINC 59408-5, %", "0 &ndash; 100"],
                  ["When measured", "Observation.effectiveDateTime", "with offset (without: "
                                                                     "clinic time)"],
                  ["Paramedic notes", "Observation, code.text \"paramedic notes\", note[0].text",
                   "free text"]],
                 [30 * mm, 80 * mm, 46 * mm]))
    S.append(Spacer(1, 3))
    S.append(Paragraph("Every Observation carries <font face='Courier'>subject</font> (the "
                       "Patient) and <font face='Courier'>encounter</font> (this visit). <b>A "
                       "value we cannot read is never guessed and never rejects the message</b>: "
                       "its column stays empty, the value is kept as sent beside it, and the "
                       "reply names it. BMI need not be sent: it follows from weight and height. "
                       "Send the Patient on every visit, first or tenth.", BODY))
    S.append(Paragraph("6.1 &middot; The structure", H3))
    S.append(code(
        "Bundle  (type: message)\n"
        "|\n"
        "+-- MessageHeader      event = patient-information,  focus -> Encounter (today)\n"
        "+-- Encounter (today)  period.start = start_time\n"
        "|     subject          -> Patient\n"
        "|     participant      -> Practitioner\n"
        "|     serviceProvider  -> Organization\n"
        "+-- Patient            identifier = patient_id; gender; birthDate   (no name, phone, address)\n"
        "+-- Practitioner       identifier = doctor_id\n"
        "+-- Organization       identifier = hospital_id\n"
        "+-- Observation x n    one per measurement (LOINC code + value + unit)\n"
        "                       subject -> Patient,  encounter -> Encounter (today)"))
    S.append(Paragraph("6.2 &middot; Example: a first visit (no old prescription)", H3))
    S.append(code(pretty(api2_first)))

    # ---------------------------------------------------------------- 7 old prescription
    S.append(PageBreak())
    S.append(part("SECTION 7", "Old prescription (the previous visit)",
                  "Sent inside API 2, so the history reaches us even if we never saw that visit"))
    S.append(Paragraph("7 &middot; How the old prescription travels", H2))
    S.append(Paragraph("When a returning patient is opened, API 2 may carry their <b>previous "
                       "visit</b> as a <b>second Encounter</b> in the same Bundle, with that "
                       "visit's own prescription &mdash; its findings, medicines, diagnoses, "
                       "tests and advice. It is a <b>backfill</b>, sent <b>only when we do not "
                       "hold that visit yet</b> (ask first: 7.4). If we already hold it, we "
                       "store nothing and answer <b>ALREADY_STORED</b>. It never changes "
                       "today's visit.", BODY))
    S.append(Spacer(1, 3))
    S.append(tbl([["Part", "FHIR", "Rule"],
                  ["The earlier visit", "a second Encounter, status finished, NOT the "
                                        "MessageHeader's focus", "<b>at most one</b> per message"],
                  ["Its doctor", "Encounter.participant &rarr; Practitioner.identifier",
                   "<b>that visit's own doctor</b>, not today's"],
                  ["Its hospital", "Encounter.serviceProvider &rarr; Organization.identifier",
                   "that visit's own hospital"],
                  ["Its time", "Encounter.period.start", "that visit's own start time, with "
                                                         "offset; earlier than today's"],
                  ["Its medicines", "MedicationRequest, encounter &rarr; the earlier Encounter",
                   "as in section 8"],
                  ["Its diagnoses, tests, advice, follow-up, notes",
                   "Condition, ServiceRequest, CarePlan &mdash; each pointing at the earlier "
                   "Encounter", "as in section 8"],
                  ["Its findings (complaints, history, O/E ...)",
                   "Observation with a finding category, pointing at the earlier Encounter",
                   "as in section 9"],
                  ["No previous visit", "send no second Encounter", "we record "
                                                                     "\"not sent\""]],
                 [36 * mm, 70 * mm, 50 * mm], req=[2, 4]))
    S.append(Spacer(1, 3))
    S.append(Paragraph("<b>Doctor, hospital and time go together, or not at all.</b> Half a "
                       "name could file the prescription against the wrong visit, so it is never "
                       "used: the visit is kept by its date alone, and a warning says so. A "
                       "previous visit that cannot be used at all (another patient, today's or a "
                       "later visit, no prescription) is set aside with the reason. <b>Today's "
                       "API 2 is accepted either way</b> &mdash; it is what confirms the "
                       "recording.", NOTE))
    S.append(Paragraph("7.1 &middot; The structure: API 2 for a returning patient", H3))
    S.append(code(
        "Bundle  (type: message)\n"
        "|\n"
        "+-- MessageHeader         event = patient-information,  focus -> Encounter (today)\n"
        "+-- Encounter (today)     ... as in 6.1\n"
        "+-- Patient, Practitioner, Organization, Observation x n   ... as in 6.1\n"
        "|\n"
        "+-- Encounter (previous)  status = finished,  NOT the focus\n"
        "|     period.start        = that visit's own start time\n"
        "|     participant         -> Practitioner of THAT visit (added if a different doctor)\n"
        "|     serviceProvider     -> Organization of THAT visit\n"
        "+-- MedicationRequest x n encounter -> Encounter (previous)   its medicines\n"
        "+-- Condition x n         encounter -> Encounter (previous)   its diagnoses\n"
        "+-- ServiceRequest x n    encounter -> Encounter (previous)   its investigations\n"
        "+-- CarePlan              encounter -> Encounter (previous)   advice, follow-up, notes\n"
        "+-- Observation x n       encounter -> Encounter (previous)   its findings (section 9)"))
    S.append(Paragraph("The <b>encounter</b> reference is what tells the old prescription from "
                       "today's: everything pointing at the previous Encounter belongs to that "
                       "visit.", BODY))
    S.append(Paragraph("7.2 &middot; Example: a returning patient, with the old prescription", H3))
    S.append(code(pretty(api2_returning)))
    S.append(Paragraph("7.3 &middot; What we reply about the old prescription", H3))
    S.append(tbl([["previous_visit status", "Meaning"],
                  ["STORED", "We did not have that visit; it is stored under its real doctor "
                             "and hospital"],
                  ["ALREADY_STORED", "We hold it with the same prescription; nothing stored. "
                                     "No need to send it again"],
                  ["UPDATED", "We held it with a different prescription; stored as its next "
                              "version"],
                  ["NOT_SENT", "No second Encounter was sent"],
                  ["NOT_PERMITTED", "That visit was at another hospital and differs; only that "
                                    "hospital's own client may correct it"],
                  ["NOT_USED", "Set aside, with the reason; today's information was stored"],
                  ["PENDING", "Safe, being filed; nothing to do"]],
                 [40 * mm, 116 * mm]))
    S.append(Paragraph("The reply to the example above:", BODY))
    S.append(code(pretty(reply2)))
    S.append(Paragraph("7.4 &middot; Ask first, to avoid sending what we hold", H3))
    S.append(code(f"GET {base}/fhir/r4/Encounter?patient.identifier=P0012345&_count=50\n"
                  "Authorization: Bearer <token>"))
    S.append(Paragraph("Returns a searchset Bundle of the visits we hold <b>at the token's "
                       "hospital</b>, newest first, each with its prescription version and "
                       "<b>fingerprint</b> &mdash; never a name, a medicine or a diagnosis. If "
                       "the previous visit is listed with the same fingerprint, send no second "
                       "Encounter; otherwise send it (the fingerprint is in 7.5). <b>If the question cannot be asked</b> "
                       "(timeout, server down), send it anyway: about two kilobytes, and the "
                       "reply says ALREADY_STORED.", BODY))
    S.append(Paragraph("7.5 &middot; The prescription fingerprint", H3))
    S.append(Paragraph("SHA-256 (64 lowercase hex) of one exact JSON text: the six keys "
                       "<font face='Courier'>items</font> (each exactly drug, dose, frequency, "
                       "duration, instructions, in prescribed order), "
                       "<font face='Courier'>diagnoses</font>, "
                       "<font face='Courier'>investigations</font>, "
                       "<font face='Courier'>advice</font>, "
                       "<font face='Courier'>follow_up</font>, "
                       "<font face='Courier'>notes</font>; text trimmed, empty text as null, a "
                       "missing list as []; keys sorted at every level, no spaces, non-ASCII "
                       "written as itself; UTF-8.", BODY))
    S.append(code(
        "{\"advice\":\"Rest and fluids\",\"diagnoses\":[\"Fever\"],\"follow_up\":\"2026-10-08\",\n"
        " \"investigations\":[\"CBC\"],\"items\":[{\"dose\":\"500 mg\",\"drug\":\"Paracetamol\",\n"
        " \"duration\":\"5 days\",\"frequency\":\"1+1+1\",\"instructions\":null}],\"notes\":null}\n"
        "(one line, no spaces or line breaks)\n"
        f"-> {fp}"))
    S.append(Paragraph("Reproduce that value before relying on your implementation; the server "
                       "computed it while this guide was made.", NOTE))

    # ---------------------------------------------------------------- 8 API 3
    S.append(PageBreak())
    S.append(part("SECTION 8", "The consultation and its prescription (API 3)",
                  "Everything the doctor recorded, from the chief complaint to the advice"))
    S.append(Paragraph("API 3 is sent when the doctor builds the prescription, and again if it "
                       "is corrected. It carries <b>the whole consultation</b>: the doctor's "
                       "findings (section 9) and the prescription (below), in one message.",
                       BODY))
    S.append(Paragraph("8 &middot; Field by field", H2))
    S.append(tbl([["What", "FHIR", "Rule"],
                  ["When issued", "MedicationRequest.authoredOn",
                   "<b>required</b>, with offset (else the Bundle timestamp)"],
                  ["Medicine", "MedicationRequest.medicationCodeableConcept.text",
                   "<b>required per medicine</b>; one MedicationRequest each, in prescribed "
                   "order"],
                  ["Dose", "dosageInstruction[0].doseAndRate[0].doseQuantity (value, unit)",
                   "e.g. 1 tablet; up to 200"],
                  ["Frequency", "dosageInstruction[0].timing.code.text "
                                "(or timing.repeat frequency/period/periodUnit)",
                   "e.g. twice daily, 1+0+1"],
                  ["Duration", "dosageInstruction[0].timing.repeat.boundsDuration",
                   "e.g. 30 days"],
                  ["Instructions", "dosageInstruction[0].patientInstruction", "up to 1000"],
                  ["Diagnosis", "Condition.code.text, one per diagnosis", "up to 500 each"],
                  ["Investigation", "ServiceRequest.code.text, one per test", "up to 500 each"],
                  ["Advice", "CarePlan.description", "up to 2000"],
                  ["Next consultation", "CarePlan.period.start", "YYYY-MM-DD"],
                  ["Notes", "CarePlan.note[0].text", "up to 2000"],
                  ["Chief complaints, history, allergies, O/E, S/E, G/H, O/H",
                   "Observation with a finding category, one per finding", "section 9"]],
                 [28 * mm, 82 * mm, 46 * mm], req=[1, 2], highlight=[12]))
    S.append(Spacer(1, 3))
    S.append(Paragraph("Each resource carries <font face='Courier'>subject</font> (the Patient) "
                       "and <font face='Courier'>encounter</font> (this visit). <b>None ordered "
                       "is not the same as not sent</b>: a prescription with no tests simply has "
                       "no ServiceRequest.", BODY))
    S.append(Paragraph("8.1 &middot; The structure", H3))
    S.append(code(
        "Bundle  (type: message)\n"
        "|\n"
        "+-- MessageHeader      event = prescription,  focus -> Encounter (today)\n"
        "+-- Encounter (today)  the SAME five fields as that visit's API 2\n"
        "+-- Patient, Practitioner, Organization   identifiers only are needed\n"
        "+-- MedicationRequest  one per medicine, in prescribed order\n"
        "|     medicationCodeableConcept.text = name;  authoredOn = issued time\n"
        "|     dosageInstruction: dose, timing (frequency, duration), patientInstruction\n"
        "+-- Condition          one per diagnosis          (code.text)\n"
        "+-- ServiceRequest     one per investigation      (code.text)\n"
        "+-- CarePlan           description = advice, period.start = next consultation,\n"
        "|                      note = notes\n"
        "+-- Observation x n    the doctor's findings, one each (section 9):\n"
        "|                      category = chief-complaint | history | allergy |\n"
        "|                                 on-examination | systemic-examination |\n"
        "|                                 gynae-obstetric\n"
        "|                      code.text = what it is about; valueString = the finding\n"
        "  every one: subject -> Patient,  encounter -> Encounter (today)"))
    S.append(Paragraph("8.2 &middot; Example: a complete consultation", H3))
    S.append(code(pretty(api3)))
    S.append(Paragraph("8.3 &middot; The reply", H3))
    S.append(code(pretty(reply)))
    S.append(Paragraph("<b>A corrected prescription</b>: send it again with the same five fields "
                       "and a new authoredOn. A real change is stored as the next version; the "
                       "same prescription again is answered ALREADY_RECEIVED. An old message "
                       "replayed after a newer one never rolls the visit back.", BODY))

    # ---------------------------------------------------------------- 9 rest of sheet
    S.append(PageBreak())
    S.append(Paragraph("9 &middot; The doctor's findings, in API 3", H2))
    S.append(Paragraph("Each finding is one <b>Observation</b> in the API 3 Bundle, pointing at "
                       "this visit's Encounter. Its <b>category</b> says which part of the sheet "
                       "it is; <b>code.text</b> says what it is about; <b>valueString</b> is what "
                       "the doctor wrote. Category system: "
                       "<font face='Courier'>urn:aimscribe:fhir:finding</font>.", BODY))
    S.append(Spacer(1, 3))
    S.append(tbl([["Part of the sheet", "category code", "code.text (the item)",
                   "valueString, and components"],
                  ["Chief complaint", mono("chief-complaint"), "the complaint, e.g. Fever",
                   "components <b>severity</b>, <b>duration</b> (code.text + valueString); "
                   "value may be left out"],
                  ["History", mono("history"), "family, personal, past, drug, social",
                   "what the doctor wrote"],
                  ["Allergy", mono("allergy"), "food, medicine, pollen, dust, mites, other",
                   "the substance, or yes / no"],
                  ["On examination (O/E)", mono("on-examination"),
                   "anaemia, jaundice, dehydration, oedema, cyanosis, lymph nodes",
                   "absent / present / mild ..."],
                  ["Systemic examination (S/E)", mono("systemic-examination"),
                   "heart, lungs, abdomen", "the finding; a note in note[0].text"],
                  ["G/H and O/H", mono("gynae-obstetric"),
                   "gynaecological, obstetric, menstrual, LMP ...", "what the doctor wrote"]],
                 [32 * mm, 34 * mm, 44 * mm, 46 * mm]))
    S.append(Spacer(1, 3))
    S.append(Paragraph("<b>One Observation per finding.</b> They are stored one row each, so "
                       "researchers can count every patient with jaundice or every complaint of "
                       "fever. Allergies may instead be sent as FHIR "
                       "<font face='Courier'>AllergyIntolerance</font> (category food / "
                       "medication / environment, code.text the substance). A corrected finding "
                       "makes the prescription's next version, like any other correction.", BODY))
    S.append(Paragraph("9.1 &middot; Two findings, as they appear in the Bundle", H3))
    S.append(code(pretty([
        {"resourceType": "Observation", "status": "final",
         "category": [{"coding": [{"system": "urn:aimscribe:fhir:finding",
                                   "code": "chief-complaint"}]}],
         "code": {"text": "Fever"},
         "component": [{"code": {"text": "severity"}, "valueString": "moderate"},
                       {"code": {"text": "duration"}, "valueString": "3 days"}],
         "subject": {"reference": "urn:uuid:<the Patient>"},
         "encounter": {"reference": "urn:uuid:<this Encounter>"}},
        {"resourceType": "Observation", "status": "final",
         "category": [{"coding": [{"system": "urn:aimscribe:fhir:finding",
                                   "code": "on-examination"}]}],
         "code": {"text": "jaundice"}, "valueString": "absent",
         "subject": {"reference": "urn:uuid:<the Patient>"},
         "encounter": {"reference": "urn:uuid:<this Encounter>"}}])))
    S.append(Paragraph("The complete set for one consultation is in the example in 8.2.", BODY))

    # ---------------------------------------------------------------- 10 replies
    S.append(Paragraph("10 &middot; Replies, and what to do about each", H2))
    S.append(tbl([["Reply", "Meaning", "CMED does"],
                  ["<b>200 ACCEPTED</b>", "Stored, before the reply was sent",
                   "Remove from the outbox"],
                  ["<b>200 ALREADY_RECEIVED</b>", "Already held", "Remove; never resend"],
                  ["400 MALFORMED_FHIR / MISSING_FIELD / INVALID_IDENTIFIER",
                   "The element is named in OperationOutcome.issue[0].expression",
                   "Fix, then send"],
                  ["401 INVALID_TOKEN", "Missing, expired or revoked token",
                   "New token once, resend; if the token itself is refused, stop and tell us"],
                  ["403 HOSPITAL_NOT_ALLOWED", "This token is for another hospital",
                   "Use that hospital's client. Do not retry"],
                  ["413 TOO_LARGE", "Over 1 MB", "Trim; usually a mistake"],
                  ["<b>200 with warnings</b>", "Accepted. Each warning names a field, what "
                                                  "was sent, and what was done with it",
                   "Remove from the outbox. Fix the field at source; never resend for a "
                   "warning"],
                  ["429, 5xx, timeout, no connection", "Busy, updating, or no internet",
                   "<b>Keep it</b>; retry with growing delay, honour Retry-After"]],
                 [52 * mm, 56 * mm, 48 * mm], highlight=[1, 2]))
    S.append(Paragraph("10.1 &middot; A reply with a warning", H3))
    S.append(Paragraph("The same API 2 as in 6.2, with the temperature sent as 98.6 and labelled "
                       "Cel. Only Fahrenheit can be 98.6, so it is stored as 37.0 &deg;C and the "
                       "reply says so:", BODY))
    S.append(code(pretty(reply_warned)))
    S.append(Spacer(1, 3))
    S.append(Paragraph("<b>Send from an outbox.</b> Write each message to a table when the "
                       "doctor clicks, send in order (a visit's API 2 before its API 3), and "
                       "remove it only on ACCEPTED or ALREADY_RECEIVED. A message that arrives a "
                       "day late still attaches to its recording; <b>a message never sent is the "
                       "only failure</b>. Quote the request_id from any reply when reporting a "
                       "problem.", BODY))

    # ---------------------------------------------------------------- 11 testing
    S.append(PageBreak())
    S.append(Paragraph("11 &middot; Testing, and the go-live checklist", H2))
    S.append(Paragraph("Our test tool runs every case that matters against the live server with "
                       "a real token: a good message, the same twice, a broken one, a changed "
                       "prescription, a nameless medicine, another hospital's message, no token, "
                       "an oversized body. Please run it and send us the output.", BODY))
    S.append(code(f"python tools/channel_b_test.py --server {base} \\\n"
                  "    --client-id <client id> --kid <kid> \\\n"
                  "    --key-file <hospital>.private.pem --hospital <CMED hospital id>"))
    S.append(Spacer(1, 4))
    S.append(tbl([["", "Before the first clinic goes live"],
                  ["[  ]", f"Address configured in one place: {mono(base)}"],
                  ["[  ]", f"GET {mono(base + '/health')} answers 200 from CMED's server"],
                  ["[  ]", "Hospital codes, public keys, server IPs and page addresses sent "
                              "to AIMS LAB (section 3)"],
                  ["[  ]", "client_id and kid received for each hospital"],
                  ["[  ]", "Token request returns 200 with the right hospital_ids"],
                  ["[  ]", "API 2 (first visit and returning) and API 3 return 200 ACCEPTED"],
                  ["[  ]", "The fingerprint example in 7.5 reproduced exactly"],
                  ["[  ]", "Outbox in place; retries tested by unplugging the network"],
                  ["[  ]", "channel_b_test.py output sent to AIMS LAB"],
                  ["[  ]", (f"Ready to change one value to {mono('https://' + permanent)} "
                            "when we announce it") if host != permanent else
                   "Token audience built from the configured address (section 4)"]],
                 [10 * mm, 146 * mm], bold_first=False))
    S.append(Spacer(1, 6))
    S.append(Paragraph("<b>What must never be sent:</b> audio; a consent or refusal field (the "
                       "patient's refusal is made on the recorder itself); a private key; any "
                       "token or key from a browser.", BAD))
    return S


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="the server's public name, no https://")
    parser.add_argument("--permanent", default="aimscribe.iriic.uiu.ac.bd",
                        help="the permanent name it will move to")
    parser.add_argument("--out", default=str(ROOT / "CMED_LIVE_CONNECTION_GUIDE.pdf"))
    args = parser.parse_args(argv)
    host = args.host.replace("https://", "").strip("/")
    build(story(host, args.permanent), Path(args.out),
          "CMED - connecting to the live AIMScribe server",
          f"AIMScribe v3 - CMED connection guide - https://{host}")


if __name__ == "__main__":
    main()
