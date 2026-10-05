#!/usr/bin/env python3
"""
Build CMED_BROWSER_SPECIFICATION.pdf - API 1 and API 3a, the two browser signals.

    python tools/make_cmed_browser_spec.py

Every rule is taken from the code that handles the connection
(recorder/api/protocol.py and websocket_server.py). No secrets, no data.
"""
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, HRFlowable, PageBreak, Preformatted)

INK = colors.HexColor("#0b0b0b")
TEAL = colors.HexColor("#17836f")
MUTED = colors.HexColor("#898781")
RULE = colors.HexColor("#e1e0d9")
BAND = colors.HexColor("#eef3f2")
CODEBG = colors.HexColor("#f7f8f7")
WARNC = colors.HexColor("#7a5200")
CRIT = colors.HexColor("#97292a")
GOODC = colors.HexColor("#006300")
WIN = colors.HexColor("#e7f6e7")
REQ = colors.HexColor("#fdf3dc")

ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Title"], fontName="Helvetica-Bold",
                    fontSize=19, textColor=INK, spaceAfter=2, alignment=0, leading=23)
SUB = ParagraphStyle("SUB", parent=ss["Normal"], fontSize=9.5, textColor=MUTED, spaceAfter=9)
H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontName="Helvetica-Bold",
                    fontSize=13, textColor=TEAL, spaceBefore=15, spaceAfter=5, leading=16)
H3 = ParagraphStyle("H3", parent=ss["Heading3"], fontName="Helvetica-Bold",
                    fontSize=10.5, textColor=INK, spaceBefore=10, spaceAfter=3)
BODY = ParagraphStyle("BODY", parent=ss["Normal"], fontSize=9.4, textColor=INK, leading=13.6)
NOTE = ParagraphStyle("NOTE", parent=BODY, textColor=WARNC)
BAD = ParagraphStyle("BAD", parent=BODY, textColor=CRIT)
GOOD = ParagraphStyle("GOOD", parent=BODY, textColor=GOODC)
CELL = ParagraphStyle("CELL", parent=ss["Normal"], fontSize=8.3, textColor=INK, leading=11.0)
MONO = ParagraphStyle("MONO", parent=ss["Normal"], fontName="Courier",
                      fontSize=7.8, textColor=INK, leading=10.8)
LBL = ParagraphStyle("LBL", parent=ss["Normal"], fontName="Helvetica-Bold",
                     fontSize=8.3, textColor=INK, leading=11.0)
CODE = ParagraphStyle("CODE", parent=ss["Code"], fontName="Courier", fontSize=7.5,
                      textColor=INK, leading=10.0, backColor=CODEBG,
                      borderPadding=6, leftIndent=0, spaceBefore=3, spaceAfter=3)


def tbl(rows, widths, mono=(), bold_first=True, highlight=None, req=None):
    data = [[Paragraph("<b>%s</b>" % h, CELL) for h in rows[0]]]
    for r in rows[1:]:
        data.append([Paragraph(str(c), MONO if i in mono else (LBL if (i == 0 and bold_first) else CELL))
                     for i, c in enumerate(r)])
    t = Table(data, colWidths=widths, hAlign="LEFT")
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("TEXTCOLOR", (0, 0), (-1, 0), TEAL),
        ("GRID", (0, 0), (-1, -1), 0.35, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafbfa")]),
    ]
    for row in (highlight or []):
        style.append(("BACKGROUND", (0, row), (-1, row), WIN))
    for row in (req or []):
        style.append(("BACKGROUND", (0, row), (-1, row), REQ))
    t.setStyle(TableStyle(style))
    return t


S = []
S.append(Paragraph("The Two Browser Signals &mdash; API 1 and API 3a", H1))
S.append(Paragraph("What CMED's page sends to the recorder on the same PC &middot; "
                   "AIMScribe v3 &middot; 5 October 2026", SUB))
S.append(HRFlowable(width="100%", thickness=1, color=RULE, spaceAfter=8))
S.append(Paragraph("This is the companion to <b>\"What CMED Sends AIMS LAB\"</b>, which covers "
                   "the two server-to-server messages. This document covers the two signals the "
                   "<b>browser</b> sends. Every rule is taken from the code that handles the "
                   "connection.", BODY))
