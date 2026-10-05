#!/usr/bin/env python3
"""
Build CMED_GUIDANCE_NOTE.pdf - three questions answered for the CMED meeting:

  1. How CMED verifies the clinic and the enrolled laptop before recording
  2. Where every recording is stored, end to end
  3. What the replies on the test page actually mean in a real clinic

    python tools/make_cmed_guidance_note.py [--out NAME.pdf]

Taken from the code: recorder/api/websocket_server.py (the doctors command),
backend/archive_worker/archive.py (the path layout), backend/src/api_v2.py
(file naming, confirmation). No secrets, no project data.
"""
import argparse
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
S.append(Paragraph("AIMScribe &mdash; Guidance Note for CMED", H1))
S.append(Paragraph("Verifying the clinic and the laptop &middot; where recordings are stored "
                   "&middot; what the replies mean &middot; AIMS LAB &middot; 5 October 2026", SUB))
S.append(HRFlowable(width="100%", thickness=1, color=RULE, spaceAfter=8))
S.append(Paragraph("A companion to the <b>CMED Integration Specification</b>, answering three "
                   "questions that came up in planning. Nothing here changes the specification "
                   "&mdash; it explains three things it assumes you already know.", BODY))
S.append(Spacer(1, 5))
S.append(tbl([["", "The question", "The short answer"],
              ["<b>A</b>", "How can CMED check which clinic a laptop belongs to, before it "
                           "starts recording?",
               "<b>Ask the recorder.</b> There is a <font face='Courier'>doctors</font> command "
               "that answers with the clinic code and the doctors registered there"],
              ["<b>B</b>", "Where does every recording end up?",
               "<b>clinic / doctor / date / one file per consultation</b>, named so you can find "
               "it without a database"],
              ["<b>C</b>", "The test page shows <font face='Courier'>200 RECORDING_STARTED</font> "
                           "and <font face='Courier'>202 ACCEPTED</font>. What does that mean in a "
                           "real clinic?",
               "<b>Both green means \"stored\", not \"safe\".</b> A third step you cannot see "
               "decides whether the recording is kept"]],
             [10 * mm, 60 * mm, 86 * mm]))

S.append(PageBreak())

# ============================================================ PART A
S.append(part("PART A", "Verifying the clinic and the enrolled laptop",
              "How to make CLINIC_MISMATCH impossible instead of handling it"))

S.append(Paragraph("1 &middot; Why this matters, and what went wrong before", H2))
S.append(Paragraph("Every AIMScribe laptop is <b>enrolled to exactly one clinic</b> when it is "
                   "installed. That enrolment is the laptop's identity and it cannot be changed "
                   "from a web page. If CMED sends a "
                   "<font face='Courier'>hospital_id</font> that is not the clinic the laptop "
                   "belongs to, the recording is <b>refused outright</b> with "
                   "<font face='Courier'>401 CLINIC_MISMATCH</font>.", BODY))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>This has already happened once in testing.</b> The page was sending "
                   "<font face='Courier'>HOSP003</font> after the clinic had been renamed to "
                   "<font face='Courier'>AALO_DHOLPUR</font>, and every recording was refused "
                   "until the page was rebuilt. <b>The lesson is that the clinic code should not "
                   "be a value CMED stores.</b> It should be a value CMED asks for.", BAD))
S.append(Spacer(1, 4))
S.append(tbl([["Who decides what", "Why"],
              ["<b>AIMS LAB</b> enrols the laptop to a clinic", "It happens once, at install, "
                                                                "with a single-use token"],
              ["<b>The laptop</b> knows its own clinic", "It is stored on the machine and signed"],
              ["<b>CMED</b> decides the patient and the doctor", "That is the clinical decision, "
                                                                 "and it is yours"],
              ["<b>CMED does not decide the clinic</b>", "<b>It reads it from the laptop.</b> "
                                                         "CMED has no part in enrolment at all"]],
             [66 * mm, 90 * mm], highlight=[4]))

S.append(Paragraph("2 &middot; The command that answers it", H2))
S.append(Paragraph("On the same connection you already use for API 1, send:", BODY))
S.append(Spacer(1, 2))
S.append(Preformatted(
    '{ "command": "doctors", "request_id": "who-is-this-pc" }', CODE))
