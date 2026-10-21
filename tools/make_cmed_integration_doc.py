#!/usr/bin/env python3
"""
Build CMED_1_SOFTWARE_INTEGRATION.pdf - for CMED's software, DBMS and data team.

    python tools/make_cmed_integration_doc.py [--out NAME.pdf]

Everything about the APIs: the keys, the exact endpoints, every field, every
reply code, where the data lands, previous prescriptions, and how to test.
Nothing about hosting - that is the server team's document.

Sources, all read rather than remembered:
  recorder/api/protocol.py, websocket_server.py      the local channel
  backend/src/clinical.py, clinical_model.py         the rules every message meets
  backend/src/cmed_auth.py                           per-hospital clients and tokens
  backend/src/fhir.py                                the FHIR R4 endpoints and mapping
  tools/cmed_fhir_client.py                          the examples in Part H are made by it
  backend/src/confirmation.py                        the match and its look-back
  backend/src/api_v2.py                              /doctors, admin, object keys
  backend/scripts/clinical/001_aims_clinical.sql     the tables
  backend/src/main_fastapi.py                        /health
No secrets: every key below is a placeholder.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reportlab.lib.units import mm
from reportlab.platypus import Spacer, Paragraph, HRFlowable, PageBreak, Preformatted, KeepTogether

from reportlab.platypus import CondPageBreak

from cmed_doc_style import (H1, SUB, H2, H3, BODY, NOTE, BAD, GOOD, CODE,
                            tbl, part, build)
from cmed_diagrams import architecture, sequence, gate, five_fields

import itertools
import json as _json

import cmed_fhir_client as _fc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "src"))
_n = itertools.count(1)
_fc.uuid = type("U", (), {"uuid4": staticmethod(lambda: "%08d-0000-4000-8000-000000000000"
                                                % next(_n))})
_fc.time = type("T", (), {"strftime": staticmethod(lambda *a: "2026-10-09T04:15:01Z"),
                          "gmtime": staticmethod(lambda: None)})

_FIVE = {"patient_id": "P0012345", "doctor_id": "DR0042", "hospital_id": "AALO_DHOLPUR",
         "start_time": "2026-10-09T10:14:32+06:00", "date": "2026-10-09"}
_API2 = {**_FIVE,
         "demographics": {"name": "Rahima Khatun", "sex": "female", "age_years": 34,
                          "date_of_birth": "1992-03-11", "phone": "01700000000",
                          "address": "Dholpur, Dhaka"},
         "paramedic": {"recorded_at": "2026-10-09T10:02:10+06:00", "weight_kg": 58.4,
                       "blood_pressure": "120/80", "pulse_bpm": 78, "temperature_c": 36.8},
         "previous_visit": {"doctor_id": "DR0007", "hospital_id": "AALO_DHOLPUR",
                            "start_time": "2026-06-02T09:40:05+06:00", "date": "2026-06-02",
                            "prescription": {"items": [{"drug": "Napa 500 mg",
                                                        "dose": "1 tablet",
                                                        "frequency": "1+1+1",
                                                        "duration": "5 days"}],
                                             "diagnoses": ["Fever"], "investigations": []}}}
_API3 = {**_FIVE, "issued_at": "2026-10-09T10:26:55+06:00",
         "items": [{"drug": "Napa 500 mg", "dose": "1 tablet", "frequency": "1+1+1",
                    "duration": "5 days", "instructions": "after food"}],
         "diagnoses": ["Acute gastritis"], "investigations": ["CBC"],
         "advice": "avoid spicy food", "follow_up": "2026-11-09"}
_MSG2 = _fc.to_fhir("patient_information", _API2)
_MSG3 = _fc.to_fhir("prescription", _API3)

import fhir as _server_fhir  # noqa: E402  the server's own reader
for _kind, _body, _msg in (("patient_information", _API2, _MSG2),
                           ("prescription", _API3, _MSG3)):
    assert _server_fhir.to_body(_server_fhir.Bundle(_msg)) == (_kind, _body), _kind

import security as _security  # noqa: E402
_security.REQUEST_ID.set("3f9c2a7e5b1d4c08a6e2f0b9d7c41e55")
_server_fhir.uuid = type("U", (), {"uuid4": staticmethod(
    lambda: "%08d-0000-4000-8000-000000000000" % next(_n)), "UUID": __import__("uuid").UUID})
_ENC_ID = "0b6c4f2e-9d1a-4c3b-8e7f-5a2d1c0b9e8f"
_SEARCH = {"resourceType": "Bundle", "type": "searchset", "total": 1,
           "link": [{"relation": "self", "url": "https://<aims-host>/fhir/r4/Encounter?"
                                                "patient.identifier=P0012345&_count=50"}],
           "entry": [{"fullUrl": f"https://<aims-host>/fhir/r4/Encounter/{_ENC_ID}",
                      "search": {"mode": "match"},
                      "resource": _server_fhir._encounter_resource(None, {
                          "source": "live", "prescription_version": 2,
                          "prescription_fingerprint": "f1e7109404fd5d4010c358f0b3afdec7"
                                                      "9699955299a089eaf2c830fb60ec4806",
                          "received_at": "2026-06-02T03:58:11.204+00:00",
                          "patient_id": "P0012345", "doctor_id": "DR0007",
                          "hospital_id": "AALO_DHOLPUR",
                          "start_time": "2026-06-02T09:40:05+06:00"}, _ENC_ID)}]}
_REPLY = _server_fhir._response_message(_MSG2, 202, "ACCEPTED", None, {
    "record_id": "5121", "received_at": "2026-10-09T04:14:33.120+00:00",
    "previous_visit": {"status": "STORED", "version": 1}})
_REPLY["timestamp"] = "2026-10-09T04:14:33.130Z"
_REFUSED = _server_fhir.outcome(403, "HOSPITAL_NOT_ALLOWED",
                                "This credential may not send data for that hospital.")


def _compact(obj, indent=0, width=96, lead=0):
    """JSON with short objects on one line, so a whole Bundle fits on a page or two."""
    flat = _json.dumps(obj, ensure_ascii=False)
    if indent + lead + len(flat) <= width or not isinstance(obj, (dict, list)) or not obj:
        return flat
    pad, inner = " " * indent, " " * (indent + 2)
    if isinstance(obj, list):
        body = [inner + _compact(v, indent + 2, width) for v in obj]
    else:
        body = [f'{inner}"{k}": ' + _compact(v, indent + 2, width, len(k) + 4)
                for k, v in obj.items()]
    opening, closing = ("[", "]") if isinstance(obj, list) else ("{", "}")
    return opening + "\n" + ",\n".join(body) + "\n" + pad + closing



S = []

# ============================================================ TITLE
S.append(Paragraph("CMED &rarr; AIMS LAB &mdash; Software Integration", H1))
S.append(Paragraph("<b>Document 1 of 2 &mdash; for CMED's software, database and data team</b> "
                   "&middot; The APIs, the keys, the fields and the data model &middot; "
                   "AIMScribe v3 &middot; AIMS LAB, United International University &middot; "
                   "20 October 2026 (per-hospital tokens and FHIR R4)", SUB))
S.append(HRFlowable(width="100%", thickness=1, color=__import__("cmed_doc_style").RULE,
                    spaceAfter=8))
S.append(tbl([["", "The two documents"],
              ["<b>1. This one</b>", "<b>Software integration</b> &mdash; the four APIs, the "
                                     "per-hospital tokens, FHIR R4, "
                                     "the exact endpoints, every field, every reply code, where "
                                     "the data lands, and how to test it. <b>For the team writing "
                                     "the code</b>"],
              ["2. The other", "<b>Server deployment</b> &mdash; the machine, the region, DNS, "
                               "TLS, ports, the services, health checks and monitoring. "
                               "<b>For the team helping us deploy</b>"]],
             [26 * mm, 130 * mm], highlight=[1]))
S.append(Spacer(1, 5))
S.append(tbl([["What this integration is, in one paragraph"],
              ["AIMScribe records the consultation. It runs as a small program on <b>the same "
               "laptop</b> as the doctor's browser. CMED's page tells that program when a patient "
               "is opened and when the prescription is built, so it knows where one consultation "
               "ends and the next begins. Separately, <b>CMED's server</b> sends us the patient's "
               "information and the prescription, so the recording has a clinical record attached. "
               "<b>Audio never goes to CMED, and nothing clinical is ever served back to CMED.</b>"]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 5))
S.append(Spacer(1, 8))
S.append(architecture())
S.append(Spacer(1, 8))
S.append(Paragraph("Every rule here is taken from the program that validates these messages on "
                   "arrival. <b>If this document and the software disagree, the software is right "
                   "and this is a bug</b> &mdash; please tell us.", BODY))

S.append(H3 and Paragraph("Contents", H3))
S.append(tbl([["Part", "Covers", "Sections"],
              ["<b>A</b>", "The keys and the exact endpoints &mdash; <b>start here</b>", "1&ndash;3"],
              ["<b>B</b>", "The five fields, and how the four messages relate", "4&ndash;5"],
              ["<b>C</b>", "The two browser signals &mdash; API 1 and API 3 Channel A", "6&ndash;11"],
              ["<b>D</b>", "The two server messages &mdash; API 2 and API 3 Channel B, "
                           "<b>and the outbox for outages</b>", "12&ndash;17"],
              ["<b>E</b>", "<b>Previous prescriptions</b> &mdash; sending them, and asking "
                           "what we already hold", "18&ndash;19"],
              ["<b>F</b>", "The data model &mdash; where every field lands", "20&ndash;21"],
              ["<b>G</b>", "Testing, handover, and one page to remember", "22&ndash;25"],
              ["<b>H</b>", "<b>Per-hospital tokens and FHIR R4, field by field</b> &mdash; "
                           "keys, the token, both messages as FHIR, every reply", "26&ndash;32"]],
             [14 * mm, 116 * mm, 26 * mm], highlight=[5, 8]))

S.append(PageBreak())

# ============================================================ PART A
S.append(part("PART A", "The credentials and the exact endpoints",
              "Copy these. Everything after this explains them"))

S.append(Paragraph("1 &middot; The gateway", H2))
S.append(Preformatted(
    'BASE_URL = https://<aims-host>          <-- AIMS LAB sends this with each registration\n'
    '\n'
    'Transport    HTTPS only, TLS 1.2 or 1.3, port 443. HTTP redirects to HTTPS\n'
    'Format       HL7 FHIR R4, application/fhir+json, UTF-8\n'
    'Body limit   1 MB per message\n'
    'Identity     one client PER HOSPITAL, with its own key pair (private half stays at CMED)\n'
    'Token        POST /api/v2/oauth/token  with a signed 5-minute assertion\n'
    '             -> a 5-minute access token for that hospital only\n'
    'Auth         Authorization: Bearer <access token>   on every FHIR call', CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Hold the host in configuration, not compiled in.</b> You will get a "
                   "staging host first and a production host later, and they differ.", NOTE))

S.append(Paragraph("2 &middot; The authentication, in full", H2))
S.append(Paragraph("CMED runs the software for many hospitals, so CMED does not have one "
                   "identity here &mdash; <b>each hospital does</b>. This is the HL7 <b>SMART "
                   "Backend Services</b> method (OAuth 2.0 client credentials with a signed "
                   "JWT assertion, RFC 7523), the standard way one health system's server "
                   "proves itself to another's.", BODY))
S.append(Spacer(1, 3))
S.append(tbl([["", "Channel B &mdash; your server to ours", "Channel A &mdash; your page to the recorder"],
              ["<b>Credential</b>", "<b>a key pair per hospital</b>; each call carries a "
                                    "5-minute <font face='Courier'>Bearer</font> token",
               "<b>none &mdash; and none is possible</b>"],
              ["Why", "Server to server, so a secret is safe there &mdash; and only the "
                      "<b>public</b> half ever leaves CMED",
               "A secret in a web page is readable by anyone who opens the page"],
              ["<b>What authorises you</b>", "a token bought with an assertion signed by that "
                                             "hospital's private key; it speaks for <b>that "
                                             "hospital only</b>",
               "<b>your page's exact web address</b>, on our allow-list, plus loopback-only, plus "
               "same-machine"],
              ["Who makes it", "<b>CMED makes the key pair</b> and sends AIMS LAB only the public "
                               "key (a JWK). We never see, store or send a private key",
               "you send us the address; we register it"],
              ["Scope", "<b>one client per hospital</b>, bound to that hospital's CMED id; "
                        "optionally only from CMED's server addresses",
               "one entry per address"],
              ["Lifetime", "assertion &le; 5 minutes, single use; token 5 minutes; client up to "
                           "1 year. AIMS LAB renews it in one call and is alerted 30 days before "
                           "it expires", "n/a"],
              ["Rotation", "register a second key (a new <font face='Courier'>kid</font>), switch, "
                           "then we revoke the old one &mdash; no window where both fail", "n/a"],
              ["<b>Where it must live</b>", "<b>the private key in your server's secret store; "
                                            "the token in memory only</b>", "n/a"]],
             [28 * mm, 66 * mm, 62 * mm], req=[1], highlight=[3]))
S.append(Spacer(1, 4))
S.append(tbl([["What a thief would get", "How much damage"],
              ["A copy of <b>our</b> database", "Public keys and token <b>hashes</b>. Nothing that "
                                               "can sign or call"],
              ["One access token", "<b>Five minutes, one hospital</b>, and only from CMED's "
                                   "addresses if an allow-list is set"],
              ["One hospital's private key", "That hospital only, until AIMS LAB revokes the "
                                             "client &mdash; one call, effective at once, live "
                                             "tokens included"],
              ["A captured assertion", "Useless: each one is accepted once (its "
                                       "<font face='Courier'>jti</font> is remembered)"]],
             [50 * mm, 106 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>A private key must never reach a browser</b> &mdash; not in page source, "
                   "not in a bundled script, not in a mobile app. A token refused with "
                   "<b>401 INVALID_TOKEN</b> is not a reason to retry the same token: ask for a "
                   "new one once; if that is refused too (<b>401 invalid_client</b>), stop, alert "
                   "your team and contact us. Part H has every detail.", BAD))
S.append(Spacer(1, 4))
S.append(tbl([["<b>The interim static key &mdash; for the switch-over only</b>"],
              ["The older " + "<font face='Courier'>X-CMED-Key</font>" + " header with plain JSON to "
               "<font face='Courier'>/api/v2/clinical/...</font> still works, <b>but only with a "
               "key issued for named hospitals</b>, which expires after at most 90 days. A key "
               "naming no hospital gets <b>403 KEY_NOT_BOUND</b>; a message for a hospital the "
               "key does not name gets <b>403 HOSPITAL_NOT_ALLOWED</b>. Every reply to it carries "
               "<font face='Courier'>Deprecation</font>, <font face='Courier'>Sunset</font> "
               "(31 January 2027 unless agreed otherwise) and a <font face='Courier'>Link</font> "
               "to the token endpoint. AIMS LAB switches static "
               "keys off entirely (server setting <font face='Courier'>AIMS_CMED_LEGACY_KEYS=off"
               "</font>) once CMED is on tokens. <b>Please move to tokens and FHIR before the "
               "first clinic goes live.</b>"]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(tbl([["<b>The address allow-list is a blocking prerequisite, and it is exact</b>"],
              ["Send AIMS LAB <b>every web address your page will be served from</b>, production "
               "and staging and preview. An address not on the list is refused with "
               "<font face='Courier'>ORIGIN_NOT_ALLOWED</font> and the connection never opens, so "
               "<b>nothing works at all</b>.<br/><br/>"
               "It must match character for character: "
               "<font face='Courier'>https://ehr.aaloclinic.com</font> and "
               "<font face='Courier'>https://www.ehr.aaloclinic.com</font> are different, so are "
               "<font face='Courier'>http</font> and <font face='Courier'>https</font>, and so is "
               "any non-default port."]],
             [156 * mm], bold_first=False))

S.append(PageBreak())
S.append(Paragraph("3 &middot; Every address CMED calls", H2))
S.append(tbl([["#", "Method", "Full address", "Auth", "Body", "OK"],
              ["<b>1</b>", "<b>GET</b>", "<font face='Courier'>https://&lt;aims-host&gt;/health"
                                        "</font>", "<b>none</b>", "none", "<b>200</b>"],
              ["<b>2</b>", "<b>GET</b>", "<font face='Courier'>https://&lt;aims-host&gt;"
                                         "/fhir/r4/metadata</font>",
               "none", "none", "200"],
              ["<b>3</b>", "<b>POST</b>", "<font face='Courier'>https://&lt;aims-host&gt;"
                                          "/api/v2/oauth/token</font>",
               "<b>signed assertion</b>", "form", "<b>200</b>"],
              ["<b>4</b>", "<b>POST</b>", "<font face='Courier'>https://&lt;aims-host&gt;"
                                          "/fhir/r4/$process-message</font> &mdash; API 2 and "
                                          "API 3b",
               "<b>Bearer</b>", "FHIR", "<b>200</b>"],
              ["<b>5</b>", "<b>GET</b>", "<font face='Courier'>https://&lt;aims-host&gt;"
                                         "/fhir/r4/Encounter?patient.identifier=&lt;id&gt;"
                                         "</font> &mdash; the visits held",
               "<b>Bearer</b>", "none", "200"],
              ["6", "GET", "<font face='Courier'>https://&lt;aims-host&gt;"
                           "/fhir/r4/.well-known/smart-configuration</font>",
               "none", "none", "200"],
              ["7", "GET", "<font face='Courier'>https://&lt;aims-host&gt;"
                           "/fhir/r4/Encounter/&lt;id&gt;</font> &mdash; one visit, by the id the "
                           "search gave", "Bearer", "none", "200"],
              ["8", "GET", "<font face='Courier'>https://&lt;aims-host&gt;"
                           "/fhir/r4/MessageDefinition/aimscribe-patient-information</font> "
                           "(and <font face='Courier'>aimscribe-prescription</font>)",
               "none", "none", "200"]],
             [8 * mm, 14 * mm, 82 * mm, 20 * mm, 14 * mm, 14 * mm], highlight=[1], req=[3, 4]))
S.append(Spacer(1, 3))
S.append(tbl([["Protocol", "Full address", "Auth", "Who"],
              ["<b>WebSocket</b>", "<font face='Courier'>ws://127.0.0.1:5050/ws</font>",
               "address allow-list", "your page, in the doctor's browser"]],
             [24 * mm, 56 * mm, 36 * mm, 40 * mm]))
S.append(Spacer(1, 3))
S.append(tbl([["Message on that socket", "Which API", "Sent when"],
              ["<font face='Courier'>{\"command\": \"start\", \"trigger\": {&hellip;}}</font>",
               "<b>API 1</b>", "the doctor opens a patient"],
              ["<font face='Courier'>{\"command\": \"prescription_built\", &hellip;}</font>",
               "<b>API 3, Channel A</b>", "the doctor presses Build Prescription"],
              ["<font face='Courier'>{\"command\": \"doctors\"}</font>", "&mdash;",
               "<b>on page load</b> &mdash; returns this PC's clinic code"],
              ["<font face='Courier'>{\"command\": \"status\"}</font>", "&mdash;",
               "optional; the recorder also pushes status unprompted"]],
             [62 * mm, 32 * mm, 62 * mm], highlight=[3]))
S.append(Spacer(1, 4))
S.append(tbl([["The ones CMED's code calls, one line each, to copy"],
              ["<font face='Courier'>GET &nbsp;https://&lt;aims-host&gt;/health</font><br/>"
               "<font face='Courier'>POST https://&lt;aims-host&gt;/api/v2/oauth/token</font><br/>"
               "<font face='Courier'>POST https://&lt;aims-host&gt;/fhir/r4/$process-message"
               "</font><br/>"
               "<font face='Courier'>GET &nbsp;https://&lt;aims-host&gt;/fhir/r4/Encounter?"
               "patient.identifier=&lt;patient_id&gt;</font><br/>"
               "<font face='Courier'>WS &nbsp;&nbsp;ws://127.0.0.1:5050/ws &nbsp;&rarr; \"start\""
               "</font><br/>"
               "<font face='Courier'>WS &nbsp;&nbsp;ws://127.0.0.1:5050/ws &nbsp;&rarr; "
               "\"prescription_built\"</font>"]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>One GET, and it returns no clinical content.</b> The visits search "
                   "(section 19) gives identifiers and a fingerprint of each prescription, so CMED "
                   "can ask what we hold before sending history &mdash; never a name, a medicine "
                   "or a diagnosis &mdash; and <b>only for the hospital the token speaks for</b>. "
                   "<b>There is no DELETE anywhere.</b> <b>Everything else is closed to CMED's "
                   "tokens</b>; the recorder endpoints need each recorder's own device "
                   "credential. During the switch-over the older JSON endpoints "
                   "(<font face='Courier'>/api/v2/clinical/patient-information</font>, "
                   "<font face='Courier'>/prescription</font>, "
                   "<font face='Courier'>/patient/{id}/visits</font>) accept the same Bearer "
                   "token, or a hospital-bound static key; they apply exactly the same rules.",
                   GOOD))

S.append(PageBreak())

# ============================================================ PART B
S.append(part("PART B", "The five fields",
              "The one rule that decides whether the whole integration works"))

S.append(Paragraph("4 &middot; Three APIs, four messages", H2))
S.append(tbl([["", "What", "From", "When", "Part"],
              ["<b>API 1</b>", "Start recording", "the browser", "the doctor opens a patient", "C"],
              ["<b>API 2</b>", "<b>Patient information</b>", "<b>your server</b>",
               "the same moment", "D"],
              ["<b>API 3</b><br/>Channel A", "Prescription built", "the browser",
               "the doctor presses Build Prescription", "C"],
              ["<b>API 3</b><br/>Channel B", "<b>The prescription</b>", "<b>your server</b>",
               "the same moment", "D"]],
             [20 * mm, 40 * mm, 28 * mm, 54 * mm, 12 * mm], highlight=[2, 4]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>They go in pairs.</b> Two moments, four messages. <b>API 3 is one event "
                   "told to two places</b> &mdash; the recorder, so it knows the consultation has "
                   "reached its end, and our server, so we receive the prescription. The earlier "
                   "integration guide calls these 6a and 6b; this document says Channel A and "
                   "Channel B. Three APIs, four messages.", GOOD))

S.append(PageBreak())
S.append(Paragraph("4.1 &middot; One whole consultation, start to finish", H2))
S.append(Paragraph("Read this once and the rest of the document is detail. Time runs down the "
                   "page; each column is one party.", BODY))
S.append(Spacer(1, 6))
S.append(sequence())
S.append(Spacer(1, 6))
S.append(Paragraph("<b>Three things to take from it.</b> The four messages happen at only "
                   "<b>two moments</b>, in pairs. <b>The match</b> &mdash; the step CMED cannot "
                   "see &mdash; is what attaches CMED's clinical record to the recording; the "
                   "recording itself is kept whether or not it matches. And the recorder "
                   "<b>keeps recording through the prescription</b>, closing only when the next "
                   "patient is opened.", GOOD))

S.append(PageBreak())
S.append(Paragraph("5 &middot; The five fields", H2))
S.append(Paragraph("All four messages about one consultation carry these same five fields, with "
                   "<b>exactly the same values, character for character</b>.", BODY))
S.append(Spacer(1, 2))
S.append(Preformatted(
    '{\n'
    '  "patient_id":  "P0012345",\n'
    '  "doctor_id":   "DR0042",\n'
    '  "hospital_id": "AALO_DHOLPUR",\n'
    '  "start_time":  "2026-10-09T10:14:32+06:00",\n'
    '  "date":        "2026-10-09"\n'
    '}', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Field", "Type", "Rule"],
              ["patient_id", "text", "Letters, digits, hyphen, underscore. 1&ndash;64 characters. "
                                     "<b>No spaces</b>"],
              ["doctor_id", "text", "same rule"],
              ["hospital_id", "text", "same rule. <b>Read it from the recorder &mdash; section 10"
                                      "</b>"],
              ["start_time", "text", "Date and time <b>with the time-zone offset</b>, e.g. "
                                     "<font face='Courier'>+06:00</font>"],
              ["date", "text", "<font face='Courier'>YYYY-MM-DD</font>, the clinic's local date"]],
             [26 * mm, 16 * mm, 114 * mm]))
S.append(Spacer(1, 4))
S.append(tbl([["Three mistakes that break the match"],
              ["<b>1. Sending the prescription's own time as start_time.</b> Both halves of API 3 "
               "repeat the <b>original</b> start_time from when the patient was opened. This is "
               "the most common integration mistake, and it is silent: both messages succeed and "
               "the prescription simply never finds its recording."],
              ["<b>2. Leaving off the offset.</b> "
               "<font face='Courier'>2026-10-09T10:14:32</font> is rejected."],
              ["<b>3. A space or other character in an identifier.</b> These become folder names "
               "on disk."]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(PageBreak())
S.append(H3 and Paragraph("5.1 &nbsp; One object, four messages", H3))
S.append(five_fields())
S.append(Spacer(1, 6))
S.append(Paragraph("<b>Build the five fields once, into one object, when the doctor opens the "
                   "patient. Keep it for the whole consultation and pass the same object to all "
                   "four messages.</b> Never rebuild them and never recompute the time. Do that "
                   "and the match cannot fail; section 23 shows it in code.", GOOD))

S.append(PageBreak())

# ============================================================ PART C
S.append(part("PART C", "The two browser signals",
              "API 1 and API 3 Channel A - and the gate"))

S.append(Paragraph("6 &middot; How the page connects", H2))
S.append(tbl([["", "Detail"],
              ["Address", "<font face='Courier'>ws://127.0.0.1:5050/ws</font> &mdash; the same on "
                          "every clinic PC"],
              ["Why <font face='Courier'>ws://</font> and not <font face='Courier'>wss://</font>",
               "It never leaves the machine. Loopback needs no certificate and the browser "
               "permits it"],
              ["Protocol", "One WebSocket held open for the clinic session. JSON, UTF-8"],
              ["Largest message", "64 KB"],
              ["When to connect", "when the page loads; reconnect quietly if it drops"],
              ["<b>Firewall changes</b>", "<b>none</b>, on CMED's side or the clinic's"]],
             [44 * mm, 112 * mm], highlight=[6]))

S.append(Paragraph("7 &middot; Every message and reply", H2))
S.append(Preformatted(
    'CMED sends:\n'
    '{ "command": "start", "request_id": "abc-123", ... }\n'
    '\n'
    'Exactly one reply per message:\n'
    '{\n'
    '  "event":      "ack",                 <-- "ack" below 400, "error" from 400\n'
    '  "command":    "start",\n'
    '  "status":     200,\n'
    '  "code":       "RECORDING_STARTED",   <-- ACT ON THIS\n'
    '  "message":    "Recording started.",  <-- for a human; may be reworded\n'
    '  "request_id": "abc-123",\n'
    '  "data":       { ... }\n'
    '}', CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Act on <font face='Courier'>code</font>, never on "
                   "<font face='Courier'>message</font>.</b> The code is a promise; the wording is "
                   "not.", GOOD))

S.append(Paragraph("8 &middot; API 1 &mdash; a patient has been opened", H2))
S.append(Preformatted(
    '{\n'
    '  "command": "start",\n'
    '  "request_id": "open-P0012345-1",\n'
    '  "trigger": {\n'
    '    "patient_id": "P0012345", "doctor_id": "DR0042",\n'
    '    "hospital_id": "AALO_DHOLPUR",\n'
    '    "start_time": "2026-10-09T10:14:32+06:00", "date": "2026-10-09"\n'
    '  }\n'
    '}', CODE))
S.append(Paragraph("The microphone starts <b>immediately</b>, before any permission check "
                   "finishes, so nothing of the consultation is missed. What comes back:", BODY))
S.append(Preformatted(
    '"data": {\n'
    '  "session_id": "01M3MK4J4XEDG5NDKE4H5B5XA2",   <-- KEEP THIS\n'
    '  "started_at": "2026-10-09T10:14:32.865Z",\n'
    '  "armed": false,\n'
    '  "supersedes": "01M3MEYY...",       <-- the session this one closed, or null\n'
    '  "authorisation": "granted",        <-- granted | pending | held\n'
    '  "confirmation": "confirming"\n'
    '}', CODE))

S.append(PageBreak())
S.append(Paragraph("9 &middot; Every reply to API 1", H2))
S.append(tbl([["Code", "Meaning", "What CMED should do"],
              ["<b>RECORDING_STARTED</b> (200)", "Recording, permission confirmed. With "
                                                 "<font face='Courier'>data.continued: true</font>: "
                                                 "a recording was already running and carries on",
               "<b>Store the session_id.</b> Show it is recording. Send API 2 as usual"],
              ["<b>RECORDING_PROVISIONAL</b> (202)", "<b>Recording.</b> Permission still being "
                                                     "checked",
               "<b>Treat as success.</b> Normal, not a warning"],
              ["SESSION_ALREADY_ACTIVE (409)", "This patient is already being recorded, "
                                               "and the prescription was built",
               "Nothing; you probably sent it twice, or reopened the profile"],
              ["MISSING_FIELD (400)", "A field is missing or malformed", "Fix and resend"],
              ["INVALID_IDENTIFIER (400)", "An id has disallowed characters", "Fix and resend"],
              ["DEVICE_NOT_ENROLLED (423)", "This PC is not registered with AIMS LAB",
               "<b>Stop.</b> Contact AIMS LAB"],
              ["ORIGIN_NOT_ALLOWED (403)", "Your page's address is not on the allow-list",
               "<b>Send us the address.</b> Section 2"],
              ["AGENT_NOT_READY (503)", "The recorder is starting up, or the microphone is "
                                        "busy",
               "<b>Nothing is required</b> &mdash; the recorder keeps trying by itself "
               "(section 11A). Resending is harmless"],
              ["<i>GATE_NOT_ARMED, PATIENT_NOTED, CLINIC_MISMATCH, AUTHORISATION_FAILED</i>",
               "<i>Sent only by older recorders</i>", "Treat as section 11A says; this version "
                                                     "accepts the click and keeps recording"]],
             [46 * mm, 50 * mm, 60 * mm], highlight=[1, 2, 3]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>RECORDING_PROVISIONAL is the one people get wrong.</b> The microphone is "
                   "live and audio is being written; the server has simply not finished confirming "
                   "permission, which normally takes a fraction of a second. Show the same "
                   "\"recording\" state as for RECORDING_STARTED. Do not warn and do not retry.",
                   NOTE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>A provisional start is never stopped by the server afterwards.</b> If our "
                   "server says no &mdash; a clinic not yet mapped, a configuration being fixed "
                   "&mdash; the recording carries on and is <b>kept on the laptop</b>; "
                   "<font face='Courier'>status</font> shows <font face='Courier'>authorisation: "
                   "\"held\"</font>, AIMS LAB is alerted, and the recorder asks again every five "
                   "minutes until the fix releases it. <b>Nothing is deleted and the page has "
                   "nothing to do.</b> If the server cannot be reached at all, the reply comes at "
                   "once rather than after a wait.", GOOD))

S.append(Paragraph("10 &middot; Never hardcode the clinic code &mdash; ask for it", H2))
S.append(Paragraph("Every AIMScribe laptop is enrolled to exactly one clinic. A recording "
                   "started with another clinic's code is <b>not uploaded</b>: it waits on the "
                   "laptop, and AIMS LAB is alerted, until someone fixes the code. <b>So do not "
                   "store clinic codes in your page &mdash; read them off the laptop on page "
                   "load.</b>", BODY))
S.append(Spacer(1, 2))
S.append(Preformatted(
    'send:  { "command": "doctors", "request_id": "who-is-this-pc" }\n'
    '\n'
    'reply: "data": {\n'
    '         "hospital_id": "AALO_DHOLPUR",     <-- use THIS as hospital_id\n'
    '         "doctors": [\n'
    '           { "doctor_id": "DR0042", "full_name": "Dr Rahman" },\n'
    '           { "doctor_id": "DR0051", "full_name": "Dr Akter" }\n'
    '         ]\n'
    '       }', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["What comes back", "Meaning", "What the page should do"],
              ["a code, and doctors listed", "Normal",
               "<b>Proceed.</b> Use the code; fill the doctor selector"],
              ["a code, <font face='Courier'>doctors: []</font>",
               "<b>Also normal</b> &mdash; a new clinic, or our server briefly unreachable",
               "<b>Proceed.</b> Let the doctor be typed. <b>An empty list must never block a "
               "clinic</b>"],
              ["<font face='Courier'>hospital_id: null</font>", "<b>This PC is not enrolled</b>",
               "<b>Do not send API 1.</b> Say so and contact us"]],
             [40 * mm, 54 * mm, 62 * mm], highlight=[1], req=[3]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>This makes a clinic mismatch impossible rather than something to handle</b>, "
                   "and lets us rename a clinic without CMED changing anything. It already caused "
                   "one failure in testing: a page was sending an old code after a clinic was "
                   "renamed, and every recording was refused until the page was rebuilt. "
                   "<b>Re-send <font face='Courier'>doctors</font> whenever the socket reopens.</b>",
                   GOOD))

S.append(PageBreak())
S.append(Paragraph("11 &middot; API 3 Channel A, the gate, and status", H2))
S.append(Preformatted(
    '{\n'
    '  "command": "prescription_built",\n'
    '  "request_id": "built-P0012345-1",\n'
    '  "patient_id": "P0012345",\n'
    '  "session_id": "01M3MK4J4XEDG5NDKE4H5B5XA2"    <-- from API 1\n'
    '}', CODE))
S.append(Spacer(1, 2))
S.append(tbl([["Field", "Required", "Rule"],
              ["command", "<b>yes</b>", "<font face='Courier'>prescription_built</font>"],
              ["<b>patient_id</b>", "<b>yes</b>", "<b>the patient the prescription was "
                                                 "written for</b>"],
              ["<b>session_id</b>", "<b>yes</b>", "<b>the one the recorder gave you</b> &mdash; "
                                                 "in API 1's reply, or status"],
              ["occurred_at", "no", "date and time with offset"]],
             [30 * mm, 20 * mm, 106 * mm], req=[1, 2, 3]))
S.append(Spacer(1, 3))
S.append(tbl([["Code", "Meaning", "Do"],
              ["<b>GATE_ARMED</b> (200)", "Accepted, for any patient opened during this "
                                          "recording", "nothing"],
              ["<b>GATE_ALREADY_ARMED</b> (200)", "Sent twice", "nothing; harmless"],
              ["PATIENT_MISMATCH (409)", "A patient not opened during this recording, or "
                                         "another session", "check the session_id"],
              ["NO_ACTIVE_SESSION (409)", "Nothing is recording", "the consultation already ended"]],
             [46 * mm, 50 * mm, 60 * mm], highlight=[1, 2]))
S.append(Spacer(1, 4))
S.append(tbl([["<b>Building the prescription does NOT stop the recording</b>"],
              ["The doctor builds it, prints it, hands it over &mdash; and then <b>keeps "
               "talking</b>. That counselling is some of the most valuable audio in the study."
               "<br/><br/>"
               "So <font face='Courier'>prescription_built</font> ends nothing. It <b>arms a "
               "gate</b>: this consultation has reached its end, so the next patient is allowed to "
               "close it. <b>The recording stops when the next patient is opened</b> &mdash; your "
               "next API 1, typically twenty to thirty seconds later &mdash; or by itself two "
               "minutes after the prescription if nobody else is opened (it waits up to ten "
               "seconds more for a pause, so no sentence is cut). That keeps the counselling "
               "and leaves the doctor's time between patients unrecorded."]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 6))
S.append(H3 and Paragraph("11.1 &nbsp; What the recorder is doing, state by state", H3))
S.append(gate())
S.append(Spacer(1, 8))
S.append(KeepTogether(tbl([["The doctor does", "CMED sends", "The recorder does"],
              ["Opens patient A", "API 1 + API 2", "starts recording A"],
              ["Consults", "&mdash;", "recording"],
              ["Presses Build Prescription", "<b>API 3, both channels</b>",
               "<b>keeps recording.</b> Arms the gate"],
              ["Prints, hands over, counsels", "&mdash;", "<b>still recording &mdash; the "
                                                          "valuable part</b>"],
              ["Opens patient B", "<b>API 1 + API 2</b>", "<b>closes A</b>, starts B"]],
             [48 * mm, 44 * mm, 64 * mm], highlight=[3, 5])))
S.append(Spacer(1, 4))
S.append(H3 and Paragraph("11.2 &nbsp; Another patient opened before the prescription "
                          "&mdash; always accepted", H3))
S.append(tbl([["The doctor does", "CMED sends", "The recorder answers, and does"],
              ["Opens patient W by mistake", "API 1 + API 2 (W)", "RECORDING_STARTED &mdash; records"],
              ["Notices, opens the right patient R", "API 1 + API 2 (R)",
               "<b>RECORDING_STARTED</b>, same session_id, "
               "<font face='Courier'>continued: true</font> &mdash; keeps recording, writes "
               "down R and the second"],
              ["Builds R's prescription", "<b>API 3, both channels, for R</b>",
               "<b>GATE_ARMED</b> &mdash; keeps recording"],
              ["Opens the next patient", "API 1 + API 2", "closes this recording, starts the next"]],
             [46 * mm, 40 * mm, 70 * mm], highlight=[2]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Every click is accepted, and nothing new is asked of CMED.</b> A patient "
                   "opened before the prescription is built may be a glance at another file, a "
                   "wrong profile corrected, or the next patient after a follow-up that needed no "
                   "prescription &mdash; that cannot be known at the moment of the click. So the "
                   "recorder gives the success reply your code already handles, keeps recording, "
                   "and writes down who was opened and when. <b>Carry on exactly as for any "
                   "patient</b>: send their API 2, and API 3 for whoever the prescription is "
                   "for, with that patient's own five fields and the session_id the recorder "
                   "gave you. When the recording ends, the AIMS LAB server reads what followed "
                   "each click &mdash; a prescription, a return to the first patient, how long "
                   "each was open &mdash; and files each part of the audio under the right "
                   "patient. A wrong profile picked first loses nothing; a follow-up with no "
                   "prescription gets its own file; a glance is filed with the patient the doctor "
                   "went back to. Anything it cannot settle is kept and shown to AIMS LAB for "
                   "review.", GOOD))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>If the doctor finishes for the day or goes to lunch</b>, they press Stop on "
                   "the recorder's own on-screen control. <b>CMED sends nothing, and must not try "
                   "to.</b> The socket also accepts <font face='Courier'>stop</font>, "
                   "<font face='Courier'>pause</font> and <font face='Courier'>resume</font>, but "
                   "<b>these are not CMED's to use</b>: they exist for AIMS LAB's own test page, and "
                   "each records a reason that becomes part of the permanent record. <b>None of "
                   "them can erase a recording</b> (recorder 3.5.0 and later): a refusal sent "
                   "over the socket is recorded as an ordinary stop and the audio is kept. Only "
                   "the doctor's on-screen \"patient refused\" control erases, and the recorder "
                   "signs that refusal with the PC's own key. <b>Send only "
                   "<font face='Courier'>start</font> and "
                   "<font face='Courier'>prescription_built</font>.</b>", BAD))
S.append(Spacer(1, 4))
S.append(H3 and Paragraph("Status, pushed to you unprompted", H3))
S.append(Preformatted(
    '{\n'
    '  "event": "status",\n'
    '  "state": "recording",           <-- idle | recording | paused | closing\n'
    '  "is_recording": true, "is_paused": false,\n'
    '  "session_id": "01M3MK4J...", "patient_ref": "P0012345",\n'
    '  "armed": false,                 <-- has the prescription signal arrived?\n'
    '  "authorisation": "granted",     <-- granted | pending | held\n'
    '  "confirmation": "confirmed",\n'
    '  "duration_seconds": 182.4,\n'
    '  "level": 0.42,                  <-- 0.0 to 1.0, how loud the room is\n'
    '  "connected_clients": 1\n'
    '}', CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Show <font face='Courier'>level</font> as a small meter.</b> A microphone "
                   "that is unplugged, muted in Windows, or set to the wrong input records perfect "
                   "silence and <b>nothing on screen looks wrong</b>. Without a meter that is "
                   "discovered when somebody plays the file back, possibly weeks and hundreds of "
                   "consultations later. It is the cheapest safeguard in the whole integration.",
                   GOOD))

S.append(PageBreak())
S.append(Paragraph("11A &middot; When the recording does not start &mdash; recovering without "
                   "the doctor", H2))
S.append(Paragraph("The doctor opens a patient and the page sends API 1 &mdash; but the recorder "
                   "was not running yet, or the socket had dropped a second earlier, or the "
                   "microphone was briefly busy, or the recorder crashed mid-consultation. <b>The "
                   "doctor must never have to notice, and must never have to re-open the "
                   "patient.</b> Two safety nets make that true: the recorder's own, and the "
                   "page's.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["<b>Net 1 &mdash; the recorder recovers by itself</b>"],
              ["<b>Microphone busy at the start:</b> the page gets "
               "<font face='Courier'>AGENT_NOT_READY</font>, and the recorder <b>keeps the "
               "trigger and tries again every five seconds, for up to fifteen minutes</b>, until "
               "it records. It stops trying the moment CMED sends another patient or the doctor "
               "presses Stop. AIMS LAB is alerted either way.<br/><br/>"
               "<b>Recorder crashed or restarted mid-consultation:</b> it is back within a minute "
               "and, <b>if that consultation was live in the last ten minutes, records it again "
               "under the same five fields</b> by itself, with the gate as it was. A "
               "consultation from earlier, or from yesterday, is never resumed.<br/><br/>"
               "<b>Server says no:</b> the recording carries on and is kept on the laptop "
               "(section 9). Nothing for the page to do."]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(tbl([["<b>Net 2 &mdash; the page reconciles</b>"],
              ["The recorder can only recover a start it received. If the socket was down when "
               "the patient was opened, only the page knows. So the page does not fire and "
               "forget: it always knows what <b>should</b> be happening &mdash; which visit is "
               "open, and whether its prescription has been built &mdash; and the recorder says "
               "what <b>is</b> happening, in the <font face='Courier'>status</font> message it "
               "pushes unprompted and again the moment a socket reconnects.<br/><br/>"
               "<b>Whenever the two disagree, the page resends the missing message, with the same "
               "five fields.</b> Repeats are safe: a second <font face='Courier'>start</font> "
               "for the same patient is <font face='Courier'>SESSION_ALREADY_ACTIVE</font>, and a "
               "second <font face='Courier'>prescription_built</font> is "
               "<font face='Courier'>GATE_ALREADY_ARMED</font>. Verified against a running "
               "recorder &mdash; section 22, <font face='Courier'>channel_a_test.py</font>."]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 5))
S.append(tbl([["What went wrong", "Who notices", "What happens, with nobody asked",
               "Doctor sees"],
              ["<b>Recorder not running</b> when the patient was opened",
               "the page: the socket will not connect", "<b>the page keeps trying every 2 s; on "
                                                       "connect, sends start for the open "
                                                       "visit</b>",
               "a small \"connecting\" dot"],
              ["<b>Socket dropped</b> at the moment of the click",
               "the page: no reply within 5 s, or the socket closed",
               "<b>the page resends start on reconnect</b>", "nothing"],
              ["<b>Microphone busy</b>",
               "the recorder (<font face='Courier'>AGENT_NOT_READY</font> to the page)",
               "<b>the recorder retries every 5 s for 15 min</b>; the page may also retry, "
               "harmlessly", "nothing"],
              ["<b>Recorder crashed</b> mid-consultation", "the recorder, on restart",
               "<b>records the same consultation again</b> if it was live within 10 min",
               "a gap of about a minute"],
              ["<b>API 3a was lost</b>",
               "the page: status says <font face='Courier'>armed: false</font> but it built "
               "the prescription", "<b>the page resends prescription_built</b>", "nothing"],
              ["<b>Wrong patient opened first</b>", "nobody needs to",
               "accepted; the server files the audio under the right patient (11.2)", "nothing"],
              ["<b>Follow-up, no prescription</b>", "nobody needs to",
               "accepted; the next patient's click is written down and the server gives each "
               "patient their own file (11.2)", "nothing"],
              ["<b>Windows shuts down</b> mid-consultation", "the recorder",
               "closes the recording cleanly; after a power cut, at most ~3 s is lost",
               "nothing"],
              ["<b>Server says no</b>", "the recorder", "<b>keeps recording; holds the audio on "
                                                      "the PC</b>; asks again every 5 min",
               "nothing"],
              ["<b>Configuration the page must fix</b>",
               "<font face='Courier'>ORIGIN_NOT_<br/>ALLOWED</font>, "
               "<font face='Courier'>DEVICE_NOT_<br/>ENROLLED</font>",
               "<b>the page stops retrying</b> &mdash; a retry cannot fix configuration",
               "a small banner that does not block work"],
              ["<b>Recording, but the microphone has gone silent</b>",
               "<font face='Courier'>level</font> at exactly 0 for 30 s; the recorder also "
               "alerts AIMS LAB", "the page shows a small warning", "\"microphone?\""]],
             [36 * mm, 38 * mm, 50 * mm, 32 * mm], highlight=[3, 4, 6, 7], req=[8]))
S.append(Spacer(1, 5))
S.append(KeepTogether(tbl([["<b>Why a late start still counts: start_time is always the original</b>"],
              ["When the page resends API 1 two minutes after the doctor opened the patient, it "
               "sends the <b>same five fields, including the original start_time</b> &mdash; not "
               "a new one. The recorder begins capturing from the moment the resend arrives, but "
               "the consultation is still identified by the moment it really began, so <b>API 2 "
               "attaches to it whenever API 2 arrives</b> &mdash; seconds, hours or days apart, "
               "in either order.<br/><br/>"
               "<b>What is lost is only the audio between the click and the recovery</b> &mdash; "
               "typically a few seconds. The consultation is not lost, and nobody had to do "
               "anything."]],
             [156 * mm], bold_first=False)))
S.append(Spacer(1, 5))
S.append(KeepTogether(tbl([["", "Four rules that keep the page's reconciliation safe"],
              ["<b>1</b>", "<b>Never invent a prescription_built.</b> Resend only one the page "
                           "actually sent, for the patient it was built for. The prescription "
                           "decides whose recording it is"],
              ["<b>2</b>", "<b>Only reconcile the visit that is open now.</b> Once the doctor has "
                           "moved to the next patient, the previous one is finished &mdash; do not "
                           "resend its start"],
              ["<b>3</b>", "<b>Stop on configuration errors.</b> "
                           "<font face='Courier'>ORIGIN_NOT_ALLOWED</font> and "
                           "<font face='Courier'>DEVICE_NOT_ENROLLED</font> will fail every time"],
              ["<b>4</b>", "<b>Never block the doctor.</b> Every retry runs in the background; "
                           "nothing modal, nothing that has to be dismissed"]],
             [8 * mm, 148 * mm], bold_first=False)))
S.append(Spacer(1, 5))
S.append(tbl([["Known limitations on the AIMS LAB side"],
              ["<b>A microphone that dies mid-consultation</b> &mdash; a headset unplugged, a "
               "driver crash &mdash; stops capture without reopening the device, and status "
               "still reports <font face='Courier'>is_recording: true</font>. The recorder "
               "alerts AIMS LAB after two silent pieces, and the "
               "<font face='Courier'>level</font> rule above is the page's signal.<br/><br/>"
               "<b>A recorder that crashes mid-consultation</b> leaves two recordings for the "
               "same five fields &mdash; the part before the crash and the part after. Both are "
               "kept; the clinical record attaches to one of them, the other stays "
               "unconfirmed, and the recorder's restart alert tells AIMS LAB they belong "
               "together."]],
             [156 * mm], bold_first=False))

S.append(PageBreak())

# ============================================================ PART D
S.append(part("PART D", "The two server messages",
              "API 2 and API 3 Channel B - every field, every limit, every reply"))

S.append(Paragraph("12 &middot; API 2 &mdash; patient information", H2))
S.append(Preformatted(
    'POST https://<aims-host>/fhir/r4/$process-message      (event: patient-information)\n'
    'Authorization: Bearer <access token>\n'
    'Content-Type: application/fhir+json\n'
    '\n'
    '# The message is a FHIR Bundle (Part H, section 29). What it carries - exactly the\n'
    '# object our server reads out of it - is this, and every rule below applies to it:\n'
    '{\n'
    '  "patient_id": "P0012345", "doctor_id": "DR0042",\n'
    '  "hospital_id": "AALO_DHOLPUR",\n'
    '  "start_time": "2026-10-09T10:14:32+06:00", "date": "2026-10-09",\n'
    '\n'
    '  "demographics": {\n'
    '    "name": "Rahima Khatun", "sex": "female", "age_years": 34,\n'
    '    "date_of_birth": "1992-03-11",\n'
    '    "phone": "01700000000", "address": "Dholpur, Dhaka"\n'
    '  },\n'
    '\n'
    '  "paramedic": {\n'
    '    "recorded_at": "2026-10-09T10:02:10+06:00",\n'
    '    "weight_kg": 58.4, "height_cm": 157,\n'
    '    "blood_pressure": "120/80", "pulse_bpm": 78,\n'
    '    "temperature_c": 36.8, "spo2_percent": 98,\n'
    '    "notes": "complains of headache"\n'
    '  },\n'
    '\n'
    '  "previous_visit": null,\n'
    '  "female_details": {}, "male_details": {}\n'
    '}', CODE))

S.append(H3 and Paragraph("12.1 &nbsp; demographics &mdash; required", H3))
S.append(tbl([["Field", "Type", "Accepted", "If wrong", "Lands in"],
              ["<b>name</b>", "text", "up to 200. <font face='Courier'>full_name</font> also "
                                      "accepted", "shortened", "patients.full_name"],
              ["<b>sex</b>", "text", "<b>exactly</b> <font face='Courier'>female</font> or "
                                     "<font face='Courier'>male</font>",
               "<b>whole message quarantined</b>", "patients.sex"],
              ["age_years", "number", "0 to 130", "stored empty", "encounter_demographics"],
              ["date_of_birth", "text", "YYYY-MM-DD", "stored empty", "patients.date_of_birth"],
              ["phone", "text", "up to 40", "shortened", "patients.phone"],
              ["address", "text", "up to 500", "shortened", "patients.address"]],
             [24 * mm, 16 * mm, 44 * mm, 36 * mm, 36 * mm], req=[1, 2]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>demographics must be present on every visit</b>, first or fiftieth. "
                   "<b>sex is the one field that can fail the whole message</b> &mdash; send the "
                   "word, not a code like M or F.", NOTE))

S.append(PageBreak())
S.append(H3 and Paragraph("12.2 &nbsp; paramedic &mdash; optional, but send it when you have it", H3))
S.append(tbl([["Field", "Type", "Accepted", "Lands in"],
              ["recorded_at", "text", "date and time <b>with offset</b>", "paramedic_observations"],
              ["weight_kg", "number", "0.5 to 499", "weight_kg"],
              ["height_cm", "number", "20 to 299", "height_cm"],
              ["blood_pressure", "text", "<font face='Courier'>\"120/80\"</font> &mdash; upper "
                                         "30&ndash;300, lower 10&ndash;250",
               "blood_pressure, also split into systolic and diastolic"],
              ["pulse_bpm", "whole number", "10 to 300", "pulse_bpm"],
              ["temperature_c", "number", "25 to 45", "temperature_c"],
              ["spo2_percent", "whole number", "0 to 100", "spo2_percent"],
              ["notes", "text", "up to 2000", "notes"]],
             [28 * mm, 22 * mm, 58 * mm, 48 * mm]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>A reading outside its range is stored empty &mdash; it never rejects the "
                   "message.</b> One mistyped temperature must not stop a prescription being "
                   "saved.", GOOD))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Anything else inside <font face='Courier'>paramedic</font> is kept.</b> If "
                   "you measure something we have not listed &mdash; respiration rate, blood "
                   "sugar, anything &mdash; send it. It is stored in an "
                   "<font face='Courier'>other</font> field alongside the rest. <b>You do not need "
                   "our permission and we do not need to change anything first.</b>", GOOD))

S.append(Paragraph("13 &middot; API 3 Channel B &mdash; the prescription", H2))
S.append(Preformatted(
    'POST https://<aims-host>/fhir/r4/$process-message      (event: prescription)\n'
    'Authorization: Bearer <access token>\n'
    'Content-Type: application/fhir+json\n'
    '\n'
    '# As FHIR in Part H, section 30. What it carries, read back by our server:\n'
    '{\n'
    '  "patient_id": "P0012345", "doctor_id": "DR0042",\n'
    '  "hospital_id": "AALO_DHOLPUR",\n'
    '  "start_time": "2026-10-09T10:14:32+06:00",   <-- the ORIGINAL\n'
    '  "date": "2026-10-09",\n'
    '\n'
    '  "issued_at": "2026-10-09T10:26:55+06:00",\n'
    '  "items": [\n'
    '    { "drug": "Napa 500 mg", "dose": "1 tablet",\n'
    '      "frequency": "1+1+1", "duration": "5 days",\n'
    '      "instructions": "after food" },\n'
    '    { "drug": "Omeprazole 20 mg", "dose": "1 capsule",\n'
    '      "frequency": "1+0+0", "duration": "14 days" }\n'
    '  ],\n'
    '  "diagnoses":      ["Acute gastritis"],\n'
    '  "investigations": ["CBC", "HbA1c"],\n'
    '  "advice":         "avoid spicy food",\n'
    '  "follow_up":      "2026-11-09",\n'
    '  "notes":          "patient counselled about diet"\n'
    '}', CODE))
S.append(Spacer(1, 2))
S.append(tbl([["Field", "Type", "Required", "Rule", "Lands in"],
              ["<b>issued_at</b>", "text", "<b>YES</b>", "date and time <b>with offset</b>",
               "prescriptions.issued_at"],
              ["<b>items</b>", "list", "<b>YES</b>", "one per medicine. <b>Send [] if none</b>",
               "prescription_items"],
              ["items[].drug", "text", "<b>YES</b>", "up to 300. <b>Every item needs one</b>",
               "drug"],
              ["items[].dose", "text", "no", "up to 200", "dose"],
              ["items[].frequency", "text", "no", "up to 200, e.g. 1+1+1", "frequency"],
              ["items[].duration", "text", "no", "up to 200, e.g. 5 days", "duration"],
              ["items[].instructions", "text", "no", "up to 1000", "instructions"],
              ["<b>diagnoses</b>", "list of text", "<b>YES</b>", "<b>Send [] if none</b>",
               "diagnoses"],
              ["<b>investigations</b>", "list of text", "<b>YES</b>", "<b>Send [] if none</b>",
               "investigations"],
              ["advice", "text", "no", "up to 2000", "prescriptions.advice"],
              ["follow_up", "text", "no", "YYYY-MM-DD <b>if sent</b>", "follow_up_date"],
              ["notes", "text", "no", "up to 2000", "prescriptions.notes"]],
             [30 * mm, 20 * mm, 18 * mm, 48 * mm, 40 * mm], req=[1, 2, 3, 8, 9]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>An empty list and a missing field are not the same thing.</b> "
                   "<font face='Courier'>\"diagnoses\": []</font> means \"the doctor recorded "
                   "none\"; leaving it out means \"CMED did not send it\", and that is reported as "
                   "a problem. <b>Always send the list.</b> The order of "
                   "<font face='Courier'>items</font> is preserved as the line number on the "
                   "prescription.", BAD))

S.append(PageBreak())
S.append(Paragraph("14 &middot; Corrections, repeats and retries", H2))
S.append(tbl([["Situation", "What to do", "What happens"],
              ["The doctor edits the prescription after sending",
               "Send API 3 Channel B again with the <b>same five fields</b>",
               "Stored as a new version. Both kept; the newest is current"],
              ["The same prescription is rebuilt and sent again &mdash; a new "
               "<font face='Courier'>issued_at</font>, keys in another order",
               "nothing special", "<font face='Courier'>200 ALREADY_RECEIVED</font>: the same "
                                  "fingerprint (section 19.3) is the same prescription, so no "
                                  "new version"],
              ["Your network failed and you are unsure", "Send it again, unchanged",
               "<b>Completely safe.</b> <font face='Courier'>200 ALREADY_RECEIVED</font> and one "
               "row, not two"],
              ["You sent something wrong", "Send the corrected version, same five fields",
               "Same as an edit"],
              ["You sent a field we do not recognise", "nothing",
               "Kept with the rest. <b>Unknown fields are never an error</b>"]],
             [44 * mm, 52 * mm, 60 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Every message is stored word for word, exactly as sent, before anything is "
                   "read out of it.</b> Nothing is ever lost because we failed to understand a "
                   "field &mdash; if we later realise we should have been reading something, we "
                   "can go back through the stored messages and get it.", GOOD))

S.append(Paragraph("15 &middot; Every reply from the server", H2))
S.append(tbl([["Code", "Means", "What CMED should do"],
              ["<b>ACCEPTED</b> (FHIR: HTTP 200; interim JSON: 202)", "Stored safely",
               "<b>Remove it from the outbox.</b> For "
                                                         "API 2, read <font face='Courier'>"
                                                         "previous_visit.status</font> (19.2)"],
              ["<b>ALREADY_RECEIVED</b> (HTTP 200)", "Already held: this exact message, or (API 3) the "
                                              "same prescription by fingerprint",
               "<b>Remove it and never send it again.</b> Retrying was safe"],
              ["400 MALFORMED_JSON", "Not valid JSON", "Fix and resend. Do not retry unchanged"],
              ["400 MISSING_FIELD", "One of the five is missing or malformed", "Fix and resend"],
              ["400 INVALID_IDENTIFIER", "An id has disallowed characters", "Fix and resend"],
              ["400 MALFORMED_FHIR", "The Bundle cannot be read; the element is named in the "
                                     "OperationOutcome", "Fix and resend"],
              ["401 INVALID_TOKEN", "Token missing, unknown, expired or revoked",
               "Get a new token <b>once</b> and resend. Refused again: stop, contact AIMS LAB"],
              ["403 INSUFFICIENT_SCOPE", "The token was not granted this action",
               "Ask for both scopes (Part H, 27)"],
              ["<b>403 HOSPITAL_NOT_ALLOWED</b>", "<b>The message is for a hospital this token "
                                                  "does not speak for</b>",
               "<b>Use that hospital's own client.</b> Do not retry; it raises an alert here"],
              ["401 INVALID_KEY / 403 KEY_NOT_BOUND", "Interim static key wrong, or naming no "
                                                     "hospital", "<b>Stop and contact AIMS LAB</b>"],
              ["413 TOO_LARGE", "Body over 1 MB", "Check for a file or image in the message"],
              ["<b>422 SCHEMA_INVALID</b>", "<b>Stored in quarantine</b>, with the bad fields "
                                            "named", "<b>Not lost.</b> Read the list, fix, resend; "
                                                     "alert your team"],
              ["429 RATE_LIMITED, 500, 502, 503, a timeout, no connection",
               "Too fast, busy, a problem on our side, or no internet",
               "<b>Keep it in the outbox and retry with growing delay.</b> Never drop it"]],
             [40 * mm, 56 * mm, 60 * mm], highlight=[1, 2], req=[13]))
S.append(Paragraph("<b>Limits, far above CMED's peak, shared by every server process:</b> "
                   "1,200 requests and 60 token requests a minute per address. Reuse a token "
                   "until ~30 s before it expires. <b>A lockout belongs to one credential, not to "
                   "an address:</b> 10 wrong tries with the same credential in 5 minutes lock "
                   "that credential for 15 minutes (429, Retry-After), so one hospital's revoked "
                   "client never blocks the others. On the token endpoint the count is per "
                   "claimed client and address. Requests carrying no credential at all are "
                   "locked per address only after 100. Addresses on a hospital's allow-list are "
                   "never locked out. <b>Clock:</b> keep CMED's server on NTP; we allow "
                   "&plusmn;60 s, and a clock outside that gets 400 \"clock skew\", never a "
                   "lockout.", GOOD))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Every reply carries a request id</b> &mdash; the "
                   "<font face='Courier'>X-Request-ID</font> header, and "
                   "<font face='Courier'>request_id</font> in the body (in FHIR replies, in "
                   "Parameters and in <font face='Courier'>OperationOutcome.issue.diagnostics"
                   "</font>). Quote it when you report a problem. Errors outside FHIR and the "
                   "token endpoint are RFC 9457 <font face='Courier'>application/problem+json"
                   "</font>. The machine-readable contract is "
                   "<font face='Courier'>openapi/channel-b.json</font> in the repository.",
                   NOTE))

S.append(PageBreak())
S.append(Paragraph("16 &middot; Outages: the outbox on CMED's local server", H2))
S.append(Paragraph("Each Aalo branch runs CMED on its own local server, so the clinic keeps "
                   "working through an internet or power cut and syncs to the cloud afterwards. "
                   "<b>The recording side already works the same way</b>: the page talks to the "
                   "recorder inside the laptop, the recorder keeps recording, holds the audio, "
                   "and uploads when the internet returns. <b>But API 2 and API 3 Channel B leave "
                   "from CMED's server, and while the internet is down they cannot reach us.</b> "
                   "They must wait on CMED's side and go when it is back. That is the outbox.",
                   BODY))
S.append(Spacer(1, 4))
S.append(tbl([["", "The outbox, as CMED's software team should build it"],
              ["<b>1</b>", "<b>A database table on the branch's local server</b> &mdash; not "
                           "memory, not a browser. It must survive a power cut and a restart"],
              ["<b>2</b>", "<b>Written at the moment of the click</b>, in the same transaction "
                           "that saves the visit or the prescription. The doctor never waits for "
                           "the network"],
              ["<b>3</b>", "<b>Sent in order</b>: for one visit, API 2 before its API 3 Channel "
                           "B; corrections after the original"],
              ["<b>4</b>", "<b>Retried with growing delay</b> while our server cannot be reached "
                           "&mdash; for example 10 s, 30 s, 1 min, 5 min, then every 5 min &mdash; "
                           "for as long as it takes. Hours or days are fine"],
              ["<b>5</b>", "<b>Each reply decides what happens next</b> &mdash; the table below"],
              ["<b>6</b>", "<b>Visible:</b> the number waiting and the age of the oldest, so a "
                           "stuck outbox is noticed the same day"]],
             [8 * mm, 148 * mm], bold_first=False, highlight=[1]))
S.append(Spacer(1, 4))
S.append(tbl([["Reply", "The outbox does"],
              ["<b>200 ACCEPTED</b>", "<b>remove</b> the message"],
              ["<b>200 ALREADY_RECEIVED</b>", "<b>remove it and never send it again</b> &mdash; "
                                              "we already hold it"],
              ["400 MALFORMED_JSON / MISSING_FIELD / INVALID_IDENTIFIER",
               "park it for a person to fix; <b>do not retry unchanged</b>; alert"],
              ["<b>422 SCHEMA_INVALID</b>", "stored in quarantine on our side, so not lost; "
                                            "park it, fix the shape, send the corrected one; "
                                            "alert"],
              ["401 INVALID_TOKEN", "get a new token once and resend; if the token itself is "
                                    "refused (401 invalid_client), <b>stop sending everything</b> "
                                    "and alert &mdash; the client is wrong or revoked"],
              ["403 HOSPITAL_NOT_ALLOWED / INSUFFICIENT_SCOPE / KEY_NOT_BOUND",
               "park it and alert: the wrong hospital's credential was used; retrying cannot "
               "help"],
              ["413 TOO_LARGE", "park it; something too big is in the body; fix and resend"],
              ["429, 5xx, timeout, no connection", "<b>keep it and retry later</b>"]],
             [62 * mm, 94 * mm], highlight=[1, 2], req=[8]))
S.append(Spacer(1, 4))
S.append(tbl([["API 2 arrives...", "What happens to the recording"],
              ["within a second or two", "Its clinical record is attached at once"],
              ["late &mdash; minutes, hours or days, after an outage",
               "<b>Confirms it when it arrives.</b> The recording was recorded and uploaded "
               "meanwhile, and enters the dataset the moment API 2 matches it; nothing has a "
               "deadline"],
              ["<b>never</b>", "<b>The recording is kept but held out of the dataset</b>, marked "
                               "<i>unconfirmed</i>, and AIMS LAB is alerted. Only API 2 - or an "
                               "AIMS LAB administrator, by name - lets it in, because API 2 is "
                               "what proves the consultation was real. So a message must never "
                               "be dropped"]],
             [40 * mm, 116 * mm], highlight=[1, 2], req=[3]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>A message arriving a day late is completely fine. A message never sent is "
                   "the only real failure.</b> Nothing on our side deletes a recording because "
                   "CMED's message was late or missing; the only thing that removes a recording "
                   "is the patient refusing consent, on the doctor's Stop button.", GOOD))

S.append(PageBreak())
S.append(Paragraph("17 &middot; The reply that looks like success but is not", H2))
S.append(tbl([["", "Proves", "Does NOT prove"],
              ["<font face='Courier'>200 RECORDING_STARTED</font>",
               "<b>The microphone is live</b> and audio is being written",
               "that the consultation will be kept"],
              ["<font face='Courier'>200 ACCEPTED</font>",
               "<b>Your message is stored</b> and will not be lost",
               "<b>that it matched a recording</b>"]],
             [48 * mm, 54 * mm, 54 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("There is a third step, on our server: <b>we compare the five fields the "
                   "recorder reported against the five in your message, and the clinical record "
                   "is attached to the recording only if they are identical.</b>", BODY))
S.append(Spacer(1, 3))
S.append(tbl([["<b>The failure that matters, stated plainly</b>"],
              ["If your <font face='Courier'>start_time</font> differs from API 1's by even a "
               "second, or loses its <font face='Courier'>+06:00</font>, or the clinic code came "
               "from your own settings instead of the laptop:<br/><br/>"
               "&nbsp;&nbsp;&bull; the recorder says <b>200 RECORDING_STARTED</b><br/>"
               "&nbsp;&nbsp;&bull; our server says <b>200 ACCEPTED</b><br/>"
               "&nbsp;&nbsp;&bull; <b>CMED sees nothing wrong at all</b><br/>"
               "&nbsp;&nbsp;&bull; and the recording is kept <b>with no clinical record "
               "attached</b> &mdash; no demographics, no prescription"
               "<br/><br/>"
               "<b>Two green replies mean both halves arrived. Whether they found each other is a "
               "separate question CMED cannot see the answer to.</b> Section 22 is how to prove "
               "it."]],
             [156 * mm], bold_first=False))

S.append(PageBreak())

# ============================================================ PART E
S.append(part("PART E", "Previous prescriptions",
              "How history reaches us, and how to ask what we already hold"))

S.append(Paragraph("18 &middot; History comes to us inside API 2", H2))
S.append(Paragraph("A returning patient's previous prescription reaches us as the "
                   "<font face='Courier'>previous_visit</font> object inside API 2. "
                   "<b>The field must always be present</b> &mdash; send "
                   "<font face='Courier'>null</font> when there is nothing to send, because "
                   "leaving it out cannot be told apart from \"CMED forgot\".", BODY))
S.append(Spacer(1, 3))
S.append(Preformatted(
    '"previous_visit": {\n'
    '  "doctor_id":   "DR0007",                     <-- that visit\'s OWN five\n'
    '  "hospital_id": "AALO_DHOLPUR",                   fields, exactly as CMED\n'
    '  "start_time":  "2026-06-02T09:40:05+06:00",      sent them at the time\n'
    '  "date":        "2026-06-02",\n'
    '  "prescription": {\n'
    '    "items": [ { "drug": "Metformin 500 mg", "dose": "1 tablet",\n'
    '                 "frequency": "1+0+1", "duration": "30 days" } ],\n'
    '    "diagnoses": ["Type 2 diabetes mellitus"], "investigations": [],\n'
    '    "advice": "review in one month", "follow_up": "2026-07-02"\n'
    '  }\n'
    '}', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Field", "Rule"],
              ["<b>date</b>", "<b>Required.</b> YYYY-MM-DD, the earlier visit's date, before "
                              "this visit"],
              ["<b>doctor_id, hospital_id, start_time</b>",
               "<b>All three, or none.</b> With them we find the visit itself and never file "
               "it under today's doctor. Half a name is refused with 422"],
              ["patient_id", "optional; if sent, must be this patient"],
              ["<b>prescription</b>", "<b>Required</b>, the same shape as API 3 Channel B "
                                      "(section 13)"]],
             [44 * mm, 112 * mm], req=[1, 2, 4]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>The date-only shape still works</b> &mdash; "
                   "<font face='Courier'>{\"date\", \"prescription\"}</font> &mdash; so nothing "
                   "already built breaks, but it names no doctor, so we file it under "
                   "<font face='Courier'>UNKNOWN</font> unless the same prescription is already "
                   "held that day. Send the five fields.", NOTE))

S.append(PageBreak())
S.append(Paragraph("19 &middot; Ask first, and read what we did", H2))
S.append(H3 and Paragraph("19.1 &nbsp; What do you already hold for this patient?", H3))
S.append(Preformatted(
    'GET https://<aims-host>/fhir/r4/Encounter?patient.identifier=P0012345&_count=50\n'
    'Authorization: Bearer <access token>\n\n200 ' + _compact(_SEARCH), CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Parameter / reply", "Meaning"],
              ["<font face='Courier'>patient.identifier</font>", "The patient id, or "
                                                     "<font face='Courier'>system|id</font>. "
                                                     "Required"],
              ["<font face='Courier'>_count</font>", "1 to 200, default 50. Newest first"],
              ["next page", "When a page is full, follow the <font face='Courier'>next</font> "
                            "link as it is (it carries an opaque "
                            "<font face='Courier'>_cursor</font>)"],
              ["<font face='Courier'>date=ltYYYY-MM-DD</font>", "Optional: only visits before "
                                                               "that date"],
              ["<b>which hospitals</b>", "<b>Only visits at the hospital(s) the token speaks "
                                         "for.</b> A visit at another hospital is not listed; "
                                         "send its history and the reply says what happened "
                                         "(19.2)"],
              ["a patient we hold nothing for", "<font face='Courier'>\"total\": 0</font>, "
                                                "no entries"],
              ["errors", "401 INVALID_TOKEN; 403 INSUFFICIENT_SCOPE; 400 INVALID_IDENTIFIER; "
                         "400 MISSING_FIELD naming <font face='Courier'>_count</font> or "
                         "<font face='Courier'>date</font> &mdash; each as an OperationOutcome"]],
             [44 * mm, 112 * mm]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Identifiers and fingerprints only</b> &mdash; never a name, a medicine or a "
                   "diagnosis, and no audio ever. Every request is logged against the patient.",
                   GOOD))
S.append(Spacer(1, 4))
S.append(tbl([["", "The rule for a returning patient"],
              ["<b>1</b>", "Ask for the visits"],
              ["<b>2</b>", "Compute the fingerprint (19.3) of the previous prescription as CMED "
                           "holds it now"],
              ["<b>3</b>", "<b>Listed with the same fingerprint</b> &rarr; send "
                           "<font face='Courier'>\"previous_visit\": null</font>. <b>Not listed, or "
                           "a different fingerprint</b> &rarr; send "
                           "<font face='Courier'>previous_visit</font>"],
              ["<b>4</b>", "<b>If the question cannot be asked</b> &mdash; our server down, a "
                           "timeout &mdash; <b>send previous_visit anyway.</b> About 2 KB, and "
                           "the reply says whether it was needed"]],
             [8 * mm, 148 * mm], bold_first=False, highlight=[3]))

S.append(PageBreak())
S.append(H3 and Paragraph("19.2 &nbsp; What the API 2 reply says about previous_visit", H3))
S.append(Preformatted(
    'In the reply message\'s Parameters (section 31):\n'
    '  { "name": "code", "valueCode": "ACCEPTED" },\n'
    '  { "name": "record_id", "valueString": "5121" },\n'
    '  { "name": "previous_visit", "part": [ { "name": "status", "valueString": "ALREADY_STORED" },\n'
    '                                        { "name": "version", "valueInteger": 2 } ] }', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["previous_visit.status", "What we did", "Meaning for CMED"],
              ["<b>ALREADY_STORED</b>", "<b>nothing</b> &mdash; held, same fingerprint",
               "<b>That history is here.</b> No need to send it again"],
              ["<b>UPDATED</b>", "stored as that visit's <b>next version</b>; the newest is read",
               "the corrected prescription is now held"],
              ["<b>STORED</b>", "stored that visit, under its real doctor and clinic",
               "it was missing and is now held"],
              ["NOT_SENT", "&mdash;", "previous_visit was null"],
              ["UNIDENTIFIED", "the date-only shape; <font face='Courier'>\"stored\"</font> "
                               "says whether anything was added", "send the five fields"],
              ["PENDING", "stored safely, still loading", "nothing; it completes by itself"],
              ["<b>NOT_PERMITTED</b>", "<b>nothing</b> &mdash; the earlier visit was at "
                                       "<b>another hospital</b>, we already hold it, and this "
                                       "copy differs",
               "history from another hospital may fill a gap, never change that hospital's own "
               "record. If it really changed, that hospital's client sends it"]],
             [34 * mm, 62 * mm, 60 * mm], highlight=[1]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Today's patient information in the same API 2 is stored as usual whatever "
                   "happens to previous_visit.</b> A whole API 2 sent twice, byte for byte, is "
                   "<font face='Courier'>200 ALREADY_RECEIVED</font>, with "
                   "<font face='Courier'>ALREADY_STORED</font> for the history it had stored.",
                   BODY))
S.append(Spacer(1, 4))
S.append(H3 and Paragraph("19.3 &nbsp; The prescription fingerprint &mdash; build it exactly like "
                          "this", H3))
S.append(tbl([["Step", "Rule"],
              ["<b>1</b>", "Take these six keys and only these: <font face='Courier'>items</font> "
                           "(in prescribed order; each entry exactly <font face='Courier'>drug, "
                           "dose, frequency, duration, instructions</font>), "
                           "<font face='Courier'>diagnoses</font>, "
                           "<font face='Courier'>investigations</font> (lists of strings), "
                           "<font face='Courier'>advice</font>, "
                           "<font face='Courier'>follow_up</font> (YYYY-MM-DD), "
                           "<font face='Courier'>notes</font>"],
              ["<b>2</b>", "Trim spaces at the ends of every text. An empty or missing text is "
                           "<font face='Courier'>null</font>; a missing list is "
                           "<font face='Courier'>[]</font>. An item with no drug is left out. A "
                           "diagnosis or investigation sent as an object counts as its "
                           "<font face='Courier'>text</font>, else its "
                           "<font face='Courier'>name</font>"],
              ["<b>3</b>", "JSON with <b>keys sorted at every level</b>, no spaces (separators "
                           "<font face='Courier'>,</font> and <font face='Courier'>:</font>), "
                           "Bangla and other characters written as themselves, not "
                           "<font face='Courier'>\\u</font> escapes. UTF-8, then SHA-256, "
                           "64 lowercase hex"]],
             [12 * mm, 144 * mm]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Worked example &mdash; reproduce this before using it.</b> This "
                   "prescription:", BODY))
S.append(Preformatted(
    '{ "issued_at": "2026-10-01T10:20:00+06:00",\n'
    '  "items": [ { "drug": "Paracetamol", "dose": "500 mg",\n'
    '               "frequency": "1+1+1", "duration": "5 days" } ],\n'
    '  "diagnoses": ["Fever"], "investigations": ["CBC"],\n'
    '  "advice": "Rest and fluids", "follow_up": "2026-10-08" }\n'
    '\n'
    'becomes exactly this text (one line, shown wrapped):\n'
    '{"advice":"Rest and fluids","diagnoses":["Fever"],"follow_up":"2026-10-08",\n'
    '"investigations":["CBC"],"items":[{"dose":"500 mg","drug":"Paracetamol",\n'
    '"duration":"5 days","frequency":"1+1+1","instructions":null}],"notes":null}\n'
    '\n'
    'fingerprint:\n'
    'f1e7109404fd5d4010c358f0b3afdec79699955299a089eaf2c830fb60ec4806', CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<font face='Courier'>issued_at</font>, key order, spacing and any field not "
                   "listed make no difference. A change to any medicine, its order, a diagnosis, "
                   "the advice, the follow-up or the notes is a different fingerprint. Our test "
                   "suite pins the same value.", NOTE))

S.append(PageBreak())

# ============================================================ PART F
S.append(part("PART F", "The data model",
              "Where every field lands, and what the two databases hold"))

S.append(Paragraph("20 &middot; Where every field ends up", H2))
S.append(Paragraph("So CMED can see that nothing is discarded. All of this lives in a database "
                   "called <font face='Courier'>aims_clinical</font>, separate from the one that "
                   "tracks recordings &mdash; which deliberately holds no patient names at all.",
                   BODY))
S.append(Spacer(1, 4))
S.append(tbl([["You send", "We store it in", "Holding"],
              ["<b>the whole message, untouched</b>", "<font face='Courier'>intake_records</font>",
               "<b>exactly what you sent</b>, once per message, before anything is parsed"],
              ["demographics", "<font face='Courier'>patients</font>, "
                               "<font face='Courier'>encounter_demographics</font>",
               "name, sex, date of birth, phone, address, and the age at this visit"],
              ["paramedic", "<font face='Courier'>paramedic_observations</font>",
               "each reading in its own column, blood pressure also split into systolic and "
               "diastolic, anything unrecognised in <font face='Courier'>other</font>"],
              ["the visit itself", "<font face='Courier'>encounters</font>",
               "one row per consultation, linked to its recording"],
              ["<b>previous_visit</b>", "<font face='Courier'>encounters</font>, "
                                        "<font face='Courier'>prescriptions</font>",
               "<b>that earlier visit's own row</b> &mdash; the one held, or a new one under its "
               "real doctor &mdash; and a new prescription version only if it changed"],
              ["items", "<font face='Courier'>prescription_items</font>",
               "<b>one row per medicine</b>, in the order you sent them"],
              ["the prescription", "<font face='Courier'>prescriptions</font>",
               "with a version number, so corrections never overwrite"],
              ["diagnoses", "<font face='Courier'>diagnoses</font>", "one row each"],
              ["investigations", "<font face='Courier'>investigations</font>", "one row each"],
              ["female_details / male_details", "their own tables", "stored as sent"]],
             [40 * mm, 52 * mm, 64 * mm], highlight=[1]))

S.append(Paragraph("21 &middot; The two databases, and why they are separate", H2))
S.append(tbl([["", "<font face='Courier'>aims_clinical</font>",
               "<font face='Courier'>aims_recordings</font>"],
              ["Holds", "<b>Everything CMED sends</b> &mdash; patients, demographics, paramedic "
                        "readings, prescriptions, previous visits",
               "The audio tracking record &mdash; sessions, segments, hashes, archive state"],
              ["<b>Patient names?</b>", "<b>yes</b>", "<b>no &mdash; by design, none at all</b>"],
              ["Size", "about 76 KB per consultation; roughly 10 GB for the study",
               "small &mdash; metadata only"],
              ["Who writes it", "the two endpoints in this document", "the recorders"]],
             [26 * mm, 66 * mm, 64 * mm], highlight=[2]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>The split is deliberate.</b> A recording can be tracked, verified, archived "
                   "and audited end to end without ever touching a patient name, which means the "
                   "operational half of the system carries no identifying data.", GOOD))
S.append(Spacer(1, 4))
S.append(Paragraph("A copy of every consultation's clinical record is <b>also written as a JSON "
                   "file beside the audio</b>, with the same name. So each consultation is one "
                   "folder containing the audio and its record together, and the database is an "
                   "index into them &mdash; if the database were lost entirely, the dataset would "
                   "still be readable.", GOOD))

S.append(PageBreak())

# ============================================================ PART G
S.append(part("PART G", "Testing and handover",
              "What to run, what to send us, and the page to keep on the desk"))

S.append(Paragraph("22 &middot; Testing, in the order that makes sense", H2))
S.append(tbl([["", "What", "Needs", "How"],
              ["<b>1</b>", "<b>The browser half &mdash; all of it</b>", "<b>a PC with the recorder "
                                                                        "installed. No internet</b>",
               "<b>Available today.</b> <font face='Courier'>python tools/channel_a_test.py "
               "--origin https://&lt;your page&gt;</font> &mdash; 22 checks, including both "
               "recovery cases in section 11A"],
              ["<b>2</b>", "Check a server message against the rules", "nothing at all",
               "<font face='Courier'>python tools/channel_b_test.py --validate my.json "
               "--kind patient_information</font>"],
              ["<b>3</b>", "<b>The server half against a live host</b>", "<b>a staging hostname "
                                                                         "from us</b>",
               "<font face='Courier'>channel_b_test.py --server https://&lt;host&gt; "
               "--client-id &lt;id&gt; --kid &lt;kid&gt; --key-file &lt;private.pem&gt; "
               "--hospital &lt;CMED id&gt;</font> &mdash; 11 checks (28.3)"],
              ["<b>4</b>", "Watch every message and reply side by side", "the staging site",
               "open <font face='Courier'>/protocol-test</font>"],
              ["<b>5</b>", "<b>A real consultation, all four messages</b>", "a clinic PC",
               "and then step 6 &mdash; do not skip it"],
              ["<b>6</b>", "<b>AIMS LAB confirms it matched and archived</b>", "<b>ask us</b>",
               "<b>the only check that proves the integration</b>"]],
             [8 * mm, 46 * mm, 44 * mm, 58 * mm], highlight=[1], req=[6]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Step 1 is unblocked today and is the half with the awkward detail</b> "
                   "&mdash; the gate, the session id, the provisional reply, reading the clinic "
                   "code. Start there while we publish a staging host for step 3. <b>Please run "
                   "step 3 and send us the output before the first clinic opens</b>; it takes under "
                   "a minute and has caught every integration problem we have seen.", GOOD))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Step 6 must be asked for explicitly.</b> Steps 1 to 5 can all pass while "
                   "the five fields silently fail to match. We can see the answer on the dashboard "
                   "in seconds &mdash; it is the only green light that means anything.", BAD))

S.append(Paragraph("22A &middot; Five commands that test the server half, before you write code", H2))
S.append(Paragraph("These need no application and no page. Run them as soon as you have the "
                   "staging hostname and your first hospital is registered (section 28). They "
                   "use the reference client, " + "<font face='Courier'>tools/cmed_fhir_client.py"
                   "</font>, which does exactly what CMED's code will do.", BODY))
S.append(Spacer(1, 3))
S.append(H3 and Paragraph("Is it up, and is it FHIR?", H3))
S.append(Preformatted(
    'curl -s https://<aims-host>/health | jq .status          # expect "healthy"\n'
    'curl -s https://<aims-host>/fhir/r4/metadata | jq .fhirVersion          # "4.0.1"\n'
    'curl -s https://<aims-host>/fhir/r4/.well-known/smart-configuration | jq .token_endpoint', CODE))
S.append(H3 and Paragraph("Does my hospital's key buy a token?", H3))
S.append(Preformatted(
    'python tools/cmed_fhir_client.py token --server https://<aims-host> \\\n'
    '    --client-id cmed-aalo-dholpur --kid <kid> --key keys/cmed-aalo-dholpur.private.pem\n'
    '# expect: 200, "token_type": "bearer", "expires_in": 300,\n'
    '#         "hospital_ids": ["AALO_DHOLPUR"]   (the token itself is not printed)', CODE))
S.append(H3 and Paragraph("Every case at once: send, repeat, break, wrong hospital", H3))
S.append(Preformatted(
    'python tools/channel_b_test.py --server https://<aims-host> \\\n'
    '    --client-id cmed-aalo-dholpur --kid <kid> \\\n'
    '    --key-file keys/cmed-aalo-dholpur.private.pem --hospital AALO_DHOLPUR\n'
    '# expect: 11/11 as expected, "Channel B: READY"', CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>It sends the same API 2 twice on purpose.</b> The first gets <b>200 ACCEPTED</b>, the "
                   "second <b>200 ALREADY_RECEIVED</b>. That proves retries are safe, which is the "
                   "most useful thing to know before writing your queue. It also breaks a message "
                   "deliberately, sends one for another hospital (<b>403</b>) and replays an "
                   "assertion (<b>401</b>) &mdash; the reply names what is wrong each time, and a "
                   "quarantined message is not lost, so nothing is wasted by experimenting.", GOOD))
S.append(H3 and Paragraph("Your own message, with a token in hand", H3))
S.append(Preformatted(
    '# $TOKEN from your own code (or the reference client); my_api2.json a FHIR Bundle\n'
    'curl -s -X POST https://<aims-host>/fhir/r4/$process-message \\\n'
    '  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/fhir+json" \\\n'
    '  --data @my_api2.json | jq \'.entry[2].resource.parameter\'\n'
    '# expect: [ { "name": "code", "valueCode": "ACCEPTED" }, ... ]\n'
    '\n'
    'curl -s -H "Authorization: Bearer $TOKEN" \\\n'
    '  "https://<aims-host>/fhir/r4/Encounter?patient.identifier=TEST0001" | jq .total\n'
    '# without a token: 401, an OperationOutcome with INVALID_TOKEN', CODE))
S.append(Spacer(1, 4))
S.append(tbl([["The one thing these commands cannot prove"],
              ["All of them can pass while the integration is still broken, because <b>ACCEPTED "
               "means \"stored\", not \"matched to a recording\"</b>. If "
               "<font face='Courier'>start_time</font> differs by a second between API 1 and API 2, "
               "or loses its offset, every reply above is still green and <b>the recording ends "
               "up with no clinical record attached</b>.<br/><br/>"
               "<b>So the real test needs a recorder running and our confirmation</b> &mdash; "
               "steps 5 and 6 of section 22. Please do not treat green curls as integration "
               "complete."]],
             [156 * mm], bold_first=False))

S.append(PageBreak())
S.append(Paragraph("23 &middot; A complete worked example, with recovery built in", H2))
S.append(Paragraph("The browser half of the integration as it should actually be written: the five "
                   "fields built once, and <b>a reconcile step that resends whatever the recorder "
                   "is missing</b> &mdash; so a recorder that was not running or a dropped "
                   "socket heals itself without the doctor knowing, on top of what the recorder "
                   "recovers by itself. Section 11A is the reasoning; this is the code.", BODY))
S.append(Spacer(1, 3))
S.append(Preformatted(
    "// ---- what SHOULD be happening: the page's own record --------------------\n"
    'let visit = null;          // the open consultation: the five fields, built once\n'
    'let built = false;         // has the doctor built ITS prescription?\n'
    'let CLINIC = null;         // read from the recorder, never hardcoded\n'
    'let socket = null, sessionId = null, stopRetrying = false, retryMs = 2000;\n'
    '\n'
    'const CONFIG_ERRORS = ["DEVICE_NOT_ENROLLED", "ORIGIN_NOT_ALLOWED"];\n'
    '\n'
    'function send(command, extra) {\n'
    '  if (!socket || socket.readyState !== WebSocket.OPEN) return false;\n'
    '  socket.send(JSON.stringify({ command, request_id: command + "-" + Date.now(),\n'
    '                               ...extra }));\n'
    '  return true;\n'
    '}\n'
    '\n'
    '// ---- the heart of it: compare SHOULD with IS, resend whatever is missing --\n'
    'function reconcile(status) {\n'
    '  if (!visit || stopRetrying) return;\n'
    '  if (!status.is_recording) {                    // nothing recording at all\n'
    '    send("start", { trigger: visit });            // SAME five fields - safe\n'
    '    return;                                       // SESSION_ALREADY_ACTIVE if dup\n'
    '  }\n'
    '  // Recording that started with another patient is fine: our start was\n'
    '  // accepted into it (continued: true), and the server sorts out whose is whose.\n'
    '  sessionId = status.session_id;\n'
    '  if (built && !status.armed) {                   // our 3a was lost\n'
    '    send("prescription_built", { patient_id: visit.patient_id,\n'
    '                                 session_id: sessionId });   // safe if dup\n'
    '  }\n'
    '}\n'
    '\n'
    'function connect() {\n'
    '  socket = new WebSocket("ws://127.0.0.1:5050/ws");\n'
    '\n'
    '  socket.onopen = () => { retryMs = 2000; showConnected(true);\n'
    '                          send("doctors"); };     // clinic code first\n'
    '\n'
    '  socket.onmessage = (e) => {\n'
    '    const m = JSON.parse(e.data);\n'
    '\n'
    '    // Status reaches us two ways: pushed unprompted (event "status"), and as\n'
    '    // the reply to our own poll (command "status", the fields under data).\n'
    '    const status = m.event === "status" ? m\n'
    '                 : m.command === "status" ? m.data : null;\n'
    '    if (status) {\n'
    '      showRecordingState(status);                 // incl. a level meter\n'
    '      reconcile(status);                          // <-- self-healing\n'
    '      return;\n'
    '    }\n'
    '    if (m.command === "doctors") {\n'
    '      CLINIC = m.data.hospital_id;\n'
    '      if (!CLINIC) { stopRetrying = true; return banner("PC not registered"); }\n'
    '      showClinicName(CLINIC);\n'
    '      fillDoctorSelector(m.data.doctors);         // [] is normal\n'
    '      return;\n'
    '    }\n'
    '    if (m.command === "start") {\n'
    '      if (m.code === "RECORDING_STARTED" || m.code === "RECORDING_PROVISIONAL" ||\n'
    '          m.code === "SESSION_ALREADY_ACTIVE") {  // all three mean: recording,\n'
    '                                                  // keep the session it gave us\n'
    '        sessionId = m.data && m.data.session_id || sessionId;\n'
    '      } else if (CONFIG_ERRORS.includes(m.code)) {\n'
    '        stopRetrying = true; banner(m.code);      // a retry cannot fix this\n'
    '      } else if (m.code === "AGENT_NOT_READY") {  // the recorder retries too;\n'
    '        setTimeout(() => send("start", { trigger: visit }), retryMs);  // harmless\n'
    '        retryMs = Math.min(retryMs * 2, 30000);   // 2, 4, 8, 16, 30, 30...\n'
    '      }\n'
    '    }\n'
    '  };\n'
    '\n'
    '  socket.onclose = () => { showConnected(false);\n'
    '                           setTimeout(connect, 2000); };  // quietly, forever\n'
    '}\n'
    '\n'
    '// ---- the doctor opens a patient --------------------------------------------\n'
    'async function openPatient(patient, doctor) {\n'
    '  visit = {                                       // built ONCE, here\n'
    '    patient_id:  patient.id,\n'
    '    doctor_id:   doctor.id,\n'
    '    hospital_id: CLINIC,\n'
    '    start_time:  new Date().toISOString(),        // the ORIGINAL, forever\n'
    '    date:        localDateString()\n'
    '  };\n'
    '  built = false; stopRetrying = false; retryMs = 2000;\n'
    '  send("start", { trigger: visit });              // if the socket is down,\n'
    '                                                  // reconcile() sends it later\n'
    '  outbox.add("API2", visit, demographics, paramedic,  // section 16: a table on\n'
    '             await previousVisitIfNeeded(patient));    // the branch server (19.1)\n'
    '}\n'
    '\n'
    '// ---- the doctor builds the prescription - this does NOT stop recording -----\n'
    '// visit is the patient the prescription is for; if a wrong profile was\n'
    '// opened first, the server files the audio under this patient.\n'
    'function prescriptionBuilt(prescription) {\n'
    '  built = true;                                   // remembered, so it can be\n'
    '  send("prescription_built", {                    // resent if it gets lost\n'
    '    patient_id: visit.patient_id, session_id: sessionId });\n'
    '  outbox.add("API3B", visit, prescription);       // same visit object\n'
    '}\n'
    '\n'
    '// A safety net for the one path with no status event: an idle recorder\n'
    '// whose socket is open but which never received the start.\n'
    'setInterval(() => send("status"), 10000);\n'
    '\n'
    'connect();', CODE))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>The single most important function is <font face='Courier'>reconcile()"
                   "</font></b>. It runs on every status message &mdash; including the one the "
                   "recorder sends the instant a socket reconnects &mdash; and it is the reason "
                   "the page's half of the doctor never having to re-open a patient. Note that "
                   "it resends the <b>same</b> <font face='Courier'>visit</font> object, so "
                   "start_time is still the original and the late recording still matches "
                   "API 2.", GOOD))

S.append(PageBreak())
S.append(Paragraph("24 &middot; What each side must hand over", H2))
S.append(tbl([["AIMS LAB gives CMED", "When"],
              ["The staging and production hostnames", "at once"],
              ["<b>For each hospital: its client id and key id (kid)</b> &mdash; nothing secret",
               "when we register the public key you send"],
              ["The seven clinic codes", "with the first registration"],
              ["<font face='Courier'>cmed_fhir_client.py</font>, "
               "<font face='Courier'>channel_b_test.py</font>, and the TypeScript reference "
               "client", "now"],
              ["Confirmation a test recording matched and archived", "<b>on request, step 6</b>"]],
             [100 * mm, 56 * mm]))
S.append(Spacer(1, 3))
S.append(tbl([["<b>CMED gives AIMS LAB &mdash; both blocking</b>", "Why it blocks"],
              ["<b>1. Every exact web address your page is served from</b>, production, staging "
               "and preview",
               "<b>Without it the page cannot open the local connection at all.</b> Nothing works. "
               "<font face='Courier'>www</font> and non-<font face='Courier'>www</font> differ, as "
               "do <font face='Courier'>http</font> and <font face='Courier'>https</font> and any "
               "non-default port"],
              ["<b>2. Your own clinic codes</b>, so we can map them to ours",
               "<b>Without the mapping a visit cannot be tied to the clinic it happened in. The "
               "record is stored and you receive a normal ACCEPTED, but it is never attached to its "
               "recording.</b> Silent from CMED's side; we are alerted"],
              ["<b>3. One public key (JWK) per hospital</b>, and optionally the addresses "
               "your servers call from",
               "<b>Without it that hospital cannot get a token.</b> Public key only &mdash; never "
               "send a private key; our server refuses one"],
              ["A named technical contact", "So a question either way does not wait a week"],
              ["A staging environment we can send to", "So we can test our side against yours"]],
             [62 * mm, 94 * mm], req=[1, 2, 3]))

S.append(CondPageBreak(90 * mm))
S.append(Paragraph("25 &middot; One page for the CMED developer", H2))
S.append(tbl([["Question", "Answer"],
              ["How many messages do I build?", "<b>Four.</b> Two from the browser, two from your "
                                                "server"],
              ["What ties them together?", "<b>The five fields, identical in all four</b>"],
              ["<b>Biggest trap</b>", "<b>start_time in API 3 is the original</b>, not when the "
                                      "prescription was built"],
              ["Where do I connect, browser side?", "<font face='Courier'>ws://127.0.0.1:5050/ws"
                                                    "</font> &mdash; the same PC, every time"],
              ["Do I need a key there?", "<b>No.</b> We register your page's web address instead"],
              ["Where do I POST, server side?", "<font face='Courier'>/fhir/r4/$process-message"
                                                "</font> &mdash; a FHIR message Bundle, event "
                                                "<font face='Courier'>patient-information</font> "
                                                "or <font face='Courier'>prescription</font>"],
              ["How do I authenticate there?", "<b>Per hospital</b>: sign a 5-minute assertion "
                                               "with that hospital's private key, trade it at "
                                               "<font face='Courier'>/api/v2/oauth/token</font>, "
                                               "send <font face='Courier'>Bearer</font>. Server "
                                               "to server only"],
              ["One key for all hospitals?", "<b>No.</b> One client per hospital; a token for "
                                             "one hospital is refused for another (403)"],
              ["What do I keep from API 1?", "<b><font face='Courier'>session_id</font></b> "
                                             "&mdash; API 3 Channel A needs it"],
              ["Where does hospital_id come from?", "<b>The <font face='Courier'>doctors</font> "
                                                    "command.</b> Never hardcoded"],
              ["Is RECORDING_PROVISIONAL bad?", "<b>No</b> &mdash; it means recording. Our "
                                                "server never stops it afterwards"],
              ["<b>Another patient opened mid-recording?</b>", "<b>Accepted</b> "
                                        "(RECORDING_STARTED, continued). Send their API 2 as usual "
                                        "(11.2)"],
              ["Does prescription_built stop it?", "<b>No.</b> It arms the gate; the next patient "
                                                   "closes the consultation"],
              ["Can I send stop or pause?", "<b>No.</b> Those belong to the doctor"],
              ["Required in API 2?", "<font face='Courier'>demographics</font>, and "
                                     "<font face='Courier'>previous_visit</font> present (null is "
                                     "fine)"],
              ["Required in API 3 Channel B?", "<font face='Courier'>issued_at</font>, "
                                               "<font face='Courier'>items</font>, "
                                               "<font face='Courier'>diagnoses</font>, "
                                               "<font face='Courier'>investigations</font> "
                                               "&mdash; lists may be empty"],
              ["Out-of-range reading?", "Stored empty. It never rejects the message"],
              ["Extra fields?", "Kept. Never an error"],
              ["Is retrying safe?", "<b>Yes.</b> One row, and "
                                    "<font face='Courier'>ALREADY_RECEIVED</font> &mdash; then "
                                    "remove it from the outbox"],
              ["<b>Internet down at the branch?</b>", "<b>Keep both server messages in the "
                                                      "outbox</b> and send when it is back. "
                                                      "Late is fine (16)"],
              ["422?", "<b>Nothing lost.</b> Quarantined, with the bad fields named"],
              ["<b>Do you already hold the previous visit?</b>", "<b>Ask:</b> "
                                                                  "<font face='Courier'>GET "
                                                                  "/fhir/r4/Encounter?patient."
                                                                  "identifier=...</font>, and "
                                                                  "compare fingerprints (19)"],
              ["<b>Does ACCEPTED mean it worked?</b>", "<b>No &mdash; stored, not matched.</b> "
                                                    "Section 17"],
              ["What must I send AIMS LAB?", "<b>Your page's exact addresses, and your clinic "
                                             "codes.</b> Both blocking"],
              ["Can I start today?", "<b>Yes &mdash; the whole browser half.</b> Section 22"]],
             [54 * mm, 102 * mm], highlight=[3, 12, 20, 22, 23]))
# ============================================================ PART H
# The FHIR examples below are made by the reference client at build time and read
# back by the server's own mapping, so they cannot drift from the code.
S.append(PageBreak())
S.append(part("PART H", "Per-hospital tokens and FHIR R4, field by field",
              "Keys, the token, both server messages as FHIR, and every reply"))

S.append(Paragraph("26 &middot; One client per hospital &mdash; making and registering the key", H2))
S.append(Paragraph("For each hospital, <b>CMED</b> makes a key pair on its own server. The "
                   "private key never leaves it. CMED sends AIMS LAB the <b>public</b> key (a "
                   "JSON Web Key) and the hospital's CMED id; AIMS LAB registers it and returns "
                   "the <b>client id</b> and <b>key id</b> (<font face='Courier'>kid</font>). "
                   "Neither is secret.", BODY))
S.append(Spacer(1, 3))
S.append(Preformatted(
    '# CMED, once per hospital (any JOSE library works; this is the reference tool):\n'
    'python tools/cmed_fhir_client.py keygen --out keys/cmed-aalo-dholpur\n'
    '#   keys/cmed-aalo-dholpur.private.pem   <- stays on CMED\'s server, owner-only\n'
    '#   keys/cmed-aalo-dholpur.public.json   <- send this to AIMS LAB\n'
    '\n'
    '# AIMS LAB registers it (the admin key is read from AIMS_ADMIN_KEY, never typed):\n'
    'python tools/cmed_fhir_client.py register --server https://<aims-host> \\\n'
    '    --client-id cmed-aalo-dholpur --hospital AALO_DHOLPUR \\\n'
    '    --jwk keys/cmed-aalo-dholpur.public.json [--cidr <CMED server range>]\n'
    '# -> { "client_id": "cmed-aalo-dholpur", "kid": "<RFC 7638 thumbprint>",\n'
    '#      "cmed_hospital_ids": ["AALO_DHOLPUR"], "expires_at": "...", ... }', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Rule", "Value"],
              ["Algorithms accepted", "<b>ES384</b> (P-384, recommended), ES256 (P-256), RS384 "
                                      "(RSA, 2048 bits or more)"],
              ["Client id", "3 to 64 of <font face='Courier'>A-Z a-z 0-9 _ . -</font>; we suggest "
                            "<font face='Courier'>cmed-&lt;clinic&gt;</font>"],
              ["Hospitals per client", "normally <b>one</b>: the CMED hospital id sent as "
                                       "<font face='Courier'>hospital_id</font> in every message"],
              ["Address allow-list", "optional; when set, tokens are issued and accepted only "
                                     "from those addresses"],
              ["Client lifetime", "up to 1 year (default 365 days); AIMS LAB renews it"],
              ["<b>Rotation</b>", "send a new public key; we add it as a second "
                                  "<font face='Courier'>kid</font>; switch your signer; we "
                                  "revoke the old <font face='Courier'>kid</font>"],
              ["<b>Revocation</b>", "AIMS LAB revokes a client in one call: its keys and "
                                    "<b>every live token</b> stop at once"],
              ["A private key sent by mistake", "refused by our server, and must then be "
                                                "replaced: treat it as exposed"]],
             [44 * mm, 112 * mm], highlight=[6]))

S.append(CondPageBreak(110 * mm))
S.append(Paragraph("27 &middot; Getting a token", H2))
S.append(Paragraph("Sign a short JWT with the hospital's private key, then post it. The "
                   "assertion lives at most <b>5 minutes</b> and is accepted <b>once</b>; the "
                   "token lives <b>5 minutes</b>. Ask for a new token about 30 seconds before the "
                   "old one expires, not one per message.", BODY))
S.append(Spacer(1, 3))
S.append(Preformatted(
    'The assertion (JWT), signed ES384:\n'
    '  header  { "alg": "ES384", "kid": "<kid from registration>", "typ": "JWT" }\n'
    '  claims  { "iss": "cmed-aalo-dholpur",              <- the client id\n'
    '            "sub": "cmed-aalo-dholpur",              <- the same\n'
    '            "aud": "https://<aims-host>/api/v2/oauth/token",\n'
    '            "iat": 1791519300, "exp": 1791519540,   <- exp at most 5 minutes ahead\n'
    '            "jti": "7c3e0f0e-...-unique" }           <- new every time, 8-256 chars\n'
    '\n'
    'POST https://<aims-host>/api/v2/oauth/token\n'
    'Content-Type: application/x-www-form-urlencoded\n'
    '\n'
    'grant_type=client_credentials\n'
    '&scope=system/Bundle.c system/Encounter.rs\n'
    '&client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer\n'
    '&client_assertion=<the signed JWT>\n'
    '\n'
    '200 { "access_token": "cat_...", "token_type": "bearer", "expires_in": 300,\n'
    '      "scope": "system/Bundle.c system/Encounter.rs",\n'
    '      "hospital_ids": ["AALO_DHOLPUR"] }\n'
    '\n'
    '401 { "error": "invalid_client", "error_description": "this assertion was already used" }',
    CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Scope", "Allows", "Also accepted as"],
              ["<font face='Courier'>system/Bundle.c</font>", "API 2 and API 3b (SMART v2)",
               "<font face='Courier'>system/MessageHeader.c</font>, "
               "<font face='Courier'>system/*.write</font>, "
               "<font face='Courier'>system/*.cruds</font>, and the earlier "
               "<font face='Courier'>system/clinical.write</font>"],
              ["<font face='Courier'>system/Encounter.rs</font>", "the visits search and one "
                                                                 "Encounter",
               "<font face='Courier'>system/*.read</font>, "
               "<font face='Courier'>system/Encounter.read</font>"]],
             [46 * mm, 32 * mm, 78 * mm]))
S.append(Spacer(1, 3))
S.append(tbl([["Token refused", "Why"],
              ["400 unsupported_grant_type / invalid_request", "not client_credentials, or no "
                                                               "assertion"],
              ["400 invalid_client", "not a JWT, an algorithm other than the three, no "
                                     "<font face='Courier'>kid</font> (printable ASCII, up to "
                                     "128), or a bad client id; <b>\"clock skew\"</b> when "
                                     "<font face='Courier'>iat</font>/<font face='Courier'>exp"
                                     "</font> are more than 60 s off (fix NTP; not counted "
                                     "towards a lockout)"],
              ["<b>401 invalid_client</b>", "unknown, revoked or expired client or key; wrong "
                                            "signature; wrong <font face='Courier'>aud</font>; "
                                            "<font face='Courier'>sub</font> &ne; "
                                            "<font face='Courier'>iss</font>; expired; more "
                                            "than 5 minutes long; <b>jti already used</b>; "
                                            "address not allowed"],
              ["400 invalid_scope", "none of the scopes asked for is granted"],
              ["429 RATE_LIMITED", "more than 60 token requests a minute from one address, or "
                                   "that client locked out after 10 wrong assertions in 5 "
                                   "minutes"]],
             [56 * mm, 100 * mm]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Discovery.</b> <font face='Courier'>GET /fhir/r4/.well-known/"
                   "smart-configuration</font> (also at the host root) gives the token address, "
                   "<font face='Courier'>private_key_jwt</font> and the algorithms; "
                   "<font face='Courier'>GET /fhir/r4/metadata</font> is the FHIR "
                   "CapabilityStatement (FHIR 4.0.1, SMART-on-FHIR security, "
                   "<font face='Courier'>$process-message</font>, Encounter search). Neither "
                   "needs a token.", NOTE))

S.append(PageBreak())
S.append(Paragraph("28 &middot; How a message maps", H2))
S.append(Paragraph("Both server messages are a FHIR <b>message</b>: a Bundle of "
                   "<font face='Courier'>\"type\": \"message\"</font> whose <b>first entry is a "
                   "MessageHeader</b>. Its <font face='Courier'>eventCoding.code</font> says which "
                   "message it is, and its <font face='Courier'>focus</font> points at this "
                   "visit's Encounter. Resources reference each other by "
                   "<font face='Courier'>fullUrl</font> (<font face='Courier'>urn:uuid:...</font>) "
                   "inside the Bundle. Our server turns the Bundle into exactly the object of "
                   "sections 12 and 13 and applies the same rules.", BODY))
S.append(Spacer(1, 3))
S.append(tbl([["Event (eventCoding.code)", "Message", "Section"],
              ["<font face='Courier'>patient-information</font>", "API 2", "12, 29"],
              ["<font face='Courier'>prescription</font>", "API 3b", "13, 30"]],
             [60 * mm, 60 * mm, 36 * mm]))
S.append(Spacer(1, 3))
S.append(H3 and Paragraph("28.1 &nbsp; The five fields", H3))
S.append(tbl([["Field", "FHIR element"],
              ["<b>patient_id</b>", "Encounter.subject &rarr; Patient.identifier"],
              ["<b>doctor_id</b>", "Encounter.participant[].individual &rarr; Practitioner"
                                   ".identifier (a PractitionerRole pointing at one also works)"],
              ["<b>hospital_id</b>", "Encounter.serviceProvider &rarr; Organization.identifier "
                                     "&mdash; <b>must be a hospital the token speaks for</b>"],
              ["<b>start_time</b>", "Encounter.period.start &mdash; <b>date and time with its "
                                    "offset</b>, the moment the patient was opened. Any "
                                    "spelling of that moment matches: "
                                    "<font face='Courier'>04:14:32.000Z</font> and "
                                    "<font face='Courier'>10:14:32+06:00</font> are the same "
                                    "visit. We store it as clinic time, whole seconds, "
                                    "<font face='Courier'>+06:00</font>"],
              ["<b>date</b>", "not sent separately: the clinic-local date of period.start. (In "
                              "the interim JSON form, a date that contradicts start_time is "
                              "refused with 400)"]],
             [30 * mm, 126 * mm], req=[1, 2, 3, 4]))
S.append(Spacer(1, 2))
S.append(Paragraph("<b>Identifiers.</b> Our <b>proposed</b> systems are "
                   "<font face='Courier'>urn:aimscribe:cmed:patient</font>, "
                   "<font face='Courier'>urn:aimscribe:cmed:practitioner</font> and "
                   "<font face='Courier'>urn:aimscribe:cmed:hospital</font>; CMED's own URIs "
                   "are better, and we register them per hospital. A resource may carry several "
                   "identifiers (MRN, NID, health ID). We take, in order: the one with the "
                   "system registered for that hospital; the one with the proposed system; the "
                   "one marked <font face='Courier'>use: official</font>; the only one there "
                   "is. Several with none of those is refused, naming the element. "
                   "Values keep the rule of section 5: "
                   "<font face='Courier'>A-Z a-z 0-9 _ -</font>, up to 64.", NOTE))
S.append(Spacer(1, 3))
S.append(H3 and Paragraph("28.2 &nbsp; API 2 &mdash; patient information", H3))
S.append(tbl([["Field (section 12)", "FHIR"],
              ["demographics.name", "Patient.name[0].text (or given + family)"],
              ["demographics.sex", "Patient.gender: <font face='Courier'>female</font> / "
                                   "<font face='Courier'>male</font>. "
                                   "<font face='Courier'>other</font> or "
                                   "<font face='Courier'>unknown</font> is left out, not "
                                   "refused"],
              ["demographics.date_of_birth", "Patient.birthDate"],
              ["demographics.phone", "Patient.telecom (system phone)"],
              ["demographics.address", "Patient.address[0].text (or line, city, district)"],
              ["demographics.age_years", "Observation, LOINC <b>30525-0</b>, unit "
                                         "<font face='Courier'>a</font>"],
              ["paramedic.weight_kg", "Observation, LOINC <b>29463-7</b> (or 3141-9); kg, g or "
                                      "[lb_av]"],
              ["paramedic.height_cm", "Observation, LOINC <b>8302-2</b> (or 8306-3); cm, m, mm or "
                                      "[in_i]"],
              ["paramedic.blood_pressure", "Observation, LOINC <b>85354-9</b>, components "
                                           "<b>8480-6</b> (systolic) and <b>8462-4</b> "
                                           "(diastolic)"],
              ["paramedic.pulse_bpm", "Observation, LOINC <b>8867-4</b>"],
              ["paramedic.temperature_c", "Observation, LOINC <b>8310-5</b> (or 8331-1); Cel or "
                                          "[degF]"],
              ["paramedic.spo2_percent", "Observation, LOINC <b>59408-5</b> (or 2708-6)"],
              ["paramedic.recorded_at", "the first Observation's effectiveDateTime"],
              ["paramedic.notes", "Observation.note[].text (an Observation with only a note is "
                                  "a note)"],
              ["anything else measured", "any other Observation with a value: kept under its "
                                         "code text or display, as section 12.2 promises"],
              ["female_details / male_details", "Observations with category code "
                                                "<font face='Courier'>female-details</font> / "
                                                "<font face='Courier'>male-details</font>: "
                                                "code.text &rarr; value"],
              ["<b>previous_visit</b>", "<b>a second Encounter in the Bundle</b> (at most one): "
                                        "its own practitioner, organization and period.start. "
                                        "Its MedicationRequest, Condition, ServiceRequest and "
                                        "CarePlan point at it with "
                                        "<font face='Courier'>encounter</font>. None = null. A "
                                        "date-only period.start is the date-only shape (18)"]],
             [44 * mm, 112 * mm], highlight=[17]))
S.append(Spacer(1, 2))
S.append(Paragraph("Observations belong to this visit when their "
                   "<font face='Courier'>encounter</font> points at the focus Encounter, or "
                   "when they have none.", BODY))
S.append(Spacer(1, 3))
S.append(CondPageBreak(70 * mm))
S.append(H3 and Paragraph("28.3 &nbsp; API 3b &mdash; the prescription", H3))
S.append(tbl([["Field (section 13)", "FHIR"],
              ["items[]", "one <b>MedicationRequest</b> per medicine, in order"],
              ["items[].drug", "medicationCodeableConcept.text (or coding display/code, or a "
                               "Medication it references)"],
              ["items[].dose", "dosageInstruction[0].doseAndRate[0].doseQuantity "
                               "(value and unit)"],
              ["items[].frequency", "dosageInstruction[0].timing.code.text, e.g. "
                                    "<font face='Courier'>1+1+1</font> (or timing.repeat "
                                    "frequency/period)"],
              ["items[].duration", "timing.repeat.boundsDuration (or "
                                   "dispenseRequest.expectedSupplyDuration)"],
              ["items[].instructions", "patientInstruction, text, additionalInstruction"],
              ["<b>issued_at</b>", "the first MedicationRequest.authoredOn; else "
                                   "Bundle.timestamp"],
              ["diagnoses[]", "one <b>Condition</b> each: code.text"],
              ["investigations[]", "one <b>ServiceRequest</b> each: code.text"],
              ["advice", "<b>CarePlan</b>.description"],
              ["follow_up", "CarePlan.period.start (YYYY-MM-DD)"],
              ["notes", "CarePlan.note[].text"]],
             [44 * mm, 112 * mm]))
S.append(Spacer(1, 2))
S.append(Paragraph("<b>No Condition means no diagnoses</b> &mdash; in FHIR an empty list is "
                   "simply no resources, so the message always says exactly what the doctor "
                   "recorded. A MedicationRequest with no medicine name is "
                   "<b>422 SCHEMA_INVALID</b>, as in section 13.", NOTE))

S.append(PageBreak())
S.append(Paragraph("29 &middot; API 2 as a FHIR message &mdash; a complete example", H2))
S.append(Paragraph("Made by the reference client from the object below, and read back by our "
                   "server into exactly that object when this document was built. CMED's "
                   "output need not match it character for character; ids and order may differ.",
                   BODY))
S.append(Spacer(1, 2))
S.append(Paragraph("<b>What it carries</b> (the object of section 12)", BODY))
S.append(Preformatted(_compact(_API2), CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>The FHIR message CMED sends</b> &mdash; POST /fhir/r4/$process-message", BODY))
S.append(Preformatted(_compact(_MSG2), CODE))

S.append(PageBreak())
S.append(Paragraph("30 &middot; API 3b as a FHIR message &mdash; a complete example", H2))
S.append(Paragraph("<b>What it carries</b> (the object of section 13)", BODY))
S.append(Preformatted(_compact(_API3), CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>The FHIR message CMED sends</b> &mdash; POST /fhir/r4/$process-message", BODY))
S.append(Preformatted(_compact(_MSG3), CODE))

S.append(PageBreak())
S.append(Paragraph("31 &middot; The reply", H2))
S.append(Paragraph("A message that was read is answered with a FHIR <b>response message</b>: "
                   "a MessageHeader whose <font face='Courier'>response</font> names your "
                   "MessageHeader's id and says <font face='Courier'>ok</font> or "
                   "<font face='Courier'>fatal-error</font>; an OperationOutcome; and a "
                   "Parameters resource carrying the same facts as the JSON replies of "
                   "section 15. <b>The HTTP status is 200</b> for ACCEPTED and ALREADY_RECEIVED "
                   "(synchronous FHIR messaging); our own code is in "
                   "<font face='Courier'>Parameters.code</font>. Errors keep their status "
                   "(400, 401, 403, 413, 422, 429). Generated by the server's own code:", BODY))
S.append(Spacer(1, 3))
S.append(Preformatted(
    '200  Content-Type: application/fhir+json\n' + _compact(_REPLY) + '\n\n'
    '# API 3b adds { "name": "version", "valueInteger": n }. A refused-in-quarantine\n'
    '# message (422) adds "quarantine_id", and the OperationOutcome names each element\n'
    '# in issue.expression.', CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>A message that could not be read, or was not authorised</b>, is "
                   "answered with an <b>OperationOutcome</b> alone. Its code is in "
                   "<font face='Courier'>issue[0].details.coding[0].code</font>; the element at "
                   "fault, if any, in <font face='Courier'>issue[0].expression</font>.", BODY))
S.append(Spacer(1, 3))
S.append(Preformatted(
    '403 ' + _compact(_REFUSED) + '\n\n'
    '400 ... "code": "MALFORMED_FHIR", "expression": ["Encounter.serviceProvider"]\n\n'
    '401 (with WWW-Authenticate: Bearer error="invalid_token") ... "code": "INVALID_TOKEN"',
    CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Code", "HTTP", "Meaning", "What CMED should do"],
              ["ACCEPTED", "200", "stored", "remove from the outbox"],
              ["ALREADY_RECEIVED", "200", "already held", "remove; never send again"],
              ["MALFORMED_FHIR", "400", "the Bundle cannot be read", "fix the named element"],
              ["MISSING_FIELD / INVALID_IDENTIFIER", "400", "one of the five fields", "fix"],
              ["SCHEMA_INVALID", "422", "quarantined, not lost", "fix and resend; alert"],
              ["INVALID_TOKEN", "401", "token missing, unknown, expired", "new token once"],
              ["INSUFFICIENT_SCOPE", "403", "token lacks the scope", "ask for both scopes"],
              ["<b>HOSPITAL_NOT_ALLOWED</b>", "403", "<b>another hospital's message</b>",
               "use that hospital's client; raises an alert here"],
              ["TOO_LARGE", "413", "over 1 MB", "fix"],
              ["RATE_LIMITED", "429", "too fast, or locked out", "wait Retry-After"]],
             [52 * mm, 14 * mm, 46 * mm, 44 * mm], highlight=[1, 2], req=[8]))

S.append(CondPageBreak(80 * mm))
S.append(Paragraph("32 &middot; Working code to start from", H2))
S.append(tbl([["File", "What it is"],
              ["<font face='Courier'>cmed-web/src/lib/cmed-fhir.ts</font>",
               "<b>TypeScript / Node</b> (the <font face='Courier'>jose</font> library): signs "
               "the assertion, caches the token, builds both messages, reads the reply. It is "
               "what our own CMED test page uses"],
              ["<font face='Courier'>tools/cmed_fhir_client.py</font>",
               "<b>Python</b> (PyJWT): the same, plus key generation, registration and a token "
               "check from the command line"],
              ["<font face='Courier'>tools/channel_b_test.py</font>",
               "the 11 checks of section 22A against any host"]],
             [64 * mm, 92 * mm]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Both reference clients are tested against the server in our test "
                   "suite:</b> a message either makes is read back by our server into exactly "
                   "the object it was made from (the TypeScript test runs wherever Node is "
                   "installed). The examples in sections 19.1, 29, 30 and 31 are produced by "
                   "the server's own code every time this document is built, so this Part and "
                   "the code cannot disagree. Both clients reuse a token until 30 s before it "
                   "expires, renew once on 401, honour Retry-After, and back off with jitter.",
                   GOOD))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Bangladesh's national FHIR guide (BD Core).</b> The messages above are "
                   "plain FHIR R4 and use the resources and LOINC codes BD Core builds on. If "
                   "CMED already produces BD Core profiles, send them: extra profile tags and "
                   "extensions are ignored. Extra identifiers are fine as long as the one this "
                   "message is about can be told apart (section 28.1). Tell us your identifier "
                   "systems before testing and we register them per hospital.", NOTE))

S.append(Spacer(1, 6))
S.append(Paragraph("Prepared by AIMS LAB, United International University. <b>Document 1 of 2</b>; "
                   "the companion is \"Server Deployment\", for the team helping us deploy. Every "
                   "rule here is taken from the code that handles these messages. If anything is "
                   "unclear, ask before building &mdash; it is far cheaper than discovering it in "
                   "a clinic.", SUB))


_ap = argparse.ArgumentParser()
_ap.add_argument("--out", default="CMED_1_SOFTWARE_INTEGRATION.pdf")
OUT = Path(__file__).resolve().parent.parent / _ap.parse_args().out
build(S, OUT, "CMED to AIMS LAB - Software Integration (Document 1 of 2)",
      "AIMScribe v3 - software integration for CMED - document 1 of 2 - October 2026")