S.append(Spacer(1, 5))
S.append(tbl([["The idea in one paragraph"],
              ["AIMScribe runs as a small program on <b>the same laptop</b> as the doctor's "
               "browser. CMED's page opens a connection to it over the computer's own internal "
               "network and sends two short messages: <b>a patient has been opened</b>, and "
               "<b>the prescription has been built</b>. That is all. The page never sees audio, "
               "never handles a recording, and never needs a password to do this."]],
             [156 * mm], bold_first=False))

# ================================================================ 1
S.append(Paragraph("1 &middot; How the page connects", H2))
S.append(Spacer(1, 2))
S.append(Preformatted('ws://127.0.0.1:5050/ws', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["", "Detail"],
              ["Address", "<font face='Courier'>ws://127.0.0.1:5050/ws</font> &mdash; the same on "
                          "every clinic PC. Never a name, never another machine"],
              ["Protocol", "A WebSocket. Messages are JSON, UTF-8"],
              ["Largest message", "64 KB"],
              ["<b>Password</b>", "<b>None.</b> See below &mdash; this surprises people"],
              ["When to connect", "When the doctor's page loads. Keep it open; reconnect if it drops"]],
             [34 * mm, 122 * mm]))
S.append(Spacer(1, 4))
S.append(H3 and Paragraph("1.1 &nbsp; Why there is no password, and what replaces it", H3))
S.append(Paragraph("A password in a web page is not a secret &mdash; anyone can read it in the "
                   "page source. So the recorder does not use one. It checks three things "
                   "instead, all of which a browser sets itself and a page cannot forge:", BODY))
S.append(Spacer(1, 3))
S.append(tbl([["", "What is checked", "What it stops"],
              ["1", "<b>The exact web address your page is served from</b> (its \"origin\"), "
                    "against a list AIMS LAB holds",
               "Any other website on the internet from driving the recorder"],
              ["2", "The address the connection was made to",
               "A trick where a hostile site makes your own browser connect for it"],
              ["3", "<b>The connection comes from this same computer</b>",
               "Any other machine on the network, full stop"]],
             [7 * mm, 76 * mm, 73 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>So the one thing CMED must give AIMS LAB for this to work is the exact "
                   "web address of your page</b> &mdash; for example "
                   "<font face='Courier'>https://ehr.aaloclinic.com</font>. Every address you "
                   "will use, including staging and test sites. An address not on the list is "
                   "refused with <font face='Courier'>ORIGIN_NOT_ALLOWED</font> and the "
                   "connection never opens.", BAD))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>It must match exactly.</b> "
                   "<font face='Courier'>https://ehr.aaloclinic.com</font> and "
                   "<font face='Courier'>https://www.ehr.aaloclinic.com</font> are two different "
                   "addresses. So are <font face='Courier'>http://</font> and "
                   "<font face='Courier'>https://</font>, and so is a different port number.", NOTE))

# ================================================================ 2
S.append(Paragraph("2 &middot; The shape of every message and every reply", H2))
S.append(H3 and Paragraph("What CMED sends", H3))
S.append(Preformatted(
    '{\n'
    '  "command":    "start",              <-- which signal\n'
    '  "request_id": "abc-123",            <-- anything you like, up to 128 characters\n'
    '  ...                                 <-- the fields for that command\n'
    '}', CODE))
S.append(H3 and Paragraph("What comes back &mdash; always exactly one reply per message", H3))
S.append(Preformatted(
    '{\n'
    '  "event":      "ack",                 <-- "ack" if it worked, "error" if not\n'
    '  "command":    "start",               <-- the command you sent\n'
    '  "status":     200,                   <-- like an HTTP status\n'
    '  "code":       "RECORDING_STARTED",   <-- ACT ON THIS\n'
    '  "message":    "Recording started.",  <-- wording for a human; may change\n'
    '  "request_id": "abc-123",             <-- your id, echoed back\n'
    '  "data":       { ... }                <-- extra detail, per command\n'
    '}', CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Act on <font face='Courier'>code</font>, never on "
                   "<font face='Courier'>message</font>.</b> The wording is for showing a person "
                   "and may be reworded at any time; the code is a promise and will not change. "
                   "Use <font face='Courier'>request_id</font> to match a reply to the message "
                   "that caused it.", GOOD))

S.append(PageBreak())

# ================================================================ 3
S.append(Paragraph("3 &middot; API 1 &mdash; a patient has been opened", H2))
S.append(Paragraph("Send this the instant the doctor opens a patient. <b>The microphone starts "
                   "immediately</b>, before any permission check finishes, so nothing of the "
                   "consultation is missed.", BODY))
