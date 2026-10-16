#!/usr/bin/env python3
"""
Build DIGITALOCEAN_DEPLOYMENT_RUNBOOK.pdf - the step-by-step manual for moving
the whole AIMScribe v3 server to DigitalOcean, written so that someone who has
never run a Linux server can follow it.

    python deploy/cloud/make_deployment_runbook.py [--out NAME.pdf]

Every port, path, service, variable and command is taken from the stack itself:
  deploy/uiu/docker-compose.yml          the nine services, the required variables
  deploy/uiu/.env.example                every setting and its default
  deploy/uiu/Caddyfile                   TLS, the only open ports, the 2 MB cap
  deploy/uiu/postgres/                   the database profiles and the schema
  deploy/uiu/backup/                     the backup image, backup.sh, restore.sh
  tools/check_key_pair.py                the server's keys against the installer's
  deploy/uiu/monitor/monitor.py          the disk floors and the alert webhook
  backend/src/api_v2.py                  the admin endpoints and their bodies
  backend/src/clinical.py                the two CMED endpoints and their replies
  backend/scripts/mint_enrolment_tokens.py
  recorder/installer.iss, recorder/core/enrollment.py
No secrets: every credential below is a placeholder or generated on the server.
"""
import argparse
import os
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, HRFlowable, PageBreak, Preformatted,
                                KeepTogether)

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

# The account owner's address, printed where each sign-up asks for it. Read
# from the environment so a personal address is not published with the
# repository: AIMS_OWNER_EMAIL=you@example.org python make_deployment_runbook.py
OWNER_EMAIL = os.getenv("AIMS_OWNER_EMAIL", "your-team-email@example.org")
HOST = "aimscribe.uiu.ac.bd"

ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Title"], fontName="Helvetica-Bold",
                    fontSize=19, textColor=INK, spaceAfter=2, alignment=0, leading=23)
SUB = ParagraphStyle("SUB", parent=ss["Normal"], fontSize=9.5, textColor=MUTED, spaceAfter=9)
H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontName="Helvetica-Bold", keepWithNext=1,
                    fontSize=13, textColor=TEAL, spaceBefore=15, spaceAfter=5, leading=16)
H3 = ParagraphStyle("H3", parent=ss["Heading3"], fontName="Helvetica-Bold", keepWithNext=1,
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
CODE = ParagraphStyle("CODE", parent=ss["Code"], fontName="Courier", fontSize=7.4,
                      textColor=INK, leading=9.9, backColor=CODEBG,
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
    t = Table(data, colWidths=widths, hAlign="LEFT", repeatRows=1)
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


def box(title, text):
    """A one-cell boxed warning or explanation."""
    return tbl([[title], [text]], [156 * mm], bold_first=False)


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


def code(text):
    return Preformatted(text, CODE)


def trouble(rows):
    """'If you see this instead' table: [(symptom, meaning and fix), ...]."""
    return tbl([["If you see this instead", "What it means, and what to do"]] + list(rows),
               [62 * mm, 94 * mm])


def expect(text):
    return Paragraph("<b>You should see:</b> " + text, GOOD)


def step(title, *flow):
    """A step heading kept with its first block, so a heading never ends a page."""
    head = Paragraph(title, H2)
    if flow:
        return [KeepTogether([head, flow[0]])] + list(flow[1:])
    return [head]


def c(s):
    return "<font face='Courier'>%s</font>" % s


S = []

# ============================================================ COVER
S.append(Paragraph("Deploying the AIMScribe Server to DigitalOcean", H1))
S.append(Paragraph("The complete manual: accounts, droplet, storage, the stack, the endpoints, "
                   "the clinic PCs, and keeping it running &middot; AIMS LAB &middot; "
                   "15 October 2026", SUB))
S.append(HRFlowable(width="100%", thickness=1, color=RULE, spaceAfter=8))
S.append(Paragraph("This manual moves the <b>whole AIMScribe v3 server</b> &mdash; nine services, "
                   "both databases, the gateway, the archive worker, the backups and the monitor "
                   "&mdash; onto one DigitalOcean droplet in Bangalore, with the audio in "
                   "Cloudflare R2. It is written for someone who has never run a Linux server: "
                   "every step says <b>what to type</b>, <b>what you should see</b>, and "
                   "<b>what to do if you see something else</b>.", BODY))
S.append(Spacer(1, 5))
S.append(tbl([["", "Stage", "Time", "Who"],
              ["<b>0</b>", "<b>Accounts</b> &mdash; DigitalOcean and Cloudflare, owned by one "
                           "email, with two-factor login", "30 min", "the owner"],
              ["<b>A</b>", "<b>The endpoint status table</b> &mdash; what works before and after. "
                           "Give this to CMED", "read it now", "you &rarr; CMED"],
              ["<b>B</b>", "Before you start: the checklist, the two R2 buckets, the plan", "45 min",
               "AIMS LAB"],
              ["<b>C</b>", "<b>The deployment, twelve steps</b>", "<b>3&ndash;4 hours</b>",
               "AIMS LAB"],
              ["<b>D</b>", "Proving every endpoint answers", "20 min", "AIMS LAB"],
              ["<b>E</b>", "<b>Daily and weekly checks</b> &mdash; one page", "5 min a day",
               "AIMS LAB"],
              ["<b>F</b>", "Updates, rollback, what was fixed, go-live checklist", "30 min", "AIMS LAB"]],
             [10 * mm, 90 * mm, 26 * mm, 30 * mm], highlight=[2, 4]))
S.append(Spacer(1, 5))
S.append(box("The one thing to understand before starting",
             "<b>CMED's page never learns the server address.</b> It talks to the recorder on "
             "the same PC, over loopback. Only <b>CMED's own server</b> and <b>each recorder</b> "
             "hold the AIMS LAB address. So this deployment changes exactly two things on the "
             "outside: one setting in CMED's server, and the server address in each recorder "
             "(set by re-running the installer, step 11). CMED's page is not touched."))
S.append(Spacer(1, 5))
S.append(tbl([["How to read the boxes in this manual"],
              ["<b>Grey box, typewriter text</b> &mdash; type it exactly. Lines starting with "
               + c("#") + " are explanations; you may type them or leave them out.<br/>"
               "<b>&lt;angle brackets&gt;</b> &mdash; replace with your own value, without the "
               "brackets.<br/>"
               "<b><font color='#006300'>Green text</font></b> &mdash; what a correct result looks "
               "like.<br/>"
               "<b>\"If you see this instead\" tables</b> &mdash; the common failures, and the fix "
               "for each.<br/>"
               "Commands that start with " + c("PS&gt;") + " are typed in <b>Windows PowerShell on "
               "your own laptop</b>. Everything else is typed <b>on the droplet</b>, over SSH."]],
             [156 * mm], bold_first=False))

S.append(PageBreak())

# ============================================================ PART 0
S.append(part("PART 0", "Accounts",
              "Two accounts, one owner email, two-factor login on both"))

S.append(Paragraph("0.1 &middot; The owner email", H2))
S.append(Paragraph("Every account and every automatic notice in this deployment goes to one "
                   "address, so nothing important lands in a mailbox nobody reads:", BODY))
S.append(Spacer(1, 3))
S.append(tbl([["Where the email is used", "Why"],
              ["<b>DigitalOcean account owner</b>", "billing, droplet alerts, password resets"],
              ["<b>Cloudflare account owner</b>", "R2 storage, billing, API token notices"],
              ["<b>" + c("AIMS_ACME_EMAIL") + "</b> in the server settings (step 6)",
               "Let's Encrypt writes here if the HTTPS certificate is about to expire"],
              ["DigitalOcean monitoring and uptime alerts (step 12)",
               "CPU, memory, disk, and \"the server stopped answering\""],
              ["The comment on your SSH key (step 1)", "so you can tell whose key it is later"]],
             [72 * mm, 84 * mm]))
S.append(Spacer(1, 3))
S.append(code("Owner email:   " + OWNER_EMAIL))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>The server's own alerts do not go to email.</b> The monitor service sends "
                   "its alerts (disk filling, recordings not confirmed, backups missing) to a "
                   "<b>webhook</b> &mdash; " + c("AIMS_ALERT_WEBHOOK") + " &mdash; such as a "
                   "Slack, Discord or Google Chat incoming-webhook address. There is no email "
                   "setting in the stack. Email alerts come from DigitalOcean's own monitoring, "
                   "set up in step 12.", NOTE))

S.append(Paragraph("0.2 &middot; DigitalOcean", H2))
S.append(tbl([["", "Do this", "Where"],
              ["1", "Sign up with <b>" + OWNER_EMAIL + "</b>. Add a payment card",
               "cloud.digitalocean.com &rarr; Sign up"],
              ["2", "<b>Turn on two-factor login</b> (an authenticator app, not SMS)",
               "avatar &rarr; My Account &rarr; Security &rarr; Two-factor authentication"],
              ["3", "Create a team named <b>AIMS LAB</b>; invite each lab member who will "
                    "administer the server, as <b>Member</b> (not Owner)",
               "Settings &rarr; Team &rarr; Invite members"],
              ["4", "Set a <b>billing alert</b> at $150 a month, so a mistake shows before the "
                    "invoice does", "Billing &rarr; Billing alerts"],
              ["5", "Create a project named <b>AIMScribe</b>; everything in this manual goes in it",
               "left menu &rarr; New Project"]],
             [8 * mm, 86 * mm, 62 * mm], req=[2]))
S.append(Spacer(1, 3))
S.append(trouble([
    ("\"Account under review\" after sign-up",
     "DigitalOcean checks new accounts. It can take a few hours. Nothing else in this manual "
     "can start until it clears."),
    ("The card is declined", "Some Bangladeshi cards need international online payments enabled "
                             "by the bank first. Or use PayPal in the same billing page."),
]))

S.append(Paragraph("0.3 &middot; Cloudflare", H2))
S.append(tbl([["", "Do this", "Where"],
              ["1", "Sign up with <b>" + OWNER_EMAIL + "</b>", "dash.cloudflare.com &rarr; Sign up"],
              ["2", "<b>Turn on two-factor login</b>", "My Profile &rarr; Authentication"],
              ["3", "Open <b>R2 Object Storage</b> and add a payment method. R2 has a free tier, "
                    "but a card is required to enable it", "left menu &rarr; R2 Object Storage"],
              ["4", "Note your <b>Account ID</b>. It is part of the storage address",
               "R2 Object Storage overview page, right-hand side"],
              ["5", "Invite lab members who need it, with the narrowest role that works",
               "Manage Account &rarr; Members"]],
             [8 * mm, 86 * mm, 62 * mm], req=[2]))
S.append(Spacer(1, 4))
S.append(box("Write the recovery codes down",
             "Both services give <b>recovery codes</b> when two-factor login is turned on. "
             "Store them with the access sheet, off this laptop. Losing the phone without them "
             "can lock AIMS LAB out of its own server and its own recordings for days."))

S.append(PageBreak())

# ============================================================ PART A
S.append(part("PART A", "The endpoint status table",
              "What works today, what works after deployment - the page to send CMED"))