S.append(Paragraph("and the recorder replies:", BODY))
S.append(Preformatted(
    '{\n'
    '  "event": "ack",\n'
    '  "command": "doctors",\n'
    '  "status": 200,\n'
    '  "code": "OK",\n'
    '  "request_id": "who-is-this-pc",\n'
    '  "data": {\n'
    '    "hospital_id": "AALO_DHOLPUR",        <-- THIS laptop belongs to THIS clinic\n'
    '    "doctors": [\n'
    '      { "doctor_id": "DR0042", "full_name": "Dr Rahman" },\n'
    '      { "doctor_id": "DR0051", "full_name": "Dr Akter" }\n'
    '    ]\n'
    '  }\n'
    '}', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Field", "What it is", "What CMED should do with it"],
              ["<b>hospital_id</b>", "<b>The clinic this laptop is enrolled to</b>",
               "<b>Use it as your hospital_id in all four messages. Do not hardcode a clinic "
               "code in the page</b>"],
              ["<b>doctors</b>", "The doctors registered at that clinic &mdash; "
                                 "<font face='Courier'>doctor_id</font> and "
                                 "<font face='Courier'>full_name</font>",
               "Use it to fill the doctor selector, so a doctor_id cannot be mistyped"]],
             [26 * mm, 56 * mm, 74 * mm], highlight=[1]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>If CMED reads the clinic code from the laptop instead of storing it, "
                   "CLINIC_MISMATCH becomes impossible</b> &mdash; and a clinic can be renamed on "
                   "our side without CMED changing or rebuilding anything.", GOOD))

S.append(PageBreak())
S.append(Paragraph("3 &middot; The three answers you must handle", H2))
S.append(tbl([["What comes back", "What it means", "What the page should do"],
              ["<font face='Courier'>hospital_id</font> is a code, "
               "<font face='Courier'>doctors</font> has entries",
               "Normal. Enrolled, and we know its doctors",
               "<b>Proceed.</b> Use the code; offer the doctor list"],
              ["<font face='Courier'>hospital_id</font> is a code, "
               "<font face='Courier'>doctors</font> is <font face='Courier'>[]</font>",
               "<b>Also normal</b> &mdash; a new clinic whose doctors we have not seen yet, or our "
               "server briefly unreachable",
               "<b>Proceed.</b> Let the doctor be typed or chosen from your own list. "
               "<b>An empty list must never block a clinic</b>"],
              ["<font face='Courier'>hospital_id</font> is <font face='Courier'>null</font>",
               "<b>This laptop is not enrolled</b>",
               "<b>Do not send API 1.</b> Show \"this PC is not registered with AIMS LAB\" and "
               "contact us"]],
             [44 * mm, 52 * mm, 60 * mm], highlight=[1], req=[3]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>The empty list is the one to get right.</b> It is a list of suggestions, "
                   "not a list of permissions &mdash; CMED decides who is consulting, not us. A "
                   "shared consulting room that has been offline all afternoon will still answer, "
                   "from a cached copy, because a stale list of colleagues is more useful than an "
                   "empty one. But a genuinely new site has nobody in it yet, and the clinic must "
                   "still be able to work.", NOTE))

S.append(Paragraph("4 &middot; The page startup sequence we recommend", H2))
S.append(tbl([["", "Step", "Why"],
              ["1", "Open the connection to "
                    "<font face='Courier'>ws://127.0.0.1:5050/ws</font>",
               "If this fails, AIMScribe is not running &mdash; say so and stop"],
              ["2", "<b>Send <font face='Courier'>doctors</font> immediately</b>",
               "One round trip on the same machine. It costs nothing"],
              ["3", "<b>Store the returned hospital_id for the session</b>",
               "This is now your clinic code. Never a hardcoded one"],
              ["4", "Fill the doctor selector from the list, if it has entries",
               "Removes mistyped doctor ids, which cause "
               "<font face='Courier'>DOCTOR_NOT_AT_CLINIC</font>"],
              ["5", "<b>Show the clinic name on screen</b>, somewhere small",
               "<b>A doctor at the wrong PC notices immediately.</b> This is worth more than it "
               "looks"],
              ["6", "Only now allow a patient to be opened", "Everything API 1 needs is known"]],
             [7 * mm, 70 * mm, 79 * mm], bold_first=False, highlight=[5]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Re-send <font face='Courier'>doctors</font> each time the connection "
                   "reopens</b>, because a reconnect may mean the recorder restarted, and in "
                   "principle a laptop could have been re-enrolled between sessions. It is one "
                   "cheap message and it keeps the page honest.", GOOD))
