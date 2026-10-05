#!/usr/bin/env python3
"""
Build CMED_SPECIFICATION.pdf - exactly what CMED must send AIMS LAB.

    python tools/make_cmed_spec.py

Every rule in it is taken from the code that actually validates the messages
(backend/src/clinical.py and clinical_model.py) and from the tables that store
them (backend/scripts/clinical/001_aims_clinical.sql). No secrets, no data.
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
CODE = ParagraphStyle("CODE", parent=ss["Code"], fontName="Courier", fontSize=7.6,
                      textColor=INK, leading=10.2, backColor=CODEBG,
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
S.append(Paragraph("What CMED Sends AIMS LAB", H1))
S.append(Paragraph("The complete specification: every field, its name, its type, and what "
                   "happens to it &middot; AIMScribe v3 &middot; 5 October 2026", SUB))
S.append(HRFlowable(width="100%", thickness=1, color=RULE, spaceAfter=8))
S.append(Paragraph("Every rule in this document is taken from the program that actually checks "
                   "the messages when they arrive. If this document and the server ever "
                   "disagree, the server is right and this is a bug &mdash; tell us.", BODY))
S.append(Spacer(1, 5))
S.append(tbl([["In one sentence"],
              ["CMED sends <b>two signals from the doctor's browser</b> so the recorder knows "
               "when a consultation starts and ends, and <b>two messages from CMED's server</b> "
               "carrying the patient's information and the prescription. "
               "<b>Audio never goes to CMED, and nothing clinical ever comes back.</b>"]],
             [156 * mm], bold_first=False))

# ================================================================ 1
S.append(Paragraph("1 &middot; The four things CMED builds", H2))
S.append(tbl([["", "What", "From where", "When", "Carries"],
              ["<b>API 1</b>", "Start recording", "the browser", "the doctor opens a patient",
               "the five fields"],
              ["<b>API 2</b>", "<b>Patient information</b>", "<b>CMED's server</b>",
               "the same moment", "<b>demographics, paramedic readings, previous visit</b>"],
              ["<b>API 3a</b>", "Prescription built", "the browser",
               "the doctor presses Build Prescription", "the five fields and the session id"],
              ["<b>API 3b</b>", "<b>The prescription</b>", "<b>CMED's server</b>",
               "the same moment", "<b>medicines, diagnoses, investigations</b>"]],
             [16 * mm, 32 * mm, 28 * mm, 42 * mm, 38 * mm], highlight=[2, 4]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>This document covers API 2 and API 3b</b> &mdash; the two messages from "
                   "CMED's server that carry patient data. The two browser signals are in a "
                   "separate integration guide.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["", "Address"],
              ["<b>API 2</b> &mdash; patient information",
               "POST https://&lt;aims-lab-server&gt;/api/v2/clinical/patient-information"],
              ["<b>API 3b</b> &mdash; prescription",
               "POST https://&lt;aims-lab-server&gt;/api/v2/clinical/prescription"],
              ["Header on both", "X-CMED-Key: &lt;the key AIMS LAB gives you&gt;"],
              ["Body", "JSON, UTF-8, under 1 MB"],
              ["Success", "HTTP 202 &mdash; the message is stored before you get this reply"]],
             [50 * mm, 106 * mm], mono=(1,)))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>The key is a server credential. It must never appear in a browser</b>, in "
                   "page source, or in anything a user can view. Both messages go server to "
                   "server.", BAD))

# ================================================================ 2
S.append(Paragraph("2 &middot; The five fields &mdash; the most important rule in this document", H2))
S.append(Paragraph("Every message about one consultation carries these same five fields, with "
                   "<b>exactly the same values, character for character</b>. They are how we "
                   "match the prescription to the right recording.", BODY))
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
              ["<b>1. Sending the prescription's time as start_time.</b> API 3b must repeat the "
               "<b>original</b> start_time from when the patient was opened &mdash; not the time "
               "the prescription was built. This is the single most common integration mistake."],
              ["<b>2. Leaving off the time-zone offset.</b> "
               "<font face='Courier'>2026-10-05T10:14:32</font> is rejected. "
               "<font face='Courier'>+06:00</font> must be there."],
              ["<b>3. Spaces or other characters in an identifier.</b> These become folder names "
               "on disk, so only letters, digits, hyphen and underscore are allowed."]],
             [156 * mm], bold_first=False))

S.append(PageBreak())

# ================================================================ 3
S.append(Paragraph("3 &middot; API 2 &mdash; patient information", H2))
S.append(Paragraph("Sent the moment the doctor opens the patient, at the same time as the "
                   "browser signal. It carries everything known <b>before</b> the consultation.", BODY))
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

S.append(H3 and Paragraph("3.1 &nbsp; demographics &mdash; required", H3))
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

S.append(H3 and Paragraph("3.2 &nbsp; paramedic &mdash; optional, but send it when you have it", H3))
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

S.append(PageBreak())

# ================================================================ 4
S.append(Paragraph("4 &middot; previous_visit &mdash; the rule, in full", H2))
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
              ["prescription", "Same shape as API 3b, section 5. Only <font face='Courier'>items"
                               "</font> really matters"],
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

# ================================================================ 5
S.append(Paragraph("5 &middot; API 3b &mdash; the prescription", H2))
S.append(Paragraph("Sent the moment the doctor presses <b>Build Prescription</b>. Note that this "
                   "does <b>not</b> stop the recording &mdash; the doctor goes on counselling the "
                   "patient, and that audio is valuable.", BODY))
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

# ================================================================ 6
S.append(Paragraph("6 &middot; Corrections, repeats and retries", H2))
S.append(tbl([["Situation", "What to do", "What happens"],
              ["The doctor edits the prescription after it was sent",
               "Send API 3b again with the <b>same five fields</b>",
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

# ================================================================ 7
S.append(Paragraph("7 &middot; Every reply, and what to do about it", H2))
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
S.append(Paragraph("<b>Please queue and retry rather than sending once and hoping.</b> If our "
                   "server is briefly unreachable, the consultation still happened and the "
                   "recording still exists &mdash; the clinical record needs to catch up to it. "
                   "A message that arrives an hour late is fine. A message that is never sent "
                   "leaves a recording that is erased after 24 hours.", BAD))

# ================================================================ 8
S.append(Paragraph("8 &middot; Rules that are easy to miss", H2))
S.append(tbl([["", "Rule", "Why"],
              ["1", "<b>Never let a message delay the doctor.</b> Send from a background queue",
               "A slow network must never make Build Prescription feel slow"],
              ["2", "<b>start_time in API 3b is the original</b>, from when the patient was opened",
               "It is how the prescription finds its recording"],
              ["3", "<b>Every timestamp carries its offset</b> (+06:00)",
               "A time without a zone is stored as empty"],
              ["4", "<b>previous_visit must be present</b>, even if null",
               "Missing means \"CMED forgot\"; null means \"nothing to send\""],
              ["5", "<b>items, diagnoses and investigations are always lists</b>, empty if none",
               "Same reason"],
              ["6", "<b>sex is the word</b> <font face='Courier'>female</font> or "
                    "<font face='Courier'>male</font>", "Codes like M, F, 1, 2 are rejected"],
              ["7", "<b>hospital_id must be the code AIMS LAB gives you</b>",
               "A recording claiming a clinic the laptop is not enrolled to is <b>refused</b>"],
              ["8", "<b>The key never reaches a browser</b>", "It is a server credential"],
              ["9", "Unknown fields are welcome", "They are kept. Do not strip data to fit"]],
             [7 * mm, 76 * mm, 73 * mm], bold_first=False))

S.append(PageBreak())

# ================================================================ 9
S.append(Paragraph("9 &middot; Where every field ends up", H2))
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

# ================================================================ 10
S.append(Paragraph("10 &middot; How CMED can test, today", H2))
S.append(Paragraph("You do not need to wait for us, and you do not need a real patient.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["", "What", "How"],
              ["1", "Check a message against the rules, with nothing running",
               "<font face='Courier'>python tools/channel_b_test.py --local</font>"],
              ["2", "Send real messages to a test server",
               "<font face='Courier'>python tools/channel_b_test.py --server https://&lt;host&gt; "
               "--key &lt;your key&gt;</font>"],
              ["3", "See every message and reply side by side",
               "open <font face='Courier'>/protocol-test</font> on the test site"]],
             [7 * mm, 62 * mm, 87 * mm], bold_first=False))
S.append(Spacer(1, 3))
S.append(Paragraph("Step 2 runs every case that matters &mdash; a good message, the same message "
                   "twice, a broken one, a changed prescription, a wrong key, an oversized body "
                   "&mdash; and checks each reply against this document. <b>Please run it and "
                   "send us the output before the first clinic goes live.</b>", GOOD))

# ================================================================ 11
S.append(Paragraph("11 &middot; What each side must hand over", H2))
S.append(tbl([["AIMS LAB gives CMED", "CMED gives AIMS LAB"],
              ["The server address", "<b>The exact web address of your page</b> (for example "
                                     "https://ehr.aaloclinic.com) &mdash; including staging"],
              ["<b>The Channel B key</b> &mdash; shown once",
               "<b>Your own clinic codes</b>, so we can map them to ours"],
              ["The seven clinic codes", "A test environment we can send to"],
              ["This document and the integration guide", "A named contact for integration"]],
             [78 * mm, 78 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>The two we need from CMED are blocking.</b> Without your page's exact "
                   "address the browser cannot talk to the recorder at all. Without your clinic "
                   "codes, a visit you describe cannot be matched to the clinic it happened in "
                   "&mdash; the record is stored, you get a normal success reply, and the "
                   "<b>recording is quietly erased after 24 hours</b>. That failure is silent "
                   "from your side, which is why it matters more than it sounds.", BAD))

S.append(PageBreak())

# ================================================================ 12
S.append(Paragraph("12 &middot; One page for the CMED developer", H2))
S.append(tbl([["Question", "Answer"],
              ["How many messages do I send?", "Two from the browser, two from the server. This "
                                               "document is the two from the server"],
              ["Where do I send them?", "<font face='Courier'>/api/v2/clinical/patient-information"
                                        "</font> and <font face='Courier'>/api/v2/clinical/"
                                        "prescription</font>"],
              ["How do I authenticate?", "Header <font face='Courier'>X-CMED-Key</font>. Server "
                                         "to server only"],
              ["What ties the messages together?", "<b>The five fields, identical in all of "
                                                   "them</b>"],
              ["Biggest trap", "<b>start_time in the prescription must be the original</b>, not "
                               "the time the prescription was built"],
              ["What is required in API 2?", "<font face='Courier'>demographics</font>, and "
                                             "<font face='Courier'>previous_visit</font> present "
                                             "(null is fine)"],
              ["What is required in API 3b?", "<font face='Courier'>issued_at</font>, "
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
S.append(Paragraph("Prepared by AIMS LAB, United International University. Every rule here is "
                   "taken from the code that validates these messages on arrival. If anything is "
                   "unclear, ask before building &mdash; it is far cheaper than discovering it "
                   "in a clinic.", SUB))


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7.3)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 9.5 * mm,
                      "AIMScribe v3 - what CMED sends AIMS LAB - October 2026")
    canvas.drawRightString(A4[0] - 18 * mm, 9.5 * mm, "page %d" % doc.page)
    canvas.restoreState()


OUT = Path(__file__).resolve().parent.parent / "CMED_SPECIFICATION.pdf"
SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                  topMargin=16 * mm, bottomMargin=20 * mm,
                  title="What CMED Sends AIMS LAB - AIMScribe v3",
                  author="AIMS LAB").build(S, onFirstPage=footer, onLaterPages=footer)
print("wrote", OUT)