S.append(Paragraph("1 &middot; Every endpoint, before and after", H2))
S.append(Paragraph("<b>This is the table to put in front of CMED.</b> \"Today\" means the system "
                   "as it runs now, on a PC on a private network. \"After\" means once Part C "
                   "is done.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["", "Endpoint", "Today", "After", "Why"],
              ["<b>1</b>", c("GET https://&lt;host&gt;/health"),
               "<b>NO</b>", "<b>YES</b>",
               "Needs a public name and a certificate. Both arrive with the droplet. No key"],
              ["<b>2</b>", c("GET https://&lt;host&gt;/"),
               "<b>NO</b>", "<b>YES</b>", "same"],
              ["<b>3</b>", c("POST https://&lt;host&gt;") + "<br/>" + c("/api/v2/clinical/patient-information")
               + "<br/><i>API 2</i>",
               "<b>NO</b>", "<b>YES</b>",
               "<b>The one CMED is waiting for.</b> Finished and tested; it has had no public "
               "address to answer on. Header " + c("X-CMED-Key")],
              ["<b>4</b>", c("POST https://&lt;host&gt;") + "<br/>" + c("/api/v2/clinical/prescription")
               + "<br/><i>API 3, Channel B</i>",
               "<b>NO</b>", "<b>YES</b>", "same as 3"],
              ["<b>5</b>", c("ws://127.0.0.1:5050/ws") + " &mdash; " + c("start")
               + "<br/><i>API 1</i>",
               "<b>YES</b>", "<b>YES</b>",
               "<b>Unaffected.</b> Loopback on the clinic PC &mdash; never involved our server"],
              ["<b>6</b>", c("ws://127.0.0.1:5050/ws") + " &mdash; " + c("prescription_built")
               + "<br/><i>API 3, Channel A</i>", "<b>YES</b>", "<b>YES</b>", "Unaffected"],
              ["<b>7</b>", c("ws://127.0.0.1:5050/ws") + " &mdash; " + c("doctors"),
               "<b>YES</b>", "<b>YES</b>", "Unaffected"],
              ["<b>8</b>", c("ws://127.0.0.1:5050/ws") + " &mdash; " + c("status"),
               "<b>YES</b>", "<b>YES</b>", "Unaffected"],
              ["<b>9</b>", c("GET https://&lt;host&gt;") + "<br/>"
               + c("/api/v2/clinical/patient/{id}/visits") + "<br/><i>new: what we hold</i>",
               "<b>NO</b>", "<b>YES</b>", "same as 3. No key: <b>401</b> " + c("INVALID_KEY")],
              ["<b>10</b>", c("POST https://&lt;host&gt;") + "<br/>" + c("/api/v2/session/reassign")
               + "<br/><i>new: recorders only</i>", "&mdash;", "<b>YES</b>",
               "Not CMED's. Needs schema script 013. No device token: <b>401</b>"],
              ["<b>11</b>", c("POST https://&lt;host&gt;") + "<br/>" + c("/api/v2/device/health")
               + "<br/><i>new: recorders only</i>", "&mdash;", "<b>YES</b>",
               "Not CMED's. Needs schema script 014. No device token: <b>401</b>"]],
             [8 * mm, 68 * mm, 13 * mm, 13 * mm, 54 * mm],
             highlight=[5, 6, 7, 8], req=[1, 2, 3, 4, 9]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Read the shape of that table.</b> The four local ones already work and are "
                   "untouched by the deployment. The five server ones CMED calls are blocked by "
                   "one cause &mdash; no public address &mdash; and are unblocked by one act. "
                   "Rows 10 and 11 are the recorders' own, and need nothing from CMED.", GOOD))

S.append(Paragraph("2 &middot; What API 2 and API 3 Channel B answer", H2))
S.append(Paragraph("The replies are the same before and after deployment. These are the ones "
                   "CMED's outbox must act on, including what happens when it sends the "
                   "same visit twice:", BODY))
S.append(Spacer(1, 3))
S.append(tbl([["CMED sends", "Reply", "What it means / what CMED does"],
              ["A new visit (API 2) or prescription (API 3b)", "<b>202</b> " + c("ACCEPTED"),
               "Stored. Remove it from the outbox. For API 2, " + c("previous_visit.status")
               + " says whether the history was " + c("ALREADY_STORED") + ", "
               + c("UPDATED") + " or " + c("STORED")],
              ["<b>The identical body again</b> (a retry after a timeout, or after an outage)",
               "<b>200</b> " + c("ALREADY_RECEIVED"),
               "<b>Nothing new stored.</b> It was already here. Remove it from the outbox"],
              ["<b>API 2 for the same visit</b> (same five fields) <b>with a different body</b>",
               "<b>202</b> " + c("ACCEPTED"),
               "Stored as a newer record of that visit; <b>the newest is the one used for "
               "matching</b>. Remove it from the outbox"],
              ["<b>A prescription for the same visit with a changed body</b>",
               "<b>202</b> " + c("ACCEPTED") + ", " + c('"version": 2'),
               "Kept as version 2 &mdash; a correction. Earlier versions are kept too"],
              ["The same prescription rebuilt &mdash; a new " + c("issued_at") + ", keys "
               "reordered", "<b>200</b> " + c("ALREADY_RECEIVED"),
               "Same fingerprint, so nothing new. Remove it from the outbox"],
              ["No key, or a wrong one", "<b>401</b>", "Do not retry. Check the key"],
              ["Body over 1 MB", "<b>413</b>", "Do not retry. Report it to AIMS LAB"],
              ["A field malformed, or a previous visit named by only some of its five "
               "fields", "<b>422</b> " + c("SCHEMA_INVALID"),
               "Kept aside for review. Do not retry the same body; fix and send again"],
              ["Our server down, the branch's internet down, or a network error",
               "no reply / <b>5xx</b>",
               "<b>Keep it in the outbox and retry later.</b> Late is fine: it attaches to its "
               "recording whenever it lands"]],
             [58 * mm, 36 * mm, 62 * mm], highlight=[2]))
S.append(Spacer(1, 3))
S.append(Paragraph("The five fields are " + c("patient_id") + ", " + c("doctor_id") + ", "
                   + c("hospital_id") + ", " + c("start_time") + " and " + c("date")
                   + ". The full rules are in CMED_1, the software integration document.", BODY))

S.append(Paragraph("3 &middot; What to say to CMED, in writing", H2))
S.append(Paragraph("A note you can send as it stands:", BODY))
S.append(Spacer(1, 2))
S.append(code(
    'The four endpoints your page uses on the clinic PC work today and are not\n'
    'affected by our deployment. Your front-end team can build and test against\n'
    'them now - install the recorder on one PC and they are live:\n'
    '\n'
    '    ws://127.0.0.1:5050/ws   commands: start, prescription_built,\n'
    '                                       doctors, status\n'
    '\n'
    'The endpoints your server uses are finished and tested, and will answer\n'
    'as soon as our DigitalOcean deployment is done:\n'
    '\n'
    '    POST  https://<host>/api/v2/clinical/patient-information   (API 2)\n'
    '    POST  https://<host>/api/v2/clinical/prescription          (API 3, Channel B)\n'
    '    GET   https://<host>/api/v2/clinical/patient/{id}/visits - what we hold\n'
    '    GET   https://<host>/health              - no key, to check we are up\n'
    '\n'
    'While a branch has no internet, keep both POSTs in an outbox on its\n'
    'local server and send them when it returns. Late is fine.\n'
    '\n'
    'Nothing about the message format, the reply codes, the key or the limits\n'
    'changes between now and then. Please hold the host as configuration, not\n'
    'a compiled-in constant: we will give you a staging host first and a\n'
    'production host later.'))

S.append(PageBreak())

# ============================================================ PART B
S.append(part("PART B", "Before you start",
              "The checklist, the storage buckets, and what will be running"))

S.append(Paragraph("4 &middot; What you need in hand", H2))
S.append(tbl([["", "What", "Where from", "Note"],
              ["&#9744;", "The two accounts from Part 0, with two-factor login on", "Part 0", ""],
              ["&#9744;", "<b>A domain name you control</b>", "a UIU subdomain from UIU IT, "
                                                             "or a registrar",
               "<b>Required.</b> A certificate cannot be issued for a bare IP address"],
              ["&#9744;", "The two R2 buckets and their tokens", "section 6 below",
               "four values, each shown once"],
              ["&#9744;", "An SSH key pair", "your laptop, step 1",
               "password login will be turned off"],
              ["&#9744;", "CMED's code for each clinic", "CMED", "<b>needed at step 10</b>"],
              ["&#9744;", "The list of clinic laptops (clinic, room)", "the clinics",
               "<b>needed at step 11</b>"],
              ["&#9744;", "A password manager or the access sheet", "AIMS LAB",
               "<b>six secrets must be stored off the server</b> (step 6)"]],
             [10 * mm, 52 * mm, 44 * mm, 50 * mm], req=[2]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>The domain is the only hard prerequisite.</b> The gateway asks Let's "
                   "Encrypt for a certificate the first time it starts, and that only works when "
                   "the name already points at the droplet. This manual uses " + c(HOST)
                   + " as the example name; use whatever name you actually get.", BAD))

S.append(Paragraph("5 &middot; What will be running when you are done", H2))
S.append(tbl([["Service", "What it does", "Exposed?"],
              ["<b>gateway</b> (Caddy)", "HTTPS, the certificate, security headers, a 2 MB body cap",
               "<b>yes &mdash; 80 and 443</b>"],
              ["<b>api</b>", "every endpoint, on internal port 6000", "no"],
              ["postgres", "both databases: " + c("aims_recordings") + " and "
               + c("aims_clinical"), "no"],
              ["pgbouncer", "connection pooling", "no"],
              ["redis", "the work queue", "no"],
              ["worker", "transcription (needs the Azure settings; optional)", "no"],
              ["<b>archive-worker</b>", "pulls the pieces of each recording, joins them, keeps "
                                       "the archive copy and uploads the encrypted cloud copy",
               "no"],
              ["backup", "nightly dump of both databases, encrypted &mdash; <b>does not run in "
                         "its image yet; step 12 replaces it</b>", "no"],
              ["monitor", "disk, unconfirmed recordings, certificate, backups", "no"]],
             [34 * mm, 90 * mm, 32 * mm], highlight=[1]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Only the gateway is reachable from outside, on ports 80 and 443.</b> "
                   "Every other service talks on a private Docker network that has no route "
                   "to the internet except through the gateway.", GOOD))

S.append(Paragraph("6 &middot; Cloudflare R2: two buckets, two tokens", H2))
S.append(Paragraph("<b>The stack needs two separate buckets, not one.</b> The server refuses to "
                   "make cloud copies if both settings name the same bucket (it logs a critical "
                   "error and makes none).", BAD))
S.append(Spacer(1, 3))
S.append(tbl([["Bucket", "What goes in it", "Setting it fills"],
              ["<b>" + c("aimscribe-segments") + "</b>",
               "The short pieces each recorder uploads while it records. The recorders upload "
               "straight to it, using a short-lived signed address from our API",
               c("MINIO_BUCKET")],
              ["<b>" + c("aimscribe-archive") + "</b>",
               "One encrypted copy of each finished consultation &mdash; the permanent "
               "cloud archive (the 3 TB)", c("AIMS_COPY_BUCKET")]],
             [38 * mm, 82 * mm, 36 * mm]))
