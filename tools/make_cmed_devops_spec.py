#!/usr/bin/env python3
"""
Build CMED_DEVOPS_SPECIFICATION.pdf - the document CMED's devops team binds to
and tests against.

    python tools/make_cmed_devops_spec.py [--out NAME.pdf]

Endpoints, limits and reply codes are taken from the running code:
  backend/src/main_fastapi.py   (/health, the front page, router mounting)
  backend/src/clinical.py       (the two CMED endpoints, MAX_BODY_BYTES)
  backend/src/api_v2.py         (/doctors, enrolment, the v2 surface)
  recorder/api/websocket_server.py, protocol.py  (the local channel)
No secrets, no keys, no project data.
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
S.append(Paragraph("AIMScribe v3 &mdash; Integration &amp; Test Guide for CMED DevOps", H1))
S.append(Paragraph("The endpoints to bind to, and how to prove the system is running &middot; "
                   "AIMS LAB, United International University &middot; 9 October 2026", SUB))
S.append(HRFlowable(width="100%", thickness=1, color=RULE, spaceAfter=8))
S.append(Paragraph("This is the operational companion to the <b>CMED Integration "
                   "Specification</b>. That document says what the messages contain; this one "
                   "says <b>where to send them, how to authenticate, and how to verify the "
                   "system is up</b> &mdash; with commands your team can run today.", BODY))
S.append(Spacer(1, 5))
S.append(tbl([["", "What CMED's devops team needs to know"],
              ["<b>1</b>", "There are <b>two endpoints</b> on our cloud server for your backend, "
                           "and <b>one local connection</b> on each clinic PC for your web page"],
              ["<b>2</b>", "<b>One unauthenticated health endpoint</b> proves the system is "
                           "running. Poll it as often as you like"],
              ["<b>3</b>", "Authentication is a single header, <font face='Courier'>X-CMED-Key"
                           "</font>, on the two backend endpoints only"],
              ["<b>4</b>", "<b>The server is moving to DigitalOcean Bangalore</b> &mdash; the "
                           "lowest-latency region available to Dhaka. Section 2"],
              ["<b>5</b>", "<b>CMED must send us two things</b> before anything works: your "
                           "page's exact web address, and your clinic codes. Section 11"]],
             [10 * mm, 146 * mm]))
S.append(Spacer(1, 5))
S.append(tbl([["Nothing CMED runs needs to change for the clinic PCs"],
              ["The recorder listens on <font face='Courier'>127.0.0.1:5050</font> &mdash; the "
               "loopback interface of the clinic PC itself. <b>No inbound firewall rule, no port "
               "forward and no network change is needed on CMED's side or the clinic's.</b> "
               "Traffic never leaves the machine. Section 8."]],
             [156 * mm], bold_first=False))

S.append(PageBreak())
S.append(part("THE ENDPOINTS", "Exact addresses, method by method",
              "Copy these. Everything else in this document explains them"))

S.append(Paragraph("0.1 &nbsp; The gateway", H2))
S.append(Paragraph("One hostname serves everything on the server side. AIMS LAB issues it with "
                   "the key; it differs between staging and production, so hold it in "
                   "configuration.", BODY))
S.append(Spacer(1, 2))
S.append(Preformatted(
    'BASE_URL = https://<aims-host>          <-- AIMS LAB sends this with the key\n'
    '\n'
    'Transport  HTTPS only, TLS 1.2 or 1.3. Port 443. HTTP redirects to HTTPS\n'
    'Encoding   JSON, UTF-8\n'
    'Auth       X-CMED-Key: <key>            on the two clinical endpoints only', CODE))

S.append(Paragraph("0.2 &nbsp; The four addresses CMED calls", H2))
S.append(tbl([["#", "Method", "Full address", "Auth", "Body", "Success"],
              ["<b>1</b>", "<b>GET</b>", "<font face='Courier'>https://&lt;aims-host&gt;/health"
                                        "</font>", "<b>none</b>", "none", "<b>200</b>"],
              ["<b>2</b>", "<b>GET</b>", "<font face='Courier'>https://&lt;aims-host&gt;/</font>",
               "none", "none", "200"],
              ["<b>3</b>", "<b>POST</b>", "<font face='Courier'>https://&lt;aims-host&gt;"
                                          "/api/v2/clinical/patient-information</font>",
               "<b>X-CMED-Key</b>", "JSON<br/>&le;1 MB", "<b>202</b>"],
              ["<b>4</b>", "<b>POST</b>", "<font face='Courier'>https://&lt;aims-host&gt;"
                                          "/api/v2/clinical/prescription</font>",
               "<b>X-CMED-Key</b>", "JSON<br/>&le;1 MB", "<b>202</b>"]],
             [8 * mm, 16 * mm, 76 * mm, 22 * mm, 17 * mm, 17 * mm], highlight=[1], req=[3, 4]))
S.append(Spacer(1, 3))
S.append(tbl([["", "Which API it is", "Sent when"],
              ["<b>3</b>", "<b>API 2</b> &mdash; patient information",
               "the doctor opens a patient"],
              ["<b>4</b>", "<b>API 3, Channel B</b> &mdash; the prescription",
               "the doctor presses Build Prescription"]],
             [8 * mm, 72 * mm, 76 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>There is no GET for clinical data and there is no DELETE.</b> CMED writes "
                   "to us and never reads back: nothing clinical and no audio is ever served to "
                   "CMED. Both clinical calls are POST, and a repeat of the same POST is safe "
                   "&mdash; it returns 202 <font face='Courier'>ALREADY_RECEIVED</font> and "
                   "stores one row.", GOOD))

S.append(Paragraph("0.3 &nbsp; The local address on each clinic PC", H2))
S.append(tbl([["Protocol", "Full address", "Auth", "Who"],
              ["<b>WebSocket</b>", "<font face='Courier'>ws://127.0.0.1:5050/ws</font>",
               "page address allow-list", "CMED's page, in the doctor's browser"]],
             [24 * mm, 56 * mm, 40 * mm, 36 * mm]))
S.append(Spacer(1, 3))
S.append(Paragraph("Not HTTP and not a REST call &mdash; one WebSocket held open for the whole "
                   "clinic session, carrying short JSON messages each way. "
                   "<b>ws://</b> and not <b>wss://</b> because it never leaves the machine; "
                   "loopback needs no certificate and a browser permits it.", BODY))
S.append(Spacer(1, 3))
S.append(tbl([["Message CMED sends", "Which API", "Sent when"],
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
S.append(tbl([["All six, on one line each"],
              ["<font face='Courier'>GET &nbsp;https://&lt;aims-host&gt;/health</font><br/>"
               "<font face='Courier'>GET &nbsp;https://&lt;aims-host&gt;/</font><br/>"
               "<font face='Courier'>POST https://&lt;aims-host&gt;/api/v2/clinical/"
               "patient-information</font><br/>"
               "<font face='Courier'>POST https://&lt;aims-host&gt;/api/v2/clinical/prescription"
               "</font><br/>"
               "<font face='Courier'>WS &nbsp;&nbsp;ws://127.0.0.1:5050/ws &nbsp;&rarr; command "
               "\"start\"</font><br/>"
               "<font face='Courier'>WS &nbsp;&nbsp;ws://127.0.0.1:5050/ws &nbsp;&rarr; command "
               "\"prescription_built\"</font>"]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Everything else under <font face='Courier'>/api/v2/</font> is closed to "
                   "CMED's key</b> &mdash; those endpoints belong to the recorders and use client "
                   "certificates. There is nothing there CMED needs.", NOTE))

S.append(PageBreak())

# ============================================================ PART A
S.append(part("PART A", "The server",
              "Where it runs, how fast it answers, and what it is built on"))

S.append(Paragraph("1 &middot; Addresses", H2))
S.append(tbl([["", "Address", "Auth", "Who calls it"],
              ["<b>Health</b>", "<font face='Courier'>GET /health</font>", "<b>none</b>",
               "<b>CMED devops, monitoring, load balancers</b>"],
              ["Front page", "<font face='Courier'>GET /</font>", "none",
               "a human, to confirm which system answered"],
              ["<b>API 2</b>", "<font face='Courier'>POST /api/v2/clinical/patient-information"
                               "</font>", "<b>X-CMED-Key</b>", "CMED's backend"],
              ["<b>API 3, Channel B</b>", "<font face='Courier'>POST /api/v2/clinical/prescription"
                                          "</font>", "<b>X-CMED-Key</b>", "CMED's backend"],
              ["API 1 &amp; API 3, Channel A", "<font face='Courier'>ws://127.0.0.1:5050/ws</font>",
               "origin allow-list", "CMED's web page, on the clinic PC"],
              ["Everything else under <font face='Courier'>/api/v2/</font>", "&mdash;",
               "device certificates", "<b>the recorders only.</b> Not CMED"]],
             [34 * mm, 58 * mm, 26 * mm, 38 * mm], highlight=[1], req=[3, 4]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>CMED only ever calls three things</b>: the health endpoint, and the two "
                   "clinical endpoints. The rest of the <font face='Courier'>/api/v2/</font> "
                   "surface belongs to the recorders and is closed to CMED's key.", GOOD))
S.append(Spacer(1, 4))
S.append(tbl([["The hostname"],
              ["<b>AIMS LAB will send the final hostname with the key.</b> Please treat it as "
               "configuration, not something compiled in &mdash; it will differ between your "
               "staging and production environments, and the staging host may be rebuilt. "
               "Throughout this document it is written as "
               "<font face='Courier'>&lt;aims-host&gt;</font>."]],
             [156 * mm], bold_first=False))

S.append(Paragraph("2 &middot; Where the server runs, and how fast it answers", H2))
S.append(Paragraph("The system is being deployed to <b>DigitalOcean, Bangalore region "
                   "(BLR1)</b>. Of all regions available to us, Bangalore is the closest to Dhaka "
                   "and gives the lowest round trip.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["Region", "Distance from Dhaka", "Typical round trip", "Verdict"],
              ["<b>Bangalore &mdash; BLR1</b>", "~2,400 km", "<b>45&ndash;70 ms</b>",
               "<b>Chosen</b>"],
              ["Singapore &mdash; SGP1", "~3,000 km", "60&ndash;95 ms", "second choice"],
              ["Frankfurt &mdash; FRA1", "~7,400 km", "130&ndash;170 ms", "too far"],
              ["New York &mdash; NYC3", "~12,900 km", "230&ndash;280 ms", "too far"]],
             [44 * mm, 36 * mm, 38 * mm, 32 * mm], highlight=[1]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Latency does not affect recording.</b> The microphone is driven by the "
                   "program on the clinic PC, over loopback, so it starts in under a millisecond "
                   "regardless of where our server is or whether it is reachable at all. The round "
                   "trip to Bangalore only affects how quickly your two backend messages are "
                   "acknowledged &mdash; and those are queued on your side anyway, so a slow "
                   "reply is never visible to a doctor.", GOOD))
S.append(Spacer(1, 4))
S.append(H3 and Paragraph("2.1 &nbsp; The machine", H3))
S.append(tbl([["", "Specification", "Note"],
              ["Provider and region", "<b>DigitalOcean, Bangalore (BLR1)</b>", "lowest ping to Dhaka"],
              ["Droplet", "<b>Basic / Regular &mdash; <font face='Courier'>s-8vcpu-16gb</font></b>",
               "the smallest standard droplet that meets every requirement"],
              ["<b>Processors</b>", "<b>8 vCPU</b>", "as specified"],
              ["<b>Memory</b>", "<b>16 GB</b>", "specification asked 8 GB; this tier includes 16"],
              ["<b>Disk</b>", "<b>320 GB SSD, included</b>",
               "specification asked 200 GB+; <b>no add-on volume needed</b>"],
              ["Outbound transfer", "<b>6 TB/month included</b>",
               "<b>so uploads to Cloudflare R2 cost nothing</b>"],
              ["Operating system", "Ubuntu Server 24.04 LTS", "supported to 2029"],
              ["Price", "<b>$96/month, $1,152/year</b>", "verify on DigitalOcean's pricing page"]],
             [34 * mm, 58 * mm, 64 * mm], req=[3, 4, 5], highlight=[6]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Why this droplet:</b> 8 vCPU on DigitalOcean's Basic line is paired with "
                   "16 GB and 320 GB as a fixed bundle, so asking for 8 cores and 200 GB lands "
                   "here and the extra memory and disk come at no additional cost. The 6 TB of "
                   "included transfer matters more than it looks: the server pushes roughly "
                   "520 GB a month up to Cloudflare R2, and on this provider that is free.", GOOD))

S.append(Paragraph("2.2 &nbsp; Storage &mdash; the 320 GB disk is not where audio lives", H2))
S.append(Paragraph("The droplet's 320 GB is <b>working space</b>. It is not the audio archive, and "
                   "it is nowhere near enough to be one. Every recording is copied to "
                   "<b>Cloudflare R2 object storage</b> and then removed from the server.", BAD))
S.append(Spacer(1, 4))
S.append(tbl([["", "<b>Server disk &mdash; 320 GB</b>", "<b>Cloudflare R2 &mdash; 3 TB</b>"],
              ["Holds", "Operating system, the eight programs, both databases, logs, and "
                        "recordings <b>in transit</b>",
               "<b>Every recording, permanently</b>, as encrypted FLAC"],
              ["How long", "Hours. A file is removed once the cloud copy is verified",
               "<b>Permanently.</b> This is the research dataset"],
              ["Capacity", "about a day and a half of recording as a buffer",
               "<b>41,600 consultations</b> &mdash; the 18,000-patient study uses 1.3 TB"],
              ["Cost", "included in the $96 droplet", "<b>$45/month, $540/year</b>"],
              ["If it fills", "archiving stops; clinics keep recording locally",
               "buy more &mdash; $15 per TB per month, no limit"]],
             [24 * mm, 64 * mm, 68 * mm], highlight=[1]))
S.append(Spacer(1, 4))
S.append(tbl([["Total cost of the cloud deployment", "Monthly", "Yearly"],
              ["Droplet &mdash; 8 vCPU / 16 GB / 320 GB, Bangalore", "$96", "$1,152"],
              ["Droplet backups", "$19", "$230"],
              ["<b>Cloudflare R2 &mdash; 3 TB of audio</b>", "<b>$45</b>", "<b>$540</b>"],
              ["Transfer to R2 (519 GB/mo, inside the included 6 TB)", "$0", "$0"],
              ["<b>Total</b>", "<b>$160</b>", "<b>$1,922</b>"]],
             [100 * mm, 28 * mm, 28 * mm], highlight=[3, 5]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>R2 charges nothing to read data back</b>, which is why it was chosen: a "
                   "research dataset gets downloaded many times, and on Amazon S3 each full read "
                   "of 3 TB would cost about $250.", GOOD))

S.append(H3 and Paragraph("2.3 &nbsp; The cloud copy keeps the same folder structure as the local archive", H3))
S.append(Paragraph("Object storage has no real folders &mdash; a key is just a long name &mdash; "
                   "but a key may contain <font face='Courier'>/</font>, and every tool from "
                   "Cloudflare's own browser to <font face='Courier'>rclone</font> displays those "
                   "as folders. <b>So the layout is entirely ours to choose, and it already "
                   "mirrors the local archive:</b>", BODY))
S.append(Spacer(1, 2))
S.append(Preformatted(
    'ON THE SERVER\n'
    '  archive/AALO_DHOLPUR/DR0042/2026-10-05/<stem>/<stem>.wav\n'
    '                                                <stem>.json\n'
    '                                                <stem>.manifest.json\n'
    '\n'
    'IN CLOUDFLARE R2\n'
    '  copies/AALO_DHOLPUR/DR0042/2026-10-05/<stem>.v1.flac.enc\n'
    '  copies/AALO_DHOLPUR/DR0042/2026-10-05/<stem>.v1.json.enc\n'
    '\n'
    '  where <stem> = P0012345_DR0042_AALO_DHOLPUR_101432_102755_20261005', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Same", "Different", "Why"],
              ["<b>clinic / doctor / date</b>, in that order",
               "a <font face='Courier'>copies/</font> prefix",
               "lets other things share the bucket later"],
              ["the file name, character for character",
               "<font face='Courier'>.flac</font> not <font face='Courier'>.wav</font>",
               "lossless, 40% smaller &mdash; verified by decoding and comparing"],
              ["", "<font face='Courier'>.enc</font> on the end",
               "<b>encrypted before it leaves UIU</b> &mdash; see below"],
              ["", "<font face='Courier'>.v1</font> version number",
               "a corrected copy never overwrites the first"],
              ["", "no per-consultation sub-folder",
               "the stem is already unique, so the extra level earns nothing in a bucket"]],
             [52 * mm, 48 * mm, 56 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>So you can browse R2 exactly as you browse the server</b> &mdash; open a "
                   "clinic, then a doctor, then a day, and the consultations are there. A restore "
                   "can walk one clinic or one date without consulting any database, which is the "
                   "reason the structure was chosen.", GOOD))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>It is also ours to change.</b> One function builds these keys "
                   "(<font face='Courier'>copy_object_key</font>), so if a different arrangement "
                   "suits the research better &mdash; year and month above clinic, say, or the "
                   "patient above the date &mdash; it is a small change, not a migration. Worth "
                   "settling before 18,000 recordings are in place rather than after.", NOTE))
S.append(Spacer(1, 4))
S.append(tbl([["One thing Cloudflare cannot do"],
              ["<b>Every file is encrypted on our server before it is uploaded</b> (AES-GCM), and "
               "the key never leaves UIU. The folder names and file names are visible to "
               "Cloudflare; <b>the audio and the clinical record are not</b>. Cloudflare stores "
               "bytes it cannot read, and so could any future provider &mdash; which is what makes "
               "putting patient recordings on commodity storage acceptable at all."]],
             [156 * mm], bold_first=False))

S.append(PageBreak())

# ============================================================ PART B
S.append(part("PART B", "Proving it is running",
              "The health endpoint, and the exact commands to test with"))

S.append(Paragraph("3 &middot; The health endpoint", H2))
S.append(Paragraph("<b>This is the endpoint to monitor.</b> No key, no body, no side effects. It "
                   "checks the database and the queue on every call.", BODY))
S.append(Spacer(1, 2))
S.append(Preformatted('curl -s https://<aims-host>/health', CODE))
S.append(Paragraph("A healthy system answers <b>HTTP 200</b> with:", BODY))
S.append(Preformatted(
    '{\n'
    '  "status":   "healthy",\n'
    '  "database": "connected",\n'
    '  "redis":    "connected",\n'
    '  "minio":    "connected",\n'
    '  "system":   "AIMScribe v3",\n'
    '  "srs":      "3.x",\n'
    '  "version":  "3.x.x",\n'
    '  "mode":     "FastAPI Async"\n'
    '}', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Field", "Watch for", "What it means"],
              ["<b>status</b>", "<font face='Courier'>healthy</font>",
               "<b>Everything is up.</b> Anything else is "
               "<font face='Courier'>degraded</font>"],
              ["database", "<font face='Courier'>connected</font>",
               "If disconnected, nothing can be stored. <b>Alert on this</b>"],
              ["redis", "<font face='Courier'>connected</font>",
               "The queue. If disconnected, archiving stalls"],
              ["<b>system</b>", "<b><font face='Courier'>AIMScribe v3</font></b>",
               "<b>Check this field.</b> See the warning below"],
              ["version", "changes after a deploy", "Useful to confirm a release landed"]],
             [26 * mm, 46 * mm, 84 * mm], highlight=[1], req=[4]))
S.append(Spacer(1, 4))
S.append(tbl([["Please check the <font face='Courier'>system</font> field, not just the status code"],
              ["There is an older <b>version 1</b> AIMScribe backend still running elsewhere, and "
               "it also answers on a <font face='Courier'>/health</font> path that looks almost "
               "identical. <b>Pointing at the wrong one costs an afternoon</b>, because every "
               "request succeeds and nothing is ever recorded. The "
               "<font face='Courier'>system</font> field is first in the response for exactly this "
               "reason &mdash; assert on it in your monitoring, not only on HTTP 200."]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(tbl([["Monitoring guidance", "Recommendation"],
              ["How often", "Every 30&ndash;60 seconds is ample"],
              ["Alert when", "<font face='Courier'>status != \"healthy\"</font> twice in a row, or "
                             "no answer for 2 minutes"],
              ["Timeout", "5 seconds"],
              ["Do not", "<b>Do not alert the clinics.</b> A doctor cannot act on this, and "
                         "recording continues regardless"]],
             [40 * mm, 116 * mm]))

S.append(Paragraph("4 &middot; Which system answered &mdash; the front page", H2))
S.append(Paragraph("Opening <font face='Courier'>https://&lt;aims-host&gt;/</font> in a browser "
                   "shows a one-screen page naming the system, its version, and where each party "
                   "connects. It is the quickest way for a human to confirm they are pointed at "
                   "AIMScribe v3 and not the version 1 backend. It reads no consultation data.", BODY))

S.append(PageBreak())

# ============================================================ PART C
S.append(part("PART C", "Binding to the two endpoints",
              "Authentication, limits, and a test you can run before writing any code"))

S.append(Paragraph("5 &middot; Authentication", H2))
S.append(tbl([["", "Detail"],
              ["Header", "<font face='Courier'>X-CMED-Key: &lt;key&gt;</font>"],
              ["Applies to", "<b>The two clinical endpoints only.</b> Not "
                             "<font face='Courier'>/health</font>"],
              ["Issued by", "AIMS LAB, once, out of band. <b>It is shown once and not "
                            "recoverable</b> &mdash; store it immediately"],
              ["Scope", "One key for CMED, covering all seven clinics"],
              ["Rotation", "Ask us; we issue the new key before revoking the old, so there is no "
                           "window where both fail"],
              ["<b>Where it must live</b>", "<b>Your server's secret store or environment.</b> "
                                            "Never in page source, never in a repository, never in "
                                            "a mobile app"]],
             [34 * mm, 122 * mm], req=[6]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>A wrong or missing key returns 401 INVALID_KEY.</b> That is a hard stop, "
                   "not something to retry &mdash; retrying a bad key just fills both our logs. "
                   "Alert your team and contact us.", BAD))

S.append(Paragraph("6 &middot; Limits and behaviour", H2))
S.append(tbl([["", "Value", "What happens at the limit"],
              ["Body size", "<b>1 MB</b>", "<font face='Courier'>413 TOO_LARGE</font>. Usually "
                                           "means an image or file got into the JSON"],
              ["Content type", "<font face='Courier'>application/json</font>, UTF-8",
               "<font face='Courier'>400 MALFORMED_JSON</font>"],
              ["Success", "<b>HTTP 202</b>",
               "<b>Stored before you are answered.</b> A 202 means it is on disk"],
              ["Duplicate send", "<b>HTTP 202</b> <font face='Courier'>ALREADY_RECEIVED</font>",
               "<b>Safe.</b> One row, not two. Retry freely"],
              ["Validation failure", "<b>HTTP 422</b> <font face='Courier'>SCHEMA_INVALID</font>",
               "<b>Stored in quarantine, not discarded.</b> The reply lists the bad fields"],
              ["Our server is down", "500 / 503",
               "<b>Queue and retry with backoff.</b> Never drop the message"],
              ["Suggested client timeout", "10 seconds",
               "Then retry. The message is queued on your side, so this is never user-facing"]],
             [38 * mm, 48 * mm, 70 * mm], highlight=[3, 4], req=[5]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>There is no rate limit for CMED.</b> Seven clinics at fourteen rooms "
                   "produce roughly one message every two seconds at peak, which is far below "
                   "anything we would need to throttle. Send as the consultations happen; just do "
                   "it from a queue so a slow reply never reaches the doctor's screen.", GOOD))

S.append(Paragraph("7 &middot; Test it now &mdash; four commands", H2))
S.append(Paragraph("These need no code and no page. Run them against the staging host as soon as "
                   "you have the key.", BODY))
S.append(Spacer(1, 3))
S.append(H3 and Paragraph("7.1 &nbsp; Is it running?", H3))
S.append(Preformatted(
    'curl -s https://<aims-host>/health | jq .\n'
    '# expect: "status": "healthy"  AND  "system": "AIMScribe v3"', CODE))
S.append(H3 and Paragraph("7.2 &nbsp; Is my key accepted? (send a patient)", H3))
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
S.append(Paragraph("<b>Send that exact command twice.</b> Both times you should get "
                   "<b>202</b> &mdash; the second is "
                   "<font face='Courier'>ALREADY_RECEIVED</font>. That proves retries are safe, "
                   "which is the single most useful thing to know before writing your queue.", GOOD))

S.append(PageBreak())
S.append(H3 and Paragraph("7.3 &nbsp; Does validation work? (deliberately break it)", H3))
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
S.append(Paragraph("<b>The reply tells you which fields were wrong, by name.</b> Build against "
                   "that: it is faster than reading the specification, and the record is kept in "
                   "quarantine rather than lost, so nothing is wasted by experimenting.", GOOD))

S.append(H3 and Paragraph("7.4 &nbsp; Send the prescription", H3))
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
               "\"stored\", not \"matched to a recording\"</b>. The match is a separate step on "
               "our side that compares the five identifying fields from your message against the "
               "five the recorder reported. If <font face='Courier'>start_time</font> differs by "
               "one second, or loses its <font face='Courier'>+06:00</font>, every reply above is "
               "still green and <b>the recording is deleted twenty-four hours later</b>.<br/><br/>"
               "<b>So the real end-to-end test needs a recorder running</b> &mdash; section 9. "
               "Please do not treat four green curls as integration complete."]],
             [156 * mm], bold_first=False))

S.append(PageBreak())

# ============================================================ PART D
S.append(part("PART D", "The clinic PC, and going live",
              "The local channel, the end-to-end test, and what each side owes the other"))

S.append(Paragraph("8 &middot; The clinic PC &mdash; nothing for devops to open", H2))
S.append(tbl([["", "Detail"],
              ["What runs there", "<b>AIMScribe.exe</b>, one per consulting room, installed by "
                                  "AIMS LAB"],
              ["Listens on", "<font face='Courier'>127.0.0.1:5050</font> &mdash; <b>loopback "
                             "only</b>"],
              ["Reachable from", "<b>That PC alone.</b> Not the LAN, not the internet, by design"],
              ["<b>Firewall changes needed</b>", "<b>None</b>, on CMED's side or the clinic's"],
              ["Authentication", "<b>No key.</b> The recorder checks your page's web address "
                                 "instead &mdash; see section 11"],
              ["If it is not running", "Your page's connection simply fails. Show \"AIMScribe is "
                                       "not running on this PC\""]],
             [40 * mm, 116 * mm], highlight=[4]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Why no key here:</b> a secret placed in a web page is readable by anyone "
                   "who opens the page, so it would protect nothing. Instead the recorder accepts "
                   "connections only from web addresses AIMS LAB has registered, only to loopback, "
                   "and only from the same machine. <b>That is why your exact page address is a "
                   "blocking prerequisite</b> rather than a detail.", GOOD))

S.append(Paragraph("9 &middot; The end-to-end test that actually proves it works", H2))
S.append(tbl([["", "Step", "Pass condition"],
              ["1", "AIMS LAB installs the recorder on one test PC and enrols it",
               "<font face='Courier'>/health</font> is healthy; the recorder's tray icon is "
               "running"],
              ["2", "Open your page on that PC and connect to "
                    "<font face='Courier'>ws://127.0.0.1:5050/ws</font>",
               "Connection opens. <b>If refused, your address is not registered</b>"],
              ["3", "Send <font face='Courier'>{\"command\":\"doctors\"}</font>",
               "<b>Returns the clinic code.</b> Use it &mdash; do not hardcode one"],
              ["4", "Open a test patient: <b>API 1</b> and <b>API 2</b> together",
               "<font face='Courier'>RECORDING_STARTED</font> or "
               "<font face='Courier'>RECORDING_PROVISIONAL</font>, and 202"],
              ["5", "Talk into the microphone for two minutes",
               "The <font face='Courier'>level</font> field in the status messages moves above 0"],
              ["6", "Build a prescription: <b>API 3 on both channels</b>",
               "<font face='Courier'>GATE_ARMED</font>, and 202"],
              ["7", "Open a second test patient",
               "First recording closes, second starts, no gap"],
              ["8", "<b>AIMS LAB confirms the recording was matched and archived</b>",
               "<b>This is the only step that proves the integration. Ask us to confirm it "
               "explicitly</b>"]],
             [7 * mm, 72 * mm, 77 * mm], bold_first=False, req=[8]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Step 8 is the one to insist on.</b> Steps 1 to 7 can all pass while the "
                   "five fields silently fail to match. Ask us to confirm, for your test patient, "
                   "that a recording exists, was confirmed, and was archived &mdash; we can see "
                   "that on the dashboard in a few seconds, and it is the only green light that "
                   "means anything.", BAD))

S.append(PageBreak())
S.append(Paragraph("10 &middot; Our test tooling, which you are welcome to use", H2))
S.append(tbl([["", "What it does", "Command"],
              ["1", "Checks a message against every rule, with no server and no network",
               "<font face='Courier'>python tools/channel_b_test.py --local</font>"],
              ["2", "<b>Runs every case that matters against a live server</b> &mdash; a good "
                    "message, the same twice, a broken one, a changed prescription, a wrong key, "
                    "an oversized body &mdash; and checks each reply",
               "<font face='Courier'>python tools/channel_b_test.py --server https://&lt;aims-host"
               "&gt; --key &lt;key&gt;</font>"],
              ["3", "Shows every message and reply side by side in a browser",
               "open <font face='Courier'>/protocol-test</font> on the staging site"]],
             [7 * mm, 86 * mm, 63 * mm], bold_first=False, highlight=[2]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Please run number 2 and send us the output before the first clinic opens.</b> "
                   "It takes under a minute and it has caught every integration problem we have "
                   "seen so far. AIMS LAB will send the script with the key.", GOOD))

S.append(Paragraph("11 &middot; What each side must hand over", H2))
S.append(tbl([["AIMS LAB gives CMED", "When"],
              ["The staging and production hostnames", "with the key"],
              ["<b>The X-CMED-Key</b> &mdash; shown once", "on request, out of band"],
              ["The seven clinic codes", "with the key"],
              ["The integration test script", "with the key"],
              ["Confirmation that a test recording matched and archived",
               "<b>on request, during step 8</b>"]],
             [100 * mm, 56 * mm]))
S.append(Spacer(1, 3))
S.append(tbl([["<b>CMED gives AIMS LAB &mdash; both are blocking</b>", "Why it blocks"],
              ["<b>1. The exact web address of your page</b>, including staging and preview "
               "&mdash; for example <font face='Courier'>https://ehr.aaloclinic.com</font>",
               "<b>Without it the page cannot open the local connection at all.</b> Nothing "
               "works. Note "
               "<font face='Courier'>www.</font> and non-<font face='Courier'>www</font> are "
               "different addresses, as are <font face='Courier'>http</font> and "
               "<font face='Courier'>https</font> and any non-default port"],
              ["<b>2. Your own clinic codes</b>, so we can map them to ours",
               "<b>Without the mapping, a visit cannot be tied to the clinic it happened in. The "
               "record is stored, you receive a normal 202, and the recording is erased after 24 "
               "hours.</b> The failure is completely silent from CMED's side, which is why it "
               "matters more than it sounds"],
              ["A named technical contact", "So a question in either direction does not wait a week"],
              ["A staging environment we can send to", "So we can test our side against yours"]],
             [62 * mm, 94 * mm], req=[1, 2]))

S.append(PageBreak())
S.append(Paragraph("12 &middot; Go-live checklist", H2))
S.append(tbl([["", "Item", "Owner"],
              ["&#9744;", "Production hostname and key issued and stored in CMED's secret store",
               "both"],
              ["&#9744;", "<b>CMED's page addresses registered</b> (production and staging)",
               "<b>CMED &rarr; AIMS LAB</b>"],
              ["&#9744;", "<b>Clinic code mapping agreed</b>", "<b>CMED &rarr; AIMS LAB</b>"],
              ["&#9744;", "<font face='Courier'>/health</font> in CMED's monitoring, asserting on "
                          "<font face='Courier'>system</font> and "
                          "<font face='Courier'>status</font>", "CMED"],
              ["&#9744;", "Both endpoints sent from a <b>durable queue with retry</b>, not inline "
                          "in the request that serves the doctor", "CMED"],
              ["&#9744;", "<b>The five fields built once per consultation and reused by all four "
                          "messages</b>", "CMED"],
              ["&#9744;", "Clinic code read from the <font face='Courier'>doctors</font> command, "
                          "not hardcoded", "CMED"],
              ["&#9744;", "Microphone <font face='Courier'>level</font> shown on the page",
               "CMED"],
              ["&#9744;", "<font face='Courier'>channel_b_test.py</font> run and output sent to "
                          "AIMS LAB", "CMED"],
              ["&#9744;", "<b>End-to-end test done, and step 8 confirmed by AIMS LAB</b>", "both"],
              ["&#9744;", "Server on DigitalOcean BLR1, backups enabled, R2 bucket live",
               "AIMS LAB"],
              ["&#9744;", "Alerting on unconfirmed recordings", "AIMS LAB"]],
             [10 * mm, 112 * mm, 34 * mm], req=[2, 3], highlight=[10]))

S.append(Paragraph("13 &middot; One page for the CMED devops engineer", H2))
S.append(tbl([["Question", "Answer"],
              ["How do I know it is up?",
               "<font face='Courier'>GET /health</font> &mdash; no key. Expect "
               "<font face='Courier'>status: healthy</font>"],
              ["<b>What else must I assert on?</b>",
               "<b><font face='Courier'>system == \"AIMScribe v3\"</font></b> &mdash; there is an "
               "older v1 backend with a similar health path"],
              ["How many endpoints do I call?", "<b>Three.</b> Health, patient-information, "
                                                "prescription"],
              ["How do I authenticate?", "<font face='Courier'>X-CMED-Key</font> header, on the "
                                         "two clinical endpoints only"],
              ["Where does the key live?", "Your server's secret store. <b>Never in a browser</b>"],
              ["Body limit?", "<b>1 MB.</b> JSON, UTF-8"],
              ["What is success?", "<b>HTTP 202</b> &mdash; stored before you are answered"],
              ["Is retrying safe?", "<b>Yes.</b> Same body twice gives one row and "
                                    "<font face='Courier'>ALREADY_RECEIVED</font>"],
              ["What about 422?", "<b>Nothing is lost.</b> Quarantined, and the reply names the "
                                  "bad fields"],
              ["What about 500 / 503?", "<b>Queue and retry with backoff.</b> Never drop"],
              ["Rate limit?", "<b>None for CMED.</b> Peak is about one message every two seconds"],
              ["Do I open a firewall port for the clinic PCs?", "<b>No.</b> The recorder is "
                                                               "loopback-only"],
              ["Where is the server?", "<b>DigitalOcean Bangalore (BLR1)</b>, 8 vCPU / 16 GB / "
                                       "320 GB, ~45&ndash;70 ms from Dhaka"],
              ["<b>Where does the audio live?</b>", "<b>Cloudflare R2, 3 TB</b>, as encrypted FLAC. "
                                                   "<b>Not on the 320 GB server disk</b>, which is working space"],
              ["Same folders as the server?", "<b>Yes</b> &mdash; "
                                             "<font face='Courier'>copies/clinic/doctor/date/</font>"],
              ["Does latency affect recording?", "<b>No.</b> The microphone is driven locally"],
              ["<b>Does a 202 mean it worked?</b>", "<b>No. It means stored, not matched.</b> See "
                                                    "section 9 step 8"],
              ["What must I send AIMS LAB?", "<b>Your page's exact addresses, and your clinic "
                                             "codes.</b> Both blocking"]],
             [54 * mm, 102 * mm], highlight=[2, 15]))
S.append(Spacer(1, 6))
S.append(Paragraph("Prepared by AIMS LAB, United International University. Companion to the CMED "
                   "Integration Specification, which carries the message-level detail. Every "
                   "endpoint, limit and reply code here is taken from the running code. Prices and "
                   "latency figures are indicative and should be confirmed before purchase.", SUB))


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7.3)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 9.5 * mm,
                      "AIMScribe v3 - integration and test guide for CMED DevOps - October 2026")
    canvas.drawRightString(A4[0] - 18 * mm, 9.5 * mm, "page %d" % doc.page)
    canvas.restoreState()


_ap = argparse.ArgumentParser()
_ap.add_argument("--out", default="CMED_DEVOPS_SPECIFICATION.pdf")
OUT = Path(__file__).resolve().parent.parent / _ap.parse_args().out
SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                  topMargin=16 * mm, bottomMargin=20 * mm,
                  title="AIMScribe v3 - Integration and Test Guide for CMED DevOps",
                  author="AIMS LAB").build(S, onFirstPage=footer, onLaterPages=footer)
print("wrote", OUT)
