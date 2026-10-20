"""
Builds PATIENT_CLICK_HANDLING.pdf: how AIMScribe v3 keeps every consultation under
the right patient when a doctor clicks another patient's profile - by mistake or on
purpose - at the start, in the middle, or between consultations.

    python tools/make_click_handling_doc.py
"""
from pathlib import Path

from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import CondPageBreak, KeepTogether, PageBreak, Paragraph, Spacer

from cmed_doc_style import BODY, GOOD, H1, H2, H3, NOTE, SUB, build, part, tbl

OUT = Path(__file__).resolve().parent.parent / "PATIENT_CLICK_HANDLING.pdf"

A = colors.HexColor("#17836f")      # first patient
B = colors.HexColor("#c98a1b")      # second patient
C = colors.HexColor("#5b6bbf")      # third patient
INK = colors.HexColor("#0b0b0b")
MUTED = colors.HexColor("#898781")


def timeline(title, total, heard, files, marks):
    """
    Two bars over the same seconds: what the recorder heard (one recording, with
    each click marked) and the files the archive writes from it.
    heard/files: [(start, end, label, colour)]; marks: [(second, label)].
    """
    # All in points. Top to bottom: title, click labels, the heard bar with the
    # click lines standing on it, then the files bar.
    w, h, left, bar = 170 * mm, 66, 30 * mm, 10
    scale = (w - left - 6 * mm) / total
    d = Drawing(w, h)
    d.add(String(0, 56, title, fontName="Helvetica-Bold", fontSize=8.5, fillColor=INK))
    rows = [("Recorder heard", 28, heard), ("Archive writes", 12, files)]
    for name, y, spans in rows:
        d.add(String(0, y + 2.5, name, fontName="Helvetica", fontSize=7.4, fillColor=MUTED))
        for start, end, label, colour in spans:
            x0, x1 = left + start * scale, left + end * scale
            d.add(Rect(x0, y, x1 - x0, bar, fillColor=colour, strokeColor=colors.white,
                       strokeWidth=1))
            if x1 - x0 > 18 * mm:
                d.add(String(x0 + 2 * mm, y + 2.8, label, fontName="Helvetica-Bold",
                             fontSize=7, fillColor=colors.white))
    for second, label in marks:
        x = left + second * scale
        d.add(Line(x, 28, x, 42, strokeColor=INK, strokeWidth=0.9))
        d.add(String(x + 1.5, 43, label, fontName="Helvetica", fontSize=6.8, fillColor=INK))
    return d