S.append(Spacer(1, 4))
S.append(tbl([["", "Do this in Cloudflare", "Result"],
              ["1", "R2 Object Storage &rarr; <b>Create bucket</b> &rarr; name "
                    + c("aimscribe-segments") + " &rarr; Location: <b>Automatic</b>, with the "
                    "hint <b>Asia-Pacific</b> if offered &rarr; Create", "bucket 1"],
              ["2", "Same again for " + c("aimscribe-archive"), "bucket 2"],
              ["3", "Leave <b>Public access off</b> on both. Never enable a public r2.dev address",
               "private"],
              ["4", "R2 &rarr; <b>Manage R2 API Tokens</b> &rarr; Create API token &rarr; name "
                    + c("aimscribe-segments-rw") + " &rarr; permission <b>Object Read &amp; "
                    "Write</b> &rarr; <b>Apply to specific buckets only</b>: "
                    + c("aimscribe-segments") + " &rarr; Create",
               "<b>Access Key ID</b> and <b>Secret Access Key</b> &mdash; shown once"],
              ["5", "Same again, named " + c("aimscribe-archive-rw") + ", for "
                    + c("aimscribe-archive") + " only", "a second pair &mdash; shown once"],
              ["6", "Copy the <b>S3 endpoint</b> shown with the token. It looks like "
                    + c("https://&lt;account-id&gt;.r2.cloudflarestorage.com"),
               "you will use it <b>without</b> the " + c("https://")]],
             [8 * mm, 108 * mm, 40 * mm], req=[4, 5]))
S.append(Spacer(1, 4))
S.append(trouble([
    ("The token page shows only \"Account API tokens\" and \"User API tokens\"",
     "Either works. Pick <b>Object Read &amp; Write</b> and restrict it to the one bucket."),
    ("The secret is gone after closing the page",
     "It is shown once. Delete that token and create a new one."),
    ("A \"Bucket lock\" or retention option is offered on " + c("aimscribe-archive"),
     "Worth turning on for the archive bucket only: it stops anyone deleting the "
     "permanent copies. Never on the segments bucket, which the server empties as it works."),
]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Keep the two pairs apart.</b> The segments token can delete; the archive "
                   "token must never be given to anything except this server's settings.", NOTE))

S.append(Paragraph("7 &middot; What the 16 GB droplet needs that the default does not", H2))
S.append(Paragraph("The stack's defaults were written for a 64 GB machine at UIU. On the "
                   "16 GB droplet four settings must change, or the database will not stay up. "
                   "The profile they point at ships with the code; step 6e sets them:", BODY))
S.append(Spacer(1, 3))
S.append(tbl([["Setting", "Default", "On the droplet", "Why"],
              [c("AIMS_PG_CONF"), c("postgresql.conf"), c("postgresql.16gb.conf"),
               "<b>The default reserves 16 GB for the database cache alone</b> &mdash; the whole "
               "machine. The 16 GB profile is in " + c("deploy/uiu/postgres") + ", tested "
               "under a 6 GB limit"],
              [c("AIMS_PG_MEMORY"), "8g", "<b>6g</b>", "leaves room for the API and the workers"],
              [c("AIMS_ARCHIVE_MEMORY"), "1g", "<b>2g</b>", "joining a long consultation in "
                                                            "memory, without being killed"],
              [c("AIMS_WORK_TMPFS"), "6g", "<b>3g</b>", "working space in memory for joining one "
                                                        "consultation"]],
             [40 * mm, 32 * mm, 42 * mm, 42 * mm], req=[1]))

S.append(PageBreak())

# ============================================================ PART C
S.append(part("PART C", "The deployment",
              "Twelve steps, three to four hours, done once"))

S += step("Step 1 &middot; Make your SSH key, then create the droplet",
          Paragraph("An SSH key is how you log in to the server without a password. You make "
                    "it once, on your own laptop. Windows 10 and 11 already include the tool.",
                    BODY))
S.append(Paragraph("1a &middot; On your laptop, in Windows PowerShell", H3))
S.append(code(
    'PS> ssh-keygen -t ed25519 -C "' + OWNER_EMAIL + '"\n'
    '#   press Enter to accept the file name\n'
    '#   type a passphrase (twice) - it protects the key if the laptop is stolen\n'
    '\n'
    'PS> Get-Content $env:USERPROFILE\\.ssh\\id_ed25519.pub | Set-Clipboard\n'
    '#   the PUBLIC key is now on the clipboard'))
S.append(expect("two files in " + c("C:\\Users\\&lt;you&gt;\\.ssh\\") + ": "
                + c("id_ed25519") + " (private &mdash; never share it, never upload it) and "
                + c("id_ed25519.pub") + " (public &mdash; this is what DigitalOcean gets). The "
                "public key is one line starting " + c("ssh-ed25519 AAAA") + "."))
S.append(Spacer(1, 3))
S.append(trouble([
    ("'ssh-keygen' is not recognized",
     "Settings &rarr; System &rarr; Optional features &rarr; Add a feature &rarr; "
     "<b>OpenSSH Client</b>. Then open a new PowerShell window."),
    ("\"id_ed25519 already exists. Overwrite?\"",
     "Answer <b>n</b>. You already have a key; use the existing " + c(".pub") + " file."),
]))
S.append(Paragraph("1b &middot; In DigitalOcean: Create &rarr; Droplets", H3))
S.append(tbl([["Field", "Choose"],
              ["Region", "<b>Bangalore &middot; BLR1</b> &mdash; the lowest latency to Dhaka"],
              ["Image", "<b>Ubuntu 24.04 (LTS) x64</b>"],
              ["Droplet type", "<b>Basic &rarr; Regular (SSD)</b>"],
              ["Size", "<b>8 vCPU / 16 GB / 320 GB SSD &mdash; " + c("s-8vcpu-16gb")
               + " &mdash; about $96 a month</b>"],
              ["Authentication", "<b>SSH Key &rarr; New SSH Key</b> &rarr; paste the clipboard "
                                 "&rarr; name it after yourself. <b>Not a password</b>"],
              ["Backups", "<b>Enable &mdash; weekly</b> (about 20% extra, ~$19 a month)"],
              ["Monitoring", "<b>Enable</b> &mdash; free; needed for the email alerts in step 12"],
              ["Hostname", c("aimscribe-prod-blr1")],
              ["Project", "AIMScribe"]],
             [34 * mm, 122 * mm], highlight=[4]))
S.append(Spacer(1, 3))
S.append(expect("after about a minute the droplet appears with a green dot and a <b>public "
                "IPv4 address</b>. Write the address down; it is " + c("&lt;droplet-ip&gt;")
                + " below."))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Optional: a reserved IP.</b> Networking &rarr; Reserved IPs &rarr; assign "
                   "one to the droplet, and use that address from now on. It is free while "
                   "attached, and if the droplet is ever rebuilt the address &mdash; and so "
                   "the DNS record &mdash; stays the same.", NOTE))

S += step("Step 2 &middot; Point the name at the droplet",
          Paragraph("At whoever controls the domain (UIU IT for a " + c("uiu.ac.bd")
                    + " name), create one record:", BODY))
S.append(code('Type  Name                    Value                TTL\n'
              'A     ' + HOST.ljust(22) + '  <droplet-ip>         300'))
S.append(Paragraph("Then wait, and check from your laptop:", BODY))
S.append(code('PS> nslookup ' + HOST + ' 1.1.1.1'))
S.append(expect(c("Address: &lt;droplet-ip&gt;") + " in the answer. Usually within 5 minutes; "
                "occasionally up to an hour."))
S.append(Spacer(1, 3))
S.append(trouble([
    ("" + c("Non-existent domain") + " or no address",
     "The record is not live yet, or was typed under the wrong name. Wait 10 minutes and "
     "try again; then check the record itself."),
    ("A different address", "An old record exists. Delete it so only the new A record remains."),
]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Do not start the stack (step 8) until this answers correctly.</b> The "
                   "gateway asks for a certificate on first start; if the name does not point "
                   "here yet the request fails and Let's Encrypt makes you wait before trying "
                   "again.", BAD))

S += step("Step 3 &middot; The firewall",
          Paragraph("DigitalOcean &rarr; Networking &rarr; <b>Firewalls</b> &rarr; Create "
                    "Firewall, name " + c("aimscribe-prod") + ", with exactly these rules, then "
                    "apply it to the droplet:", BODY))
S.append(tbl([["Direction", "Protocol", "Port", "Source / destination", "Why"],
              ["Inbound", "TCP", "<b>22</b>", "<b>your own IP only</b>", "SSH, administration"],
              ["Inbound", "TCP", "<b>80</b>", "All IPv4, All IPv6", "certificate issue and renewal"],
              ["Inbound", "TCP", "<b>443</b>", "All IPv4, All IPv6", "<b>the API</b>"],
              ["Inbound", "UDP", "<b>443</b>", "All IPv4, All IPv6", "HTTP/3"],
              ["Outbound", "all", "all", "all", "R2 uploads, updates, certificates"]],
             [22 * mm, 20 * mm, 16 * mm, 46 * mm, 52 * mm], req=[1], highlight=[3]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Nothing else.</b> No database port, no Redis port, no API port. Use this "
                   "DigitalOcean firewall rather than " + c("ufw") + " on the droplet: Docker "
                   "opens its ports around " + c("ufw") + ", so " + c("ufw") + " alone would give "
                   "a false sense of safety.", NOTE))
S.append(Spacer(1, 3))
S.append(trouble([
    ("You do not know your own IP", "Search \"what is my IP\" from the laptop you will "
                                    "administer from, and use that address with " + c("/32") + "."),
    ("Your IP changes (home broadband)", "Add each address you use, or use the DigitalOcean "
                                         "web console (droplet &rarr; Access) when locked out."),
]))

S += step("Step 4 &middot; First login, a working user, and Docker",
          Paragraph("From your laptop, log in as " + c("root") + " the first time only:", BODY))
S.append(code(
    'PS> ssh root@<droplet-ip>\n'
    '#   first time: "Are you sure you want to continue connecting?" -> yes\n'
    '#   then your key passphrase'))
S.append(expect(c("root@aimscribe-prod-blr1:~#") + " &mdash; you are on the server."))
S.append(Paragraph("Now, on the droplet, create the user you will use from now on, and lock "
                   "the door behind it:", BODY))
S.append(code(
    '# a working user with sudo, and your key\n'
    'adduser --disabled-password --gecos "" aims\n'
    'usermod -aG sudo aims\n'
    'rsync --archive --chown=aims:aims ~/.ssh /home/aims\n'
    'passwd aims            # set a password - sudo will ask for it\n'
    '\n'
    '# no root login, no password logins\n'
    'sed -i "s/^#*PermitRootLogin.*/PermitRootLogin no/" /etc/ssh/sshd_config\n'
    'sed -i "s/^#*PasswordAuthentication.*/PasswordAuthentication no/" /etc/ssh/sshd_config\n'
    'grep -rl "PasswordAuthentication yes" /etc/ssh/sshd_config.d/ \\\n'
    '  | xargs -r sed -i "s/PasswordAuthentication yes/PasswordAuthentication no/"\n'
    'sshd -t && systemctl restart ssh\n'
    'sshd -T | grep -E "^(permitrootlogin|passwordauthentication)"'))
S.append(expect(c("permitrootlogin no") + " and " + c("passwordauthentication no") + "."))
S.append(Spacer(1, 3))
S.append(box("Keep this root window open until the next login works",
             "Open a <b>second</b> PowerShell window and run " + c("ssh aims@&lt;droplet-ip&gt;")
             + ". Only when that succeeds, close the root window. If it fails, the root window "
             "is still open to fix it &mdash; and the DigitalOcean web console (droplet &rarr; "
             "Access &rarr; Launch Droplet Console) always works as a last resort."))
