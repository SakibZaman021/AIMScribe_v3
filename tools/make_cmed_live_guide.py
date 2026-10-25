#!/usr/bin/env python3
"""
Build the CMED connection guide for the live server - how the three channels
make one consultation, the commands on the doctor's PC, every awkward case, the
API gateway and why it exists, and the FHIR messages in full.

    python tools/make_cmed_live_guide.py --host <the server's public name> [--out NAME.pdf]

The host is an argument, never written here: the guide is rebuilt when the
server moves to its permanent name. Every example message in it is built by
the reference client (tools/cmed_fhir_client.py) and then read back by the
server's own FHIR reader (backend/src/fhir.py) and validator
(backend/src/clinical.py) while the PDF is made - a message the server would
refuse cannot reach the page. The fingerprint example is recomputed with the
server's function. Sources for the rules: CMED_DATA_CONTRACT.md, the
recorder's protocol (recorder/api/protocol.py) and the Caddyfile.
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
from reportlab.platypus import (CondPageBreak, HRFlowable, KeepTogether, PageBreak,
                                Paragraph, Preformatted, Spacer)

import cmed_diagrams as diagrams
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
    S.append(Paragraph("<b>For CMED's server and software teams</b> &middot; how one consultation "
                       "travels, every awkward case, the gateway and why it exists, and the FHIR "
                       "messages in full &middot; AIMS LAB, United International University", SUB))
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
                    "in section 3 answers 200.<br/><b>Keep it in one configuration value</b> "
                    "(for example "
                    f"{mono('AIMS_SERVER_URL')}), never compiled in: every address below, and "
                    "the token audience, follow from it.")]],
                 [156 * mm], bold_first=False, highlight=[1]))
    S.append(Spacer(1, 5))
    S.append(tbl([["What this guide covers", "Section"],
                  ["<b>Three channels, one consultation</b> &mdash; the part most often "
                   "misread", "1"],
                  ["<b>The API gateway</b>: what it does, in order, and why it exists", "2"],
                  ["Every address CMED calls, in full", "3"],
                  ["<b>On the doctor's PC</b>: start, prescription_built and stop, exactly",
                   "4"],
                  ["<b>Every awkward case</b>: wrong patient, no prescription, last patient, "
                   "no internet ...", "5"],
                  ["What CMED sends us before the first message", "6"],
                  ["Getting a token", "7"],
                  ["The message envelope and the five fields", "8"],
                  ["Current patient information (API 2)", "9"],
                  ["Old prescription (the previous visit, inside API 2)", "10"],
                  ["Current prescription (API 3)", "11"],
                  ["The doctor's findings (in API 3)", "12"],
                  ["Replies and what to do about each", "13"],
                  ["Testing, and the go-live checklist", "14"]],
                 [136 * mm, 20 * mm], highlight=[1, 4, 5]))
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
                   "6, 7"],
                  ["<b>About every 5 minutes</b>", "CMED's server signs a short note with the "
                   f"private key and swaps it for a 5-minute token at "
                   f"{mono('/api/v2/oauth/token')}", "7"],
                  ["<b>1. Doctor opens a patient</b>", "<b>API 1</b> &mdash; the doctor's "
                   "browser tells <b>AIMScribe.exe</b> on the same PC to start recording. "
                   "On the PC only; never to our server", "4"],
                  ["", "<b>API 2</b> &mdash; CMED's server sends us <b>what the paramedic "
                   "collected</b> (sex, date of birth or age, health screening) and, for a "
                   "returning patient, <b>the previous prescription if we do not hold it "
                   "yet</b>", "9, 10"],
                  ["<b>2. Doctor builds the prescription</b>", "<b>API 3</b> &mdash; the "
                   "browser tells AIMScribe.exe <i>prescription built</i>: the recording is now "
                   "<b>armed</b>, and the next patient's API 1 ends it and starts a new one", "4"],
                  ["", "<b>API 3</b> &mdash; CMED's server sends us <b>the whole "
                   "consultation</b>: chief complaints, history, allergies, O/E, S/E, G/H and "
                   "O/H, investigations, diagnosis, medicines, advice, next visit", "11, 12"],
                  ["<b>3. Doctor logs out, or ends the day</b>", "<b>stop</b> &mdash; the "
                   "browser tells AIMScribe.exe to close the last recording. Nothing else ends "
                   "the last patient's recording but the doctor's own Stop button or the PC "
                   "shutting down", "4, 5"],
                  ["<b>Every reply</b>", "200 ACCEPTED (sometimes with warnings): remove the "
                   "message from the outbox. Anything else: keep it and retry", "13"]],
                 [38 * mm, 100 * mm, 18 * mm], highlight=[4, 6, 7]))
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
                  ["<b>Previous prescription</b> &mdash; only if we do not hold it (10.4)",
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
                       "doctor, hospital, start time, date (section 8 says why they are in API 2 "
                       "and API 3). Any other value is accepted however it is written; what we "
                       "had to interpret comes back as a warning.", GOOD))

    # ---------------------------------------------------------------- 1 channels
    S.append(PageBreak())
    S.append(part("SECTION 1", "Three channels, one consultation",
                  "The part most often misread: two of CMED's programs talk to two of ours"))
    S.append(Paragraph("1 &middot; Who talks to whom", H2))
    S.append(Paragraph("One consultation reaches AIMS LAB along <b>three separate paths</b>. "
                       "CMED writes two of them: its <b>page</b> in the doctor's browser talks to "
                       "<b>AIMScribe.exe</b> on the same PC, and CMED's <b>server</b> talks to our "
                       "server. The third, the audio, CMED never touches.", BODY))
    S.append(Spacer(1, 4))
    S.append(diagrams.architecture())
    S.append(Spacer(1, 4))
    S.append(tbl([["Path", "From &rarr; to", "Carries", "Written by"],
                  ["<b>A &middot; on the PC</b>", "CMED's page &rarr; AIMScribe.exe, "
                   f"{mono('ws://127.0.0.1:5050/ws')}",
                   "<b>start</b> (API 1), <b>prescription_built</b> (API 3a), <b>stop</b>",
                   "CMED's <b>page</b> (section 4)"],
                  ["<b>B &middot; server to server</b>", f"CMED's server &rarr; {mono(base)}",
                   "<b>API 2</b> patient information, <b>API 3b</b> the consultation",
                   "CMED's <b>server</b> (sections 6&ndash;13)"],
                  ["<b>Audio</b>", "AIMScribe.exe &rarr; Cloudflare R2 and our server",
                   "the recording, in encrypted pieces", "nobody at CMED: automatic"]],
                 [32 * mm, 50 * mm, 44 * mm, 30 * mm], highlight=[1, 2]))
    S.append(Spacer(1, 5))
    S.append(tbl([["The rule that is missed: each click has two halves"],
                  ["<b>Opening a patient</b> sends <b>start</b> from the page to the recorder "
                   "<b>and</b> API 2 from CMED's server to ours. <b>Build Prescription</b> sends "
                   "<b>prescription_built</b> to the recorder <b>and</b> API 3b to our server. "
                   "Neither half replaces the other: the recorder never sees API 2 or 3b, and "
                   "our server never sees the page. If the page half is missing, there is no "
                   "audio; if the server half is missing, the audio is <b>never confirmed and "
                   "is erased after 7 days</b> (section 5, case 11)."]],
                 [156 * mm], bold_first=False, highlight=[1]))

    S.append(PageBreak())
    S.append(Paragraph("1.1 &middot; How the halves find each other: the five fields", H3))
    S.append(Paragraph("The recording and CMED's messages travel separately, often minutes "
                       "apart, sometimes days apart after an outage. The <b>only</b> thing that "
                       "joins them is five values, built once when the patient is opened and "
                       "passed unchanged to every message about that visit.", BODY))
    S.append(Spacer(1, 3))
    S.append(diagrams.five_fields())
    S.append(PageBreak())
    S.append(Paragraph("1.2 &middot; One consultation, in order", H3))
    S.append(diagrams.sequence())
    S.append(Spacer(1, 4))
    S.append(KeepTogether([Paragraph("1.3 &middot; The recorder's states", H3),
                           Paragraph("This is the idea most often misread. <b>prescription_built does not stop "
                       "the recording</b>: the doctor usually goes on explaining the "
                       "prescription, and that audio is some of the most valuable in the "
                       "study. It only arms the recording, so that the <b>next</b> patient's "
                       "start closes it. <b>No clock ever ends a recording</b>: timers cut real "
                       "consultations short when they were tried.", BODY),
                           Spacer(1, 3), diagrams.gate()]))

    # ---------------------------------------------------------------- 2 gateway
    S.append(PageBreak())
    S.append(part("SECTION 2", "The API gateway",
                  "One public door, so everything behind it can stay private"))
    S.append(Paragraph("2 &middot; What the gateway is", H2))
    S.append(Paragraph(f"{mono(base)} is the server's <b>API gateway</b>: the only part of "
                       "the AIMScribe server the internet can reach. Behind it run the "
                       "application server, two PostgreSQL databases, a cache, the workers, "
                       "the backups and the monitor &mdash; none of which has a public "
                       "address. Every request CMED makes arrives at the gateway, which checks "
                       "it and passes it on over a private network.", BODY))
    S.append(Spacer(1, 4))
    S.append(diagrams.gateway())
    S.append(PageBreak())
    S.append(Paragraph("2.1 &middot; What it does to every request, in order, and why", H3))
    S.append(tbl([["#", "What the gateway does", "Why", "What CMED notices"],
                  ["1", "Accepts <b>TLS 1.2 and 1.3</b> only, with a certificate for the name "
                        "that it renews by itself; HTTP is redirected to HTTPS, and browsers "
                        "are told never to use plain HTTP again (HSTS, one year)",
                   "Patient identifiers and prescriptions cross the internet; a certificate "
                   "that expires unnoticed stops every clinic at once",
                   "Always <b>https://</b>. No VPN, no extra encryption"],
                  ["2", "Refuses a body over <b>2 MB</b> before it reaches the application, "
                        "which refuses over 1 MB itself",
                   "Audio never comes this way, so a large body is a mistake or an attack; "
                   "stopping it at the door keeps the application free",
                   "Nothing: API 2 and API 3 are a few kilobytes"],
                  ["3", "Answers <b>404</b> for internal routes: the API's documentation, the "
                        "old v1 pipeline, the archive's and the sweep's own routes",
                   "A route that is not reachable cannot be attacked; the documentation would "
                   "be a map of every route", "Nothing: CMED uses none of them"],
                  ["4", "Sends portal routes (dashboard, database viewer, admin) to a "
                        "<b>Google sign-in check</b> first; nobody gets in unless an admin "
                        "granted their address",
                   "People and machines share one name, but never the same door",
                   "Nothing: CMED's paths do not need it"],
                  ["5", "Writes the caller's real address and a <b>request id</b> on every "
                        "request, replacing any the caller sent",
                   "The application's rate limits, lock-outs and per-hospital address list "
                   "depend on the address; a forged one would defeat them",
                   "Each reply carries <b>X-Request-ID</b>: quote it when reporting a "
                   "problem"],
                  ["6", "Passes the request over the <b>private network</b>; during an "
                        "update, <b>holds it up to 30 seconds</b> and retries instead of "
                        "failing it; checks the application's health every 10 seconds",
                   "Updates restart the application; a held request is a pause, not an "
                   "error", "An update shows as a slower reply, rarely as an error"],
                  ["7", "<b>Logs</b> method, path and status, and drops query strings, "
                        "tokens and keys",
                   "A path can carry a patient identifier; a log must never hold a "
                   "credential", "Nothing"]],
                 [6 * mm, 58 * mm, 52 * mm, 40 * mm]))
    S.append(Spacer(1, 3))
    S.append(Paragraph("The checks that need to know <b>who</b> is calling are made behind the "
                       "gateway, by the application: the token (section 7), the hospital it "
                       "speaks for, the per-hospital address list, 1,200 requests a minute per "
                       "address and 60 token requests a minute, and a 15-minute lock-out after "
                       "10 wrong credentials.", BODY))
    S.append(Paragraph("2.2 &middot; What can be reached from the internet", H3))
    S.append(tbl([["Who", "Paths", "Answer"],
                  ["<b>CMED's server</b>", f"{mono('/api/v2/oauth/token')}, "
                   f"{mono('/fhir/r4/*')}, {mono('/health')}",
                   "open, behind a token (except /health and the discovery documents)"],
                  ["Doctors' PCs", f"{mono('/api/v2/device')}, {mono('/session')}, "
                   f"{mono('/grant')}, {mono('/heartbeat')} ...",
                   "open, behind each PC's own signed identity"],
                  ["AIMS LAB staff", f"{mono('/dashboard')}, {mono('/db')}, "
                   f"{mono('/api/v2/admin/*')}", "Google sign-in, granted by an admin"],
                  ["Nobody", f"{mono('/docs')}, {mono('/openapi.json')}, {mono('/api/v1/*')}, "
                   f"{mono('/api/v2/archive/*')}, {mono('/api/v2/maintenance/*')}",
                   "404, always"]],
                 [32 * mm, 80 * mm, 44 * mm], highlight=[1]))
    S.append(Paragraph("2.3 &middot; Why a gateway, and not something else", H3))
    S.append(tbl([["The alternative", "Why it was not chosen"],
                  ["<b>The application server on the internet directly</b>",
                   "It would have to handle certificates itself; its internal routes would be "
                   "one configuration mistake away from public; and every update would show "
                   "CMED connection errors"],
                  ["<b>A VPN between CMED and AIMS LAB</b>",
                   "Software and keys to install and keep running on CMED's side, one more "
                   "thing to fail at each site, and it still needs authentication inside it. "
                   "HTTPS with signed tokens gives the same encryption with nothing to "
                   "install"],
                  ["<b>An IP address allow-list alone</b>",
                   "It proves where a request came from, not who sent it, and addresses "
                   "change. Kept as an optional extra per hospital (section 6, item 3), "
                   "never on its own"],
                  ["<b>One shared API key</b>",
                   "One leak would speak for every hospital, and a key cannot expire by "
                   "itself. Each hospital has its own key pair, and tokens die in 5 minutes "
                   "(section 7)"],
                  ["<b>CMED writing into our database</b>",
                   "It would hand CMED's server database credentials, skip every check, give "
                   "no reply to act on, and tie both systems to one table layout"],
                  ["<b>AIMS LAB fetching from CMED</b>",
                   "CMED would have to run a public API of its own and keep it up. CMED "
                   "pushing from an outbox works through outages with nothing exposed on "
                   "CMED's side"],
                  ["<b>A cloud provider's API gateway product</b>",
                   "Another company in the path of patient data and another bill. What is "
                   "needed (TLS, routing, limits, holding requests during updates) is one "
                   "small program, Caddy, on the same server"]],
                 [52 * mm, 104 * mm]))
    S.append(Spacer(1, 3))
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
    S.append(PageBreak())
    S.append(Paragraph("3 &middot; Every address CMED's server calls", H2))
    S.append(tbl([["#", "Method and full address", "Auth", "Purpose"],
                  ["1", f"GET {mono(base + '/health')}", "none",
                   "Is the server up? <b>200</b> with <font face='Courier'>{\"status\":\"healthy\", "
                   "...}</font>"],
                  ["2", f"POST {mono(base + '/api/v2/oauth/token')}", "signed assertion",
                   "<b>A 5-minute access token</b> for one hospital (section 7)"],
                  ["3", f"POST {mono(base + '/fhir/r4/$process-message')}", "Bearer",
                   "<b>API 2 and API 3</b>: patient information and prescriptions (9&ndash;11)"],
                  ["4", f"GET {mono(base + '/fhir/r4/Encounter?patient.identifier=&lt;id&gt;')}",
                   "Bearer", "The visits we already hold for a patient (section 10)"],
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
    S.append(Paragraph("The page's commands to the recorder never reach this server: they "
                       "stay on the doctor's PC (section 4).", BODY))

    # ---------------------------------------------------------------- 4 the PC
    S.append(PageBreak())
    S.append(part("SECTION 4", "On the doctor's PC: the three commands",
                  "What CMED's page sends AIMScribe.exe, what comes back, and what to do"))
    S.append(Paragraph("4 &middot; The connection", H2))
    S.append(Paragraph(f"CMED's page opens one WebSocket to {mono('ws://127.0.0.1:5050/ws')} "
                       "when it loads, and keeps it open. The address is the PC itself, so it "
                       "works with no internet. The recorder accepts it only from a page "
                       "address registered with AIMS LAB (section 6, item 4), and sends its "
                       "status as soon as it connects. Each command is one JSON object; each "
                       "gets exactly one reply, carrying back the page's own "
                       f"{mono('request_id')}.", BODY))
    S.append(Spacer(1, 3))
    S.append(tbl([["Rules for the page"],
                  ["<b>Never hold up the doctor.</b> If the recorder does not answer, or the "
                   "connection fails, the doctor carries on exactly as without AIMScribe, and "
                   "CMED's server still sends API 2 and API 3b. Show a small notice at most."],
                  ["<b>Reconnect by itself</b>, with a growing delay (1, 2, 5, 10 seconds), "
                   "and never more than one connection per page."],
                  ["<b>Keep the session_id</b> from the latest start reply, for "
                   "prescription_built."],
                  ["<b>At most 10 messages a second</b>; more are answered AGENT_NOT_READY."]],
                 [156 * mm], bold_first=False))

    S.append(Paragraph("4.1 &middot; start (API 1): the doctor opens a patient", H3))
    S.append(Paragraph("Sent <b>every</b> time a patient is opened &mdash; also a second "
                       "patient before the first one's prescription, also the same patient "
                       "again. The recorder sorts out which is which (section 5).", BODY))
    S.append(code(pretty({"command": "start", "request_id": "c-0001", "trigger": FIVE})))
    S.append(Paragraph("The reply:", BODY))
    S.append(code(pretty({"event": "ack", "command": "start", "status": 200,
                          "code": "RECORDING_STARTED", "message": "Recording started.",
                          "request_id": "c-0001",
                          "data": {"session_id": "01JC8ZK3Q4V7W9X2Y5T6R8M0NB",
                                   "started_at": "2026-09-13T04:14:32.410Z", "armed": False,
                                   "supersedes": None, "authorisation": "granted",
                                   "confirmation": "pending"},
                          "timestamp": "2026-09-13T04:14:32.415Z"})))
    S.append(Paragraph(f"{mono('RECORDING_PROVISIONAL')} (202) means the same: recording, "
                       "while the server's permission is still on its way. When a recording "
                       "was already running unarmed, the reply adds "
                       + mono('"continued": true') + " and " + mono("session_id")
                       + " is the running one &mdash; nothing else changes for the page.",
                       BODY))

    S.append(Paragraph("4.2 &middot; prescription_built (API 3a): Build Prescription", H3))
    S.append(Paragraph("Sent when the doctor builds the prescription, <b>and again</b> if they "
                       "correct it. It names the patient and the session_id from the latest "
                       "start reply. It does <b>not</b> stop the recording.", BODY))
    S.append(code(pretty({"command": "prescription_built", "request_id": "c-0002",
                          "patient_id": "P0012345",
                          "session_id": "01JC8ZK3Q4V7W9X2Y5T6R8M0NB",
                          "occurred_at": "2026-09-13T10:26:55+06:00"})))
    S.append(code(pretty({"event": "ack", "command": "prescription_built", "status": 200,
                          "code": "GATE_ARMED",
                          "message": "This consultation will end when the next patient is "
                                     "opened.", "request_id": "c-0002",
                          "data": {"session_id": "01JC8ZK3Q4V7W9X2Y5T6R8M0NB",
                                   "armed": True}})))
    S.append(Paragraph("The patient must be one opened during this recording, and the "
                       "session_id the running one; otherwise the answer is PATIENT_MISMATCH. "
                       "A follow-up with no prescription sends no prescription_built.", BODY))

    S.append(Paragraph("4.3 &middot; stop: the doctor logs out or ends the day", H3))
    S.append(Paragraph("The next patient's start closes a recording. After the <b>last</b> "
                       "patient there is no next one, so the page sends <b>stop</b> when the "
                       "doctor logs out of CMED or ends the day. Without it, the recording "
                       "runs on until the doctor's own Stop button or the PC shutting down, "
                       "and keeps whatever is said in the room. The audio is always kept: "
                       "stop never erases anything.", BODY))
    S.append(code(pretty({"command": "stop", "request_id": "c-0003",
                          "reason": "consultation_finished"})))
    S.append(Paragraph(f"{mono('reason')} is one of "
                       f"{mono('consultation_finished')}, {mono('doctor_stopped')}, "
                       f"{mono('patient_left')}, {mono('urgent_interruption')}, "
                       f"{mono('technical_problem')}, {mono('software_update')}. The reply is "
                       f"{mono('OK')}, or {mono('NO_ACTIVE_SESSION')} when nothing was "
                       "recording &mdash; both are fine. <b>Do not send stop when the page "
                       "merely reloads or a tab closes</b>: a reload in the middle of a "
                       "consultation would cut it.", BODY))

    S.append(Paragraph("4.4 &middot; Every reply code, and what the page does", H3))
    S.append(tbl([["Code", "Status", "Meaning", "The page does"],
                  ["RECORDING_STARTED, RECORDING_PROVISIONAL", "200, 202", "Recording",
                   "Keep the session_id; carry on"],
                  ["GATE_ARMED, GATE_ALREADY_ARMED", "200",
                   "The next patient will close this recording", "Nothing"],
                  ["OK, NO_ACTIVE_SESSION", "200, 409", "Stopped, or nothing was running",
                   "Nothing"],
                  ["MISSING_FIELD, INVALID_IDENTIFIER", "400",
                   "A field is missing or malformed; <b>nothing is recorded</b>",
                   "A bug in the page: log it with the field named in the message"],
                  ["PATIENT_MISMATCH", "409", "prescription_built for a patient not opened "
                                              "in this recording, or an old session_id",
                   "Use the latest start reply's session_id"],
                  ["ORIGIN_NOT_ALLOWED", "403", "This page address is not registered",
                   "Send us the address (section 6)"],
                  ["AUTHORISATION_FAILED, CLINIC_MISMATCH, DOCTOR_NOT_AT_CLINIC, "
                   "DEVICE_NOT_ENROLLED", "401, 404, 423", "A set-up problem at this PC",
                   "Carry on; tell AIMS LAB the code and which PC"],
                  ["AGENT_NOT_READY", "503", "Starting up, too many messages, or the "
                                             "microphone is busy",
                   "Retry in a few seconds; a start the recorder could not begin is retried "
                   "by the recorder itself for 15 minutes"],
                  ["(no connection)", "&mdash;", "AIMScribe.exe is not running",
                   "Carry on; reconnect in the background"]],
                 [46 * mm, 18 * mm, 46 * mm, 46 * mm], highlight=[1, 2, 3]))

    # ---------------------------------------------------------------- 5 cases
    S.append(PageBreak())
    S.append(part("SECTION 5", "Every awkward case",
                  "What CMED does, what AIMScribe does, and where the audio ends up"))
    S.append(Paragraph("A click cannot show intent: a wrong pick, a glance at another file "
                       "and the next patient after a follow-up all look the same when they "
                       "happen. So <b>nothing is refused, stopped or deleted at the "
                       "click</b>. The recorder keeps one recording and marks every click and "
                       "prescription in it to the second; when the recording closes, the "
                       "server decides whose audio is whose, and cuts it into one file per "
                       "patient at the exact click.", BODY))
    S.append(Spacer(1, 3))
    S.append(tbl([["Rule", "Evidence", "Verdict"],
                  ["R1", "A prescription for a patient", "That patient was consulted"],
                  ["R2", "The doctor went back to an earlier patient (opened again, or built "
                         "their prescription)", "What lay between was a glance: it is theirs"],
                  ["R3", "Open under 60 s, never returned to, never prescribed",
                   "A wrong pick: it belongs to the next patient"],
                  ["R4", "Open 60 s or more, no prescription, no return",
                   "A real consultation without a prescription: its own file"],
                  ["R5", "Anything still unclear",
                   "Kept, labelled by the best rule, flagged for a person &mdash; never "
                   "deleted"]],
                 [12 * mm, 84 * mm, 60 * mm]))
    S.append(Spacer(1, 5))
    cases = [
        ("1", "<b>Wrong patient opened</b>, the right one seconds later",
         "start and API 2 for both, as normal", "One recording, one file: the right patient "
         "(R3, R1). The wrong patient's API 2 is kept, with no audio"),
        ("2", "<b>A glance</b> at another file, then back",
         "start for each, as normal", "One file, the patient consulted (R2)"),
        ("3", "<b>Follow-up with no prescription</b>",
         "start and API 2. No prescription_built and no API 3b: there is none",
         "The recording runs on to the next patient's start and is cut at that click: "
         "its own file (R4), confirmed by its API 2"),
        ("4", "<b>The last patient of the day</b>",
         "<b>stop</b> when the doctor logs out or ends the day (4.3)",
         "Closed at stop. Without it: at the doctor's Stop button or PC shutdown"),
        ("5", "<b>The doctor forgets to log out</b>",
         "nothing more can be done by CMED",
         "The recording runs until the next patient, Stop, or shutdown; the next morning's "
         "first click still cuts it at the right second. Nothing is lost; the file is "
         "longer"),
        ("6", "<b>A corrected prescription</b>",
         "prescription_built again; API 3b again with the <b>same five fields</b> and a new "
         "authoredOn", "GATE_ALREADY_ARMED; the correction is stored as the next version"),
        ("7", "<b>Page reloaded</b>, or the same patient clicked twice",
         "start again, same five fields", "Same recording; nothing changes"),
        ("8", "<b>AIMScribe.exe not running</b> (PC just started, crashed)",
         "carry on; API 2 and API 3b are still sent",
         "No audio for that visit; the clinical record is stored. A recorder started "
         "again within 10 minutes of a crash resumes the recording by itself"),
        ("9", "<b>The clinic's internet is down</b>",
         "the page and recorder keep working (same PC); CMED's server keeps API 2 and 3b "
         "in its <b>outbox</b>", "The recorder keeps the audio on the PC and uploads it "
         "later; the halves are matched whenever both arrive. Nothing is lost"),
        ("10", "<b>API 3b arrives before API 2</b>, or the messages before the audio",
         "send in order when possible", "Order does not matter: either half waits for "
         "the other"),
        ("11", "<b>API 2 and API 3b never sent</b> for a recording",
         "this must not happen", "After 2 minutes: unconfirmed, alert. After <b>7 days "
         "unconfirmed: erased</b>. While CMED has sent nothing for that clinic for a day "
         "(an outage), nothing is erased"),
        ("12", "<b>start_time differs</b> between start and API 2 / 3b",
         "this must not happen: pass the same five fields (1.1)",
         "The halves never meet: as case 11"),
        ("13", "<b>The patient refuses</b> to be recorded",
         "nothing: there is no consent field to send",
         "The doctor uses the recorder's on-screen refusal; the audio and the record are "
         "erased everywhere"),
    ]
    S.append(tbl([["#", "Case", "CMED does", "AIMScribe does, and the result"]]
                 + [list(c) for c in cases],
                 [8 * mm, 44 * mm, 48 * mm, 56 * mm], req=[11, 12]))
    S.append(Spacer(1, 4))
    S.append(Paragraph("Cases 11 and 12 are the only ones in which a consultation is lost, "
                       "and both are on CMED's side: <b>every opened patient's API 2 must "
                       "reach us</b>, with the five fields exactly as start carried "
                       "them.", BAD))

    # ---------------------------------------------------------------- 6 before
    S.append(PageBreak())
    S.append(Paragraph("6 &middot; What CMED sends us before the first message", H2))
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
                   "The recorder on each PC accepts commands only from these pages (section 4). "
                   "<b>Without it the recorder never starts</b>"],
                  ["<b>5</b>", "Confirmation that CMED's server clock is on NTP",
                   "Assertions are checked within &plusmn;60 seconds"]],
                 [8 * mm, 78 * mm, 70 * mm], req=[1, 2, 4]))
    S.append(Spacer(1, 4))
    S.append(Paragraph("Send items 1&ndash;4 to AIMS LAB by email. Keys are public by design; "
                       "there is nothing secret in them. The private keys stay in CMED's secret "
                       "store.", BODY))

    # ---------------------------------------------------------------- 7 token
    S.append(Paragraph("7 &middot; Getting a token", H2))
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
                   "200 ACCEPTED (section 13)"]],
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

    # ---------------------------------------------------------------- 8 envelope
    S.append(PageBreak())
    S.append(Paragraph("8 &middot; The message envelope and the five fields", H2))
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

    # ---------------------------------------------------------------- 9 API 2
    S.append(PageBreak())
    S.append(part("SECTION 9", "Patient information (API 2)",
                  "What the paramedic collected, sent the moment the patient is opened"))
    S.append(Paragraph("API 2 is sent when the recording starts, before the doctor has written "
                       "anything. So it carries only <b>what the paramedic collected</b>: sex, "
                       "date of birth or age, and the health screening. <b>Not the name, phone "
                       "or address</b>: they are not needed, and if sent they are removed before "
                       "anything is stored. Everything the doctor records comes later, in "
                       "API 3.", BODY))
    S.append(Paragraph("9 &middot; Field by field", H2))
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
    S.append(Paragraph("9.1 &middot; The structure", H3))
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
    S.append(Paragraph("9.2 &middot; Example: a first visit (no old prescription)", H3))
    S.append(code(pretty(api2_first)))

    # ---------------------------------------------------------------- 10 old prescription
    S.append(PageBreak())
    S.append(part("SECTION 10", "Old prescription (the previous visit)",
                  "Sent inside API 2, so the history reaches us even if we never saw that visit"))
    S.append(Paragraph("10 &middot; How the old prescription travels", H2))
    S.append(Paragraph("When a returning patient is opened, API 2 may carry their <b>previous "
                       "visit</b> as a <b>second Encounter</b> in the same Bundle, with that "
                       "visit's own prescription &mdash; its findings, medicines, diagnoses, "
                       "tests and advice. It is a <b>backfill</b>, sent <b>only when we do not "
                       "hold that visit yet</b> (ask first: 10.4). If we already hold it, we "
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
                   "as in section 11"],
                  ["Its diagnoses, tests, advice, follow-up, notes",
                   "Condition, ServiceRequest, CarePlan &mdash; each pointing at the earlier "
                   "Encounter", "as in section 11"],
                  ["Its findings (complaints, history, O/E ...)",
                   "Observation with a finding category, pointing at the earlier Encounter",
                   "as in section 12"],
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
    S.append(Paragraph("10.1 &middot; The structure: API 2 for a returning patient", H3))
    S.append(code(
        "Bundle  (type: message)\n"
        "|\n"
        "+-- MessageHeader         event = patient-information,  focus -> Encounter (today)\n"
        "+-- Encounter (today)     ... as in 9.1\n"
        "+-- Patient, Practitioner, Organization, Observation x n   ... as in 9.1\n"
        "|\n"
        "+-- Encounter (previous)  status = finished,  NOT the focus\n"
        "|     period.start        = that visit's own start time\n"
        "|     participant         -> Practitioner of THAT visit (added if a different doctor)\n"
        "|     serviceProvider     -> Organization of THAT visit\n"
        "+-- MedicationRequest x n encounter -> Encounter (previous)   its medicines\n"
        "+-- Condition x n         encounter -> Encounter (previous)   its diagnoses\n"
        "+-- ServiceRequest x n    encounter -> Encounter (previous)   its investigations\n"
        "+-- CarePlan              encounter -> Encounter (previous)   advice, follow-up, notes\n"
        "+-- Observation x n       encounter -> Encounter (previous)   its findings (section 12)"))
    S.append(Paragraph("The <b>encounter</b> reference is what tells the old prescription from "
                       "today's: everything pointing at the previous Encounter belongs to that "
                       "visit.", BODY))
    S.append(Paragraph("10.2 &middot; Example: a returning patient, with the old prescription", H3))
    S.append(code(pretty(api2_returning)))
    S.append(Paragraph("10.3 &middot; What we reply about the old prescription", H3))
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
    S.append(Paragraph("10.4 &middot; Ask first, to avoid sending what we hold", H3))
    S.append(code(f"GET {base}/fhir/r4/Encounter?patient.identifier=P0012345&_count=50\n"
                  "Authorization: Bearer <token>"))
    S.append(Paragraph("Returns a searchset Bundle of the visits we hold <b>at the token's "
                       "hospital</b>, newest first, each with its prescription version and "
                       "<b>fingerprint</b> &mdash; never a name, a medicine or a diagnosis. If "
                       "the previous visit is listed with the same fingerprint, send no second "
                       "Encounter; otherwise send it (the fingerprint is in 10.5). <b>If the question cannot be asked</b> "
                       "(timeout, server down), send it anyway: about two kilobytes, and the "
                       "reply says ALREADY_STORED.", BODY))
    S.append(Paragraph("10.5 &middot; The prescription fingerprint", H3))
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

    # ---------------------------------------------------------------- 11 API 3
    S.append(PageBreak())
    S.append(part("SECTION 11", "The consultation and its prescription (API 3)",
                  "Everything the doctor recorded, from the chief complaint to the advice"))
    S.append(Paragraph("API 3 is sent when the doctor builds the prescription, and again if it "
                       "is corrected. It carries <b>the whole consultation</b>: the doctor's "
                       "findings (section 12) and the prescription (below), in one message.",
                       BODY))
    S.append(Paragraph("11 &middot; Field by field", H2))
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
                   "Observation with a finding category, one per finding", "section 12"]],
                 [28 * mm, 82 * mm, 46 * mm], req=[1, 2], highlight=[12]))
    S.append(Spacer(1, 3))
    S.append(Paragraph("Each resource carries <font face='Courier'>subject</font> (the Patient) "
                       "and <font face='Courier'>encounter</font> (this visit). <b>None ordered "
                       "is not the same as not sent</b>: a prescription with no tests simply has "
                       "no ServiceRequest.", BODY))
    S.append(Paragraph("11.1 &middot; The structure", H3))
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
        "+-- Observation x n    the doctor's findings, one each (section 12):\n"
        "|                      category = chief-complaint | history | allergy |\n"
        "|                                 on-examination | systemic-examination |\n"
        "|                                 gynae-obstetric\n"
        "|                      code.text = what it is about; valueString = the finding\n"
        "  every one: subject -> Patient,  encounter -> Encounter (today)"))
    S.append(Paragraph("11.2 &middot; Example: a complete consultation", H3))
    S.append(code(pretty(api3)))
    S.append(Paragraph("11.3 &middot; The reply", H3))
    S.append(code(pretty(reply)))
    S.append(Paragraph("<b>A corrected prescription</b>: send it again with the same five fields "
                       "and a new authoredOn. A real change is stored as the next version; the "
                       "same prescription again is answered ALREADY_RECEIVED. An old message "
                       "replayed after a newer one never rolls the visit back.", BODY))

    # ---------------------------------------------------------------- 12 findings
    S.append(PageBreak())
    S.append(Paragraph("12 &middot; The doctor's findings, in API 3", H2))
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
    S.append(Paragraph("12.1 &middot; Two findings, as they appear in the Bundle", H3))
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
    S.append(Paragraph("The complete set for one consultation is in the example in 11.2.", BODY))

    # ---------------------------------------------------------------- 13 replies
    S.append(Paragraph("13 &middot; Replies, and what to do about each", H2))
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
    S.append(Paragraph("13.1 &middot; A reply with a warning", H3))
    S.append(Paragraph("The same API 2 as in 9.2, with the temperature sent as 98.6 and labelled "
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

    # ---------------------------------------------------------------- 14 testing
    S.append(PageBreak())
    S.append(Paragraph("14 &middot; Testing, and the go-live checklist", H2))
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
                              "to AIMS LAB (section 6)"],
                  ["[  ]", "client_id and kid received for each hospital"],
                  ["[  ]", "Token request returns 200 with the right hospital_ids"],
                  ["[  ]", "API 2 (first visit and returning) and API 3 return 200 ACCEPTED"],
                  ["[  ]", "The fingerprint example in 10.5 reproduced exactly"],
                  ["[  ]", "<b>The page:</b> start on every patient opened; prescription_built "
                           "with the latest session_id; stop at log-out and end of day, never "
                           "on reload (section 4)"],
                  ["[  ]", "<b>The page never waits for the recorder</b>: tested with "
                           "AIMScribe.exe closed (section 5, case 8)"],
                  ["[  ]", "<b>The same five fields</b> in start, API 2 and API 3b, "
                           "start_time unchanged (1.1); checked on our dashboard as "
                           "<i>confirmed</i>"],
                  ["[  ]", "Outbox in place; retries tested by unplugging the network"],
                  ["[  ]", "channel_b_test.py output sent to AIMS LAB"],
                  ["[  ]", (f"Ready to change one value to {mono('https://' + permanent)} "
                            "when we announce it") if host != permanent else
                   "Token audience built from the configured address (section 7)"]],
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