def main():
    s = [Paragraph("Wrong or deliberate patient clicks", H1),
         Paragraph("How AIMScribe v3 keeps every consultation under the right patient when a "
                   "doctor opens another patient's profile - by mistake or on purpose - at "
                   "the start, in the middle, or between consultations. Follow-up to the "
                   "design review; updated 20 October 2026 for recorder 3.5.0.", SUB)]

    s.append(Paragraph("The problem", H2))
    s.append(Paragraph("A recording starts when the doctor opens a patient in CMED (API 1). "
                       "Doctors sometimes open the wrong patient first and correct it within "
                       "seconds; sometimes open another patient's file on purpose for a moment; "
                       "and sometimes finish a follow-up visit with no new prescription, so no "
                       "'consultation finished' signal (API 3a) ever comes before the next "
                       "patient. In every case the recording must end up under the patient who "
                       "was actually consulted - and no audio may be lost or deleted.", BODY))
    s.append(Spacer(1, 2 * mm))
    s.append(tbl([["Constraint", "Consequence for the design"],
                  ["CMED's code is final", "Only the messages CMED already sends can be used; "
                   "every reply must be one their code already handles"],
                  ["Only API 1 (patient opened) and API 3a (prescription built) reach the "
                   "recorder", "The recorder cannot see API 2 or 3b; only our server sees all four"],
                  ["At the moment of a click, intent is unknowable", "A wrong pick, a glance and "
                   "the next patient after a follow-up look identical when they happen"]],
                 [62 * mm, 112 * mm]))

    s.append(Paragraph("The principle: never refuse, never stop, never delete - decide later", H2))
    s.append(tbl([["Where", "What it does"],
                  ["Recorder, live",
                   "Before a prescription, every click on any patient is accepted "
                   "(RECORDING_STARTED with the running recording) and the recording carries on, "
                   "so a glance never cuts a consultation. Each click and each prescription is "
                   "signed into the recording with its exact second of audio. After a "
                   "prescription, the next different patient closes the recording and starts a "
                   "new one."],
                  ["Server, when the recording closes",
                   "Reads the order of clicks and prescriptions, plus every API 2 and 3b, and "
                   "applies five rules (below) to decide whose audio is whose. A 3b that arrives "
                   "later makes it decide again."],
                  ["Archive",
                   "Cuts the audio at the exact sample of a click into one file per patient. The "
                   "signed recording is never altered: the files rejoin byte for byte to it, "
                   "which is checked."]],
                 [34 * mm, 140 * mm]))
    s.append(Spacer(1, 2 * mm))
    s.append(Paragraph("The five rules", H3))
    s.append(tbl([["Rule", "Evidence", "Verdict"],
                  ["R1", "A prescription (3a or 3b) for a patient", "That patient was consulted; "
                   "their part is never merged away"],
                  ["R2", "The doctor went back to an earlier patient (reopened them, or built their "
                   "prescription)", "What lay between was a glance: it belongs to that patient"],
                  ["R3", "A patient open under 60 s, never returned to, never prescribed",
                   "A wrong pick or stray click: it belongs to the next patient"],
                  ["R4", "A patient open 60 s or more, no prescription, no return",
                   "A real consultation without a prescription (follow-up): its own file"],
                  ["R5", "Anything still unclear", "Kept, labelled by the best rule, and flagged "
                   "for a person on the dashboard's Needs review panel - never deleted"]],
                 [14 * mm, 82 * mm, 78 * mm]))

    # ---------------------------------------------------------------- cases
    s.append(PageBreak())
    s.append(part("CASES", "Every case, at the start, in the middle and between consultations",
                  "What the doctor did, what arrives, what the recorder does live, and the files "
                  "the archive writes."))
    s.append(Spacer(1, 3 * mm))
    cases = [
        ("Start", "Opens Rahim by mistake, opens Karim 8 s later, writes Karim's prescription",
         "API 1 Rahim, API 1 Karim, API 2 for both, 3a/3b Karim",
         "One recording; both clicks accepted", "<b>One file, Karim</b> (R3+R1). Rahim's API 2 "
         "kept, with no audio"),
        ("Start", "Opens the right patient, deliberately opens another for 10 s, goes back, "
         "prescribes", "API 1 A, API 1 B, API 1 A again, 3a/3b A",
         "One recording, nothing cut", "<b>One file, A</b> (R2). B's API 2 kept, no audio"),
        ("Start", "Opens A, opens B within a minute and stays with B, prescribes B",
         "API 1 A, API 1 B, 3a/3b B", "One recording",
         "<b>One file, B</b> (R3+R1): a first minute that was never returned to is a correction"),
        ("Middle", "Mid-consultation glance at another file, then back to the patient and "
         "prescribes", "API 1 B mid-way, then 3a/3b A", "Recording carries on",
         "<b>One file, A</b> (R2): building A's prescription counts as going back"),
        ("Between", "Follow-up with no prescription (Sakib), then the next patient (Salma), "
         "who is prescribed", "API 1 Sakib, API 1 Salma, 3a/3b Salma",
         "One recording until the next click after Salma's prescription",
         "<b>Two files</b>: Sakib from the start to Salma's click (R4), Salma from there (R1)"),
        ("Between", "Two or three follow-ups in a row, then a prescribed patient",
         "API 1 for each, 3a/3b for the last", "One recording",
         "<b>One file per patient</b>, each cut at the next patient's click (R4 ... R1)"),
        ("Between", "Accidental tap on another patient after the prescription",
         "3a A, API 1 B, then API 1 C", "A closes at the tap; a new recording starts",
         "A stays A; the short tap on B is merged into C (R3)"),
        ("End", "Last patient, prescription, nobody clicks again", "3a A only",
         "Closes itself 2 min after the prescription (up to 10 s more, at a pause)",
         "<b>One file, A</b>; the doctor's own time is not recorded"),
        ("End", "Last patient, a follow-up with no prescription, no further click", "API 1 only",
         "Closes after 15 min without CMED activity, at Stop, or cleanly at Windows shutdown",
         "<b>One file</b> (R4)"),
        ("Any", "The same patient clicked twice or the page reloaded", "API 1 again",
         "Same recording, no extra marker", "No change"),
    ]
    s.append(tbl([["When", "What the doctor did", "What arrives", "Recorder, live", "Files written"]]
                 + [list(r) for r in cases],
                 [15 * mm, 46 * mm, 36 * mm, 36 * mm, 41 * mm]))

    # ---------------------------------------------------------------- pictures
    s.append(PageBreak())
    s.append(Paragraph("Four of them, drawn to scale", H2))
    figs = [
        timeline("1. Wrong pick at the start: Rahim by mistake, Karim 8 s later", 54,
                 [(0, 54, "one recording", MUTED)],
                 [(0, 54, "Karim  (R3+R1)", B)],
                 [(0, "Rahim"), (8, "Karim"), (48, "3a Karim")]),
        timeline("2. Deliberate click on another file, then back", 90,
                 [(0, 90, "one recording", MUTED)],
                 [(0, 90, "A  (R2)", A)],
                 [(0, "A"), (40, "B"), (50, "A again"), (80, "3a A")]),
        timeline("3. Glance in the middle of a long consultation", 300,
                 [(0, 300, "one recording", MUTED)],
                 [(0, 300, "A  (R2)", A)],
                 [(0, "A"), (180, "B"), (190, "3a A")]),
        timeline("4. Follow-up with no prescription, then the next patient (live run)", 116,
                 [(0, 116, "one recording", MUTED)],
                 [(0, 70.1, "Sakib  (R4)", A), (70.1, 116, "Salma  (R1)", B)],
                 [(0, "Sakib"), (70.1, "Salma"), (110, "3a Salma")]),
    ]
    for f in figs:
        s.append(KeepTogether([f, Spacer(1, 4 * mm)]))

    s.append(Paragraph("Two API 2s for one recording", H2))
    s.append(Paragraph("Each click CMED accepts brings that patient's API 2 to our server. API 2 "
                       "means only 'this profile was opened' - it cannot tell a consultation "
                       "from a glance, and neither is wrong data: both are genuine records "
                       "about real patients. So no API 2 is ever deleted or trusted on its own. "
                       "The recorder asks the server for a permission for each clicked patient, "
                       "which ties that patient's API 2 and 3b to this recording. The rules "
                       "above decide which of them gets audio; the others stay as 'opened, "
                       "not consulted'. A late record for a patient whose part was moved "
                       "elsewhere is never attached to someone else's file.", BODY))

    s.append(CondPageBreak(60 * mm))
    s.append(tbl([["What a click can never do (recorder 3.5.0 and the current server)"],
                  ["<b>Erase a consultation.</b> Nothing the page sends can: a refusal sent over "
                   "the socket is recorded as an ordinary stop and the audio is kept. Only the "
                   "doctor's on-screen 'patient refused' control erases, and the recorder signs "
                   "that refusal with the PC's own key; the server then removes the audio, the "
                   "clinical record, the archive file and its cloud copy."],
                  ["<b>Put one patient's audio in another's file.</b> Each recording has its "
                   "own clip writer: even with a full disk, a clip that waited is written into "
                   "the recording it was captured in, never the next patient's. Every clip's "
                   "signed record names that clip, so the server refuses one filed elsewhere."],
                  ["<b>Reach another doctor's recorder.</b> With two Windows users signed in, "
                   "a page in the other user's session is refused."]],
                 [174 * mm], bold_first=True))

    # ---------------------------------------------------------------- evidence
    s.append(CondPageBreak(90 * mm))
    s.append(Paragraph("Evidence", H2))
    s.append(tbl([["What", "Result"],
                  ["Live, real recorder, real server and archive (16 October)",
                   "Wrong pick: one file, Karim, R3+R1. Follow-up: Sakib 0-70.1 s (R4) and Salma "
                   "from 70.1 s (R1). Counselling limit closed the recording 120.2 s after the "
                   "prescription. A pause during a recording kept the cut exact"],
                  ["Integrity of the cut", "Sakib's and Salma's files rejoined to the signed "
                   "recording's SHA-256 exactly"],
                  ["Server rules", "backend tests/test_labelling.py: every case above plus rapid "
                   "clicks, double clicks, out-of-order events, a late 3b changing a verdict"],
                  ["Recorder", "tests/test_markers.py and test_channel_a.py: replies, markers, "
                   "both time limits, the grace at a pause"],
                  ["Archive", "archive_worker test_parts.py: cuts exact to the sample, parts "
                   "rejoin, interrupted runs resume, re-cuts refused if the hash differs"],
                  ["Against CMED's finished software", "Not yet: one session with "
                   "tools/cmed_build_session.py will show their page's real messages"]],
                 [54 * mm, 120 * mm]))

    s.append(CondPageBreak(60 * mm))
    s.append(Paragraph("What remains, and how it is caught", H2))
    s.append(tbl([["Case", "Why it cannot be settled automatically", "What happens"],
                  ["A follow-up shorter than 60 s", "Looks exactly like a wrong pick",
                   "Merged into the next patient; flagged for review"],
                  ["A glance with no return signal and no prescription", "Nothing says it was "
                   "a glance", "Kept as its own part; flagged for review"],
                  ["A phone call in the room during a follow-up", "Speech is speech; no software "
                   "can tell a call from a consultation", "The doctor's Pause button; the "
                   "15-minute limit caps it"],
                  ["CMED's page not sending API 2 for a click", "Their code, not ours",
                   "The part is archived unconfirmed and kept; checked in the CMED session"]],
                 [46 * mm, 64 * mm, 64 * mm]))
    s.append(Spacer(1, 3 * mm))
    s.append(Paragraph("To check it yourself: the CMED page's manual-test cards 1-8 "
                       "(http://localhost:3000/manual-test) and Part 4A of the local manual.",
                       GOOD))

    build(s, OUT, "AIMScribe v3 - Wrong or deliberate patient clicks",
          "AIMScribe v3 - Patient click handling - AIMS LAB, UIU")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