S.append(Spacer(1, 4))
S.append(Paragraph("As " + c("aims") + ", the updates, the clock, a swap file and Docker:", BODY))
S.append(code(
    'ssh aims@<droplet-ip>\n'
    '\n'
    'sudo apt update && sudo apt upgrade -y\n'
    'sudo apt install -y unattended-upgrades fail2ban git curl jq python3\n'
    'sudo dpkg-reconfigure -plow unattended-upgrades      # answer Yes\n'
    '\n'
    '# the clock - recordings are filed by the clinic\'s local date\n'
    'sudo timedatectl set-timezone Asia/Dhaka\n'
    'sudo timedatectl set-ntp true\n'
    '\n'
    '# 4 GB of swap: a cushion, so a spike never kills the database\n'
    'sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile\n'
    'sudo mkswap /swapfile && sudo swapon /swapfile\n'
    'echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab\n'
    '\n'
    '# Docker, with the compose plugin\n'
    'curl -fsSL https://get.docker.com | sudo sh\n'
    'sudo usermod -aG docker aims\n'
    'sudo systemctl enable --now docker\n'
    'exit                                 # log out and back in, so the group applies\n'
    'ssh aims@<droplet-ip>\n'
    'docker compose version && timedatectl | grep "Time zone" && free -h'))
S.append(expect(c("Docker Compose version v2...") + ", " + c("Time zone: Asia/Dhaka")
                + ", and " + c("Swap: 4.0Gi") + "."))
S.append(Spacer(1, 3))
S.append(trouble([
    (c("permission denied ... docker.sock"),
     "You did not log out and back in after " + c("usermod") + ". Run " + c("exit")
     + ", then log in again."),
    (c("Could not get lock /var/lib/dpkg/lock"),
     "Ubuntu is installing its own updates in the background on first boot. Wait five "
     "minutes and run the command again."),
    ("A purple screen asks which services to restart", "Press Enter to accept the default."),
]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>The timezone line is not cosmetic.</b> A recording is filed under the "
                   "clinic's local date. A server on UTC files the evening's consultations "
                   "under tomorrow.", NOTE))

S += step("Step 5 &middot; Get the code",
          code('cd ~\n'
               'git clone https://github.com/SakibZaman021/AIMScribe_v3.git\n'
               'cd AIMScribe_v3/deploy/uiu\n'
               'ls'))
S.append(expect(c("Caddyfile  backup  docker-compose.yml  monitor  pgbouncer  postgres ...")
                + " and, with " + c("ls -a") + ", " + c(".env.example") + "."))
S.append(Spacer(1, 3))
S.append(Paragraph("The folder is named " + c("uiu") + " for historical reasons. It is the "
                   "<b>server</b> stack and the right one for this droplet. "
                   + c("deploy/local") + " is the development stack; never use it here.", NOTE))

S.append(PageBreak())
S += step("Step 6 &middot; The settings file",
          Paragraph("All settings live in one file, " + c("deploy/uiu/.env") + ". It holds every "
                    "secret on the machine, is readable only by you, and is <b>never "
                    "committed</b> &mdash; the repository is public. Work through 6a to 6g in "
                    "one SSH session, in the " + c("deploy/uiu") + " folder.", BODY))
S.append(Paragraph("6a &middot; Start from the example, and generate the secrets on the server",
                   H3))
S.append(code(
    'cd ~/AIMScribe_v3/deploy/uiu\n'
    'cp .env.example .env && chmod 600 .env\n'
    '\n'
    '# setv NAME VALUE - replaces the line NAME=... in .env, or adds it\n'
    'setv() { sed -i "s|^$1=.*|$1=$2|" .env; grep -q "^$1=" .env || echo "$1=$2" >> .env; }\n'
    '\n'
    '# passwords and service keys: random, 64 hex characters each\n'
    'for v in POSTGRES_SUPERUSER_PASSWORD AIMS_RECORDINGS_PASSWORD \\\n'
    '         AIMS_CLINICAL_PASSWORD AIMS_MONITOR_PASSWORD REDIS_PASSWORD \\\n'
    '         AIMS_ADMIN_KEY AIMS_WORKER_KEY AIMSCRIBE_WEBHOOK_SECRET; do\n'
    '  setv $v "$(openssl rand -hex 32)"\n'
    'done\n'
    '\n'
    '# the two encryption keys: 32 random bytes each, base64\n'
    'setv AIMS_BACKUP_KEY "$(openssl rand -base64 32)"\n'
    'setv AIMS_COPY_KEY   "$(openssl rand -base64 32)"'))
S.append(Paragraph("<b>Keep this SSH window open until 6g:</b> " + c("setv") + " exists only in "
                   "this session. If the window closes, paste the " + c("setv()") + " line "
                   "again before continuing. Hex is used for the passwords because one of them "
                   "is placed inside a database address, where " + c("/") + " or " + c("+")
                   + " would break it.", NOTE))

S.append(Paragraph("6b &middot; Who this server is", H3))
S.append(code(
    'setv AIMS_PUBLIC_HOST ' + HOST + '              # the name from step 2\n'
    'setv AIMS_ACME_EMAIL  ' + OWNER_EMAIL + '\n'
    'setv TZ               Asia/Dhaka\n'
    'setv AIMS_DB_PATH      /srv/aimscribe/db\n'
    'setv AIMS_ARCHIVE_PATH /srv/aimscribe/archive\n'
    'setv AIMS_BACKUP_PATH  /srv/aimscribe/backup\n'
    '\n'
    '# the exact address CMED\'s dashboard is served from (no wildcards);\n'
    '# ask CMED - a placeholder is harmless, it only governs browser access\n'
    'setv AIMS_ALLOWED_ORIGINS https://<cmed-dashboard-host>'))

S.append(Paragraph("6c &middot; The two R2 buckets from Part B, section 6", H3))
S.append(code(
    '# the endpoint is the HOST NAME ONLY - no https:// in front\n'
    'R2=<account-id>.r2.cloudflarestorage.com\n'
    '\n'
    '# the pieces, uploaded by the recorders while they record\n'
    'setv MINIO_ENDPOINT   $R2\n'
    'setv MINIO_BUCKET     aimscribe-segments\n'
    'setv MINIO_SECURE     true\n'
    'setv MINIO_REGION     auto\n'
    'setv MINIO_ACCESS_KEY <segments token Access Key ID>\n'
    'setv MINIO_SECRET_KEY <segments token Secret Access Key>\n'
    '\n'
    '# the permanent encrypted copy of each consultation\n'
    'setv AIMS_COPY_ENDPOINT   $R2\n'
    'setv AIMS_COPY_BUCKET     aimscribe-archive\n'
    'setv AIMS_COPY_REGION     auto\n'
    'setv AIMS_COPY_SECURE     true\n'
    'setv AIMS_COPY_ACCESS_KEY <archive token Access Key ID>\n'
    'setv AIMS_COPY_SECRET_KEY <archive token Secret Access Key>'))
S.append(Paragraph("<b>" + c(".env.example") + " suggests Amazon for the copy store "
                   "(" + c("s3.ap-south-1.amazonaws.com") + ").</b> The lines above replace that "
                   "with R2, which is the policy for this deployment.", NOTE))

S.append(Paragraph("6d &middot; The server's two signing keys", H3))
S.append(Paragraph("The server signs two things with private keys: each <b>recording grant</b>, "
                   "and each <b>purge receipt</b> that lets a clinic PC delete audio it has "
                   "safely uploaded. Every recorder checks those signatures with the matching "
                   "<b>public</b> keys, which the installer copies to " + c("C:\\ProgramData\\"
                   "AIMScribe\\keys") + ". <b>The private and public halves must be a pair</b>, "
                   "or no recording can be authorised and no audio is ever deleted from a PC.",
                   BODY))
S.append(Spacer(1, 3))
S.append(Paragraph("Make a new pair on the droplet, put the private halves in " + c(".env")
                   + ", and keep the public halves for the installer:", BODY))
S.append(code(
    'mkdir -p ~/keys && cd ~/keys && umask 077\n'
    'openssl genpkey -algorithm ed25519 -out grant.pem\n'
    'openssl genpkey -algorithm ed25519 -out receipt.pem\n'
    'openssl pkey -in grant.pem   -pubout -out aimslab_grant_pub.pem\n'
    'openssl pkey -in receipt.pem -pubout -out aimslab_receipt_pub.pem\n'
    '\n'
    'cd ~/AIMScribe_v3/deploy/uiu\n'
    '# .env holds a key on one line, with its line breaks written as \\n\n'
    'for k in GRANT RECEIPT; do\n'
    '  f=~/keys/$(echo $k | tr A-Z a-z).pem\n'
    '  sed -i "/^AIMS_${k}_PRIVATE_KEY=/d" .env\n'
    '  printf "%s\\n" "AIMS_${k}_PRIVATE_KEY=$(awk \'BEGIN{ORS="\\\\n"}1\' $f)" >> .env\n'
    'done\n'
    '\n'
    '# prove each private key in .env matches its public file\n'
    'for k in grant receipt; do\n'
    '  K=$(echo $k | tr a-z A-Z)\n'
    '  grep "^AIMS_${K}_PRIVATE_KEY=" .env | cut -d= -f2- | xargs -0 printf "%b" \\\n'
    '    | openssl pkey -pubout | diff -q - ~/keys/aimslab_${k}_pub.pem >/dev/null \\\n'
    '    && echo "$k: MATCH" || echo "$k: DIFFERENT"\n'
    'done'))
S.append(expect(c("grant: MATCH") + " and " + c("receipt: MATCH") + "."))
S.append(Spacer(1, 3))
S.append(Paragraph("Then copy the two <b>public</b> files to your laptop, into the recorder "
                   "source, so the installer you build in step 11 carries them:", BODY))
S.append(code(
    'PS> cd <your AIMScribe_v3 folder>\\recorder\\keys\n'
    'PS> scp aims@<droplet-ip>:keys/aimslab_grant_pub.pem .\n'
    'PS> scp aims@<droplet-ip>:keys/aimslab_receipt_pub.pem .'))
S.append(Spacer(1, 3))
S.append(Paragraph("Store " + c("grant.pem") + " and " + c("receipt.pem") + " in the password "
                   "manager (6g), then delete them from the droplet with "
                   + c("shred -u ~/keys/grant.pem ~/keys/receipt.pem") + ". The copies in "
                   + c(".env") + " are the working ones.", NOTE))
S.append(Spacer(1, 3))
S.append(box("Why new keys, and not the ones already in the repository",
             "On 15 October 2026 the public keys in " + c("recorder/keys") + " did <b>not</b> "
             "match the private keys of the AIMS LAB development server. Nobody can say for "
             "certain which server holds their private halves. Starting with a fresh pair "
             "removes the doubt, and costs nothing: every clinic PC is re-run through the "
             "installer in step 11 anyway, which installs the new public keys."))

S.append(Paragraph("6e &middot; Sizing for the 16 GB droplet", H3))
S.append(Paragraph("The database profile for this droplet is part of the code: "
                   + c("deploy/uiu/postgres/postgresql.16gb.conf") + ". Its header shows how the "
                   "16 GB is shared between the services. Point the settings at it:", BODY))
S.append(code(
    'cd ~/AIMScribe_v3/deploy/uiu\n'
    'grep -E "^(shared_buffers|effective_cache_size|work_mem)" postgres/postgresql.16gb.conf\n'
    '\n'
    'setv AIMS_PG_CONF        ./postgres/postgresql.16gb.conf\n'
    'setv AIMS_PG_MEMORY      6g\n'
    'setv AIMS_ARCHIVE_MEMORY 2g\n'
    'setv AIMS_WORK_TMPFS     3g\n'
    '\n'
    '# how long an archived WAV stays on this disk once its cloud copy is proven\n'
    'setv AIMS_ARCHIVE_LOCAL_RETENTION_DAYS 2       # two clinic days: about 66 GB'))