S.append(Spacer(1, 3))
S.append(Preformatted(
    '{\n'
    '  "command": "start",\n'
    '  "request_id": "open-P0012345-1",\n'
    '  "trigger": {\n'
    '    "patient_id":  "P0012345",\n'
    '    "doctor_id":   "DR0042",\n'
    '    "hospital_id": "AALO_DHOLPUR",\n'
    '    "start_time":  "2026-10-05T10:14:32+06:00",\n'
    '    "date":        "2026-10-05"\n'
    '  }\n'
    '}', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Field", "Required", "Rule"],
              ["command", "<b>yes</b>", "the word <font face='Courier'>start</font>"],
              ["request_id", "no", "any text up to 128 characters, echoed back"],
              ["<b>trigger</b>", "<b>yes</b>", "an object holding the five fields"],
              ["trigger.patient_id", "<b>yes</b>", "letters, digits, hyphen, underscore. 1&ndash;64. No spaces"],
              ["trigger.doctor_id", "<b>yes</b>", "same rule"],
              ["trigger.hospital_id", "<b>yes</b>", "same rule. <b>Must be the clinic code AIMS LAB gives you</b>"],
              ["trigger.start_time", "<b>yes</b>", "date and time <b>with the offset</b>, e.g. +06:00"],
              ["trigger.date", "<b>yes</b>", "YYYY-MM-DD, the clinic's local date"]],
             [38 * mm, 20 * mm, 98 * mm], req=[1, 3, 4, 5, 6, 7, 8]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>These five values must be identical to the ones in API 2</b>, the "
                   "server-to-server message you send at the same moment. They are what ties the "
                   "recording to the clinical record. Generate them once and use the same "
                   "variables for both.", BAD))
S.append(Spacer(1, 4))
S.append(H3 and Paragraph("What comes back", H3))
S.append(Preformatted(
    '"data": {\n'
    '  "session_id": "01M3MK4J4XEDG5NDKE4H5B5XA2",   <-- KEEP THIS\n'
    '  "started_at": "2026-10-05T10:14:32.865Z",\n'
    '  "armed": false,\n'
    '  "previous_session_stopped": true,\n'
    '  "previous_session_id": "01M3MEYY...",\n'
    '  "authorisation": "granted",\n'
    '  "confirmation": "confirming"\n'
    '}', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Code you may receive", "Meaning", "What CMED should do"],
              ["<b>RECORDING_STARTED</b> (200)", "Recording, and permission confirmed",
               "<b>Store the session_id.</b> Show the doctor it is recording"],
              ["<b>RECORDING_PROVISIONAL</b> (202)", "<b>Recording.</b> Permission is still being "
                                                     "checked with the server",
               "<b>Treat exactly like success.</b> This is normal, not a warning"],
              ["SESSION_ALREADY_ACTIVE (409)", "This same patient is already being recorded",
               "Nothing. You probably sent it twice"],
              ["GATE_NOT_ARMED (409)", "The previous consultation has not had its prescription "
                                       "built yet", "Ask the doctor to build the prescription "
                                                    "for the previous patient first"],
              ["<b>AUTHORISATION_FAILED</b> (401)", "<b>The server refused this recording, so "
                                                    "it was stopped and deleted</b>",
               "<b>Show the doctor it is not recording.</b> Contact AIMS LAB"],
              ["CLINIC_MISMATCH (401)", "hospital_id is not the clinic this PC belongs to",
               "<b>Stop.</b> A configuration problem &mdash; contact AIMS LAB"],
              ["DOCTOR_NOT_AT_CLINIC (404)", "That doctor is not registered at this clinic",
               "Check the doctor_id"],
              ["MISSING_FIELD (400)", "A field is missing or malformed", "Fix and resend"],
              ["INVALID_IDENTIFIER (400)", "An id has characters that are not allowed",
               "Fix and resend"],
              ["DEVICE_NOT_ENROLLED (423)", "This PC has not been registered with AIMS LAB",
               "<b>Stop.</b> Contact AIMS LAB"],
              ["AGENT_NOT_READY (503)", "The recorder is starting up", "Wait a moment and retry"]],
             [46 * mm, 52 * mm, 58 * mm], highlight=[1, 2]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>RECORDING_PROVISIONAL is the one people get wrong.</b> It means the "
                   "microphone is live and audio is being captured &mdash; the server simply has "
                   "not finished confirming permission yet, which normally takes a fraction of a "
                   "second. Show the doctor the same \"recording\" state you would for "
                   "RECORDING_STARTED. Do not show a warning, and do not retry.", NOTE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>But a provisional start can still fail afterwards.</b> If the server then "
                   "refuses it, you receive <font face='Courier'>AUTHORISATION_FAILED</font> or "
                   "<font face='Courier'>DEVICE_NOT_ENROLLED</font> <b>a moment later, and the "
                   "recording is stopped and deleted</b>. So do not treat the first reply as "
                   "final: keep listening, and if one of those arrives, change what the doctor "
                   "sees from \"recording\" to \"not recording\". The <font face='Courier'>status"
                   "</font> messages in section 6 are the reliable way to show this.", BAD))