S.append(Spacer(1, 4))
S.append(tbl([["One thing this does not do"],
              ["The <font face='Courier'>doctors</font> command tells you the clinic the laptop "
               "belongs to. <b>It does not prove the laptop is healthy</b> &mdash; that it can "
               "reach our server, that its disk has room, that its microphone works. Those show "
               "up in the <font face='Courier'>status</font> messages the recorder pushes "
               "(section 8 of the specification), and the microphone one is worth displaying: "
               "<b>a muted or unplugged microphone records perfect silence and nothing on screen "
               "looks wrong.</b>"]],
             [156 * mm], bold_first=False))

S.append(PageBreak())

# ============================================================ PART B
S.append(part("PART B", "Where every recording is stored",
              "The pathway from the doctor's laptop to the permanent archive"))

S.append(Paragraph("5 &middot; The three places a recording lives", H2))
S.append(tbl([["", "Where", "What is there", "For how long"],
              ["<b>1</b>", "<b>The doctor's laptop</b><br/>the spool folder",
               "The consultation as it is recorded, in 30&ndash;90 second pieces",
               "Until the upload succeeds. Holds about 13 hours if our server is unreachable"],
              ["<b>2</b>", "<b>The AIMS LAB server</b><br/>the archive folder",
               "<b>One finished file per consultation</b>, with its clinical record beside it",
               "Until it is copied to cloud storage and verified"],
              ["<b>3</b>", "<b>Cloud storage</b><br/>Cloudflare R2",
               "The permanent copy, as FLAC &mdash; same audio, 40% smaller",
               "<b>Permanently.</b> This is the research dataset"]],
             [8 * mm, 36 * mm, 54 * mm, 58 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>A recording is only deleted from one place once the next place has it and "
                   "has been checked.</b> The laptop will not erase a piece until the server has "
                   "acknowledged it; the server will not erase a file until the cloud copy has "
                   "been downloaded again and compared. Nothing is ever deleted on the strength "
                   "of an upload that was merely accepted.", GOOD))

S.append(Paragraph("6 &middot; The folder layout on the server", H2))
S.append(Paragraph("Four levels, in this order &mdash; clinic, then doctor, then the clinic's "
                   "local date, then one file per consultation:", BODY))
S.append(Spacer(1, 2))
S.append(Preformatted(
    '<archive root>/<hospital_id>/<doctor_id>/<YYYY-MM-DD>/<name>.wav\n'
    '                                                     /<name>.json', CODE))
S.append(Paragraph("A real example:", BODY))
S.append(Preformatted(
    '/srv/aimscribe/archive/\n'
    '  AALO_DHOLPUR/\n'
    '    DR0042/\n'
    '      2026-10-05/\n'
    '        P0012345_DR0042_AALO_DHOLPUR_101432_102755_20261005.wav\n'
    '        P0012345_DR0042_AALO_DHOLPUR_101432_102755_20261005.json\n'
    '        P0012387_DR0042_AALO_DHOLPUR_103010_104402_20261005.wav\n'
    '        P0012387_DR0042_AALO_DHOLPUR_103010_104402_20261005.json\n'
    '    DR0051/\n'
    '      2026-10-05/\n'
    '        ...', CODE))
S.append(Spacer(1, 3))
S.append(H3 and Paragraph("6.1 &nbsp; How the file name is built", H3))
S.append(Preformatted(
    'P0012345 _ DR0042 _ AALO_DHOLPUR _ 101432 _ 102755 _ 20261005\n'
    '   |          |          |            |        |         |\n'
    'patient    doctor     clinic      started    ended     date\n'
    '                                  10:14:32  10:27:55  5 Oct 2026', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Part", "From", "Note"],
              ["patient", "your <font face='Courier'>patient_id</font>", "exactly as CMED sent it"],
              ["doctor", "your <font face='Courier'>doctor_id</font>", "exactly as CMED sent it"],
              ["clinic", "the laptop's enrolment", "<b>not</b> the one CMED sent &mdash; see Part A"],
              ["started, ended", "the recorder", "<b>the clinic's local time</b>, not UTC"],
              ["date", "the clinic's local date", "the same day as the folder above it"]],
             [30 * mm, 50 * mm, 76 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Why this matters to CMED: you can locate any recording from the five "
                   "fields alone, without asking us and without a database.</b> If you know the "
                   "patient, the doctor and the day, you know the folder and you very nearly know "
                   "the file name.", GOOD))

S.append(PageBreak())
S.append(H3 and Paragraph("6.2 &nbsp; The JSON file beside the audio", H3))
S.append(Paragraph("Every recording has a file with <b>the same name</b> and a "
                   "<font face='Courier'>.json</font> ending. It holds the clinical record CMED "
                   "sent &mdash; the demographics, the paramedic readings, the prescription, the "
                   "previous visit &mdash; together with the recording's own details and its "
                   "signature chain.", BODY))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>So each consultation is self-contained.</b> One folder, two files, and "
                   "everything needed to understand that consultation is in them. The database is "
                   "an index into this, not the only copy &mdash; if the database were lost "
                   "entirely, the dataset would still be readable.", GOOD))