S.append(expect(c("shared_buffers = 4GB") + ", " + c("effective_cache_size = 10GB") + ", "
                + c("work_mem = 16MB") + "."))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>The disk floors need nothing.</b> Left empty, each of the monitor's "
                   "floors is capped at 15% of its disk &mdash; 48 GB here &mdash; so a floor "
                   "written for the UIU array no longer alarms for ever on a 320 GB disk.", GOOD))

S.append(Paragraph("6f &middot; Alerts, and the optional parts", H3))
S.append(code(
    '# where the monitor posts its alerts (a Slack/Discord/Google Chat webhook URL).\n'
    '# Left empty, alerts go only to the log.\n'
    'setv AIMS_ALERT_WEBHOOK https://<your chat webhook address>\n'
    '\n'
    '# transcription: only if this deployment transcribes. Otherwise leave empty.\n'
    '#   AZURE_TRANSCRIBE_ENDPOINT, AZURE_TRANSCRIBE_API_KEY,\n'
    '#   AZURE_NER_ENDPOINT, AZURE_NER_API_KEY'))

S.append(Paragraph("6g &middot; Check, then put six secrets somewhere safe", H3))
S.append(code(
    'for v in POSTGRES_SUPERUSER_PASSWORD AIMS_RECORDINGS_PASSWORD AIMS_CLINICAL_PASSWORD \\\n'
    '  AIMS_MONITOR_PASSWORD REDIS_PASSWORD AIMS_WORKER_KEY AIMS_ADMIN_KEY AIMS_PUBLIC_HOST \\\n'
    '  AIMS_ACME_EMAIL AIMS_DB_PATH AIMS_ARCHIVE_PATH AIMS_BACKUP_PATH AIMS_BACKUP_KEY \\\n'
    '  AIMS_COPY_KEY AIMS_GRANT_PRIVATE_KEY AIMS_RECEIPT_PRIVATE_KEY MINIO_ENDPOINT \\\n'
    '  MINIO_ACCESS_KEY MINIO_SECRET_KEY AIMS_COPY_BUCKET AIMS_COPY_ACCESS_KEY \\\n'
    '  AIMS_COPY_SECRET_KEY AIMS_PG_CONF; do\n'
    '  grep -q "^$v=." .env && echo "ok       $v" || echo "MISSING  $v"\n'
    'done\n'
    'grep -c "^MINIO_BUCKET=aimscribe-segments$" .env     # must print 1\n'
    'docker compose config --quiet && echo "compose settings: OK"'))
S.append(expect("every line " + c("ok") + ", then " + c("1") + ", then "
                + c("compose settings: OK") + ". No secret is printed by these checks."))
S.append(Spacer(1, 3))
S.append(trouble([
    (c("MISSING  &lt;name&gt;"), "Set it with " + c("setv") + " as above, then run the check "
                                 "again."),
    (c("required variable ... is missing a value"),
     "One of the eleven the stack refuses to start without is empty. The message names it."),
    (c("setv: command not found"), "The session was closed. Paste the " + c("setv()")
                                    + " line from 6a again."),
]))
S.append(Spacer(1, 4))
S.append(tbl([["Six secrets that must also exist OFF this machine", "If lost"],
              [c("AIMS_COPY_KEY"), "<b>Every cloud copy in R2 is permanently unreadable</b>"],
              [c("AIMS_BACKUP_KEY"), "<b>Every database backup is unreadable</b>"],
              [c("grant.pem") + " / " + c("AIMS_GRANT_PRIVATE_KEY"),
               "every clinic PC must be re-installed with new public keys"],
              [c("receipt.pem") + " / " + c("AIMS_RECEIPT_PRIVATE_KEY"), "same"],
              [c("AIMS_ADMIN_KEY"), "no administration until " + c(".env") + " is read on the "
                                                                          "server"],
              [c("POSTGRES_SUPERUSER_PASSWORD"), "restores and repairs become much harder"]],
             [70 * mm, 86 * mm], req=[1, 2]))
S.append(Spacer(1, 3))
S.append(Paragraph("To read a value to copy it into the password manager, without leaving it "
                   "in the shell history: " + c("grep ^AIMS_COPY_KEY= .env") + ". Do this on "
                   "your own screen, never in a screen share.", NOTE))

S += step("Step 7 &middot; The folders",
          code('sudo mkdir -p /srv/aimscribe/{db,archive,backup}\n'
               '# the archive worker runs as user 10001 inside its container, and must\n'
               '# be able to write the archive; the database and backup containers\n'
               '# set their own ownership\n'
               'sudo chown -R 10001:10001 /srv/aimscribe/archive\n'
               '# the nightly backup in step 12 runs as you\n'
               'sudo chown aims:aims /srv/aimscribe/backup\n'
               'df -h /srv'))
S.append(expect("about " + c("310G") + " available on " + c("/") + "."))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Do not " + c("chown") + " the archive folder to " + c("aims")
                   + ".</b> The archive worker would then fail to write any recording, with "
                   + c("Permission denied") + " in its log.", BAD))

S += step("Step 8 &middot; Start it",
          code('cd ~/AIMScribe_v3/deploy/uiu\n'
               'docker compose up -d --build\n'
               '#   the first time builds four images: 5-10 minutes\n'
               '\n'
               'docker compose ps'))
S.append(expect("nine services. " + c("aims-postgres") + ", " + c("aims-pgbouncer") + ", "
                + c("aims-redis") + " and " + c("aims-api") + " say " + c("(healthy)")
                + "; the rest say " + c("Up") + ". The first start creates both databases and "
                "applies the whole schema by itself."))
S.append(Spacer(1, 3))
S.append(code('docker compose logs --tail 60 api\n'
              'docker compose logs -f gateway        # Ctrl+C to stop watching'))
S.append(expect("in the gateway log, " + c("certificate obtained successfully") + " for your "
                "host, within a minute or two."))
S.append(Spacer(1, 3))
S.append(trouble([
    (c("aims-postgres") + " restarting, or its log says " + c("could not map anonymous shared "
                                                             "memory"),
     "The 16 GB profile is not in use. Check " + c("AIMS_PG_CONF") + " (6e), then "
     + c("docker compose up -d") + "."),
    (c("aims-postgres") + " log: " + c("could not open configuration file"),
     "The file named in " + c("AIMS_PG_CONF") + " does not exist. Check the spelling in "
     "6e, and that " + c("git pull") + " brought " + c("postgres/postgresql.16gb.conf") + "."),
    ("Gateway log: " + c("NXDOMAIN") + ", " + c("timeout") + " or "
     + c("rateLimited"),
     "DNS is not pointing here yet (step 2), or port 80 is closed (step 3). Fix it, then "
     + c("docker compose restart gateway") + ". After a rate limit, wait an hour."),
    ("API log: " + c("AIMS_COPY_BUCKET is the same bucket as the pieces"),
     "Both settings name one bucket. Set them as in 6c, then " + c("docker compose up -d") + "."),
    ("API log: " + c("AIMS_RECEIPT_PRIVATE_KEY is not set"), "6d was not completed."),
    ("Archive-worker log: " + c("Permission denied") + " on " + c("/archive"),
     "Step 7's " + c("chown") + " was skipped or used the wrong user."),
    ("A service is " + c("Exited"), c("docker compose logs --tail 80 &lt;service&gt;")
     + " shows why. Fix the setting, then " + c("docker compose up -d") + "."),
]))

S += step("Step 9 &middot; Prove it answers &mdash; from your laptop, not the server",
          code('PS> curl.exe -s https://' + HOST + '/health'))
S.append(expect(c('"status":"healthy"') + ", " + c('"database":"connected"') + ", "
                + c('"redis":"connected"') + ", " + c('"minio":"connected"') + ", "
                + c('"copy_bucket":"connected"') + ", and " + c('"system"') + " starting with "
                + c("AIMScribe v3") + ". The two storage answers are real: the server asks "
                "each R2 bucket, with its own keys, at most every 30 seconds."))
S.append(Spacer(1, 3))
S.append(trouble([
    ("Could not resolve host", "Step 2 is not finished."),
    ("A certificate or TLS error", "The gateway has not got its certificate yet. Watch "
                                   + c("docker compose logs -f gateway") + "."),
    (c('"status":"degraded"'), "Read which part is not " + c("connected") + ". The "
                               "database or Redis: " + c("docker compose ps") + ". "
                               + c('"minio"') + " or " + c('"copy_bucket"') + " saying "
                               + c("refused") + ": that bucket's keys are wrong (6c); "
                               + c("bucket missing") + ": the bucket name is wrong; "
                               + c("unreachable") + ": the endpoint is wrong."),
    ("Connection refused or timed out", "Port 443 is closed in the firewall (step 3), or the "
                                        "gateway is not running."),
]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Run it from somewhere else, not from the droplet.</b> Testing from the "
                   "server proves the program is running; it does not prove that DNS, the "
                   "firewall and the certificate work &mdash; which is the point of this step.",
                   BAD))

S += step("Step 10 &middot; Register the clinics, the doctors and CMED's key",
          Paragraph("These are admin API calls, authenticated with " + c("X-Admin-Key")
                    + ". Run them on the droplet; the key is read from " + c(".env")
                    + " into a variable and never typed.", BODY))
S.append(code(
    'cd ~/AIMScribe_v3/deploy/uiu\n'
    'A=$(grep ^AIMS_ADMIN_KEY= .env | cut -d= -f2)\n'
    'H=https://' + HOST + '/api/v2\n'
    '\n'
    '# one clinic. cmed_hospital_id is CMED\'s code for it - the mapping that\n'
    '# lets CMED\'s visits be matched to this clinic\'s recordings\n'
    'curl -s -X POST $H/admin/hospital -H "X-Admin-Key: $A" \\\n'
    '  -H "Content-Type: application/json" \\\n'
    '  -d \'{ "hospital_id": "AALO_DHOLPUR",\n'
    '        "name": "Aalo Clinic, Dholpur",\n'
    '        "timezone": "Asia/Dhaka",\n'
    '        "cmed_hospital_id": "<CMED code for this clinic>" }\'\n'
    '\n'
    '# repeat for AALO_KARAIL, AALO_MIRPUR, AALO_SHYAMPUR,\n'
    '#            AALO_NARAYANGANJ, AALO_ERSHADNAGAR, AMADER_SUSASTHO\n'
    '\n'
    '# a doctor (optional - CMED names the doctor with every consultation;\n'
    '# this only puts a readable name against the id in reports)\n'
    'curl -s -X POST $H/admin/doctor -H "X-Admin-Key: $A" \\\n'
    '  -H "Content-Type: application/json" \\\n'
    '  -d \'{ "doctor_id": "DR0042", "hospital_id": "AALO_DHOLPUR",\n'
    '        "full_name": "Dr Rahman", "active": true }\'\n'
    '\n'
    '# CMED\'s Channel B key - PRINTED ONCE, NEVER RECOVERABLE\n'
    'curl -s -X POST $H/admin/cmed-key -H "X-Admin-Key: $A" \\\n'
    '  -H "Content-Type: application/json" \\\n'
    '  -d \'{ "label": "cmed-production", "created_by": "AIMS LAB" }\''))