S.append(PageBreak())

# ================================================================ 4
S.append(Paragraph("4 &middot; API 3a &mdash; the prescription has been built", H2))
S.append(Paragraph("Send this the moment <b>Build Prescription</b> succeeds, at the same time as "
                   "the server sends API 3b.", BODY))
S.append(Spacer(1, 3))
S.append(Preformatted(
    '{\n'
    '  "command": "prescription_built",\n'
    '  "request_id": "built-P0012345-1",\n'
    '  "patient_id": "P0012345",\n'
    '  "session_id": "01M3MK4J4XEDG5NDKE4H5B5XA2"\n'
    '}', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Field", "Required", "Rule"],
              ["command", "<b>yes</b>", "the words <font face='Courier'>prescription_built</font>"],
              ["request_id", "no", "any text, echoed back"],
              ["<b>patient_id</b>", "<b>yes</b>", "must be the patient currently being recorded"],
              ["<b>session_id</b>", "<b>yes</b>", "<b>the one API 1 gave you</b> for this consultation"],
              ["occurred_at", "no", "date and time with offset, if you want to record it"]],
             [34 * mm, 20 * mm, 102 * mm], req=[1, 3, 4]))
S.append(Spacer(1, 4))
S.append(tbl([["Code", "Meaning", "What CMED should do"],
              ["<b>GATE_ARMED</b> (200)", "Accepted", "Nothing"],
              ["<b>GATE_ALREADY_ARMED</b> (200)", "You sent it twice", "Nothing. Harmless"],
              ["PATIENT_MISMATCH (409)", "The patient named is not the one being recorded",
               "Check you used the right session"],
              ["NO_ACTIVE_SESSION (409)", "Nothing is being recorded right now",
               "The consultation already ended"]],
             [46 * mm, 52 * mm, 58 * mm], highlight=[1, 2]))

