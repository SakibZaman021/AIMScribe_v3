#!/usr/bin/env python3
"""
Build CMED_1_SOFTWARE_INTEGRATION.pdf - for CMED's software, DBMS and data team.

    python tools/make_cmed_integration_doc.py [--out NAME.pdf]

Everything about the APIs: the keys, the exact endpoints, every field, every
reply code, where the data lands, previous prescriptions, and how to test.
Nothing about hosting - that is the server team's document.

Sources, all read rather than remembered:
  recorder/api/protocol.py, websocket_server.py      the local channel
  backend/src/clinical.py, clinical_model.py         the POSTs and the visits GET
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

from cmed_doc_style import (H1, SUB, H2, H3, BODY, NOTE, BAD, GOOD, CODE,
                            tbl, part, build)
from cmed_diagrams import architecture, sequence, gate, five_fields

S = []

# ============================================================ TITLE
S.append(Paragraph("CMED &rarr; AIMS LAB &mdash; Software Integration", H1))
S.append(Paragraph("<b>Document 1 of 2 &mdash; for CMED's software, database and data team</b> "
                   "&middot; The APIs, the keys, the fields and the data model &middot; "
                   "AIMScribe v3 &middot; AIMS LAB, United International University &middot; "
                   "16 October 2026", SUB))
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
               "<b>key</b>", "JSON", "<b>202</b>"],
              ["<b>5</b>", "<b>GET</b>", "<font face='Courier'>https://&lt;aims-host&gt;"
                                         "/api/v2/clinical/patient/{patient_id}/visits</font>",
               "<b>key</b>", "none", "200"]],
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
S.append(tbl([["All seven, one line each, to copy"],
              ["<font face='Courier'>GET &nbsp;https://&lt;aims-host&gt;/health</font><br/>"
               "<font face='Courier'>GET &nbsp;https://&lt;aims-host&gt;/</font><br/>"
               "<font face='Courier'>POST https://&lt;aims-host&gt;/api/v2/clinical/"
               "patient-information</font><br/>"
               "<font face='Courier'>POST https://&lt;aims-host&gt;/api/v2/clinical/prescription"
               "</font><br/>"
               "<font face='Courier'>GET &nbsp;https://&lt;aims-host&gt;/api/v2/clinical/patient/"
               "{patient_id}/visits</font><br/>"
               "<font face='Courier'>WS &nbsp;&nbsp;ws://127.0.0.1:5050/ws &nbsp;&rarr; \"start\""
               "</font><br/>"
               "<font face='Courier'>WS &nbsp;&nbsp;ws://127.0.0.1:5050/ws &nbsp;&rarr; "
               "\"prescription_built\"</font>"]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>One GET, and it returns no clinical content.</b> The visits list "
                   "(section 19) gives identifiers and a fingerprint of each prescription, so CMED "
                   "can ask what we hold before sending history &mdash; never a name, a medicine "
                   "or a diagnosis. <b>There is no DELETE anywhere.</b> <b>Everything else under "
                   "<font face='Courier'>/api/v2/</font> is closed to CMED's key</b>; those "
                   "endpoints belong to the recorders and need each recorder's own device "
                   "credential.", GOOD))

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
              ["<b>202 ACCEPTED</b>", "Stored safely", "<b>Remove it from the outbox.</b> For "
                                                         "API 2, read <font face='Courier'>"
                                                         "previous_visit.status</font> (19.2)"],
              ["<b>200 ALREADY_RECEIVED</b>", "Already held: this exact message, or (API 3) the "
                                              "same prescription by fingerprint",
               "<b>Remove it and never send it again.</b> Retrying was safe"],
              ["400 MALFORMED_JSON", "Not valid JSON", "Fix and resend. Do not retry unchanged"],
              ["400 MISSING_FIELD", "One of the five is missing or malformed", "Fix and resend"],
              ["400 INVALID_IDENTIFIER", "An id has disallowed characters", "Fix and resend"],
              ["401 INVALID_KEY", "Key missing or wrong", "<b>Stop and contact AIMS LAB</b>"],
              ["413 TOO_LARGE", "Body over 1 MB", "Check for a file or image in the JSON"],
              ["<b>422 SCHEMA_INVALID</b>", "<b>Stored in quarantine</b>, with the bad fields "
                                            "named", "<b>Not lost.</b> Read the list, fix, resend; "
                                                     "alert your team"],
              ["429, 500, 502, 503, a timeout, no connection", "Busy, a problem on our side, or "
                                                               "no internet",
               "<b>Keep it in the outbox and retry with growing delay.</b> Never drop it"]],
             [40 * mm, 56 * mm, 60 * mm], highlight=[1, 2], req=[8]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>No rate limit applies to CMED.</b> Seven clinics at fourteen rooms produce "
                   "roughly one message every two seconds at peak. Send as consultations happen "
                   "&mdash; but from the outbox in section 16.", GOOD))

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
              ["<b>202 ACCEPTED</b>", "<b>remove</b> the message"],
              ["<b>200 ALREADY_RECEIVED</b>", "<b>remove it and never send it again</b> &mdash; "
                                              "we already hold it"],
              ["400 MALFORMED_JSON / MISSING_FIELD / INVALID_IDENTIFIER",
               "park it for a person to fix; <b>do not retry unchanged</b>; alert"],
              ["<b>422 SCHEMA_INVALID</b>", "stored in quarantine on our side, so not lost; "
                                            "park it, fix the shape, send the corrected one; "
                                            "alert"],
              ["401 INVALID_KEY", "<b>stop sending everything</b> and alert &mdash; the key is "
                                  "wrong or revoked; retrying cannot help"],
              ["413 TOO_LARGE", "park it; something too big is in the body; fix and resend"],
              ["429, 5xx, timeout, no connection", "<b>keep it and retry later</b>"]],
             [62 * mm, 94 * mm], highlight=[1, 2], req=[7]))
S.append(Spacer(1, 4))
S.append(tbl([["API 2 arrives...", "What happens to the recording"],
              ["within a second or two", "Its clinical record is attached at once"],
              ["late &mdash; minutes, hours or days, after an outage",
               "<b>Attached when it arrives.</b> The recording was recorded, uploaded and archived "
               "meanwhile; nothing has a deadline"],
              ["<b>never</b>", "<b>The recording is still kept and archived</b>, marked "
                               "<i>unconfirmed</i>, and AIMS LAB is alerted. It simply has no "
                               "clinical record &mdash; which is why a message must never be "
                               "dropped"]],
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
              ["<font face='Courier'>202 ACCEPTED</font>",
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
               "&nbsp;&nbsp;&bull; our server says <b>202 ACCEPTED</b><br/>"
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
    'GET https://<aims-host>/api/v2/clinical/patient/{patient_id}/visits?limit=50\n'
    'X-CMED-Key: <key>\n'
    '\n'
    '200 { "status": 200, "code": "OK", "patient_id": "P0012345",\n'
    '      "visits": [\n'
    '        { "patient_id": "P0012345", "doctor_id": "DR0007",\n'
    '          "hospital_id": "AALO_DHOLPUR",\n'
    '          "start_time": "2026-06-02T09:40:05+06:00", "date": "2026-06-02",\n'
    '          "source": "live", "prescription_version": 2,\n'
    '          "prescription_fingerprint": "f1e71094...ec4806",\n'
    '          "received_at": "2026-06-02T03:58:11.204+00:00" } ],\n'
    '      "next_cursor": null }', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Parameter / reply", "Meaning"],
              ["<font face='Courier'>limit</font>", "1 to 200, default 50. Newest first"],
              ["<font face='Courier'>cursor</font>", "When a page is full, "
                                                     "<font face='Courier'>next_cursor</font> is "
                                                     "the value to pass as "
                                                     "<font face='Courier'>cursor</font> for the "
                                                     "next page, unchanged"],
              ["<font face='Courier'>before</font>", "Optional, YYYY-MM-DD: only visits before "
                                                     "that date"],
              ["a patient we hold nothing for", "<font face='Courier'>\"visits\": []</font>"],
              ["errors", "401 INVALID_KEY; 400 INVALID_IDENTIFIER; 400 MISSING_FIELD naming "
                         "<font face='Courier'>limit</font> or <font face='Courier'>before</font>"]],
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
    '202 { "status": 202, "code": "ACCEPTED", "record_id": "5121",\n'
    '      "previous_visit": { "status": "ALREADY_STORED", "version": 2, ... } }', CODE))
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
              ["PENDING", "stored safely, still loading", "nothing; it completes by itself"]],
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

S.append(Paragraph("22A &middot; Five commands that test the server half, before you write code", H2))
S.append(Paragraph("These need no application and no page. Run them as soon as you have the "
                   "staging hostname and the key.", BODY))
S.append(Spacer(1, 3))
S.append(H3 and Paragraph("Is it up?", H3))
S.append(Preformatted(
    'curl -s https://<aims-host>/health | jq .\n'
    '# expect: "status": "healthy"\n'
    '#   and   "system" STARTS WITH "AIMScribe v3"\n'
    '#         (today it reads "AIMScribe v3 - AIMS LAB server")', CODE))
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
S.append(Paragraph("<b>Send that exact command twice.</b> The first gets <b>202</b>, the "
                   "second <b>200</b> &mdash; "
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
S.append(H3 and Paragraph("What do you hold for this patient?", H3))
S.append(Preformatted(
    'curl -s -H "X-CMED-Key: $CMED_KEY" \\\n'
    '  https://<aims-host>/api/v2/clinical/patient/TEST0001/visits | jq .\n'
    '# expect: 200, "code": "OK", the visits held, each with a fingerprint\n'
    '# without the key: 401 INVALID_KEY', CODE))
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
S.append(tbl([["The one thing these commands cannot prove"],
              ["All of them can pass while the integration is still broken, because <b>a 202 "
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
               "record is stored and you receive a normal 202, but it is never attached to its "
               "recording.</b> Silent from CMED's side; we are alerted"],
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
                                                                  "<font face='Courier'>GET .../"
                                                                  "visits</font>, and compare "
                                                                  "fingerprints (19)"],
              ["<b>Does a 202 mean it worked?</b>", "<b>No &mdash; stored, not matched.</b> "
                                                    "Section 17"],
              ["What must I send AIMS LAB?", "<b>Your page's exact addresses, and your clinic "
                                             "codes.</b> Both blocking"],
              ["Can I start today?", "<b>Yes &mdash; the whole browser half.</b> Section 22"]],
             [54 * mm, 102 * mm], highlight=[3, 11, 19, 21, 22]))
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