S.append(expect("each clinic: " + c('{"status":"ok","hospital_id":"AALO_DHOLPUR",'
                                    '"cmed_hospital_id":"..."}') + ". The key call: "
                + c('{"cmed_key":"...","label":"cmed-production",...}') + "."))
S.append(Spacer(1, 3))
S.append(trouble([
    (c("401") + " " + c("invalid admin key"), "The " + c("A=") + " line was run in another "
                                               "folder. " + c("cd") + " into "
                                               + c("deploy/uiu") + " and run it again."),
    (c("422") + " with " + c("field required"), "The JSON body is missing a field, or a quote "
                                                 "was lost in copying. Retype the body."),
    (c("400") + " " + c("timezone ... is not a known zone name"), "Use " + c("Asia/Dhaka")
                                                                    + " exactly."),
]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Capture the CMED key the moment it prints</b> and send it to CMED over a "
                   "channel that is not plain email if you can. It cannot be shown again. To "
                   "replace it: issue a new one with a new label, let CMED switch, then revoke "
                   "the old label with " + c("POST $H/admin/cmed-key/revoke") + " and body "
                   + c('{"label": "cmed-production"}') + ".", BAD))
S.append(Spacer(1, 4))
S.append(box("<b>" + c("cmed_hospital_id") + " is the blocking prerequisite</b>",
             "Without it, a visit CMED describes cannot be tied to the clinic it happened in: "
             "the record is stored, <b>CMED receives a normal 202</b>, an "
             + c("unmapped_clinic") + " alert is raised, and the recording is kept but never "
             "gets its clinical record. <b>Get CMED's seven codes before this step.</b> If they are "
             "late, register the clinics anyway and repeat the same call with the code later; "
             "it updates, it does not duplicate."))

S += step("Step 11 &middot; Install the clinic PCs against the new address",
          Paragraph("A recorder is enrolled to <b>one clinic and one server</b>. Moving a PC to "
                    "this server means a fresh single-use token and one run of the installer. "
                    "<b>Do one PC first, record a full test consultation, and only then do the "
                    "rest.</b>", BODY))
S.append(Paragraph("11a &middot; Mint the tokens (on the droplet)", H3))
S.append(Paragraph("The fleet is described in a CSV, " + c("backend/scripts/laptops.csv")
                   + ": <b>one row per laptop, not per doctor</b>. <b>Add rows; never delete "
                   "rows</b> &mdash; a trimmed file reads as clinics that no longer exist.", BODY))
S.append(code(
    '#   hospital_id,hospital_name,room,doctor_id,doctor_name\n'
    '#   AALO_DHOLPUR,"Aalo Clinic, Dholpur",Room 3,,\n'
    '#   AALO_DHOLPUR,"Aalo Clinic, Dholpur",Room 4,,\n'
    'nano ~/AIMScribe_v3/backend/scripts/laptops.csv      # Ctrl+O, Enter, Ctrl+X\n'
    '\n'
    'cd ~/AIMScribe_v3/backend\n'
    'AIMS_BACKEND_URL=https://' + HOST + ' \\\n'
    'AIMS_ADMIN_KEY=$(grep ^AIMS_ADMIN_KEY= ../deploy/uiu/.env | cut -d= -f2) \\\n'
    '  python3 scripts/mint_enrolment_tokens.py scripts/laptops.csv'))
S.append(expect(c("backend  https://" + HOST) + ", one " + c("hospital ... ready")
                + " per clinic, and a folder " + c("scripts/enrolment_&lt;date&gt;_&lt;time&gt;/")
                + " with one instruction sheet per PC. Unused tokens expire after 14 days."))
S.append(Spacer(1, 3))
S.append(trouble([
    (c("AIMS_BACKEND_URL is not set"),
     "Run it again exactly as shown, with the " + c("AIMS_BACKEND_URL=") + " line."),
    (c("AIMS_ADMIN_KEY is not set"), "The path to " + c(".env") + " was wrong. Run it from "
                                      + c("~/AIMScribe_v3/backend") + "."),
]))
S.append(Spacer(1, 3))
S.append(Paragraph("Copy the instruction sheets to your laptop with " + c("scp -r") + ", then "
                   "<b>delete the folder from the droplet</b>: the tokens are credentials until "
                   "they are used.", NOTE))

S.append(Paragraph("11b &middot; Prove the keys belong together, then build the installer",
                   H3))
S.append(box("REQUIRED before building the installer and before enrolling any PC",
             "On 15 October 2026 the public keys in the repository's " + c("recorder/keys")
             + " did not belong to the development server's private keys. An installer built "
             "from them makes <b>every recording fail its grant check</b>, and no PC ever "
             "frees its disk. Three fingerprints must be the same: the server's, the "
             "installer's, and the first PC's. No private key leaves the droplet to check "
             "this."))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>1. On the droplet</b>: the server's private keys against the public "
                   "halves made in 6d.", BODY))
S.append(code(
    'cd ~/AIMScribe_v3/deploy/uiu\n'
    'docker compose run --rm --no-deps --user root \\\n'
    '  -v ~/keys:/keys:ro -v ~/AIMScribe_v3/tools:/tools:ro \\\n'
    '  api python /tools/check_key_pair.py --from-env --keys /keys'))
S.append(expect(c("grant    MATCH       &lt;16 characters&gt;") + " and the same for "
                + c("receipt") + ". Write the two fingerprints down."))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>2. On your laptop</b>, with the two files from 6d in "
                   + c("recorder\\keys") + ":", BODY))
S.append(code('PS> cd <your AIMScribe_v3 folder>\n'
              'PS> python tools\\check_key_pair.py --show --keys recorder\\keys'))
S.append(expect("the same two fingerprints as on the droplet. <b>If either differs, stop</b>: "
                "copy the files from 6d again. Then build " + c("AIMScribeSetup.exe")
                + " as usual; the installer copies these keys to every PC it runs on."))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>3. On the first clinic PC</b>, after 11c: "
                   + c("python tools\\check_key_pair.py --show --keys "
                       "C:\\ProgramData\\AIMScribe\\keys")
                   + " (from a copy of the tools folder) shows the same two fingerprints.",
                   BODY))

S.append(Paragraph("11c &middot; On each clinic PC", H3))
S.append(tbl([["", "Do this", "Note"],
              ["1", "<b>Only for a PC already enrolled to another AIMS LAB server:</b> wait until "
                    "it has nothing left to upload (the folder " + c("C:\\ProgramData\\AIMScribe"
                    "\\spool") + " has no session folders). Then delete "
                    + c("C:\\ProgramData\\AIMScribe\\state\\device.json"),
               "<b>Without this the PC stays enrolled to the old server</b> and ignores the new "
               "token. A brand-new PC skips this row"],
              ["2", "Run " + c("AIMScribeSetup.exe") + " as administrator", ""],
              ["3", "<b>Backend URL:</b> " + c("https://" + HOST), "exactly, with https"],
              ["4", "<b>CMED web address:</b> the address the CMED dashboard is opened from",
               "the page's origin"],
              ["5", "<b>Enrolment token:</b> that PC's token from its sheet", "one PC, one token"],
              ["6", "Finish. The agent starts and enrols itself on first start",
               "needs the internet once"]],
             [8 * mm, 98 * mm, 50 * mm], req=[1]))
S.append(Spacer(1, 3))
S.append(expect("on the droplet, " + c("docker compose logs api | grep -i enrol")
                + " shows the PC enrolling, and from then on its heartbeat every 30 seconds."))
S.append(Spacer(1, 3))
S.append(trouble([
    ("The installer says \"leave empty &mdash; already enrolled\"",
     "Row 1 was skipped. Cancel, delete " + c("device.json") + ", run it again."),
    ("The PC refuses to record: \"not registered with AIMS LAB\"",
     "Enrolment failed. Usually an expired or already-used token: mint a new one for that row."),
    ("Recording starts but is never authorised",
     "The PC's public keys do not match " + c(".env") + ". Run the three checks in 11b, "
     "rebuild the installer with the files from 6d, and run it again on that PC."),
]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Tell CMED the new host</b> for their server's setting (Part A, section 3). "
                   "That is the only change on their side.", GOOD))

S += step("Step 12 &middot; Backups, the restore drill, and email alerts",
          Paragraph("The stack's " + c("backup") + " service makes an encrypted dump of both "
                    "databases every night at " + c("AIMS_BACKUP_AT") + " (02:30 Dhaka time) "
                    "into " + c("/srv/aimscribe/backup") + ", and keeps 30 days. It runs in "
                    "its own small image, " + c("deploy/uiu/backup/Dockerfile") + ": the plain "
                    + c("postgres:16-alpine") + " it used before could neither read its "
                    "schedule nor encrypt, and never made a backup. Make one now, to prove it, "
                    "rather than waiting for tonight.", BODY))
S.append(Paragraph("12a &middot; The first backup, now", H3))
S.append(code(
    'cd ~/AIMScribe_v3/deploy/uiu\n'
    'docker compose run --rm backup --now\n'
    'ls -lh /srv/aimscribe/backup\n'
    'docker compose logs --tail 3 backup        # the nightly service\'s own plan'))
S.append(expect(c("aims_recordings -&gt; aims_recordings_&lt;date&gt;_&lt;time&gt;.sql.gz.enc")
                + " and the same for " + c("aims_clinical") + ", then " + c("done; 2 backup "
                "file(s) held") + ". The service's log ends " + c("next backup in &lt;h&gt;h "
                "&lt;m&gt;m") + " &mdash; a real wait, never a negative one."))
S.append(Spacer(1, 3))
S.append(trouble([
    (c("service \"postgres\" is not running"), "Start the stack first (step 8)."),
    (c("Permission denied") + " writing " + c("/backups"),
     "Step 7's last " + c("chown") + " was skipped."),
    (c("is only ... bytes; keeping it for inspection"),
     "The dump failed or the database is empty. Read the first error above it."),
    (c("AIMS_BACKUP_KEY is set but this image has no openssl"),
     "The service is still on the plain image. " + c("docker compose up -d --build backup")
     + "."),
]))
S.append(Spacer(1, 3))
S.append(Paragraph("12b &middot; The restore drill", H3))
S.append(Paragraph("Restore the newest backup into a <b>scratch</b> database, count what is in "
                   "it, and drop it. The script refuses to write into either live database.",
                   BODY))
S.append(code(
    'cd ~/AIMScribe_v3/deploy/uiu\n'
    'F=$(ls -t /srv/aimscribe/backup/aims_recordings_*.enc | head -1); echo $F\n'
    'docker compose run --rm backup restore /backups/$(basename $F) aims_restore_test\n'
    '\n'
    'docker compose exec -T postgres psql -U aims_admin -d postgres \\\n'
    '  -c "DROP DATABASE aims_restore_test"'))
S.append(expect(c("Decompressed ...: &lt;n&gt; lines") + ", " + c("CREATE DATABASE") + ", then "
                "a table of row counts with " + c("sessions") + " matching the dashboard, then "
                + c("DROP DATABASE") + "."))
S.append(Spacer(1, 3))
S.append(trouble([
    (c("bad decrypt"), "The key in " + c(".env") + " is not the one the file was made with. "
                       "This is exactly why the key is stored off the machine."),
    (c("Restore failed; last lines:"), "Read them. Do not ignore it: fix it today."),
    (c("Refusing to restore over the live database"),
     "As intended. Restore into a new name, check it, then swap it in by hand."),
]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>A backup nobody has restored is not a backup.</b> Do this drill today, "
                   "then once a month. The backups live on the droplet's own disk, which the "
                   "weekly DigitalOcean backup also copies; for a copy off the droplet, download "
                   "the newest " + c(".enc") + " files with " + c("scp") + " once a week "
                   "(Part E).", BAD))