# ================================================================ 5
S.append(Paragraph("5 &middot; The most important idea in this document", H2))
S.append(tbl([["<b>Building the prescription does NOT stop the recording.</b>"],
              ["The doctor builds the prescription, prints it, hands it to the patient &mdash; "
               "and then <b>keeps talking</b>. That counselling, the part where the doctor "
               "explains how to take the medicine and what to watch for, is some of the most "
               "valuable audio in the whole study.<br/><br/>"
               "So <font face='Courier'>prescription_built</font> does not end anything. It "
               "<b>arms a gate</b>: it tells the recorder \"this consultation has reached its "
               "end, so the next patient is allowed to close it\".<br/><br/>"
               "<b>The recording actually stops when the next patient is opened</b> &mdash; "
               "when your next API 1 arrives, typically twenty to thirty seconds later."]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(tbl([["What the doctor does", "What CMED sends", "What the recorder does"],
              ["Opens patient A", "API 1 (A)", "Starts recording A"],
              ["Consults", "&mdash;", "Recording"],
              ["Presses Build Prescription", "<b>API 3a (A)</b>", "<b>Keeps recording.</b> Arms "
                                                                  "the gate"],
              ["Prints it, hands it over, counsels", "&mdash;", "<b>Still recording</b> &mdash; "
                                                                "this is the valuable part"],
              ["Opens patient B", "<b>API 1 (B)</b>", "<b>Closes A</b>, starts recording B"]],
             [48 * mm, 40 * mm, 68 * mm], highlight=[3, 5]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>What if the doctor finishes for the day, or goes to lunch?</b> There is a "
                   "Stop button on the recorder's own small on-screen control, which the doctor "
                   "presses. <b>CMED does not need to send anything</b>, and must not try to: "
                   "stopping is a clinical decision with a reason attached, and it belongs to the "
                   "doctor, not to the page.", NOTE))

S.append(PageBreak())

# ================================================================ 6
S.append(Paragraph("6 &middot; Messages the recorder sends you, unprompted", H2))
S.append(Paragraph("As soon as the connection opens, and whenever something changes, the recorder "
                   "pushes a status message. You do not have to use it &mdash; but it is how you "
                   "show the doctor what is really happening.", BODY))
S.append(Spacer(1, 3))
S.append(Preformatted(
    '{\n'
    '  "event": "status",\n'
    '  "state": "recording",            <-- idle | recording | paused | closing\n'
    '  "is_recording": true,\n'
    '  "is_paused": false,\n'
    '  "session_id": "01M3MK4J...",\n'
    '  "patient_ref": "P0012345",\n'
    '  "armed": false,                  <-- has the prescription signal arrived?\n'
    '  "authorisation": "granted",\n'
    '  "confirmation": "confirmed",\n'
    '  "duration_seconds": 182.4,\n'
    '  "level": 0.42,                   <-- 0.0 to 1.0, how loud the room is\n'
    '  "connected_clients": 1\n'
    '}', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Useful for", "Which field", "Suggested use"],
              ["\"Is it recording?\"", "<font face='Courier'>is_recording</font>",
               "A red dot on the page"],
              ["<b>\"Is the microphone working?\"</b>", "<font face='Courier'>level</font>",
               "<b>A small meter.</b> A dead microphone sits at 0.0 and is otherwise invisible "
               "until the recording is played back"],
              ["\"How long so far?\"", "<font face='Courier'>duration_seconds</font>", "A timer"],
              ["\"Has the prescription signal landed?\"", "<font face='Courier'>armed</font>",
               "A tick beside Build Prescription"]],
             [44 * mm, 42 * mm, 70 * mm], highlight=[2]))

# ================================================================ 7
S.append(Paragraph("7 &middot; Commands CMED must not send", H2))
S.append(Paragraph("The connection also accepts <font face='Courier'>stop</font>, "
                   "<font face='Courier'>pause</font> and <font face='Courier'>resume</font>. "
                   "<b>These are not CMED's to use.</b> They belong to the doctor, through the "
                   "recorder's own on-screen control, because each one records a clinical reason "
                   "that becomes part of the permanent record &mdash; including \"the patient did "
                   "not consent\", which erases the recording everywhere.", BAD))
S.append(Spacer(1, 3))
S.append(Paragraph("The only two commands CMED should ever send are "
                   "<font face='Courier'>start</font> and "
                   "<font face='Courier'>prescription_built</font>.", BODY))

# ================================================================ 8
S.append(Paragraph("8 &middot; Rules that are easy to miss", H2))
S.append(tbl([["", "Rule", "Why"],
              ["1", "<b>The five fields in API 1 must match API 2 exactly</b>",
               "They are the only link between the recording and the clinical record"],
              ["2", "<b>Keep the session_id</b> that API 1 returns",
               "API 3a needs it. Without it you cannot arm the gate"],
              ["3", "<b>Treat RECORDING_PROVISIONAL as success</b>",
               "The microphone is already live. It is not a warning"],
              ["4", "<b>prescription_built does not stop anything</b>",
               "The counselling after it is valuable audio"],
              ["5", "Act on <font face='Courier'>code</font>, never on "
                    "<font face='Courier'>message</font>", "The wording may change; the code will not"],
              ["6", "<b>Reconnect if the connection drops</b>, and reconnect quietly",
               "A dropped socket does not stop a recording in progress"],
              ["7", "<b>Give AIMS LAB every web address you will use</b>",
               "Staging and preview addresses are refused unless listed"],
              ["8", "<b>Never send stop, pause or resume</b>", "Those belong to the doctor"],
              ["9", "If the connection will not open at all, say \"AIMScribe is not running\"",
               "It usually means the program is not started on that PC"]],
             [7 * mm, 70 * mm, 79 * mm], bold_first=False))

S.append(PageBreak())

# ================================================================ 9
S.append(Paragraph("9 &middot; A complete working example", H2))
S.append(Paragraph("This is the whole integration. Everything else in this document is detail.", BODY))
S.append(Spacer(1, 3))
S.append(Preformatted(
    'let socket, sessionId = null;\n'
    '\n'
    'function connect() {\n'
    '  socket = new WebSocket("ws://127.0.0.1:5050/ws");\n'
    '\n'
    '  socket.onmessage = (e) => {\n'
    '    const m = JSON.parse(e.data);\n'
    '\n'
    '    if (m.event === "status") { showRecordingState(m); return; }\n'
    '\n'
    '    if (m.command === "start") {\n'
    '      if (m.code === "RECORDING_STARTED" || m.code === "RECORDING_PROVISIONAL") {\n'
    '        sessionId = m.data.session_id;      // keep it - API 3a needs it\n'
    '        showRecording(true);\n'
    '      } else {\n'
    '        showProblem(m.code);                // act on the code, not the message\n'
    '      }\n'
    '    }\n'
    '  };\n'
    '\n'
    '  socket.onclose = () => { showRecording(false); setTimeout(connect, 2000); };\n'
    '  socket.onerror = () => showProblem("AIMScribe is not running on this PC");\n'
    '}\n'
    '\n'
    '// The doctor opens a patient. Build the five fields ONCE and reuse them.\n'
    'function openPatient(visit) {\n'
    '  socket.send(JSON.stringify({\n'
    '    command: "start",\n'
    '    request_id: "open-" + visit.patient_id,\n'
    '    trigger: {\n'
    '      patient_id:  visit.patient_id,\n'
    '      doctor_id:   visit.doctor_id,\n'
    '      hospital_id: visit.hospital_id,\n'
    '      start_time:  visit.start_time,   // SAME object sent to API 2\n'
    '      date:        visit.date\n'
    '    }\n'
    '  }));\n'
    '  sendPatientInformationFromYourServer(visit);   // API 2\n'
    '}\n'
    '\n'
    '// The doctor presses Build Prescription.\n'
    'function prescriptionBuilt(visit) {\n'
    '  socket.send(JSON.stringify({\n'
    '    command: "prescription_built",\n'
    '    request_id: "built-" + visit.patient_id,\n'
    '    patient_id: visit.patient_id,\n'
    '    session_id: sessionId\n'
    '  }));\n'
    '  sendPrescriptionFromYourServer(visit);         // API 3b, same start_time\n'
    '}\n'
    '\n'
    'connect();', CODE))

# ================================================================ 10
S.append(Paragraph("10 &middot; One page for the CMED developer", H2))
S.append(tbl([["Question", "Answer"],
              ["Where do I connect?", "<font face='Courier'>ws://127.0.0.1:5050/ws</font> "
                                      "&mdash; the same PC, every time"],
              ["Do I need a password?", "<b>No.</b> You need your page's web address on our "
                                        "allow-list instead"],
              ["What must I give AIMS LAB?", "<b>The exact address of your page</b>, including "
                                             "staging"],
              ["How many commands?", "<b>Two.</b> <font face='Courier'>start</font> and "
                                     "<font face='Courier'>prescription_built</font>"],
              ["What do I keep from the reply?", "<b>session_id</b> &mdash; API 3a needs it"],
              ["Is RECORDING_PROVISIONAL bad?", "<b>No.</b> It means recording. Treat it as success"],
              ["Does prescription_built stop it?", "<b>No.</b> It arms the gate. The next patient "
                                                   "closes the consultation"],
              ["What stops a recording then?", "The next API 1, or the doctor's own Stop button"],
              ["Can I send stop or pause?", "<b>No.</b> Those belong to the doctor"],
              ["What if the socket drops?", "Reconnect quietly. The recording continues regardless"],
              ["What if it will not connect?", "Show \"AIMScribe is not running\". The program is "
                                               "probably not started"],
              ["Act on which field?", "<font face='Courier'>code</font>, never "
                                      "<font face='Courier'>message</font>"],
              ["Biggest trap", "<b>The five fields must be identical in API 1 and API 2</b>"]],
             [50 * mm, 106 * mm]))
S.append(Spacer(1, 6))
S.append(Paragraph("Prepared by AIMS LAB, United International University. Companion document: "
                   "\"What CMED Sends AIMS LAB\", covering API 2 and API 3b.", SUB))


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7.3)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 9.5 * mm,
                      "AIMScribe v3 - the two browser signals - October 2026")
    canvas.drawRightString(A4[0] - 18 * mm, 9.5 * mm, "page %d" % doc.page)
    canvas.restoreState()


OUT = Path(__file__).resolve().parent.parent / "CMED_BROWSER_SPECIFICATION.pdf"
SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                  topMargin=16 * mm, bottomMargin=20 * mm,
                  title="The Two Browser Signals - AIMScribe v3",
                  author="AIMS LAB").build(S, onFirstPage=footer, onLaterPages=footer)
print("wrote", OUT)
