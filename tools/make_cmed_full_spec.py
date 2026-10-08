#!/usr/bin/env python3
"""
Build CMED_INTEGRATION_SPECIFICATION.pdf - the complete CMED contract in one
document: both browser signals and both server messages.

    python tools/make_cmed_full_spec.py

This supersedes CMED_SPECIFICATION.pdf and CMED_BROWSER_SPECIFICATION.pdf; it
carries all of their content with continuous numbering and the sections that
were duplicated across the two merged into one.

Every rule is taken from the code that handles or validates the messages:
  recorder/api/protocol.py, recorder/api/websocket_server.py  (browser)
  backend/src/clinical.py, clinical_model.py                  (server)
  backend/scripts/clinical/001_aims_clinical.sql              (storage)
No secrets, no project data.
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
PARTBG = colors.HexColor("#17836f")

ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Title"], fontName="Helvetica-Bold",
                    fontSize=20, textColor=INK, spaceAfter=2, alignment=0, leading=24)
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
PARTT = ParagraphStyle("PARTT", parent=ss["Normal"], fontName="Helvetica-Bold",
                       fontSize=15, textColor=colors.white, leading=19)
PARTS = ParagraphStyle("PARTS", parent=ss["Normal"], fontSize=9,
                       textColor=colors.HexColor("#cfe6e0"), leading=12)


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


def part(label, title, blurb):
    """A full-width banner opening one part of the document."""
    inner = Table([[Paragraph(label, PARTS)], [Paragraph(title, PARTT)],
                   [Paragraph(blurb, PARTS)]], colWidths=[150 * mm], hAlign="LEFT")
    inner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PARTBG),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (0, 0), 8), ("BOTTOMPADDING", (0, -1), (-1, -1), 9),
        ("TOPPADDING", (0, 1), (-1, -1), 1),
    ]))
    return inner


S = []

# ============================================================ TITLE
S.append(Paragraph("The CMED Integration Specification", H1))
S.append(Paragraph("Everything CMED builds, in one document &mdash; the two browser signals and "
                   "the two server messages &middot; AIMScribe v3 &middot; AIMS LAB, United "
                   "International University &middot; 5 October 2026", SUB))
S.append(HRFlowable(width="100%", thickness=1, color=RULE, spaceAfter=8))
S.append(tbl([["The whole integration in one paragraph"],
              ["AIMScribe runs as a small program on <b>the same laptop</b> as the doctor's "
               "browser. CMED's page connects to it over the computer's own internal "
               "network &mdash; no internet involved &mdash; and tells it when a patient is "
               "opened and when the prescription is built, so the recorder knows where one "
               "consultation ends and the next begins. "
               "Separately, <b>CMED's server</b> sends us the patient's information and the "
               "prescription, so the recording has a clinical record attached to it. "
               "<b>Audio never goes to CMED, and nothing clinical ever comes back to CMED.</b>"]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 5))
S.append(Paragraph("Every rule in this document is taken from the program that actually handles "
                   "or checks these messages when they arrive. <b>If this document and the "
                   "software ever disagree, the software is right and this is a bug</b> &mdash; "
                   "please tell us.", BODY))

S.append(H3 and Paragraph("What is in here", H3))
S.append(tbl([["Part", "Covers", "Who builds it", "Sections"],
              ["<b>A</b>", "How it all fits together, and the five fields that tie it together",
               "read this first", "1&ndash;2"],
              ["<b>B</b>", "<b>The two browser signals</b> &mdash; API 1 and API 3 on Channel A",
               "CMED's front-end developer", "3&ndash;9"],
              ["<b>C</b>", "<b>The two server messages</b> &mdash; API 2 and API 3 on Channel B",
               "CMED's back-end developer", "10&ndash;15"],
              ["<b>D</b>", "Where the data lands, how to test, what each side hands over",
               "both, and the project leads", "16&ndash;21"]],
             [14 * mm, 76 * mm, 38 * mm, 24 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>A front-end developer needs Parts A, B and D. A back-end developer needs "
                   "Parts A, C and D.</b> Nobody needs to read all of it to start.", GOOD))
S.append(Spacer(1, 5))
S.append(tbl([["Three things that cause almost every integration failure"],
              ["<b>1. The five fields must be identical in all four messages</b> (section 2). "
               "Above all, <font face='Courier'>start_time</font> in the prescription messages is "
               "the time the <b>patient was opened</b>, not the time the prescription was built."],
              ["<b>2. Building the prescription does not stop the recording</b> (section 7). The "
               "counselling afterwards is valuable audio. The consultation closes when the "
               "<b>next</b> patient is opened."],
              ["<b>3. CMED must give AIMS LAB two things</b> (section 20): the exact web address "
               "of your page, and your own clinic codes. Without the second one a recording is "
               "<b>silently erased after 24 hours</b> while CMED sees nothing wrong."]],
             [156 * mm], bold_first=False))

S.append(PageBreak())

# ============================================================ PART A
S.append(part("PART A", "How it all fits together",
              "The four messages, and the five fields that tie them to one consultation"))

# ---------------------------------------------------------- 1
S.append(Paragraph("1 &middot; Three APIs &mdash; and why they are four messages", H2))
S.append(tbl([["", "What", "From where", "When", "Carries", "Part"],
              ["<b>API 1</b>", "Start recording", "the browser", "the doctor opens a patient",
               "the five fields", "B"],
              ["<b>API 2</b>", "<b>Patient information</b>", "<b>CMED's server</b>",
               "the same moment", "demographics, paramedic readings, previous visit", "C"],
              ["<b>API 3</b><br/>Channel A", "Prescription built", "the browser",
               "the doctor presses Build Prescription", "the patient and session id", "B"],
              ["<b>API 3</b><br/>Channel B", "<b>The prescription</b>", "<b>CMED's server</b>",
               "the same moment", "medicines, diagnoses, investigations", "C"]],
             [15 * mm, 29 * mm, 25 * mm, 34 * mm, 42 * mm, 11 * mm], highlight=[2, 4]))
S.append(Spacer(1, 4))
S.append(Spacer(1, 4))
S.append(tbl([["On the naming &mdash; there are three APIs, not four"],
              ["CMED's existing integration guide numbers these <b>API 1, API 2 and API 3</b>, "
               "and that numbering is correct and unchanged. <b>API 3 is one event</b> &mdash; "
               "the prescription has been built &mdash; but it has to be told to <b>two "
               "different places</b>: the recorder on the doctor's PC, so it knows the "
               "consultation has reached its end, and the AIMS LAB server, so we receive the "
               "prescription itself.<br/><br/>"
               "This document therefore writes <b>\"API 3, Channel A\"</b> for the signal to the "
               "PC and <b>\"API 3, Channel B\"</b> for the message to our server. They are the "
               "two halves of API 3, matching sections 6a and 6b of the earlier guide. "
               "<b>An earlier draft of this document called them API 3a and API 3b; those labels "
               "are withdrawn.</b> Three APIs, four messages."]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>They go in pairs.</b> When the doctor opens a patient, the browser sends "
                   "API 1 and your server sends API 2 at the same moment. When the doctor presses "
                   "Build Prescription, the browser sends API 3 on Channel A and your server sends API 3 on Channel B. "
                   "Four messages, two moments.", GOOD))
S.append(Spacer(1, 4))
S.append(tbl([["Why two channels instead of one?"],
              ["The browser signal has to be <b>instant</b>, because the microphone must start "
               "before the doctor says anything &mdash; so it goes straight to the program on the "
               "same laptop, with no internet involved. The patient data has to be "
               "<b>trustworthy</b>, so it travels server to server with a secret key, where no "
               "browser and no user can touch or alter it. Neither path could do both jobs well, "
               "which is why there are two."]],
             [156 * mm], bold_first=False))

# ---------------------------------------------------------- 2
S.append(Paragraph("2 &middot; The five fields &mdash; the most important rule in this document", H2))
S.append(Paragraph("Every one of the four messages about a single consultation carries these same "
                   "five fields, with <b>exactly the same values, character for character</b>. "
                   "They are how we match the prescription to the right recording.", BODY))
S.append(Spacer(1, 3))
S.append(Preformatted(
    '{\n'
    '  "patient_id":  "P0012345",\n'
    '  "doctor_id":   "DR0042",\n'
    '  "hospital_id": "AALO_DHOLPUR",\n'
    '  "start_time":  "2026-10-05T10:14:32+06:00",\n'
    '  "date":        "2026-10-05"\n'
    '}', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Field", "Type", "Rule", "Example"],
              ["patient_id", "text", "Letters, digits, hyphen, underscore. 1&ndash;64 characters. "
                                     "<b>No spaces</b>", "P0012345"],
              ["doctor_id", "text", "same rule", "DR0042"],
              ["hospital_id", "text", "same rule. <b>Must be the clinic code AIMS LAB gives "
                                      "you</b>", "AALO_DHOLPUR"],
              ["start_time", "text", "Date and time <b>with the time-zone offset</b>",
               "2026-10-05T10:14:32+06:00"],
              ["date", "text", "The clinic's local date", "2026-10-05"]],
             [26 * mm, 16 * mm, 72 * mm, 42 * mm], mono=(3,)))
S.append(Spacer(1, 4))
S.append(tbl([["Three mistakes that break the match"],
              ["<b>1. Sending the prescription's own time as start_time.</b> API 3 on Channel A and API 3 on Channel B "
               "must repeat the <b>original</b> start_time from when the patient was opened "
               "&mdash; not the time the prescription was built. This is the single most common "
               "integration mistake, and it is silent: both messages succeed, and the "
               "prescription simply never finds its recording."],
              ["<b>2. Leaving off the time-zone offset.</b> "
               "<font face='Courier'>2026-10-05T10:14:32</font> is rejected. "
               "<font face='Courier'>+06:00</font> must be there."],
              ["<b>3. Spaces or other characters in an identifier.</b> These become folder names "
               "on disk, so only letters, digits, hyphen and underscore are allowed."]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>The practical advice: build the five fields once, into a single object, at "
                   "the moment the doctor opens the patient. Keep that object for the whole "
                   "consultation and pass the same one to all four messages.</b> Never rebuild "
                   "them, and never recompute the time. The worked example in section 18 does "
                   "exactly this.", GOOD))

S.append(PageBreak())

# ============================================================ PART B
S.append(part("PART B", "The two browser signals",
              "API 1 and API 3 on Channel A &middot; built by CMED's front-end developer"))

# ---------------------------------------------------------- 3
S.append(Paragraph("3 &middot; How the page connects", H2))
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
S.append(H3 and Paragraph("3.1 &nbsp; Why there is no password, and what replaces it", H3))
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

# ---------------------------------------------------------- 4
S.append(Paragraph("4 &middot; The shape of every message and every reply", H2))
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

# ---------------------------------------------------------- 5
S.append(Paragraph("5 &middot; API 1 &mdash; a patient has been opened", H2))
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
                   "server-to-server message you send at the same moment.", BAD))
S.append(Spacer(1, 4))
S.append(H3 and Paragraph("5.1 &nbsp; What comes back", H3))
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
              ["<b>AUTHORISATION_FAILED</b> (401)", "<b>The server refused this recording, so it "
                                                    "was stopped and deleted</b>",
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
              ["ORIGIN_NOT_ALLOWED (403)", "Your page's address is not on the allow-list",
               "<b>Send AIMS LAB the address.</b> See section 3.1"],
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
                   "sees from \"recording\" to \"not recording\". The "
                   "<font face='Courier'>status</font> messages in section 8 are the reliable way "
                   "to show this.", BAD))

S.append(PageBreak())

# ---------------------------------------------------------- 6
S.append(Paragraph("6 &middot; API 3, Channel A &mdash; the prescription has been built", H2))
S.append(Paragraph("Send this the moment <b>Build Prescription</b> succeeds, at the same time as "
                   "your server sends API 3 on Channel B.", BODY))
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

# ---------------------------------------------------------- 7
S.append(Paragraph("7 &middot; The most important idea in this document", H2))
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
              ["Opens patient A", "API 1 (A) + API 2 (A)", "Starts recording A"],
              ["Consults", "&mdash;", "Recording"],
              ["Presses Build Prescription", "<b>API 3 on Channel A (A) + API 3 on Channel B (A)</b>",
               "<b>Keeps recording.</b> Arms the gate"],
              ["Prints it, hands it over, counsels", "&mdash;", "<b>Still recording</b> &mdash; "
                                                                "this is the valuable part"],
              ["Opens patient B", "<b>API 1 (B) + API 2 (B)</b>", "<b>Closes A</b>, starts "
                                                                  "recording B"]],
             [48 * mm, 44 * mm, 64 * mm], highlight=[3, 5]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>What if the doctor finishes for the day, or goes to lunch?</b> There is a "
                   "Stop button on the recorder's own small on-screen control, which the doctor "
                   "presses. <b>CMED does not need to send anything</b>, and must not try to: "
                   "stopping is a clinical decision with a reason attached, and it belongs to the "
                   "doctor, not to the page.", NOTE))

S.append(PageBreak())

# ---------------------------------------------------------- 8
S.append(Paragraph("8 &middot; Messages the recorder sends you, unprompted", H2))
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
S.append(Spacer(1, 4))
S.append(Paragraph("<b>The microphone meter is worth the hour it takes to build.</b> A microphone "
                   "that has been unplugged, muted in Windows, or set to the wrong input records "
                   "perfect silence, and nothing on screen looks wrong. Without a meter that is "
                   "discovered when somebody plays the file back &mdash; which may be weeks and "
                   "hundreds of consultations later.", GOOD))

# ---------------------------------------------------------- 9
S.append(Paragraph("9 &middot; Commands CMED must not send", H2))
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

S.append(PageBreak())

# ============================================================ PART C
S.append(part("PART C", "The two server messages",
              "API 2 and API 3 on Channel B &middot; built by CMED's back-end developer"))

# ---------------------------------------------------------- 10
S.append(Paragraph("10 &middot; How CMED's server connects", H2))
S.append(tbl([["", "Address"],
              ["<b>API 2</b> &mdash; patient information",
               "POST https://&lt;aims-lab-server&gt;/api/v2/clinical/patient-information"],
              ["<b>API 3, Channel B</b> &mdash; prescription",
               "POST https://&lt;aims-lab-server&gt;/api/v2/clinical/prescription"],
              ["Header on both", "X-CMED-Key: &lt;the key AIMS LAB gives you&gt;"],
              ["Body", "JSON, UTF-8, under 1 MB"],
              ["Success", "HTTP 202 &mdash; the message is stored before you get this reply"]],
             [50 * mm, 106 * mm], mono=(1,)))
S.append(Spacer(1, 4))
S.append(Spacer(1, 4))
S.append(tbl([["Also on the same server", "Address", "Key needed?"],
              ["<b>Is the system running?</b>", "<font face='Courier'>GET /health</font>",
               "<b>No</b> &mdash; poll it freely"],
              ["Which system is this?", "<font face='Courier'>GET /</font>", "No"]],
             [50 * mm, 62 * mm, 44 * mm], highlight=[1]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>The hostname is configuration, not a constant.</b> AIMS LAB sends the "
                   "staging and production hostnames with the key, and they differ. The server "
                   "runs on <b>DigitalOcean in Bangalore</b>, the lowest-latency region available "
                   "to Dhaka at roughly 45&ndash;70 ms. <b>That latency never affects "
                   "recording</b> &mdash; the microphone is driven by the program on the clinic "
                   "PC over loopback, so it starts instantly whether or not our server is "
                   "reachable.", GOOD))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Operational detail &mdash; endpoints, limits, monitoring, the test "
                   "commands and the go-live checklist &mdash; is in the companion "
                   "\"Integration &amp; Test Guide for CMED DevOps\".</b> This document stays "
                   "with the messages themselves.", NOTE))
S.append(Paragraph("<b>The key is a server credential. It must never appear in a browser</b>, in "
                   "page source, or in anything a user can view. Both messages go server to "
                   "server.", BAD))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Never let these messages delay the doctor.</b> Put them on a background "
                   "queue and let the page carry on. If our server is briefly unreachable, the "
                   "consultation still happened and the recording still exists &mdash; the "
                   "clinical record just needs to catch up. A message that arrives an hour late "
                   "is fine.", GOOD))

# ---------------------------------------------------------- 11
S.append(Paragraph("11 &middot; API 2 &mdash; patient information", H2))
S.append(Paragraph("Sent the moment the doctor opens the patient, at the same time as the "
                   "browser's API 1. It carries everything known <b>before</b> the consultation.", BODY))
S.append(Spacer(1, 3))
S.append(Preformatted(
    '{\n'
    '  "patient_id": "P0012345", "doctor_id": "DR0042",\n'
    '  "hospital_id": "AALO_DHOLPUR",\n'
    '  "start_time": "2026-10-05T10:14:32+06:00", "date": "2026-10-05",\n'
    '\n'
    '  "demographics": {\n'
    '    "name": "Rahima Khatun",\n'
    '    "sex": "female",\n'
    '    "age_years": 34,\n'
    '    "date_of_birth": "1992-03-11",\n'
    '    "phone": "01700000000",\n'
    '    "address": "Dholpur, Dhaka"\n'
    '  },\n'
    '\n'
    '  "paramedic": {\n'
    '    "recorded_at": "2026-10-05T10:02:10+06:00",\n'
    '    "weight_kg": 58.4,\n'
    '    "height_cm": 157,\n'
    '    "blood_pressure": "120/80",\n'
    '    "pulse_bpm": 78,\n'
    '    "temperature_c": 36.8,\n'
    '    "spo2_percent": 98,\n'
    '    "notes": "complains of headache"\n'
    '  },\n'
    '\n'
    '  "previous_visit": null,\n'
    '\n'
    '  "female_details": {},\n'
    '  "male_details": {}\n'
    '}', CODE))

S.append(H3 and Paragraph("11.1 &nbsp; demographics &mdash; required", H3))
S.append(tbl([["Field name", "Type", "Accepted", "If it is wrong", "Goes to"],
              ["<b>name</b>", "text", "up to 200 characters. "
                                      "<font face='Courier'>full_name</font> also accepted",
               "shortened", "patients.full_name"],
              ["<b>sex</b>", "text", "<b>exactly</b> <font face='Courier'>female</font> or "
                                     "<font face='Courier'>male</font>",
               "<b>the whole message is quarantined</b>", "patients.sex"],
              ["age_years", "number", "0 to 130", "stored empty", "encounter_demographics"],
              ["date_of_birth", "text", "YYYY-MM-DD", "stored empty", "patients.date_of_birth"],
              ["phone", "text", "up to 40 characters", "shortened", "patients.phone"],
              ["address", "text", "up to 500 characters", "shortened", "patients.address"]],
             [26 * mm, 16 * mm, 44 * mm, 36 * mm, 34 * mm], req=[1, 2]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>demographics must be present on every visit</b>, first or fiftieth. "
                   "<b>sex is the one field that can fail the whole message</b> &mdash; anything "
                   "other than the two words is reported back as a problem, so send the word, "
                   "not a code like M or F.", NOTE))

S.append(PageBreak())
S.append(H3 and Paragraph("11.2 &nbsp; paramedic &mdash; optional, but send it when you have it", H3))
S.append(tbl([["Field name", "Type", "Accepted", "Goes to"],
              ["recorded_at", "text", "date and time <b>with offset</b>", "paramedic_observations"],
              ["weight_kg", "number", "0.5 to 499", "weight_kg"],
              ["height_cm", "number", "20 to 299", "height_cm"],
              ["blood_pressure", "text", "<font face='Courier'>\"120/80\"</font> &mdash; upper "
                                         "30&ndash;300, lower 10&ndash;250",
               "blood_pressure, and split into systolic and diastolic"],
              ["pulse_bpm", "whole number", "10 to 300", "pulse_bpm"],
              ["temperature_c", "number", "25 to 45", "temperature_c"],
              ["spo2_percent", "whole number", "0 to 100", "spo2_percent"],
              ["notes", "text", "up to 2000 characters", "notes"]],
             [28 * mm, 22 * mm, 60 * mm, 46 * mm]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>A reading outside its range is stored as empty &mdash; it never rejects "
                   "the message.</b> One mistyped temperature must not stop a prescription being "
                   "saved.", GOOD))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Anything else you put inside <font face='Courier'>paramedic</font> is "
                   "kept.</b> If you measure something we have not listed &mdash; respiration "
                   "rate, blood sugar, anything &mdash; just send it. It is stored in an "
                   "<font face='Courier'>other</font> field alongside the rest. You do not need "
                   "our permission and you do not need us to change anything first.", GOOD))

# ---------------------------------------------------------- 12
S.append(Paragraph("12 &middot; previous_visit &mdash; the rule, in full", H2))
S.append(Paragraph("<b>This field must always be present.</b> Send "
                   "<font face='Courier'>null</font> when there is nothing to send. Leaving the "
                   "field out entirely is reported as a problem, because we cannot tell "
                   "\"first visit\" from \"CMED forgot\".", BAD))
S.append(Spacer(1, 4))
S.append(tbl([["The situation", "What to send"],
              ["The patient's first ever visit", "<font face='Courier'>\"previous_visit\": null</font>"],
              ["A returning patient, and <b>AIMS LAB does not yet hold</b> the previous visit",
               "<b>the full previous visit</b>, as shown below"],
              ["A returning patient, and AIMS LAB <b>already holds</b> the previous visit",
               "<font face='Courier'>\"previous_visit\": null</font>"]],
             [78 * mm, 78 * mm], highlight=[2]))
S.append(Spacer(1, 4))
S.append(Preformatted(
    '"previous_visit": {\n'
    '  "date": "2026-06-02",\n'
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
              ["<b>date</b>", "<b>Required</b> if you send previous_visit at all. YYYY-MM-DD. "
                              "The date of that earlier consultation. Without it the whole "
                              "previous visit is ignored"],
              ["prescription", "Same shape as API 3 on Channel B, section 13. Only "
                               "<font face='Courier'>items</font> really matters"],
              ["diagnoses, notes", "May sit inside <font face='Courier'>prescription</font> or "
                                   "beside it &mdash; both work"]],
             [30 * mm, 126 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Sending it more often than necessary is completely safe.</b> If you are "
                   "ever unsure whether we have the previous visit, send it. The database will "
                   "not duplicate anything: a repeated previous visit updates the one row we "
                   "already have. <b>The dangerous direction is the other one</b> &mdash; "
                   "deciding not to send it when we do not actually have it, which leaves a "
                   "permanent hole in that patient's history that nobody notices.", GOOD))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Recommendation for the first release: send it every time for every "
                   "returning patient.</b> It costs about 2 kilobytes. Once we build an endpoint "
                   "that lets CMED ask us what we already hold, you can switch to sending it "
                   "only when needed.", NOTE))

S.append(PageBreak())

# ---------------------------------------------------------- 13
S.append(Paragraph("13 &middot; API 3, Channel B &mdash; the prescription", H2))
S.append(Paragraph("Sent the moment the doctor presses <b>Build Prescription</b>, at the same "
                   "time as the browser's API 3 on Channel A. Remember that this does <b>not</b> stop the "
                   "recording &mdash; see section 7.", BODY))
S.append(Spacer(1, 3))
S.append(Preformatted(
    '{\n'
    '  "patient_id": "P0012345", "doctor_id": "DR0042",\n'
    '  "hospital_id": "AALO_DHOLPUR",\n'
    '  "start_time": "2026-10-05T10:14:32+06:00",   <-- the ORIGINAL start time\n'
    '  "date": "2026-10-05",\n'
    '\n'
    '  "issued_at": "2026-10-05T10:26:55+06:00",\n'
    '  "items": [\n'
    '    { "drug": "Napa 500 mg", "dose": "1 tablet",\n'
    '      "frequency": "1+1+1", "duration": "5 days",\n'
    '      "instructions": "after food" },\n'
    '    { "drug": "Omeprazole 20 mg", "dose": "1 capsule",\n'
    '      "frequency": "1+0+0", "duration": "14 days",\n'
    '      "instructions": "before breakfast" }\n'
    '  ],\n'
    '  "diagnoses":      ["Acute gastritis"],\n'
    '  "investigations": ["CBC", "HbA1c"],\n'
    '  "advice":         "avoid spicy food",\n'
    '  "follow_up":      "2026-11-05",\n'
    '  "notes":          "patient counselled about diet"\n'
    '}', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Field name", "Type", "Required?", "Rule", "Goes to"],
              ["<b>issued_at</b>", "text", "<b>YES</b>", "date and time <b>with offset</b>",
               "prescriptions.issued_at"],
              ["<b>items</b>", "list", "<b>YES</b>", "one entry per medicine. "
                                                     "<b>Send [] if none</b>", "prescription_items"],
              ["items[].drug", "text", "<b>YES</b>", "up to 300 characters. "
                                                     "<b>Every item needs one</b>", "drug"],
              ["items[].dose", "text", "no", "up to 200", "dose"],
              ["items[].frequency", "text", "no", "up to 200, e.g. 1+1+1", "frequency"],
              ["items[].duration", "text", "no", "up to 200, e.g. 5 days", "duration"],
              ["items[].instructions", "text", "no", "up to 1000", "instructions"],
              ["<b>diagnoses</b>", "list of text", "<b>YES</b>", "<b>Send [] if none</b>",
               "diagnoses"],
              ["<b>investigations</b>", "list of text", "<b>YES</b>", "<b>Send [] if none</b>",
               "investigations"],
              ["advice", "text", "no", "up to 2000", "prescriptions.advice"],
              ["follow_up", "text", "no", "YYYY-MM-DD <b>if sent at all</b>", "follow_up_date"],
              ["notes", "text", "no", "up to 2000", "prescriptions.notes"]],
             [32 * mm, 22 * mm, 18 * mm, 48 * mm, 36 * mm], req=[1, 2, 3, 8, 9]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>An empty list and a missing field are not the same thing.</b> "
                   "<font face='Courier'>\"diagnoses\": []</font> means \"the doctor recorded "
                   "none\". Leaving the field out means \"CMED did not send it\", and that is "
                   "reported as a problem. Please always send the list.", BAD))
S.append(Spacer(1, 3))
S.append(Paragraph("The order of <font face='Courier'>items</font> is kept exactly as you send "
                   "it, and stored as the line number on the prescription.", BODY))

S.append(PageBreak())

# ---------------------------------------------------------- 14
S.append(Paragraph("14 &middot; Corrections, repeats and retries", H2))
S.append(tbl([["Situation", "What to do", "What happens"],
              ["The doctor edits the prescription after it was sent",
               "Send API 3 on Channel B again with the <b>same five fields</b>",
               "We store it as a new version. Both are kept; the newest is the current one"],
              ["Your network failed, you are not sure it arrived", "Send it again, unchanged",
               "<b>Completely safe.</b> You get <font face='Courier'>202 ALREADY_RECEIVED</font> "
               "and one row, not two"],
              ["You sent something wrong and want to replace it",
               "Send the corrected version with the same five fields", "Same as an edit"],
              ["You sent a field we do not recognise", "nothing",
               "It is kept with the rest of the message. Unknown fields are never an error"]],
             [44 * mm, 52 * mm, 60 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Every message is stored word for word, exactly as you sent it, before "
                   "anything is read out of it.</b> So nothing is ever lost because we failed to "
                   "understand a field. If we later realise we should have been reading something, "
                   "we can go back through the stored messages and get it.", GOOD))

# ---------------------------------------------------------- 15
S.append(Paragraph("15 &middot; Every reply from the server, and what to do about it", H2))
S.append(tbl([["Code", "Means", "What CMED should do"],
              ["<b>202 ACCEPTED</b>", "Stored safely", "Nothing. Mark it sent"],
              ["<b>202 ALREADY_RECEIVED</b>", "This exact message was already stored",
               "Nothing. <b>Retrying is safe</b>"],
              ["400 MALFORMED_JSON", "The body is not valid JSON",
               "Fix and resend. Do not retry unchanged"],
              ["400 MISSING_FIELD", "One of the five fields is missing or malformed",
               "Fix and resend"],
              ["400 INVALID_IDENTIFIER", "An id has characters outside A&ndash;Z a&ndash;z "
                                         "0&ndash;9 _ -", "Fix and resend"],
              ["401 INVALID_KEY", "The key is missing or wrong", "<b>Stop and contact AIMS LAB</b>"],
              ["413 TOO_LARGE", "The body is over 1 MB", "Usually a mistake &mdash; check for a "
                                                         "file or image in the message"],
              ["<b>422 SCHEMA_INVALID</b>", "<b>Stored in quarantine</b>, with a list of which "
                                            "fields were wrong",
               "<b>Not lost.</b> Read the field list in the reply, fix, resend"],
              ["500 / 503", "Our server has a problem", "<b>Retry with a growing delay.</b> "
                                                        "Never drop the message"]],
             [40 * mm, 56 * mm, 60 * mm], highlight=[1, 2], req=[8]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Please queue and retry rather than sending once and hoping.</b> A message "
                   "that is never sent leaves a recording that nobody can describe &mdash; and an "
                   "unconfirmed recording is erased after 24 hours.", BAD))

S.append(PageBreak())

# ============================================================ PART D
S.append(part("PART D", "Shared &mdash; storage, testing and handover",
              "For both developers, and for the project leads on each side"))

# ---------------------------------------------------------- 16
S.append(Paragraph("16 &middot; Where every field ends up", H2))
S.append(Paragraph("So CMED can see that nothing is thrown away. All of this lives in a database "
                   "called <font face='Courier'>aims_clinical</font>, separate from the one that "
                   "tracks recordings &mdash; which deliberately holds no patient names at all.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["You send", "We store it in", "Holding"],
              ["the whole message, untouched", "<font face='Courier'>intake_records</font>",
               "exactly what you sent, once per message"],
              ["demographics", "<font face='Courier'>patients</font>, "
                               "<font face='Courier'>encounter_demographics</font>",
               "name, sex, date of birth, phone, address, age at this visit"],
              ["paramedic", "<font face='Courier'>paramedic_observations</font>",
               "each reading in its own column, blood pressure also split into upper and lower, "
               "anything unrecognised in <font face='Courier'>other</font>"],
              ["the visit itself", "<font face='Courier'>encounters</font>",
               "one row per consultation, linked to its recording"],
              ["previous_visit", "<font face='Courier'>encounters</font> (marked as a previous "
                                 "visit)", "a second row, dated to that earlier day"],
              ["items", "<font face='Courier'>prescription_items</font>",
               "<b>one row per medicine</b>, in the order you sent them"],
              ["the prescription itself", "<font face='Courier'>prescriptions</font>",
               "with a version number, so corrections do not overwrite"],
              ["diagnoses", "<font face='Courier'>diagnoses</font>", "one row each"],
              ["investigations", "<font face='Courier'>investigations</font>", "one row each"],
              ["female_details / male_details", "their own tables", "stored as sent"]],
             [40 * mm, 50 * mm, 66 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("A copy of all of it is also written as a <b>JSON file beside the audio "
                   "recording</b>, with the same name. So each consultation is one folder "
                   "containing the audio and its clinical record together, and the database is "
                   "the index into them.", GOOD))

# ---------------------------------------------------------- 17
S.append(Paragraph("17 &middot; Rules that are easy to miss &mdash; both channels", H2))
S.append(H3 and Paragraph("The five fields, and the pairing", H3))
S.append(tbl([["", "Rule", "Why"],
              ["1", "<b>The five fields must be identical in all four messages</b>",
               "They are the only link between the recording and the clinical record"],
              ["2", "<b>start_time in API 3 on Channel A and 3b is the original</b>, from when the patient "
                    "was opened", "It is how the prescription finds its recording"],
              ["3", "<b>Every timestamp carries its offset</b> (+06:00)",
               "A time without a zone is stored as empty"],
              ["4", "<b>hospital_id must be the code AIMS LAB gives you</b>",
               "A recording claiming a clinic the laptop is not enrolled to is <b>refused</b>"]],
             [7 * mm, 70 * mm, 79 * mm], bold_first=False))
S.append(H3 and Paragraph("The browser side", H3))
S.append(tbl([["", "Rule", "Why"],
              ["5", "<b>Keep the session_id</b> that API 1 returns",
               "API 3 on Channel A needs it. Without it you cannot arm the gate"],
              ["6", "<b>Treat RECORDING_PROVISIONAL as success</b>",
               "The microphone is already live. It is not a warning"],
              ["7", "<b>But keep listening</b> &mdash; a provisional start can be refused a "
                    "moment later", "The recording is then stopped and deleted, and the page "
                                    "must stop saying \"recording\""],
              ["8", "<b>prescription_built does not stop anything</b>",
               "The counselling after it is valuable audio"],
              ["9", "Act on <font face='Courier'>code</font>, never on "
                    "<font face='Courier'>message</font>", "The wording may change; the code will not"],
              ["10", "<b>Reconnect if the connection drops</b>, and reconnect quietly",
               "A dropped socket does not stop a recording in progress"],
              ["11", "<b>Give AIMS LAB every web address you will use</b>",
               "Staging and preview addresses are refused unless listed"],
              ["12", "<b>Never send stop, pause or resume</b>", "Those belong to the doctor"],
              ["13", "If the connection will not open at all, say \"AIMScribe is not running\"",
               "It usually means the program is not started on that PC"]],
             [7 * mm, 70 * mm, 79 * mm], bold_first=False))
S.append(H3 and Paragraph("The server side", H3))
S.append(tbl([["", "Rule", "Why"],
              ["14", "<b>Never let a message delay the doctor.</b> Send from a background queue",
               "A slow network must never make Build Prescription feel slow"],
              ["15", "<b>previous_visit must be present</b>, even if null",
               "Missing means \"CMED forgot\"; null means \"nothing to send\""],
              ["16", "<b>items, diagnoses and investigations are always lists</b>, empty if none",
               "Same reason"],
              ["17", "<b>sex is the word</b> <font face='Courier'>female</font> or "
                     "<font face='Courier'>male</font>", "Codes like M, F, 1, 2 are rejected"],
              ["18", "<b>The key never reaches a browser</b>", "It is a server credential"],
              ["19", "<b>Retry on 500 and 503</b>, with a growing delay",
               "Retrying is safe; dropping the message is not"],
              ["20", "Unknown fields are welcome", "They are kept. Do not strip data to fit"]],
             [7 * mm, 70 * mm, 79 * mm], bold_first=False))

S.append(PageBreak())

# ---------------------------------------------------------- 18
S.append(Paragraph("18 &middot; A complete working example", H2))
S.append(Paragraph("The browser half of the integration, with the five fields built once and "
                   "reused by all four messages. Everything else in this document is detail.", BODY))
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
    '    // Unprompted status - the reliable way to show what is really happening.\n'
    '    if (m.event === "status") { showRecordingState(m); return; }\n'
    '\n'
    '    if (m.command === "start") {\n'
    '      if (m.code === "RECORDING_STARTED" || m.code === "RECORDING_PROVISIONAL") {\n'
    '        sessionId = m.data.session_id;      // keep it - API 3 on Channel A needs it\n'
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
    '// The doctor opens a patient.\n'
    '// Build the five fields ONCE, here, and reuse this object everywhere.\n'
    'function openPatient(patient, doctor) {\n'
    '  const visit = {\n'
    '    patient_id:  patient.id,\n'
    '    doctor_id:   doctor.id,\n'
    '    hospital_id: CLINIC_CODE,           // the code AIMS LAB gave you\n'
    '    start_time:  new Date().toISOString(),   // with offset\n'
    '    date:        localDateString()           // the clinic\'s local day\n'
    '  };\n'
    '  currentVisit = visit;                 // keep for API 3 on Channel A and 3b\n'
    '\n'
    '  socket.send(JSON.stringify({\n'
    '    command: "start",\n'
    '    request_id: "open-" + visit.patient_id,\n'
    '    trigger: visit                      // the SAME object\n'
    '  }));\n'
    '\n'
    '  sendPatientInformationFromYourServer(visit, demographics, paramedic);   // API 2\n'
    '}\n'
    '\n'
    '// The doctor presses Build Prescription.\n'
    'function prescriptionBuilt(prescription) {\n'
    '  socket.send(JSON.stringify({\n'
    '    command: "prescription_built",\n'
    '    request_id: "built-" + currentVisit.patient_id,\n'
    '    patient_id: currentVisit.patient_id,\n'
    '    session_id: sessionId\n'
    '  }));\n'
    '\n'
    '  // API 3 on Channel B - note it reuses currentVisit, so start_time is the ORIGINAL.\n'
    '  sendPrescriptionFromYourServer(currentVisit, prescription);\n'
    '}\n'
    '\n'
    'connect();', CODE))

S.append(PageBreak())

# ---------------------------------------------------------- 19
S.append(Paragraph("19 &middot; How CMED can test, today", H2))
S.append(Paragraph("You do not need to wait for us, and you do not need a real patient.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["", "What", "How"],
              ["1", "Check a server message against the rules, with nothing running",
               "<font face='Courier'>python tools/channel_b_test.py --local</font>"],
              ["2", "Send real server messages to a test server",
               "<font face='Courier'>python tools/channel_b_test.py --server https://&lt;host&gt; "
               "--key &lt;your key&gt;</font>"],
              ["3", "See every message and reply side by side",
               "open <font face='Courier'>/protocol-test</font> on the test site"],
              ["4", "Test the browser signals against a real recorder",
               "install the recorder on one PC, then open your page and watch the replies"],
              ["5", "<b>Confirm the server is up at any time</b>",
               "<font face='Courier'>curl https://&lt;host&gt;/health</font> &mdash; no key needed"]],
             [7 * mm, 62 * mm, 87 * mm], bold_first=False))
S.append(Spacer(1, 3))
S.append(Paragraph("Step 2 runs every case that matters &mdash; a good message, the same message "
                   "twice, a broken one, a changed prescription, a wrong key, an oversized body "
                   "&mdash; and checks each reply against this document. <b>Please run it and "
                   "send us the output before the first clinic goes live.</b>", GOOD))

# ---------------------------------------------------------- 20
S.append(Paragraph("20 &middot; What each side must hand over", H2))
S.append(tbl([["AIMS LAB gives CMED", "CMED gives AIMS LAB"],
              ["The server address", "<b>The exact web address of your page</b> (for example "
                                     "https://ehr.aaloclinic.com) &mdash; including staging"],
              ["<b>The Channel B key</b> &mdash; shown once",
               "<b>Your own clinic codes</b>, so we can map them to ours"],
              ["The seven clinic codes", "A test environment we can send to"],
              ["This document", "A named contact for integration"]],
             [78 * mm, 78 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>The two we need from CMED are blocking.</b> Without your page's exact "
                   "address the browser cannot talk to the recorder at all &mdash; nothing works. "
                   "Without your clinic codes, a visit you describe cannot be matched to the "
                   "clinic it happened in: the record is stored, <b>you get a normal success "
                   "reply</b>, and the <b>recording is quietly erased after 24 hours</b>. That "
                   "failure is completely silent from your side, which is why it matters more "
                   "than it sounds.", BAD))

# ---------------------------------------------------------- 21
S.append(Paragraph("21 &middot; One page for the CMED developer", H2))
S.append(tbl([["Question", "Answer"],
              ["How many messages do I build?", "<b>Four.</b> Two from the browser (Part B), two "
                                                "from your server (Part C)"],
              ["What ties them together?", "<b>The five fields, identical in all four</b>"],
              ["Biggest trap", "<b>start_time in the prescription messages is the original</b>, "
                               "not the time the prescription was built"],
              ["<i>Browser</i> &mdash; where do I connect?",
               "<font face='Courier'>ws://127.0.0.1:5050/ws</font> &mdash; the same PC, every time"],
              ["Do I need a password there?", "<b>No.</b> You need your page's web address on our "
                                              "allow-list instead"],
              ["What do I keep from the reply?", "<b>session_id</b> &mdash; API 3 on Channel A needs it"],
              ["Is RECORDING_PROVISIONAL bad?", "<b>No.</b> It means recording. But keep "
                                                "listening &mdash; it can still be refused"],
              ["Does prescription_built stop it?", "<b>No.</b> It arms the gate. The next patient "
                                                   "closes the consultation"],
              ["What stops a recording then?", "The next API 1, or the doctor's own Stop button"],
              ["Can I send stop or pause?", "<b>No.</b> Those belong to the doctor"],
              ["What if the socket drops?", "Reconnect quietly. The recording continues regardless"],
              ["What if it will not connect?", "Show \"AIMScribe is not running\". The program is "
                                               "probably not started on that PC"],
              ["<i>Server</i> &mdash; where do I send?",
               "<font face='Courier'>/api/v2/clinical/patient-information</font> and "
               "<font face='Courier'>/api/v2/clinical/prescription</font>"],
              ["How do I authenticate?", "Header <font face='Courier'>X-CMED-Key</font>. Server "
                                         "to server only, never in a browser"],
              ["What is required in API 2?", "<font face='Courier'>demographics</font>, and "
                                             "<font face='Courier'>previous_visit</font> present "
                                             "(null is fine)"],
              ["What is required in API 3 on Channel B?", "<font face='Courier'>issued_at</font>, "
                                              "<font face='Courier'>items</font>, "
                                              "<font face='Courier'>diagnoses</font>, "
                                              "<font face='Courier'>investigations</font> "
                                              "&mdash; lists may be empty"],
              ["What if a reading is out of range?", "Stored empty. It never rejects the message"],
              ["What if I send extra fields?", "They are kept. Never an error"],
              ["Is retrying safe?", "<b>Yes.</b> Same body twice gives one row and "
                                    "<font face='Courier'>ALREADY_RECEIVED</font>"],
              ["What if I get 422?", "Nothing is lost &mdash; it is quarantined. Read the field "
                                     "list, fix, resend"],
              ["When do I send previous_visit?", "Whenever we might not have it. <b>For the first "
                                                 "release, send it every time</b>"],
              ["Can a message slow the doctor down?", "It must not. Queue it and retry in the "
                                                      "background"],
              ["How do I test?", "<font face='Courier'>tools/channel_b_test.py</font>, and the "
                                 "<font face='Courier'>/protocol-test</font> page"]],
             [50 * mm, 106 * mm]))
S.append(Spacer(1, 6))
S.append(Paragraph("Prepared by AIMS LAB, United International University. This document replaces "
                   "the two earlier separate specifications and carries all of their content. "
                   "Every rule here is taken from the code that handles these messages. If "
                   "anything is unclear, ask before building &mdash; it is far cheaper than "
                   "discovering it in a clinic.", SUB))


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7.3)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 9.5 * mm,
                      "AIMScribe v3 - the CMED integration specification - AIMS LAB, October 2026")
    canvas.drawRightString(A4[0] - 18 * mm, 9.5 * mm, "page %d" % doc.page)
    canvas.restoreState()


import argparse
_ap = argparse.ArgumentParser()
_ap.add_argument("--out", default="CMED_INTEGRATION_SPECIFICATION.pdf",
                 help="output file name, for when the real one is open in a viewer")
OUT = Path(__file__).resolve().parent.parent / _ap.parse_args().out
SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                  topMargin=16 * mm, bottomMargin=20 * mm,
                  title="The CMED Integration Specification - AIMScribe v3",
                  author="AIMS LAB").build(S, onFirstPage=footer, onLaterPages=footer)
print("wrote", OUT)