S.append(Paragraph("12c &middot; Email alerts from DigitalOcean", H3))
S.append(tbl([["", "Do this in DigitalOcean", "Emails the owner when"],
              ["1", "Monitoring &rarr; <b>Create alert policy</b>: Disk utilisation above "
                    "<b>80%</b> for 5 min, droplet " + c("aimscribe-prod-blr1"),
               "the disk is filling"],
              ["2", "Same: Memory above <b>90%</b> for 10 min", "memory is short"],
              ["3", "Same: CPU above <b>90%</b> for 15 min", "the machine is overloaded"],
              ["4", "Monitoring &rarr; <b>Uptime</b> &rarr; New check: "
                    + c("https://" + HOST + "/health") + ", from the region nearest Dhaka",
               "<b>the server stops answering</b>, and when the certificate nears expiry"]],
             [8 * mm, 98 * mm, 50 * mm], highlight=[4]))

S.append(PageBreak())

# ============================================================ PART D
S.append(part("PART D", "Proving every endpoint",
              "The checks to run after Part C, and to send CMED as evidence"))

S.append(Paragraph("8 &middot; The twelve checks", H2))
S.append(tbl([["", "Check", "Command or action", "Pass"],
              ["<b>1</b>", "The name resolves", c("nslookup &lt;host&gt; 1.1.1.1"),
               "the droplet's IP"],
              ["<b>2</b>", "The certificate is real", c("curl.exe -sI https://&lt;host&gt;/health"),
               c("HTTP/1.1 200") + ", no TLS error"],
              ["<b>3</b>", "<b>Health, from outside</b>", c("curl.exe -s https://&lt;host&gt;/health"),
               "<b>" + c('"status":"healthy"') + "</b>"],
              ["<b>3b</b>", "Every migration is applied", "the same reply",
               "<b>" + c('"schema":"ok"') + "</b>, not " + c('"missing ..."')],
              ["<b>4</b>", "It is the right system", "the same reply",
               "<b>" + c("system") + " starts with " + c("AIMScribe v3") + "</b>"],
              ["<b>5</b>", "The key is enforced", "POST API 2 with no key", "<b>401</b>"],
              ["<b>6</b>", "<b>API 2 accepts</b>", "POST API 2 with the key", "<b>202</b> "
                                                                             + c("ACCEPTED")],
              ["<b>7</b>", "<b>A repeat is recognised</b>", "POST the identical body again",
               "<b>200</b> " + c("ALREADY_RECEIVED")],
              ["<b>8</b>", "<b>API 3 Channel B accepts</b>", "POST the prescription",
               "<b>202</b> " + c("ACCEPTED")],
              ["<b>9</b>", "Only 80 and 443 are open", "from your laptop: "
               + c("Test-NetConnection &lt;host&gt; -Port 5432"), c("TcpTestSucceeded : False")],
              ["<b>10</b>", "The visits list answers, and only with the key",
               c("curl.exe -s https://&lt;host&gt;/api/v2/clinical/patient/TESTP0001/visits")
               + ", then with " + c("-H \"X-CMED-Key: ...\""),
               "<b>401</b>, then <b>200</b> " + c('"code":"OK"')],
              ["<b>11</b>", "The recorders' new endpoints exist and are guarded",
               c("curl.exe -s -X POST https://&lt;host&gt;/api/v2/device/health -H "
                 "\"Content-Type: application/json\" -d \"{}\""),
               "<b>401</b> " + c("device token required") + " (a 404 means the old code)"],
              ["<b>12</b>", "<b>A real consultation matches</b>",
               "one PC, CMED sending all four messages", "<b>confirmed</b> on the dashboard"]],
             [9 * mm, 42 * mm, 60 * mm, 45 * mm], highlight=[3, 6, 7, 8, 12]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Checks 3 to 8 in one command,</b> from a laptop with the repository and "
                   "Python:", BODY))
S.append(code('PS> python tools\\channel_b_test.py --server https://' + HOST
              + ' --key <CMED key>'))
S.append(Paragraph("Keep the output and send it to CMED: it is the evidence that our side is "
                   "ready. Its records use test identifiers (" + c("TESTP0001") + ", "
                   + c("CMED-TEST-01") + "); they confirm nothing and raise one harmless "
                   + c("unmapped_clinic") + " alert.", GOOD))
S.append(Spacer(1, 4))
S.append(box("And then the one check that actually matters",
             "Checks 1 to 11 prove the gateway works. <b>They do not prove the integration "
             "works.</b> Record one real consultation on one clinic PC with CMED sending all "
             "four messages, and <b>confirm that the recording was matched and archived</b>. "
             "Every other check can pass while the five identifying fields silently fail to "
             "match &mdash; and then the recording is kept with no clinical record attached."))

S.append(Paragraph("9 &middot; Where things are on the droplet", H2))
S.append(tbl([["What", "Path", "Note"],
              ["The code and the stack", c("/home/aims/AIMScribe_v3/deploy/uiu"),
               "where every " + c("docker compose") + " command is run"],
              ["<b>The settings and the keys</b>", c("&hellip;/deploy/uiu/.env"),
               "<b>mode 600. Never commit it</b>"],
              ["The 16 GB database profile", c("&hellip;/deploy/uiu/postgres/postgresql.16gb.conf"),
               "part of the code; chosen in 6e"],
              ["The databases", c("/srv/aimscribe/db"), "grows 20&ndash;40 GB a year"],
              ["<b>The archive</b>", c("/srv/aimscribe/archive"),
               "<b>clinic / doctor / date / consultation</b>. Each WAV leaves this disk "
               "2 days after its cloud copy is proven (6e); its JSON and manifest stay"],
              ["Nightly backups", c("/srv/aimscribe/backup"), "encrypted, 30 days"],
              ["<b>The permanent cloud copy</b>", "R2 bucket " + c("aimscribe-archive"),
               "encrypted with " + c("AIMS_COPY_KEY")]],
             [42 * mm, 66 * mm, 48 * mm], highlight=[5, 7]))

S.append(PageBreak())

# ============================================================ PART E
S.append(part("PART E", "Daily and weekly checks",
              "Five minutes a day keeps a silent failure from becoming a lost month"))

S.append(Paragraph("10 &middot; Every clinic day (5 minutes)", H2))
S.append(code(
    'ssh aims@<droplet-ip>\n'
    'cd ~/AIMScribe_v3/deploy/uiu\n'
    'A=$(grep ^AIMS_ADMIN_KEY= .env | cut -d= -f2); H=https://' + HOST + '/api/v2\n'
    '\n'
    'docker compose ps                    # nine Up, four (healthy)\n'
    'df -h /srv | tail -1                       # Use% below 80\n'
    'curl -s $H/dashboard/summary -H "X-Admin-Key: $A" | jq .\n'
    'curl -s "$H/admin/alerts?limit=20" -H "X-Admin-Key: $A" | jq .'))
S.append(tbl([["Look at", "Healthy", "Act when"],
              ["Services", "nine " + c("Up"),
               "<b>anything restarting or exited</b>"],
              ["Disk", "under 80%", "<b>over 80%</b>: archived WAVs leave 2 days after "
                                   "their copy is proven, so a rising disk means copies are "
                                   "not being proven &mdash; look for "
                                   + c("cloud_copy_overdue") + " in the alerts"],
              ["<b>Unconfirmed recordings</b>", "zero once any outage is over",
               "<b>any left</b>: kept and archived, but CMED's record never arrived or did not "
               "match. Ask CMED to check its outbox for that visit. Nothing is ever deleted for "
               "this"],
              ["Offline periods", "dashboard &rarr; Recorders &rarr; <i>Offline this week</i>",
               "a room offline for hours, or <i>microphone silent at times</i>: look at that "
               "PC's network or microphone"],
              ["Silent recorders", "every clinic PC reported recently",
               "a PC silent during clinic hours: killed agent or no network"],
              ["Alerts", "nothing new", c("unmapped_clinic") + ", " + c("chain_entry_rejected")
               + ", " + c("spool_critical") + ", " + c("authorisation_held") + ", "
               + c("start_abandoned") + ", " + c("reassign_not_applied") + ": investigate the "
               "same day. " + c("possibly_two_consultations") + ": check the recording and "
               "split it if two patients ran together"]],
             [42 * mm, 50 * mm, 64 * mm], highlight=[3]))

S.append(Paragraph("11 &middot; Every week (20 minutes)", H2))
S.append(tbl([["", "Check", "How"],
              ["1", "Last night's backup exists", c("ls -lt /srv/aimscribe/backup | head -3")],
              ["2", "A copy of the backups off the droplet",
               c("PS&gt; scp aims@&lt;droplet-ip&gt;:/srv/aimscribe/backup/*.enc .")
               + " to a UIU machine; delete copies older than 30 days"],
              ["3", "Cloud copies are arriving", "Cloudflare &rarr; R2 &rarr; "
                                                 + c("aimscribe-archive") + ": object count grew"],
              ["4", "Security updates applied", c("cat /var/run/reboot-required")
               + " &mdash; if it exists, reboot <b>after clinic hours</b>"],
              ["5", "The certificate", "Uptime check green; nothing from Let's Encrypt in the "
                                       "owner mailbox"],
              ["6", "Costs", "DigitalOcean and Cloudflare billing pages, against the plan"]],
             [8 * mm, 52 * mm, 96 * mm]))

S.append(Paragraph("12 &middot; Every month", H2))
S.append(Paragraph("The restore drill (step 12b). Then write the date and the row counts in "
                   "the access sheet. If it fails, it is the most important problem on the "
                   "list until it is fixed.", BODY))

S.append(Paragraph("13 &middot; The dashboard page", H2))
S.append(Paragraph("The dashboard is " + c("https://&lt;host&gt;/api/v2/dashboard/") + " and "
                   "asks for the admin key, which it keeps in memory for that tab only. The "
                   "gateway gives this one path a policy that allows the page's own script and "
                   "style files and its own API calls, and nothing else; every other path stays "
                   "closed to browsers. The JSON calls in section 10 carry the same numbers.",
                   BODY))

S.append(PageBreak())

# ============================================================ PART F
S.append(part("PART F", "Updates, rollback, what was fixed, go-live",
              "What to do later, and what to settle before the clinics depend on it"))

S.append(Paragraph("14 &middot; Updating to a new release", H2))
S.append(code(
    'cd ~/AIMScribe_v3/deploy/uiu\n'
    'git log -1 --oneline > ~/before-update.txt     # where you were, for rollback\n'
    'git pull\n'
    'ls ../../backend/scripts/                      # any NEW numbered .sql file?\n'
    'docker compose up -d --build\n'
    'docker compose ps'))
S.append(Paragraph("<b>Outside clinic hours.</b> A consultation in progress survives an API "
                   "restart &mdash; the gateway holds requests for up to 30 seconds and the "
                   "recorder keeps its pieces and retries &mdash; but there is no reason to "
                   "test that.", NOTE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>A new migration is not applied by itself.</b> The schema scripts run only "
                   "on the very first start of an empty database; a new droplet built with this "
                   "manual already has every one. <b>A database created before this release needs "
                   "three applied by hand</b>, in order, as the role that owns the tables &mdash; "
                   + c("013_v3_reassign.sql") + " (recordings from older recorders that move to "
                   "the patient their prescription named), " + c("014_v3_device_health.sql")
                   + " (the recorders' offline log) and " + c("015_v3_consultation_parts.sql")
                   + " (which patient was opened at which second of a recording, and the "
                   "per-patient files cut from it). Without 015, every recording that has a "
                   "second patient click or a prescription signal stops at that step and never "
                   "completes; without 014, the offline log is refused and stays on the "
                   "laptops.", BAD))