S.append(Paragraph("7 &middot; What CMED can and cannot see", H2))
S.append(tbl([["", "Can CMED see it?", "Why"],
              ["The audio", "<b>No. Never</b>",
               "Audio never leaves AIMS LAB's control. CMED is not sent any, and there is no "
               "endpoint that would serve it"],
              ["Whether a recording exists for a visit", "<b>Not today</b>",
               "If this would help you, we can add a small endpoint for it. Ask"],
              ["That a message was stored", "<b>Yes</b>", "The "
                                                          "<font face='Courier'>202</font> reply "
                                                          "to API 2 and API 3 on Channel B"],
              ["That recording is in progress", "<b>Yes</b>",
               "The <font face='Courier'>status</font> messages on the local connection"],
              ["The dashboard and the database", "<b>No</b>",
               "AIMS LAB only. It holds every clinic's data together"]],
             [50 * mm, 32 * mm, 74 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>This is deliberate and it protects CMED as much as us.</b> CMED never "
                   "holds audio of a consultation, so CMED carries none of the obligation that "
                   "comes with holding it.", NOTE))

S.append(PageBreak())

# ============================================================ PART C
S.append(part("PART C", "What the replies actually mean",
              "Reading the test page, and the third step it does not show you"))

S.append(Paragraph("8 &middot; The two lines on the test page, decoded", H2))
S.append(Preformatted(
    'recorder:    200 RECORDING_STARTED  - Recording.\n'
    'API 2/3:     202 ACCEPTED           - Stored.', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["The line", "Which channel", "What it proves", "What it does NOT prove"],
              ["<font face='Courier'>200 RECORDING_STARTED</font>",
               "the browser to the recorder on that PC",
               "<b>The microphone is live</b> and audio is being written to disk",
               "That the consultation will be kept"],
              ["<font face='Courier'>202 ACCEPTED</font>",
               "your server to the AIMS LAB server",
               "<b>Your message is stored</b>, word for word, and will not be lost",
               "<b>That it matched a recording</b>"]],
             [40 * mm, 34 * mm, 42 * mm, 40 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Both lines green means both halves arrived. It does not yet mean the "
                   "consultation is safe.</b> There is a third step, which happens on our server "
                   "a second or two later and which the test page does not show.", BAD))

S.append(Paragraph("9 &middot; The third step &mdash; the match", H2))
S.append(Paragraph("The recording arrives knowing five things. Your message arrives knowing the "
                   "same five things. <b>Our server compares them, and the recording is only kept "
                   "if they are identical.</b>", BODY))
S.append(Spacer(1, 3))
S.append(tbl([["", "What happens", "Result"],
              ["<b>1</b>", "The recorder starts and tells us: patient, doctor, clinic, start time, "
                           "date", "Recording"],
              ["<b>2</b>", "Your server sends API 2 with the same five fields", "Stored"],
              ["<b>3</b>", "<b>We match them</b>", "<b>Confirmed</b> &mdash; the recording joins "
                                                   "the dataset"],
              ["<b>3*</b>", "<b>They do not match, or API 2 never arrives</b>",
               "<b>The recording is kept for 24 hours, then deleted</b>"]],
             [10 * mm, 86 * mm, 60 * mm], highlight=[3], req=[4]))
S.append(Spacer(1, 4))
S.append(tbl([["The failure that matters, stated plainly"],
              ["If your <font face='Courier'>start_time</font> in API 2 is even slightly different "
               "from the one in API 1 &mdash; a second out, a missing "
               "<font face='Courier'>+06:00</font>, a clinic code from your own list instead of "
               "the laptop's &mdash; then:<br/><br/>"
               "&nbsp;&nbsp;&bull; the recorder says <b>200 RECORDING_STARTED</b><br/>"
               "&nbsp;&nbsp;&bull; our server says <b>202 ACCEPTED</b><br/>"
               "&nbsp;&nbsp;&bull; <b>CMED sees nothing wrong at all</b><br/>"
               "&nbsp;&nbsp;&bull; and the recording is <b>deleted 24 hours later</b><br/><br/>"
               "<b>This is the single most important thing in this note.</b> Two green replies do "
               "not mean success. They mean both halves arrived; whether they found each other is "
               "a separate question that CMED cannot see the answer to."]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>How to be safe:</b> build the five fields <b>once</b>, into one object, at "
                   "the moment the doctor opens the patient; take the clinic code from the "
                   "<font face='Courier'>doctors</font> command rather than from your own "
                   "settings; and pass that same object to all four messages without recomputing "
                   "anything. If you do that, the match cannot fail.", GOOD))

S.append(PageBreak())
S.append(Paragraph("10 &middot; What happens when API 2 is late, or never comes", H2))
S.append(tbl([["API 2 arrives...", "What happens to the recording"],
              ["within a second or two", "<b>Confirmed.</b> Nobody notices anything"],
              ["late &mdash; minutes, or an hour",
               "<b>Recording is never cut.</b> It keeps going, we keep asking, and it is "
               "confirmed the moment API 2 arrives"],
              ["<b>never</b>", "<b>Recording is still not cut.</b> It is uploaded and marked "
                               "<i>unconfirmed</i>, kept out of the dataset, and we are alerted. "
                               "<b>Deleted after 24 hours</b>"]],
             [40 * mm, 116 * mm], highlight=[1], req=[3]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Notice what never happens: a missing message never stops the doctor and "
                   "never cuts a recording.</b> The clinic keeps working whatever our server is "
                   "doing. That is why queueing and retrying on your side is the whole of your "
                   "obligation &mdash; a message that arrives an hour late is completely fine, "
                   "and a message that is never sent is the only real failure.", GOOD))

S.append(Paragraph("11 &middot; One real consultation, end to end", H2))
S.append(tbl([["Time", "Who", "What", "State"],
              ["10:14:30", "page", "connection open; <font face='Courier'>doctors</font> asked "
                                   "and answered", "clinic known"],
              ["10:14:32", "page", "<b>API 1</b> to the recorder", "<b>recording</b>"],
              ["10:14:32", "your server", "<b>API 2</b> to AIMS LAB", "stored"],
              ["10:14:33", "us", "<b>the five fields match</b>", "<b>confirmed</b>"],
              ["10:14:35", "recorder", "first 30&ndash;90 second piece uploaded", "recording"],
              ["10:26:55", "page", "<b>API 3, Channel A</b> &mdash; gate armed", "<b>still "
                                                                                 "recording</b>"],
              ["10:26:55", "your server", "<b>API 3, Channel B</b> &mdash; the prescription",
               "stored"],
              ["10:27:10", "doctor", "prints it, hands it over, counsels the patient",
               "<b>still recording &mdash; the valuable part</b>"],
              ["10:27:55", "page", "<b>API 1 for the next patient</b>",
               "<b>this one closes</b>, next one opens"],
              ["10:28:10", "server", "pieces joined into one file, named, checked", "archived"],
              ["10:30:00", "server", "copied to cloud storage and verified",
               "<b>permanent</b>"]],
             [18 * mm, 24 * mm, 70 * mm, 44 * mm], highlight=[4, 11]))
S.append(Spacer(1, 4))
S.append(Paragraph("The consultation lasted 13 minutes and 23 seconds. <b>CMED sent four "
                   "messages, none of which the doctor waited for.</b>", GOOD))

S.append(PageBreak())
S.append(Paragraph("12 &middot; The difference between the test page and a real clinic", H2))
S.append(tbl([["", "On the test page", "In a real clinic"],
              ["The clinic code", "typed into the page, or built into it",
               "<b>read from the laptop</b> with the "
               "<font face='Courier'>doctors</font> command"],
              ["The recorder", "may be the only one running", "fourteen rooms, each its own laptop "
                                                              "and its own enrolment"],
              ["<b>The match</b>", "<b>not shown</b>", "<b>decides whether the recording lives.</b> "
                                                       "Section 9"],
              ["API 2 timing", "sent by hand, in order", "sent by your server, possibly late, "
                                                         "possibly retried"],
              ["A failure", "you see it on screen", "<b>often silent.</b> Which is why the morning "
                                                    "check on unconfirmed recordings exists"],
              ["The microphone", "you are watching it", "<b>nobody is.</b> Show the "
                                                        "<font face='Courier'>level</font> meter"]],
             [32 * mm, 54 * mm, 70 * mm], highlight=[3]))

S.append(Paragraph("13 &middot; One page to remember", H2))
S.append(tbl([["Question", "Answer"],
              ["How do I know which clinic a laptop is?",
               "Send <font face='Courier'>{\"command\": \"doctors\"}</font> and read "
               "<font face='Courier'>hospital_id</font>"],
              ["Should I store the clinic code in my page?",
               "<b>No.</b> Ask the laptop every time the connection opens"],
              ["What if <font face='Courier'>doctors</font> comes back empty?",
               "<b>Normal.</b> Proceed. Never block the clinic"],
              ["What if <font face='Courier'>hospital_id</font> is null?",
               "<b>Not enrolled.</b> Do not record; contact AIMS LAB"],
              ["Where is a recording stored?",
               "<font face='Courier'>clinic / doctor / date / "
               "patient_doctor_clinic_start_end_date.wav</font>"],
              ["What is beside it?", "A <font face='Courier'>.json</font> of the same name, with "
                                     "the clinical record CMED sent"],
              ["Does CMED ever get audio?", "<b>No. Never</b>"],
              ["Does <font face='Courier'>200 RECORDING_STARTED</font> mean safe?",
               "<b>No.</b> It means the microphone is live"],
              ["Does <font face='Courier'>202 ACCEPTED</font> mean safe?",
               "<b>No.</b> It means stored, not matched"],
              ["<b>So what makes it safe?</b>", "<b>The five fields matching between API 1 and "
                                                "API 2</b>"],
              ["What if they do not match?", "<b>Silently deleted after 24 hours.</b> CMED sees "
                                             "two green replies"],
              ["How do I make that impossible?", "<b>Build the five fields once and reuse the same "
                                                 "object for all four messages</b>"],
              ["What if API 2 is an hour late?", "Completely fine. It is confirmed when it arrives"],
              ["What if it is never sent?", "<b>The recording is lost.</b> Queue and retry"]],
             [54 * mm, 102 * mm], highlight=[10, 12]))
S.append(Spacer(1, 6))
S.append(Paragraph("Prepared by AIMS LAB, United International University. Companion to the CMED "
                   "Integration Specification. Every behaviour described here is taken from the "
                   "code that implements it.", SUB))


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7.3)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 9.5 * mm,
                      "AIMScribe v3 - guidance note for CMED - AIMS LAB, October 2026")
    canvas.drawRightString(A4[0] - 18 * mm, 9.5 * mm, "page %d" % doc.page)
    canvas.restoreState()


_ap = argparse.ArgumentParser()
_ap.add_argument("--out", default="CMED_GUIDANCE_NOTE.pdf")
OUT = Path(__file__).resolve().parent.parent / _ap.parse_args().out
SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                  topMargin=16 * mm, bottomMargin=20 * mm,
                  title="AIMScribe - Guidance Note for CMED",
                  author="AIMS LAB").build(S, onFirstPage=footer, onLaterPages=footer)
print("wrote", OUT)
