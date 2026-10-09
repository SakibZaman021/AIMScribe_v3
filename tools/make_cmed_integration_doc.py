#!/usr/bin/env python3
"""
Build CMED_1_SOFTWARE_INTEGRATION.pdf - for CMED's software, DBMS and data team.

    python tools/make_cmed_integration_doc.py [--out NAME.pdf]

Everything about the APIs: the keys, the exact endpoints, every field, every
reply code, where the data lands, previous prescriptions, and how to test.
Nothing about hosting - that is the server team's document.

Sources, all read rather than remembered:
  recorder/api/protocol.py, websocket_server.py      the local channel
  backend/src/clinical.py, clinical_model.py         the two POST endpoints
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
from reportlab.platypus import Spacer, Paragraph, HRFlowable, PageBreak, Preformatted

from cmed_doc_style import (H1, SUB, H2, H3, BODY, NOTE, BAD, GOOD, CODE,
                            tbl, part, build)

S = []

# ============================================================ TITLE
S.append(Paragraph("CMED &rarr; AIMS LAB &mdash; Software Integration", H1))
S.append(Paragraph("<b>Document 1 of 2 &mdash; for CMED's software, database and data team</b> "
                   "&middot; The APIs, the keys, the fields and the data model &middot; "
                   "AIMScribe v3 &middot; AIMS LAB, United International University &middot; "
                   "9 October 2026", SUB))
S.append(HRFlowable(width="100%", thickness=1, color=__import__("cmed_doc_style").RULE,
                    spaceAfter=8))
S.append(tbl([["", "The two documents"],
              ["<b>1. This one</b>", "<b>Software integration</b> &mdash; the four APIs, the key, "
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
S.append(Paragraph("Every rule here is taken from the program that validates these messages on "
                   "arrival. <b>If this document and the software disagree, the software is right "
                   "and this is a bug</b> &mdash; please tell us.", BODY))

S.append(H3 and Paragraph("Contents", H3))
S.append(tbl([["Part", "Covers", "Sections"],
              ["<b>A</b>", "The keys and the exact endpoints &mdash; <b>start here</b>", "1&ndash;3"],
              ["<b>B</b>", "The five fields, and how the four messages relate", "4&ndash;5"],
              ["<b>C</b>", "The two browser signals &mdash; API 1 and API 3 Channel A", "6&ndash;11"],
              ["<b>D</b>", "The two server messages &mdash; API 2 and API 3 Channel B", "12&ndash;17"],
              ["<b>E</b>", "<b>Previous prescriptions</b> &mdash; today, and what we will add",
               "18&ndash;19"],
              ["<b>F</b>", "The data model &mdash; where every field lands", "20&ndash;21"],
              ["<b>G</b>", "Testing, handover, and one page to remember", "22&ndash;25"]],
             [14 * mm, 116 * mm, 26 * mm], highlight=[5]))

S.append(PageBreak())

# ============================================================ PART A
S.append(part("PART A", "The keys and the exact endpoints",
              "Copy these. Everything after this explains them"))

S.append(Paragraph("1 &middot; The gateway", H2))
S.append(Preformatted(
    'BASE_URL = https://<aims-host>          <-- AIMS LAB sends this with the key\n'
    '\n'
    'Transport    HTTPS only, TLS 1.2 or 1.3, port 443. HTTP redirects to HTTPS\n'
    'Encoding     JSON, UTF-8\n'
    'Body limit   1 MB\n'
    'Auth         X-CMED-Key: <key>     on the two clinical endpoints only', CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Hold the host in configuration, not compiled in.</b> You will get a "
                   "staging host first and a production host later, and they differ.", NOTE))

S.append(Paragraph("2 &middot; The authentication, in full", H2))
S.append(tbl([["", "Channel B &mdash; your server to ours", "Channel A &mdash; your page to the recorder"],
              ["<b>Credential</b>", "<b><font face='Courier'>X-CMED-Key</font> header</b>",
               "<b>none &mdash; and none is possible</b>"],
              ["Why", "Server to server, so a secret is safe",
               "A secret in a web page is readable by anyone who opens the page"],
              ["<b>What authorises you</b>", "the key",
               "<b>your page's exact web address</b>, on our allow-list, plus loopback-only, plus "
               "same-machine"],
              ["Issued by", "AIMS LAB, once, out of band. <b>Shown once and not recoverable</b>",
               "you send us the address; we register it"],
              ["Scope", "one key for CMED, covering all seven clinics", "one entry per address"],
              ["Rotation", "ask us &mdash; new key issued before the old is revoked, so there is "
                           "no window where both fail", "n/a"],
              ["<b>Where it must live</b>", "<b>your server's secret store or environment</b>",
               "n/a"]],
             [28 * mm, 60 * mm, 68 * mm], req=[1], highlight=[3]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>The key must never reach a browser</b> &mdash; not in page source, not in "
                   "a bundled script, not in a mobile app. A wrong or missing key returns "
                   "<b>401 INVALID_KEY</b>, which is a hard stop: do not retry it, alert your team "
                   "and contact us.", BAD))
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
              ["<b>2</b>", "<b>GET</b>", "<font face='Courier'>https://&lt;aims-host&gt;/</font>",
               "none", "none", "200"],
              ["<b>3</b>", "<b>POST</b>", "<font face='Courier'>https://&lt;aims-host&gt;"
                                          "/api/v2/clinical/patient-information</font>",
               "<b>key</b>", "JSON", "<b>202</b>"],
              ["<b>4</b>", "<b>POST</b>", "<font face='Courier'>https://&lt;aims-host&gt;"
                                          "/api/v2/clinical/prescription</font>",
               "<b>key</b>", "JSON", "<b>202</b>"]],
             [8 * mm, 16 * mm, 78 * mm, 18 * mm, 16 * mm, 14 * mm], highlight=[1], req=[3, 4]))
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
S.append(tbl([["All six, one line each, to copy"],
              ["<font face='Courier'>GET &nbsp;https://&lt;aims-host&gt;/health</font><br/>"
               "<font face='Courier'>GET &nbsp;https://&lt;aims-host&gt;/</font><br/>"
               "<font face='Courier'>POST https://&lt;aims-host&gt;/api/v2/clinical/"
               "patient-information</font><br/>"
               "<font face='Courier'>POST https://&lt;aims-host&gt;/api/v2/clinical/prescription"
               "</font><br/>"
               "<font face='Courier'>WS &nbsp;&nbsp;ws://127.0.0.1:5050/ws &nbsp;&rarr; \"start\""
               "</font><br/>"
               "<font face='Courier'>WS &nbsp;&nbsp;ws://127.0.0.1:5050/ws &nbsp;&rarr; "
               "\"prescription_built\"</font>"]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>There is no GET for clinical data and no DELETE anywhere.</b> CMED writes "
                   "to us and never reads back &mdash; see section 18, which is the one place that "
                   "will change. <b>Everything else under <font face='Courier'>/api/v2/</font> is "
                   "closed to CMED's key</b>; those endpoints belong to the recorders and use "
                   "client certificates.", GOOD))

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
    '  "previous_session_stopped": true,\n'
    '  "previous_session_id": "01M3MEYY...",\n'
    '  "authorisation": "granted",\n'
    '  "confirmation": "confirming"\n'
    '}', CODE))

S.append(PageBreak())
S.append(Paragraph("9 &middot; Every reply to API 1", H2))
S.append(tbl([["Code", "Meaning", "What CMED should do"],
              ["<b>RECORDING_STARTED</b> (200)", "Recording, permission confirmed",
               "<b>Store the session_id.</b> Show it is recording"],
              ["<b>RECORDING_PROVISIONAL</b> (202)", "<b>Recording.</b> Permission still being "
                                                     "checked",
               "<b>Treat as success.</b> Normal, not a warning"],
              ["SESSION_ALREADY_ACTIVE (409)", "This patient is already being recorded",
               "Nothing; you probably sent it twice"],
              ["GATE_NOT_ARMED (409)", "The previous consultation has no prescription yet",
               "Ask the doctor to build the previous prescription"],
              ["<b>AUTHORISATION_FAILED</b> (401)", "<b>Refused by the server; recording stopped "
                                                    "and deleted</b>",
               "<b>Show it is not recording.</b> Contact AIMS LAB"],
              ["CLINIC_MISMATCH (401)", "hospital_id is not this PC's clinic",
               "<b>Stop.</b> Use section 10 to avoid this entirely"],
              ["DOCTOR_NOT_AT_CLINIC (404)", "That doctor is not registered here", "Check doctor_id"],
              ["MISSING_FIELD (400)", "A field is missing or malformed", "Fix and resend"],
              ["INVALID_IDENTIFIER (400)", "An id has disallowed characters", "Fix and resend"],
              ["DEVICE_NOT_ENROLLED (423)", "This PC is not registered with AIMS LAB",
               "<b>Stop.</b> Contact AIMS LAB"],
              ["ORIGIN_NOT_ALLOWED (403)", "Your page's address is not on the allow-list",
               "<b>Send us the address.</b> Section 2"],
              ["AGENT_NOT_READY (503)", "The recorder is starting up", "Wait and retry"]],
             [46 * mm, 50 * mm, 60 * mm], highlight=[1, 2]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>RECORDING_PROVISIONAL is the one people get wrong.</b> The microphone is "
                   "live and audio is being written; the server has simply not finished confirming "
                   "permission, which normally takes a fraction of a second. Show the same "
                   "\"recording\" state as for RECORDING_STARTED. Do not warn and do not retry.",
                   NOTE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>But a provisional start can still fail afterwards.</b> If the server then "
                   "refuses it you receive <font face='Courier'>AUTHORISATION_FAILED</font> or "
                   "<font face='Courier'>DEVICE_NOT_ENROLLED</font> <b>a moment later, and the "
                   "recording is stopped and deleted</b>. Do not treat the first reply as final: "
                   "keep listening, and change the display if one of those arrives. The "
                   "<font face='Courier'>status</font> messages in section 11 are the reliable way "
                   "to show this.", BAD))

S.append(Paragraph("10 &middot; Never hardcode the clinic code &mdash; ask for it", H2))
S.append(Paragraph("Every AIMScribe laptop is enrolled to exactly one clinic, and a mismatch is "
                   "<b>refused</b>. <b>So do not store clinic codes in your page &mdash; read "
                   "them off the laptop on page load.</b>", BODY))
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
S.append(Paragraph("<b>This makes CLINIC_MISMATCH impossible rather than something to handle</b>, "
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
              ["<b>patient_id</b>", "<b>yes</b>", "the patient currently being recorded"],
              ["<b>session_id</b>", "<b>yes</b>", "<b>the one API 1 returned</b>"],
              ["occurred_at", "no", "date and time with offset"]],
             [30 * mm, 20 * mm, 106 * mm], req=[1, 2, 3]))
S.append(Spacer(1, 3))
S.append(tbl([["Code", "Meaning", "Do"],
              ["<b>GATE_ARMED</b> (200)", "Accepted", "nothing"],
              ["<b>GATE_ALREADY_ARMED</b> (200)", "Sent twice", "nothing; harmless"],
              ["PATIENT_MISMATCH (409)", "Not the patient being recorded", "check the session"],
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
               "next API 1, typically twenty to thirty seconds later."]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(tbl([["The doctor does", "CMED sends", "The recorder does"],
              ["Opens patient A", "API 1 + API 2", "starts recording A"],
              ["Consults", "&mdash;", "recording"],
              ["Presses Build Prescription", "<b>API 3, both channels</b>",
               "<b>keeps recording.</b> Arms the gate"],
              ["Prints, hands over, counsels", "&mdash;", "<b>still recording &mdash; the "
                                                          "valuable part</b>"],
              ["Opens patient B", "<b>API 1 + API 2</b>", "<b>closes A</b>, starts B"]],
             [48 * mm, 44 * mm, 64 * mm], highlight=[3, 5]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>If the doctor finishes for the day or goes to lunch</b>, they press Stop on "
                   "the recorder's own on-screen control. <b>CMED sends nothing, and must not try "
                   "to.</b> The socket also accepts <font face='Courier'>stop</font>, "
                   "<font face='Courier'>pause</font> and <font face='Courier'>resume</font>, but "
                   "<b>these are not CMED's to use</b>: each records a clinical reason that becomes "
                   "part of the permanent record, including \"the patient did not consent\", which "
                   "erases the recording everywhere. <b>Send only "
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
    '  "authorisation": "granted", "confirmation": "confirmed",\n'
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

# ============================================================ PART D
S.append(part("PART D", "The two server messages",
              "API 2 and API 3 Channel B - every field, every limit, every reply"))

S.append(Paragraph("12 &middot; API 2 &mdash; patient information", H2))
S.append(Preformatted(
    'POST https://<aims-host>/api/v2/clinical/patient-information\n'
    'X-CMED-Key: <key>\n'
    '\n'
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
    'POST https://<aims-host>/api/v2/clinical/prescription\n'
    'X-CMED-Key: <key>\n'
    '\n'
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
              ["Your network failed and you are unsure", "Send it again, unchanged",
               "<b>Completely safe.</b> <font face='Courier'>202 ALREADY_RECEIVED</font> and one "
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
              ["<b>202 ACCEPTED</b>", "Stored safely", "Nothing. Mark it sent"],
              ["<b>202 ALREADY_RECEIVED</b>", "This exact message was already stored",
               "Nothing. <b>Retrying is safe</b>"],
              ["400 MALFORMED_JSON", "Not valid JSON", "Fix and resend. Do not retry unchanged"],
              ["400 MISSING_FIELD", "One of the five is missing or malformed", "Fix and resend"],
              ["400 INVALID_IDENTIFIER", "An id has disallowed characters", "Fix and resend"],
              ["401 INVALID_KEY", "Key missing or wrong", "<b>Stop and contact AIMS LAB</b>"],
              ["413 TOO_LARGE", "Body over 1 MB", "Check for a file or image in the JSON"],
              ["<b>422 SCHEMA_INVALID</b>", "<b>Stored in quarantine</b>, with the bad fields "
                                            "named", "<b>Not lost.</b> Read the list, fix, resend"],
              ["500 / 503", "Our server has a problem", "<b>Retry with growing delay.</b> Never "
                                                        "drop the message"]],
             [40 * mm, 56 * mm, 60 * mm], highlight=[1, 2], req=[8]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>No rate limit applies to CMED.</b> Seven clinics at fourteen rooms produce "
                   "roughly one message every two seconds at peak. Send as consultations happen "
                   "&mdash; but from a queue.", GOOD))

S.append(Paragraph("16 &middot; Never let a message delay the doctor", H2))
S.append(Paragraph("<b>Put both endpoints on a durable background queue.</b> A slow network must "
                   "never make Build Prescription feel slow. If our server is briefly unreachable "
                   "the consultation still happened and the recording still exists &mdash; the "
                   "clinical record just needs to catch up.", BODY))
S.append(Spacer(1, 3))
S.append(tbl([["API 2 arrives...", "What happens to the recording"],
              ["within a second or two", "<b>Confirmed.</b> Nobody notices anything"],
              ["late &mdash; minutes, or an hour",
               "<b>Recording is never cut.</b> It keeps going, we keep asking, and it is confirmed "
               "the moment API 2 arrives"],
              ["<b>never</b>", "<b>Recording is still not cut.</b> It is uploaded and marked "
                               "<i>unconfirmed</i>, kept out of the dataset, and we are alerted. "
                               "<b>Deleted after 24 hours</b>"]],
             [40 * mm, 116 * mm], highlight=[1], req=[3]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>A message arriving an hour late is completely fine. A message never sent is "
                   "the only real failure.</b> Notice what never happens: a missing message does "
                   "not stop the doctor and does not cut a recording.", GOOD))

S.append(PageBreak())
S.append(Paragraph("17 &middot; The reply that looks like success but is not", H2))
S.append(tbl([["", "Proves", "Does NOT prove"],
              ["<font face='Courier'>200 RECORDING_STARTED</font>",
               "<b>The microphone is live</b> and audio is being written",
               "that the consultation will be kept"],
              ["<font face='Courier'>202 ACCEPTED</font>",
               "<b>Your message is stored</b> and will not be lost",
               "<b>that it matched a recording</b>"]],
             [48 * mm, 54 * mm, 54 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("There is a third step, on our server, a second or two later: <b>we compare the "
                   "five fields the recorder reported against the five in your message, and the "
                   "recording is only kept if they are identical.</b>", BODY))
S.append(Spacer(1, 3))
S.append(tbl([["<b>The failure that matters, stated plainly</b>"],
              ["If your <font face='Courier'>start_time</font> differs from API 1's by even a "
               "second, or loses its <font face='Courier'>+06:00</font>, or the clinic code came "
               "from your own settings instead of the laptop:<br/><br/>"
               "&nbsp;&nbsp;&bull; the recorder says <b>200 RECORDING_STARTED</b><br/>"
               "&nbsp;&nbsp;&bull; our server says <b>202 ACCEPTED</b><br/>"
               "&nbsp;&nbsp;&bull; <b>CMED sees nothing wrong at all</b><br/>"
               "&nbsp;&nbsp;&bull; and the recording is <b>deleted twenty-four hours later</b>"
               "<br/><br/>"
               "<b>Two green replies mean both halves arrived. Whether they found each other is a "
               "separate question CMED cannot see the answer to.</b> Section 22 is how to prove "
               "it."]],
             [156 * mm], bold_first=False))

S.append(PageBreak())

# ============================================================ PART E
S.append(part("PART E", "Previous prescriptions",
              "How history reaches us today, and the read API we will add"))

S.append(Paragraph("18 &middot; Today: history comes to us inside API 2", H2))
S.append(Paragraph("<b>There is no API for CMED to read previous prescriptions from us.</b> The "
                   "clinical surface is two POST endpoints and nothing else &mdash; data flows "
                   "CMED to AIMS LAB only. Previous prescription information reaches us as the "
                   "<font face='Courier'>previous_visit</font> object inside API 2.", BAD))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>The field must always be present.</b> Send "
                   "<font face='Courier'>null</font> when there is nothing to send. Leaving it out "
                   "is reported as a problem, because we cannot tell \"first visit\" from \"CMED "
                   "forgot\".", BODY))
S.append(Spacer(1, 3))
S.append(tbl([["The situation", "What to send"],
              ["The patient's first ever visit",
               "<font face='Courier'>\"previous_visit\": null</font>"],
              ["A returning patient, and <b>AIMS LAB may not hold</b> the previous visit",
               "<b>the full previous visit</b>, below"],
              ["A returning patient, and AIMS LAB <b>already holds</b> it",
               "<font face='Courier'>\"previous_visit\": null</font>"]],
             [78 * mm, 78 * mm], highlight=[2]))
S.append(Spacer(1, 3))
S.append(Preformatted(
    '"previous_visit": {\n'
    '  "date": "2026-06-02",                       <-- REQUIRED if you send it\n'
    '  "prescription": {\n'
    '    "items": [\n'
    '      { "drug": "Metformin 500 mg", "dose": "1 tablet",\n'
    '        "frequency": "1+0+1", "duration": "30 days",\n'
    '        "instructions": "after food" }\n'
    '    ],\n'
    '    "diagnoses": ["Type 2 diabetes mellitus"],\n'
    '    "notes": "review in one month"\n'
    '  }\n'
    '}', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Field", "Rule"],
              ["<b>date</b>", "<b>Required</b> if you send previous_visit at all. YYYY-MM-DD, the "
                              "date of that earlier consultation. <b>Without it the whole previous "
                              "visit is ignored</b>"],
              ["prescription", "Same shape as API 3 Channel B, section 13. Only "
                               "<font face='Courier'>items</font> really matters"],
              ["diagnoses, notes", "May sit inside <font face='Courier'>prescription</font> or "
                                   "beside it &mdash; both work"]],
             [30 * mm, 126 * mm], req=[1]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Sending it more often than necessary is completely safe.</b> A repeated "
                   "previous visit updates the one row we already have &mdash; nothing is "
                   "duplicated. <b>The dangerous direction is the other one</b>: deciding not to "
                   "send it when we do not actually hold it leaves a permanent hole in that "
                   "patient's history that nobody notices.", GOOD))
S.append(Spacer(1, 3))
S.append(tbl([["<b>Recommendation for the first release</b>"],
              ["<b>Send <font face='Courier'>previous_visit</font> every time, for every returning "
               "patient.</b> It costs about 2 KB per consultation. Because there is no read API "
               "yet, CMED has no way to know what we hold &mdash; so the only safe rule is to send "
               "it always. Switch to conditional sending once section 19 exists."]],
             [156 * mm], bold_first=False))

S.append(PageBreak())
S.append(Paragraph("19 &middot; The read API we will add, when you need it", H2))
S.append(Paragraph("You have said you will need to read previous prescriptions back in future. "
                   "<b>This is not built yet.</b> Here is the design, so your team can plan around "
                   "it and tell us if the shape is wrong before we write it.", NOTE))
S.append(Spacer(1, 4))
S.append(H3 and Paragraph("19.1 &nbsp; The light one, which solves the conditional rule", H3))
S.append(Preformatted(
    'PLANNED - not yet available\n'
    '\n'
    'GET https://<aims-host>/api/v2/clinical/patient/{patient_id}/last-visit\n'
    'X-CMED-Key: <key>\n'
    '\n'
    '200 {\n'
    '      "patient_id": "P0012345",\n'
    '      "held": true,\n'
    '      "last_visit_date": "2026-06-02",\n'
    '      "prescription_versions": 2\n'
    '    }\n'
    '\n'
    '200 { "patient_id": "P0099999", "held": false }     <-- we hold nothing', CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>This is the one that matters most.</b> One cheap call before sending API 2 "
                   "tells CMED whether we already hold the previous visit, which turns the "
                   "three-way rule in section 18 from a guess into a decision &mdash; and removes "
                   "the need to send 2 KB of history on every single consultation.", GOOD))
S.append(Spacer(1, 4))
S.append(H3 and Paragraph("19.2 &nbsp; The full one, for reading history back", H3))
S.append(Preformatted(
    'PLANNED - not yet available\n'
    '\n'
    'GET https://<aims-host>/api/v2/clinical/patient/{patient_id}/history?limit=5\n'
    'X-CMED-Key: <key>\n'
    '\n'
    '200 {\n'
    '  "patient_id": "P0012345",\n'
    '  "count": 2,\n'
    '  "visits": [\n'
    '    {\n'
    '      "date": "2026-10-09",\n'
    '      "doctor_id": "DR0042",\n'
    '      "hospital_id": "AALO_DHOLPUR",\n'
    '      "issued_at": "2026-10-09T10:26:55+06:00",\n'
    '      "version": 1,\n'
    '      "diagnoses": ["Acute gastritis"],\n'
    '      "investigations": ["CBC", "HbA1c"],\n'
    '      "advice": "avoid spicy food",\n'
    '      "follow_up": "2026-11-09",\n'
    '      "items": [\n'
    '        { "line_no": 1, "drug": "Napa 500 mg", "dose": "1 tablet",\n'
    '          "frequency": "1+1+1", "duration": "5 days",\n'
    '          "instructions": "after food" }\n'
    '      ]\n'
    '    },\n'
    '    { "date": "2026-06-02", "...": "..." }\n'
    '  ]\n'
    '}', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Parameter", "Meaning", "Default"],
              ["<font face='Courier'>limit</font>", "how many visits back, newest first", "5, max 50"],
              ["<font face='Courier'>from</font>, <font face='Courier'>to</font>",
               "restrict to a date range", "none"],
              ["<font face='Courier'>hospital_id</font>", "only visits at one clinic", "all"]],
             [34 * mm, 82 * mm, 40 * mm]))
S.append(Spacer(1, 4))
S.append(tbl([["What it will and will not return"],
              ["<b>Will:</b> the prescriptions, diagnoses, investigations, advice, follow-up dates "
               "and medicine lines CMED itself sent us, newest first, with their version "
               "numbers.<br/>"
               "<b>Will not:</b> <b>any audio, ever</b>, and no transcript. Those are not served "
               "to CMED under any circumstances, and no parameter will change that.<br/>"
               "<b>Will not:</b> another clinic's patients &mdash; the key is scoped to CMED's own "
               "hospitals."]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(tbl([["", "Before we build it, we need from CMED", "Why"],
              ["<b>1</b>", "Confirmation that the shape above is what you want",
               "Cheap to change now, expensive later"],
              ["<b>2</b>", "<b>Whether you need 19.1, 19.2, or both</b>",
               "<b>19.1 is a day's work; 19.2 is a few days.</b> If the conditional rule is the "
               "only need, 19.1 alone is enough"],
              ["<b>3</b>", "Roughly how often you would call it",
               "A per-consultation call and a nightly batch are sized differently"],
              ["<b>4</b>", "Whether a doctor's own notes should be included",
               "Those are clinical text we hold; it is a policy question, not a technical one"]],
             [8 * mm, 68 * mm, 80 * mm], highlight=[2]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Until this exists, section 18's recommendation stands: send "
                   "<font face='Courier'>previous_visit</font> every time.</b> It is the only rule "
                   "that cannot lose history.", BAD))

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
              ["<b>previous_visit</b>", "<font face='Courier'>encounters</font>, marked as a "
                                        "previous visit", "a second row, dated to that earlier day"],
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
               "<b>Available today.</b> Connect, <font face='Courier'>doctors</font>, API 1, the "
               "gate, API 3 Channel A"],
              ["<b>2</b>", "Check a server message against the rules", "nothing at all",
               "<font face='Courier'>python tools/channel_b_test.py --local</font>"],
              ["<b>3</b>", "<b>The server half against a live host</b>", "<b>a staging hostname "
                                                                         "from us</b>",
               "<font face='Courier'>channel_b_test.py --server https://&lt;host&gt; --key &lt;key&gt;"
               "</font>"],
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

S.append(Paragraph("22A &middot; Four commands that test the server half, before you write code", H2))
S.append(Paragraph("These need no application and no page. Run them as soon as you have the "
                   "staging hostname and the key.", BODY))
S.append(Spacer(1, 3))
S.append(H3 and Paragraph("Is it up?", H3))
S.append(Preformatted(
    'curl -s https://<aims-host>/health | jq .\n'
    '# expect: "status": "healthy"  AND  "system": "AIMScribe v3"', CODE))
S.append(H3 and Paragraph("Is my key accepted? Send a patient", H3))
S.append(Preformatted(
    'curl -s -o /dev/null -w "%{http_code}\\n" \\\n'
    '  -X POST https://<aims-host>/api/v2/clinical/patient-information \\\n'
    '  -H "X-CMED-Key: $CMED_KEY" \\\n'
    '  -H "Content-Type: application/json" \\\n'
    '  -d \'{\n'
    '        "patient_id":  "TEST0001",\n'
    '        "doctor_id":   "DR0042",\n'
    '        "hospital_id": "AALO_DHOLPUR",\n'
    '        "start_time":  "2026-10-09T10:14:32+06:00",\n'
    '        "date":        "2026-10-09",\n'
    '        "demographics": { "name": "Test Patient", "sex": "female",\n'
    '                          "age_years": 34 },\n'
    '        "previous_visit": null\n'
    '      }\'\n'
    '# expect: 202', CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Send that exact command twice.</b> Both times you get <b>202</b> &mdash; "
                   "the second is <font face='Courier'>ALREADY_RECEIVED</font>. That proves "
                   "retries are safe, which is the most useful thing to know before writing your "
                   "queue.", GOOD))
S.append(H3 and Paragraph("Does validation work? Break it deliberately", H3))
S.append(Preformatted(
    '# previous_visit omitted on purpose, and sex is a code rather than a word\n'
    'curl -s -X POST https://<aims-host>/api/v2/clinical/patient-information \\\n'
    '  -H "X-CMED-Key: $CMED_KEY" -H "Content-Type: application/json" \\\n'
    '  -d \'{ "patient_id":"TEST0002", "doctor_id":"DR0042",\n'
    '        "hospital_id":"AALO_DHOLPUR",\n'
    '        "start_time":"2026-10-09T10:20:00+06:00", "date":"2026-10-09",\n'
    '        "demographics": { "name":"Test", "sex":"F" } }\' | jq .\n'
    '# expect: 422 SCHEMA_INVALID, naming demographics.sex and previous_visit', CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>The reply names the bad fields.</b> Build against that &mdash; it is faster "
                   "than reading this document, and the record is quarantined rather than lost, so "
                   "nothing is wasted by experimenting.", GOOD))
S.append(H3 and Paragraph("Send the prescription", H3))
S.append(Preformatted(
    'curl -s -o /dev/null -w "%{http_code}\\n" \\\n'
    '  -X POST https://<aims-host>/api/v2/clinical/prescription \\\n'
    '  -H "X-CMED-Key: $CMED_KEY" -H "Content-Type: application/json" \\\n'
    '  -d \'{\n'
    '        "patient_id":  "TEST0001",\n'
    '        "doctor_id":   "DR0042",\n'
    '        "hospital_id": "AALO_DHOLPUR",\n'
    '        "start_time":  "2026-10-09T10:14:32+06:00",   <-- SAME as API 2\n'
    '        "date":        "2026-10-09",\n'
    '        "issued_at":   "2026-10-09T10:26:55+06:00",\n'
    '        "items": [ { "drug": "Napa 500 mg", "dose": "1 tablet",\n'
    '                     "frequency": "1+1+1", "duration": "5 days" } ],\n'
    '        "diagnoses":      ["Acute gastritis"],\n'
    '        "investigations": []\n'
    '      }\'\n'
    '# expect: 202', CODE))
S.append(Spacer(1, 4))
S.append(tbl([["The one thing these four commands cannot prove"],
              ["All four can pass while the integration is still broken, because <b>a 202 means "
               "\"stored\", not \"matched to a recording\"</b>. If "
               "<font face='Courier'>start_time</font> differs by a second between API 1 and API 2, "
               "or loses its offset, every reply above is still green and <b>the recording is "
               "deleted twenty-four hours later</b>.<br/><br/>"
               "<b>So the real test needs a recorder running and our confirmation</b> &mdash; "
               "steps 5 and 6 of section 22. Please do not treat four green curls as integration "
               "complete."]],
             [156 * mm], bold_first=False))

S.append(PageBreak())
S.append(Paragraph("23 &middot; A complete worked example", H2))
S.append(Preformatted(
    'let socket, sessionId = null, currentVisit = null, CLINIC = null;\n'
    '\n'
    'function connect() {\n'
    '  socket = new WebSocket("ws://127.0.0.1:5050/ws");\n'
    '\n'
    '  socket.onopen = () => {\n'
    '    // Read the clinic code off THIS laptop. Never hardcode it.\n'
    '    socket.send(JSON.stringify({ command: "doctors" }));\n'
    '  };\n'
    '\n'
    '  socket.onmessage = (e) => {\n'
    '    const m = JSON.parse(e.data);\n'
    '\n'
    '    if (m.event === "status") { showRecordingState(m); return; }\n'
    '\n'
    '    if (m.command === "doctors") {\n'
    '      CLINIC = m.data.hospital_id;            // may be null = not enrolled\n'
    '      if (!CLINIC) return showProblem("This PC is not registered");\n'
    '      showClinicName(CLINIC);                 // so a wrong PC is obvious\n'
    '      fillDoctorSelector(m.data.doctors);     // [] is normal - do not block\n'
    '      return;\n'
    '    }\n'
    '\n'
    '    if (m.command === "start") {\n'
    '      if (m.code === "RECORDING_STARTED" ||\n'
    '          m.code === "RECORDING_PROVISIONAL") {\n'
    '        sessionId = m.data.session_id;        // keep it - API 3a needs it\n'
    '        showRecording(true);\n'
    '      } else {\n'
    '        showProblem(m.code);                  // act on code, not message\n'
    '      }\n'
    '    }\n'
    '  };\n'
    '\n'
    '  socket.onclose = () => { showRecording(false); setTimeout(connect, 2000); };\n'
    '  socket.onerror = () => showProblem("AIMScribe is not running on this PC");\n'
    '}\n'
    '\n'
    '// The doctor opens a patient.\n'
    '// Build the five fields ONCE, here, and reuse this object everywhere.\n'
    'function openPatient(patient, doctor) {\n'
    '  currentVisit = {\n'
    '    patient_id:  patient.id,\n'
    '    doctor_id:   doctor.id,\n'
    '    hospital_id: CLINIC,                 // from the recorder, not settings\n'
    '    start_time:  new Date().toISOString(),\n'
    '    date:        localDateString()       // the clinic\'s local day\n'
    '  };\n'
    '\n'
    '  socket.send(JSON.stringify({\n'
    '    command: "start",\n'
    '    request_id: "open-" + currentVisit.patient_id,\n'
    '    trigger: currentVisit                // the SAME object\n'
    '  }));\n'
    '\n'
    '  queueToOurBackend("API2", currentVisit, demographics, paramedic,\n'
    '                    previousVisit);      // never inline - always queued\n'
    '}\n'
    '\n'
    '// The doctor presses Build Prescription. This does NOT stop recording.\n'
    'function prescriptionBuilt(prescription) {\n'
    '  socket.send(JSON.stringify({\n'
    '    command: "prescription_built",\n'
    '    request_id: "built-" + currentVisit.patient_id,\n'
    '    patient_id: currentVisit.patient_id,\n'
    '    session_id: sessionId\n'
    '  }));\n'
    '\n'
    '  // currentVisit is reused, so start_time is the ORIGINAL. This is the\n'
    '  // single most important line in the integration.\n'
    '  queueToOurBackend("API3B", currentVisit, prescription);\n'
    '}\n'
    '\n'
    'connect();', CODE))

S.append(PageBreak())
S.append(Paragraph("24 &middot; What each side must hand over", H2))
S.append(tbl([["AIMS LAB gives CMED", "When"],
              ["The staging and production hostnames", "with the key"],
              ["<b>The <font face='Courier'>X-CMED-Key</font></b> &mdash; shown once",
               "on request, out of band"],
              ["The seven clinic codes", "with the key"],
              ["<font face='Courier'>channel_b_test.py</font>", "with the key"],
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
               "record is stored, you receive a normal 202, and the recording is erased after 24 "
               "hours.</b> The failure is completely silent from CMED's side"],
              ["A named technical contact", "So a question either way does not wait a week"],
              ["A staging environment we can send to", "So we can test our side against yours"]],
             [62 * mm, 94 * mm], req=[1, 2]))

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
              ["Where do I POST, server side?", "<font face='Courier'>/api/v2/clinical/"
                                                "patient-information</font> and "
                                                "<font face='Courier'>/api/v2/clinical/prescription"
                                                "</font>"],
              ["How do I authenticate there?", "<font face='Courier'>X-CMED-Key</font>. Server to "
                                               "server only, never in a browser"],
              ["What do I keep from API 1?", "<b><font face='Courier'>session_id</font></b> "
                                             "&mdash; API 3 Channel A needs it"],
              ["Where does hospital_id come from?", "<b>The <font face='Courier'>doctors</font> "
                                                    "command.</b> Never hardcoded"],
              ["Is RECORDING_PROVISIONAL bad?", "<b>No</b> &mdash; it means recording. But keep "
                                                "listening; it can still be refused"],
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
                                    "<font face='Courier'>ALREADY_RECEIVED</font>"],
              ["422?", "<b>Nothing lost.</b> Quarantined, with the bad fields named"],
              ["<b>Can I read previous prescriptions back?</b>", "<b>Not yet &mdash; no read API "
                                                                 "exists.</b> Send "
                                                                 "<font face='Courier'>"
                                                                 "previous_visit</font> every time. "
                                                                 "Section 19 is the planned design"],
              ["<b>Does a 202 mean it worked?</b>", "<b>No &mdash; stored, not matched.</b> "
                                                    "Section 17"],
              ["What must I send AIMS LAB?", "<b>Your page's exact addresses, and your clinic "
                                             "codes.</b> Both blocking"],
              ["Can I start today?", "<b>Yes &mdash; the whole browser half.</b> Section 22"]],
             [54 * mm, 102 * mm], highlight=[3, 19, 20]))
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