S.append(code(
    'U=$(docker compose exec -T postgres printenv POSTGRES_USER)\n'
    'for f in 013_v3_reassign.sql 014_v3_device_health.sql \\\n'
    '         015_v3_consultation_parts.sql; do\n'
    '  docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$U" \\\n'
    '    -d aims_recordings -c "SET ROLE aims_recordings" -f /aims/scripts/$f\n'
    'done\n'
    '# check: one line must come back\n'
    'docker compose exec -T postgres psql -U "$U" -d aims_recordings -tAc \\\n'
    '  "select to_regclass(\'device_health_log\'), to_regclass(\'consultation_parts\'), \\\n'
    '   pg_get_constraintdef(oid) \\\n'
    '   from pg_constraint where conname = \'chain_entries_entry_type_check\'"'))
S.append(expect(c("device_health_log") + ", " + c("consultation_parts") + " and a CHECK that "
                "lists " + c("'reassign'") + " and " + c("'marker'")
                + ". Run the scripts again and nothing changes: all three are safe to repeat. "
                "Within a minute " + c("https://" + HOST + "/health") + " shows "
                + c('"schema":"ok"') + "; until then it names the file still missing, and the "
                "monitor alerts."))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Order: the server first, then the recorders.</b> A new recorder sends "
                   "each patient click and prescription signal to " + c("/session/marker")
                   + ", and its offline log; an old server answers 404, and a new one without "
                   "015 answers 500. The recorder keeps them waiting until the server is ready "
                   "&mdash; nothing is lost &mdash; but nothing after that step in the recording "
                   "uploads until then. So update the server, apply 013, 014 and 015, then "
                   "roll the installer out to the clinic PCs.", NOTE))

S.append(Paragraph("15 &middot; If it has to be undone", H2))
S.append(tbl([["", "Situation", "What to do"],
              ["<b>1</b>", "A release misbehaves",
               c("git checkout &lt;the commit in ~/before-update.txt&gt;") + " then "
               + c("docker compose up -d --build") + ". Return to " + c("main") + " with "
               + c("git checkout main") + " when fixed"],
              ["<b>2</b>", "The deployment fails before any clinic uses it",
               "<b>Nothing to undo.</b> Destroy the droplet. The old stack is untouched"],
              ["<b>3</b>", "It works, but a problem appears on day one",
               "Re-run the installer on the PCs with the old address and fresh tokens from the "
               "old server, and tell CMED to revert their setting. <b>Keep the droplet "
               "running</b> &mdash; it holds recordings the old one does not"],
              ["<b>4</b>", "The droplet is lost entirely",
               "Restore DigitalOcean's weekly backup as a new droplet, or rebuild with this "
               "manual and restore last night's database backup. <b>The cloud copies in R2 "
               "are unaffected</b> &mdash; if " + c("AIMS_COPY_KEY") + " was kept off the "
               "machine"]],
             [8 * mm, 50 * mm, 98 * mm], req=[4]))

S.append(Paragraph("16 &middot; What was found, and what is fixed", H2))
S.append(Paragraph("Found while checking this manual against the code on 15 October 2026, and "
                   "fixed in the code the same week. Listed so that anyone who read an earlier "
                   "copy knows the workaround is gone.", BODY))
S.append(Spacer(1, 3))
S.append(tbl([["", "Was", "Now"],
              ["<b>1</b>", "Every archived WAV stayed on the server disk: full in about 9 days",
               "Removed from this disk " + c("AIMS_ARCHIVE_LOCAL_RETENTION_DAYS") + " days (2 here) "
               "after its cloud copy is proven, and never without one. JSON, manifest and "
               "catalogue stay; the manifest says where the copy is"],
              ["<b>2</b>", "The backup service never made a backup, and could not encrypt",
               "Its own image with " + c("openssl") + "; the schedule is plain arithmetic. "
               "Step 12, run with " + c("--now") + " to prove it"],
              ["<b>3</b>", "The cloud copy was FLAC", "<b>The raw WAV</b>, as agreed, encrypted, "
                                                      "and decrypted again and hashed before upload. "
                                                      "Older FLAC copies still restore"],
              ["<b>4</b>", "The dashboard loaded blank through the gateway",
               "A policy for that path only; checked in a browser behind Caddy"],
              ["<b>5</b>", c("/health") + " said storage was connected without asking",
               "It asks both buckets, every 30 seconds at most, and says which one failed"],
              ["<b>6</b>", "The monitor's 500 GB floor alarmed for ever on a 320 GB disk; two "
                           "floors never reached it", "Each floor capped at 15% of its disk; all "
                                                      "three passed through"],
              ["<b>7</b>", "The gateway did not start: its log filter named a module Caddy does "
                           "not have", "Fixed, and the whole Caddyfile validated in "
                                       + c("caddy:2.8-alpine")],
              ["<b>8</b>", "The repository's public keys did not match the server",
               "11b now proves the pair before the installer is built"],
              ["<b>9</b>", "A recording without CMED's API 2 was deleted after 24 hours",
               "<b>Kept and archived</b>; CMED's record attaches whenever it arrives. Only a "
               "patient's refusal erases"],
              ["<b>10</b>", "A server \"no\" &mdash; a wrong key, an unmapped clinic &mdash; "
                            "deleted the recording on the laptop",
               "<b>Held on the laptop</b> and asked again every 5 minutes; the fix releases it"],
              ["<b>11</b>", "A wrong patient opened first locked the room until Stop",
               "The prescription decides: the whole recording moves to that patient (013)"],
              ["<b>12</b>", "An offline laptop's heartbeats and alerts were lost",
               "Logged on the laptop, delivered on reconnect, then deleted there (014)"]],
             [8 * mm, 66 * mm, 82 * mm]))

S.append(PageBreak())
S.append(Paragraph("17 &middot; Go-live checklist", H2))
S.append(tbl([["", "Item", "Where"],
              ["&#9744;", "Both accounts owned by " + OWNER_EMAIL + ", two-factor on, recovery "
                          "codes stored", "Part 0"],
              ["&#9744;", "Billing alert set on DigitalOcean", "0.2"],
              ["&#9744;", "Two R2 buckets, two tokens, public access off", "6"],
              ["&#9744;", "Droplet in BLR1, " + c("s-8vcpu-16gb") + ", backups and monitoring on",
               "step 1"],
              ["&#9744;", "Name resolves to the droplet", "step 2"],
              ["&#9744;", "Firewall: 22 from your IP only; 80 and 443", "step 3"],
              ["&#9744;", "Root and password logins off; timezone Asia/Dhaka; swap on", "step 4"],
              ["&#9744;", "Every " + c("ok") + " in 6g; " + c("compose settings: OK"), "step 6"],
              ["&#9744;", "<b>Six secrets stored off the machine</b>", "6g"],
              ["&#9744;", "<b>Key fingerprints identical</b> on the droplet, the laptop and "
                          "the first PC; installer rebuilt", "6d, 11b"],
              ["&#9744;", "Archive folder owned by 10001", "step 7"],
              ["&#9744;", "Nine services up; certificate obtained", "step 8"],
              ["&#9744;", c("/health") + " healthy <b>from outside</b>", "step 9"],
              ["&#9744;", "<b>All seven clinics registered with CMED's codes</b>", "step 10"],
              ["&#9744;", "CMED key issued and delivered; CMED has the host", "step 10, Part A"],
              ["&#9744;", "One PC installed and a real consultation <b>confirmed</b>",
               "step 11, Part D"],
              ["&#9744;", "First backup made with " + c("--now") + ", and <b>restored</b>",
               "step 12"],
              ["&#9744;", "Email alerts and the uptime check active; webhook set", "12c, 6f"],
              ["&#9744;", c("/health") + " shows both buckets " + c("connected"), "step 9"],
              ["&#9744;", "Schema scripts 013 and 014 present (automatic on a new database; "
                          "by hand on an old one)", "14"],
              ["&#9744;", "Checks 10 and 11 answer 401, not 404", "Part D"],
              ["&#9744;", "channel_b_test output sent to CMED", "Part D"]],
             [10 * mm, 116 * mm, 30 * mm], req=[9, 14, 16, 17, 19, 21]))

S.append(Paragraph("18 &middot; One page to remember", H2))
S.append(tbl([["Question", "Answer"],
              ["Which compose file?", "<b>" + c("deploy/uiu/docker-compose.yml") + "</b> &mdash; "
                                      "the server stack, despite the folder name"],
              ["Which droplet?", "<b>" + c("s-8vcpu-16gb") + ", Bangalore</b>, Ubuntu 24.04, "
                                 "backups and monitoring on"],
              ["What must exist first?", "<b>A domain name pointing at the droplet</b>"],
              ["Which ports open?", "<b>80 and 443 only</b>, plus SSH from your own IP"],
              ["How many buckets?", "<b>Two</b>: " + c("aimscribe-segments") + " and "
                                    + c("aimscribe-archive")],
              ["What must change for 16 GB?", c("AIMS_PG_CONF") + ", " + c("AIMS_PG_MEMORY") + ", "
                                              + c("AIMS_ARCHIVE_MEMORY") + ", "
                                              + c("AIMS_WORK_TMPFS")],
              ["<b>Where do CMED's clinic codes go?</b>", "<b>" + c("cmed_hospital_id")
                                                        + "</b> on each clinic. Step 10"],
              ["Which secrets must leave the machine?", "<b>Six</b>, led by " + c("AIMS_COPY_KEY")
                                                        + " and " + c("AIMS_BACKUP_KEY")],
              ["What does CMED change?", "<b>One setting</b> &mdash; our host. Their page changes "
                                         "nothing"],
              ["What does each clinic PC need?", "<b>One installer run</b> with the new address "
                                                 "and its own token"],
              ["First thing after it is up?", c("curl https://&lt;host&gt;/health") + " <b>from "
                                              "somewhere else</b>"],
              ["Most valuable half hour?", "<b>The restore drill.</b> Step 12b"],
              ["Who gets the notices?", OWNER_EMAIL]],
             [54 * mm, 102 * mm], highlight=[7, 12]))
S.append(Spacer(1, 6))
S.append(Paragraph("Prepared by AIMS LAB, United International University. Companions: "
                   "CMED_1 Software Integration, CMED_2 Server Deployment, and the Cloud Hosting "
                   "Plan. Every port, path, service, setting and command here was checked "
                   "against the stack itself.", SUB))


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7.3)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 9.5 * mm,
                      "AIMScribe v3 - DigitalOcean deployment manual - AIMS LAB, October 2026")
    canvas.drawRightString(A4[0] - 18 * mm, 9.5 * mm, "page %d" % doc.page)
    canvas.restoreState()


_ap = argparse.ArgumentParser()
_ap.add_argument("--out", default="DIGITALOCEAN_DEPLOYMENT_RUNBOOK.pdf")
OUT = Path(__file__).resolve().parent / _ap.parse_args().out
SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                  topMargin=16 * mm, bottomMargin=20 * mm,
                  title="Deploying the AIMScribe Server to DigitalOcean",
                  author="AIMS LAB").build(S, onFirstPage=footer, onLaterPages=footer)
print("wrote", OUT)
