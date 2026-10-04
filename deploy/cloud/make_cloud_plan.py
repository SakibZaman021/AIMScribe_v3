"""Build CLOUD_HOSTING_PLAN.pdf - the complete cloud plan, in plain language.

    python deploy/cloud/make_cloud_plan.py [--out NAME.pdf]

No secrets and no project data: it is a document generator, safe in the repo.
"""
import argparse
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, HRFlowable, PageBreak)

INK = colors.HexColor("#0b0b0b")
TEAL = colors.HexColor("#17836f")
BLUE = colors.HexColor("#2a78d6")
MUTED = colors.HexColor("#898781")
RULE = colors.HexColor("#e1e0d9")
BAND = colors.HexColor("#eef3f2")
WARNC = colors.HexColor("#7a5200")
CRIT = colors.HexColor("#97292a")
GOODC = colors.HexColor("#006300")
WIN = colors.HexColor("#e7f6e7")

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
CELL = ParagraphStyle("CELL", parent=ss["Normal"], fontSize=8.4, textColor=INK, leading=11.2)
MONO = ParagraphStyle("MONO", parent=ss["Normal"], fontName="Courier",
                      fontSize=7.9, textColor=INK, leading=11.0)
LBL = ParagraphStyle("LBL", parent=ss["Normal"], fontName="Helvetica-Bold",
                     fontSize=8.4, textColor=INK, leading=11.2)


def tbl(rows, widths, mono=(), bold_first=True, highlight=None):
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
    if highlight:
        for row in highlight:
            style.append(("BACKGROUND", (0, row), (-1, row), WIN))
    t.setStyle(TableStyle(style))
    return t


S = []
S.append(Paragraph("AIMScribe v3 &mdash; The Complete Cloud Plan", H1))
S.append(Paragraph("Full cloud and partial cloud, DigitalOcean and Amazon, priced monthly and "
                   "yearly &middot; Storage on Cloudflare R2 &middot; AIMS LAB &middot; "
                   "5 October 2026", SUB))
S.append(HRFlowable(width="100%", thickness=1, color=RULE, spaceAfter=8))
S.append(tbl([["This plan assumes", "Value"],
              ["Consultations in the collection phase", "18,000, over about 2.5 months"],
              ["Consultations per clinic day", "277"],
              ["Average recording", "120 MB &mdash; 24 minutes at 44.1 kHz, mono, 16-bit"],
              ["<b>Audio storage you will buy</b>", "<b>3 TB</b>"],
              ["<b>Storage provider</b>", "<b>Cloudflare R2</b> &mdash; your policy"],
              ["Running", "permanently, not only for the study"]],
             [66 * mm, 90 * mm]))

# ================================================================ 1
S.append(Paragraph("1 &middot; The words, before the numbers", H2))
S.append(Paragraph("A rented computer is sold by three numbers, and a storage service by two "
                   "more. None of them are explained anywhere you buy them, so here they are.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["Word", "What it really means", "Think of it as"],
              ["<b>vCPU</b><br/>(\"cores\", \"workers\")",
               "How many jobs the machine can do in the same instant. 4 vCPU means four things "
               "happen side by side instead of waiting in line",
               "<b>How many staff you employ.</b> One person does one job at a time"],
              ["<b>GB RAM</b><br/>(\"memory\")",
               "The space those staff have to work in. A program must be loaded into memory "
               "before it can run. Run out and the machine crawls, then stops",
               "<b>The size of the desk.</b> Too small and the work falls on the floor"],
              ["<b>GB disk</b><br/>(\"SSD\", \"storage\")",
               "Where things are kept when nothing is using them: the operating system, the "
               "databases, files waiting their turn",
               "<b>The filing cabinet.</b> It holds things; it does no work"],
              ["<b>Object storage</b><br/>(R2, S3, Spaces)",
               "A storage service you reach over the network, not a disk. Unlimited, cheap, "
               "replicated by the provider. You cannot run a database on it, but it is ideal "
               "for finished files",
               "<b>A warehouse</b> down the road. Enormous, cheap, slightly further away"],
              ["<b>Egress</b><br/>(\"data transfer out\")",
               "What a provider charges to send your own data out of their network. <b>This is "
               "the cost that surprises people</b>",
               "<b>A toll on the road out.</b> Free at some gates, expensive at others"]],
             [30 * mm, 74 * mm, 52 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("So <b>\"8 GB / 4 vCPU / 160 GB\"</b> is one rented computer: four staff, a "
                   "desk big enough for them, and a 160 GB filing cabinet. Section 5 justifies "
                   "each of those three numbers.", GOOD))

S.append(PageBreak())

# ================================================================ 2
S.append(Paragraph("2 &middot; What goes into the 3 TB, and how long it lasts", H2))
S.append(Paragraph("The format decides everything else, so it comes first. <b>FLAC is not a "
                   "backup and not a different recording. It is the same audio stored smaller</b> "
                   "&mdash; lossless, every sample identical when unpacked. The system already "
                   "proves it on every file: it decodes the FLAC again and compares it to the "
                   "original before accepting the copy. Any audio tool opens it directly.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["What you store", "Per recording", "The 18,000 study", "What 3 TB holds",
               "Months of recording"],
              ["Raw WAV only", "120 MB", "2,160 GB", "25,000 recordings", "about 3.5"],
              ["<b>FLAC only</b>", "<b>72 MB</b>", "<b>1,296 GB</b>", "<b>41,600 recordings</b>",
               "<b>about 6</b>"],
              ["Both (the old plan)", "192 MB", "3,456 GB", "15,600 recordings", "about 2"]],
             [30 * mm, 24 * mm, 30 * mm, 36 * mm, 36 * mm], highlight=[2]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Store FLAC only.</b> Identical quality, and your 3 TB lasts nearly twice "
                   "as long. The whole 18,000-patient study fits in 1.3 TB, leaving 1.7 TB of "
                   "room &mdash; about four more months of recording before any decision is "
                   "needed.", GOOD))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>No Glacier, no deep archive.</b> Deep archive is cheap because reading "
                   "from it takes 12 to 48 hours. Your researchers will read this dataset "
                   "constantly for the next year, so everything stays in normal storage where it "
                   "opens instantly. Revisit this in a few years, when old recordings genuinely "
                   "are not being touched &mdash; not now.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["When 3 TB fills, you choose", "What it means"],
              ["Buy more", "R2 has no limit; it is $15 per TB per month. Another 3 TB is $45"],
              ["Move the oldest months to deep archive",
               "Cheapest, and only sensible once nobody is reading them"],
              ["Delete the oldest months",
               "Only if the research is finished. Nothing else holds a copy by then"]],
             [52 * mm, 104 * mm]))

# ================================================================ 3
S.append(Paragraph("3 &middot; Storage: Cloudflare R2 &mdash; your policy, costed", H2))
S.append(tbl([["Item", "Rate", "At 3 TB", "Monthly", "Yearly"],
              ["Storage", "$0.015 per GB", "3,000 GB", "<b>$45</b>", "<b>$540</b>"],
              ["Uploading files (writes)", "$4.50 per million", "~22,000/month", "under $1", "~$2"],
              ["Reading files (reads)", "$0.36 per million", "research use", "under $1", "~$3"],
              ["<b>Sending data out (egress)</b>", "<b>$0 &mdash; always</b>", "any amount",
               "<b>$0</b>", "<b>$0</b>"],
              ["<b>Total</b>", "", "", "<b>~$46</b>", "<b>~$545</b>"]],
             [44 * mm, 32 * mm, 30 * mm, 24 * mm, 26 * mm], highlight=[4, 5]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Why R2 is the right policy choice, in one sentence:</b> every other "
                   "provider charges you to read your own data back, and you are going to read "
                   "this dataset many times over.", GOOD))
S.append(Spacer(1, 3))
S.append(Paragraph("Reading the full 3 TB once would cost about <b>$250 on Amazon S3</b> and "
                   "about <b>$30 on DigitalOcean Spaces</b>. On R2 it costs <b>nothing</b>, "
                   "however often you do it. For a research dataset that will be downloaded for "
                   "transcription, for model training, and again every time somebody re-runs an "
                   "experiment, that is the difference between a fixed bill and a growing one.", BODY))

S.append(PageBreak())

# ================================================================ 4
S.append(Paragraph("4 &middot; The trap that decides which server to rent", H2))
S.append(Paragraph("R2 never charges you to take data <b>out</b>. But something has to put data "
                   "<b>in</b> &mdash; and that is your server, sending about <b>519 GB every "
                   "month</b> up to Cloudflare. Whether that is free depends entirely on who you "
                   "rent the server from.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["Your server is at", "What it charges to send 519 GB/month out", "Monthly", "Yearly"],
              ["<b>DigitalOcean</b>", "The droplet includes <b>5,000 GB</b> of transfer a month. "
                                      "519 is well inside it", "<b>$0</b>", "<b>$0</b>"],
              ["<b>Amazon EC2</b>", "First 100 GB free, then <b>$0.1093 per GB</b> to leave AWS. "
                                    "419 GB is billable", "<b>$46</b>", "<b>$549</b>"],
              ["Amazon EC2 &rarr; Amazon S3", "Free, if you create a VPC gateway endpoint "
                                              "(free, but you must set it up)", "$0", "$0"]],
             [34 * mm, 78 * mm, 22 * mm, 22 * mm], highlight=[1]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>So R2 and Amazon are a poor pair, and R2 and DigitalOcean are a good "
                   "one.</b> Choosing Amazon and R2 together costs you $549 a year for nothing "
                   "&mdash; the data is simply walking out of Amazon's gate. If the storage "
                   "policy is R2, the sensible server is DigitalOcean. If the server must be "
                   "Amazon, the sensible storage is S3.", BAD))
S.append(Spacer(1, 4))
S.append(Paragraph("This is the single most important sentence in this document, and it is the "
                   "kind of thing no pricing page tells you.", NOTE))

# ================================================================ 5
S.append(Paragraph("5 &middot; The server, number by number", H2))
S.append(H3 and Paragraph("Staff (vCPU): the work needs 1.5, so buy 4", H3))
S.append(tbl([["Job", "How much work it really is", "Staff needed"],
              ["Compressing audio to FLAC",
               "The only heavy job. About 2 minutes of one worker per consultation. 277 a day is "
               "roughly 9 hours of one worker, spread across 24 hours", "under 1"],
              ["The website, the API, CMED's messages",
               "About 1.2 requests a second across all 14 rooms, measured. That is very light",
               "well under 1"],
              ["Both databases", "10 GB of data, simple lookups", "well under 1"],
              ["Queue, gateway, housekeeping", "Background work", "negligible"],
              ["<b>Total</b>", "", "<b>about 1.5</b>"]],
             [44 * mm, 82 * mm, 30 * mm], highlight=[5]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Two would work. Four is what to buy</b> &mdash; the extra is for the busy "
                   "hour when fourteen rooms finish at once and fourteen recordings want "
                   "compressing together. Eight would be paying for staff who sit idle; an "
                   "earlier draft of this plan said eight, and that was wrong.", NOTE))
S.append(Spacer(1, 4))
S.append(H3 and Paragraph("Desk (memory): 8 GB", H3))
S.append(tbl([["Part", "Normal use", "Its cap"],
              ["The API server", "320 MB", "1 GB"],
              ["PostgreSQL, both databases", "240 MB", "1 GB"],
              ["Archive worker", "250 MB", "768 MB"],
              ["Queue (Redis)", "23 MB", "256 MB"],
              ["Gateway (HTTPS)", "40 MB", "256 MB"],
              ["<b>Everything together</b>", "<b>about 1 GB</b>", "<b>3.4 GB</b>"]],
             [56 * mm, 50 * mm, 50 * mm], highlight=[6]))
S.append(Spacer(1, 3))
S.append(Paragraph("Measured under a test twice as heavy as the real clinics. <b>4 GB is too "
                   "tight</b> &mdash; the caps alone come to 3.4 GB and leave nothing for the "
                   "operating system. <b>8 GB is right.</b> <b>16 GB is waste</b> on a 10 GB "
                   "database.", BODY))

S.append(PageBreak())

# ================================================================ 6
S.append(Paragraph("6 &middot; Filing cabinet (disk): 160 GB, and no extra disk", H2))
S.append(Paragraph("The audio does not stay on the server. It is uploaded to R2 and deleted "
                   "locally. So the disk only holds what must physically live on the machine:", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["What", "Size", "Why it has to be there"],
              ["Operating system", "20 GB", "Linux, packages, updates"],
              ["The programs", "15 GB", "Eight parts: the API, PostgreSQL, Redis, the archive "
                                        "worker, the gateway, the connection pooler, the viewer, "
                                        "CMED's page"],
              ["<b>Both databases</b>", "<b>10 GB</b>",
               "Patient records, prescriptions, previous visits, and the tracking record of every "
               "audio file. <b>Grows 20&ndash;40 GB a year</b>"],
              ["Database safety log", "10 GB", "Every change written down before it is applied, "
                                               "so a power cut cannot corrupt anything"],
              ["Backup staging", "10 GB", "Last night's backup, before and after upload"],
              ["Working space", "3 GB", "One consultation being joined and compressed"],
              ["<b>Upload buffer</b>", "<b>30 GB</b>",
               "<b>If R2 is unreachable, finished recordings wait here instead of being lost.</b> "
               "30 GB is about a day and a half of recording"],
              ["Logs", "5 GB", "What each part did, for diagnosing problems"],
              ["Kept free deliberately", "20 GB", "The worker refuses to write below this, so a "
                                                  "full disk can never damage a recording"],
              ["Never-full headroom", "25 GB", "No disk should run above 80% full"],
              ["<b>Total needed</b>", "<b>148 GB</b>", ""]],
             [38 * mm, 20 * mm, 98 * mm], highlight=[11]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>A DigitalOcean droplet of this size includes 160 GB. 148 fits inside 160, "
                   "so no extra disk is needed and none is budgeted.</b> On Amazon the disk is "
                   "billed separately and must be ordered at 200 GB &mdash; that is a real "
                   "difference between the two, not a preference.", GOOD))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>The one line to watch over the years</b> is the databases, at "
                   "20&ndash;40 GB a year. On 160 GB that is comfortable for roughly three "
                   "years. The decision to grow belongs to 2029, not today.", NOTE))

# ================================================================ 7
S.append(Paragraph("7 &middot; Full cloud &mdash; DigitalOcean", H2))
S.append(Paragraph("Everything rented. Nothing at UIU except the laptops in the clinics.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["What to buy", "Exactly what to pick", "Monthly", "Yearly"],
              ["The server", "Droplet &rarr; Basic &rarr; Regular. <b>8 GB / 4 vCPU / 160 GB "
                             "SSD</b>. Region: <b>Bangalore (BLR1)</b>", "$48", "$576"],
              ["Server backups", "Tick \"Enable backups\" when creating it &mdash; 20% of the "
                                 "droplet price", "$9.60", "$115"],
              ["Audio storage", "<b>Cloudflare R2</b>, 3 TB, bought from Cloudflare separately",
               "$45", "$540"],
              ["Domain name", "e.g. aimscribe.uiu.ac.bd &mdash; or use a UIU subdomain for free",
               "~$1", "~$12"],
              ["HTTPS certificate", "Free, renews itself", "$0", "$0"],
              ["Database", "<b>Nothing to buy.</b> PostgreSQL runs on the server itself", "$0", "$0"],
              ["Sending data to R2", "Included in the droplet's 5 TB transfer", "<b>$0</b>", "<b>$0</b>"],
              ["<b>TOTAL</b>", "", "<b>$104</b>", "<b>$1,243</b>"]],
             [32 * mm, 78 * mm, 22 * mm, 24 * mm], highlight=[8]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>For the 2.5-month collection phase: about $200</b>, because storage is "
                   "only half full on average while it fills.", BODY))

S.append(PageBreak())

# ================================================================ 8
S.append(Paragraph("8 &middot; Full cloud &mdash; Amazon Web Services", H2))
S.append(Paragraph("Use the <b>Mumbai region (ap-south-1)</b> &mdash; nearest to Dhaka, about "
                   "40 milliseconds away. Two versions are priced: with R2 as your policy "
                   "states, and with S3 which avoids the transfer charge.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["What to buy", "Exactly what to pick", "Monthly", "Yearly"],
              ["The server", "EC2 &rarr; <b>t4g.large</b> (2 vCPU, 8 GB, ARM). "
                             "<b>t4g.xlarge</b> (4 vCPU, 16 GB) if you want the headroom "
                             "DigitalOcean gives by default", "$49<br/><i>(xlarge $98)</i>",
               "$588<br/><i>($1,176)</i>"],
              ["<b>The disk</b>", "<b>EBS gp3 volume, 200 GB.</b> On Amazon this is a separate "
                                  "purchase &mdash; it does not come with the server",
               "$18", "$216"],
              ["Backups", "EBS snapshots, daily, keep 7 days", "$10", "$120"],
              ["Fixed address", "Elastic IP &mdash; free while attached to a running server",
               "$0", "$0"],
              ["Database", "<b>Nothing to buy.</b> PostgreSQL runs on the server itself",
               "$0<br/><i>(RDS ~$25)</i>", "$0<br/><i>($300)</i>"],
              ["Audio storage &mdash; <b>option 1</b>", "<b>Cloudflare R2</b>, 3 TB, per your policy",
               "$45", "$540"],
              ["<b>&hellip; plus transfer out</b>", "<b>519 GB/month leaving AWS at $0.1093/GB</b>",
               "<b>$46</b>", "<b>$549</b>"],
              ["<b>TOTAL with R2</b>", "", "<b>$168</b>", "<b>$2,013</b>"],
              ["Audio storage &mdash; <b>option 2</b>", "Amazon S3 Standard, 3 TB, same region",
               "$75", "$900"],
              ["<b>TOTAL with S3</b>", "<b>transfer free via a VPC gateway endpoint</b>",
               "<b>$152</b>", "<b>$1,824</b>"]],
             [38 * mm, 70 * mm, 24 * mm, 24 * mm], highlight=[8, 10]))
S.append(Spacer(1, 4))
S.append(H3 and Paragraph("Three things you must set up on Amazon that DigitalOcean does for you", H3))
S.append(tbl([["", "What", "Why it matters"],
              ["1", "A <b>VPC gateway endpoint</b> for S3 (free)",
               "Without it, traffic between your own server and your own S3 bucket can be billed "
               "as if it left Amazon. This single setting is the difference between $0 and about "
               "$46 a month"],
              ["2", "A <b>billing alarm</b> at, say, $250",
               "Amazon will let a misconfiguration run for a month before you see it. "
               "DigitalOcean's prices are mostly fixed; Amazon's are mostly per-use"],
              ["3", "<b>Versioning</b> on the storage bucket",
               "So a mistaken delete can be undone. It costs almost nothing at this size"]],
             [7 * mm, 54 * mm, 95 * mm], bold_first=False))

S.append(PageBreak())

# ================================================================ 9
S.append(Paragraph("9 &middot; Partial cloud &mdash; your own server, storage in the cloud", H2))
S.append(Paragraph("The clinics and the audio work exactly the same. The difference is that the "
                   "computer running the software sits at UIU instead of being rented.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["What", "Detail", "Monthly", "Yearly"],
              ["The server", "The 8-core / 8 GB machine AIMS LAB offers", "$0", "$0"],
              ["<b>Its disk</b>", "<b>100 GB is not enough &mdash; 160 GB is needed.</b> "
                                  "A 256 GB SSD is a one-off purchase of roughly $30",
               "&mdash;", "<b>~$30 once</b>"],
              ["Audio storage", "Cloudflare R2, 3 TB", "$45", "$540"],
              ["Domain name", "A UIU subdomain", "~$1", "~$12"],
              ["<b>TOTAL</b>", "", "<b>$46</b>", "<b>$552</b>"]],
             [32 * mm, 78 * mm, 22 * mm, 24 * mm], highlight=[5]))
S.append(Spacer(1, 4))
S.append(tbl([["What you gain", "What you take on"],
              ["<b>Saves about $700 a year</b> against full cloud",
               "UIU's network rules must allow it: a fixed name, one port open, no filtering of "
               "the connection to Cloudflare"],
              ["Patient data stays on a university machine, which simplifies the ethics question",
               "Power cuts, cooling and the physical machine become your problem"],
              ["No monthly server bill, ever",
               "Nobody else is on call. If it stops at 11pm, somebody at AIMS LAB drives in"],
              ["Fast local access for researchers on campus",
               "Backups, patching and disk monitoring are all yours to do"]],
             [78 * mm, 78 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>The honest summary:</b> partial cloud is cheaper by about $58 a month, "
                   "and the saving is real. What you pay instead is in attention and "
                   "availability &mdash; somebody has to own that machine. For a 2.5-month "
                   "study with staff on hand, that is a fair trade. For permanent running with "
                   "nobody specifically assigned to it, the rented server is worth the money.", BODY))

# ================================================================ 10
S.append(Paragraph("10 &middot; Everything side by side", H2))
S.append(tbl([["Setup", "Server", "Storage", "Transfer", "Monthly", "Yearly"],
              ["<b>Partial &mdash; own server + R2</b>", "$0", "$45", "$0", "<b>$46</b>",
               "<b>$552</b>"],
              ["<b>Full &mdash; DigitalOcean + R2</b>", "$58", "$45", "$0", "<b>$104</b>",
               "<b>$1,243</b>"],
              ["Full &mdash; DigitalOcean + Spaces", "$58", "$60", "$0", "$119", "$1,423"],
              ["Full &mdash; Amazon + S3", "$77", "$75", "$0", "$152", "$1,824"],
              ["Full &mdash; Amazon + R2", "$77", "$45", "<b>$46</b>", "$168", "$2,013"]],
             [48 * mm, 20 * mm, 20 * mm, 22 * mm, 22 * mm, 24 * mm], highlight=[1, 2]))
S.append(Spacer(1, 5))
S.append(Paragraph("<b>If the storage policy is Cloudflare R2, there are only two sensible "
                   "answers:</b> your own server (cheapest, if somebody owns it), or "
                   "DigitalOcean (nobody owns it, and it still costs less than Amazon). "
                   "<b>Amazon with R2 is the worst of all five</b> &mdash; you pay Amazon $549 a "
                   "year for the privilege of sending your data to Cloudflare.", GOOD))

S.append(PageBreak())

# ================================================================ 11
S.append(Paragraph("11 &middot; What runs where", H2))
S.append(tbl([["Layer", "What", "Lives on", "If it stops"],
              ["Clinic", "AIMScribe agent, one per room", "the doctor's laptop",
               "That room stops. The other thirteen continue"],
              ["Clinic", "CMED's page", "the doctor's laptop, in a browser",
               "No new consultation can be opened"],
              ["Server", "The API: permissions, CMED's messages, the dashboard", "the server",
               "Laptops keep recording and hold up to 13 hours each"],
              ["Server", "<b>aims_recordings</b> &mdash; audio tracking", "the server's disk",
               "Nothing can be confirmed or archived"],
              ["Server", "<b>aims_clinical</b> &mdash; patients, prescriptions", "the server's disk",
               "Recording continues but goes unconfirmed"],
              ["Server", "Archive worker &mdash; joins, compresses, uploads", "the server",
               "Recordings queue in the 30 GB buffer"],
              ["Storage", "The FLAC recordings", "Cloudflare R2",
               "Finished recordings wait in the buffer; clinics unaffected for ~1.5 days"]],
             [18 * mm, 48 * mm, 36 * mm, 54 * mm]))

# ================================================================ 12
S.append(Paragraph("12 &middot; Keeping it running", H2))
S.append(tbl([["Risk", "What already protects you", "What you must still do"],
              ["Server down an hour", "Laptops hold ~13 hours each. Nothing is lost",
               "Nothing. This is designed for"],
              ["Server down a day", "Laptops begin to fill; clinics eventually stop",
               "<b>Alerting</b>, so it is noticed in minutes not at end of shift"],
              ["R2 unreachable", "The 30 GB buffer absorbs about a day and a half",
               "An alert when the buffer passes half full"],
              ["Disk fills", "The worker refuses to write rather than corrupt anything",
               "Watch the dashboard; it reports this"],
              ["A recording CMED never describes", "Erased after 24 hours &mdash; correct, but "
                                                   "silent",
               "Check the unconfirmed count every morning. It should be zero"],
              ["The databases are lost", "Nightly encrypted backup to R2",
               "<b>Restore one on day one, then monthly.</b> A backup nobody has read is not a "
               "backup"],
              ["Someone deletes audio by mistake", "Nothing, by default",
               "Turn on R2 object versioning"],
              ["The bill surprises you", "R2 is a fixed rate with no egress",
               "On Amazon only: set a billing alarm"]],
             [34 * mm, 60 * mm, 62 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>One server, not two.</b> What keeps clinics recording through an outage "
                   "is not a second server &mdash; it is the buffer on each laptop, which you "
                   "already have. A second server doubles the cost and more than doubles the "
                   "complexity. Spend the effort on alerting and on testing restores.", GOOD))

# ================================================================ 13
S.append(Paragraph("13 &middot; What must be built before any of this works", H2))
S.append(Paragraph("Today the system writes finished recordings to the server's own disk and "
                   "never removes them. That fills any disk, on any provider, in about a week.", BAD))
S.append(Spacer(1, 4))
S.append(tbl([["", "Work", "Why", "Effort"],
              ["1", "<b>Upload the finished recording to R2, verify it arrived, then delete it "
                    "from the server</b>",
               "Without this nothing else in this plan is possible", "a few days"],
              ["2", "Keep the FLAC, drop the WAV",
               "Same audio, 40% less storage, your 3 TB lasts twice as long", "half a day"],
              ["3", "Point the dashboard and the restore tool at R2",
               "So a recording can still be found and fetched", "a day"],
              ["4", "Alert when the upload buffer passes half full",
               "The buffer only helps if somebody is warned before it fills", "a day"]],
             [7 * mm, 56 * mm, 64 * mm, 29 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>About a week of work, and it must be finished and tested before "
                   "collection starts, not during it.</b> The order matters: upload, verify, "
                   "<i>then</i> delete. Never delete on the strength of an upload that was "
                   "merely accepted.", NOTE))

S.append(PageBreak())

# ================================================================ 14
S.append(Paragraph("14 &middot; Setting it up, in order", H2))
S.append(tbl([["", "Step", "Where"],
              ["1", "Open a Cloudflare account and create an R2 bucket. Turn on versioning",
               "Cloudflare"],
              ["2", "Create the server: 8 GB / 4 vCPU / 160 GB, Bangalore, backups ticked",
               "DigitalOcean"],
              ["3", "Point a domain name at it, and let the certificate issue itself", "DNS"],
              ["4", "Install Docker, copy the project, fill in the settings file", "the server"],
              ["5", "Start everything with one command; eleven database migrations run themselves",
               "the server"],
              ["6", "Register the seven clinics and issue CMED's key", "the server"],
              ["7", "Run the load test: 28 rooms at once. It must lose nothing", "your laptop"],
              ["8", "Install the recorder on one clinic PC and record one real consultation",
               "a clinic"],
              ["9", "<b>Restore one recording from R2 and prove it opens</b>", "your laptop"],
              ["10", "Turn on alerting, then open the clinics", "&mdash;"]],
             [7 * mm, 112 * mm, 37 * mm], bold_first=False))

# ================================================================ 15
S.append(Paragraph("15 &middot; One page to remember", H2))
S.append(tbl([["Question", "Answer"],
              ["What does \"8 GB / 4 vCPU / 160 GB\" mean?",
               "One rented computer: four staff, a desk for them, a 160 GB filing cabinet"],
              ["Why 4 staff?", "The measured work needs 1.5. Four covers the busy hour. "
                               "Eight would be waste"],
              ["Why 160 GB and no extra disk?",
               "Audio lives in R2, so the server needs 148 GB &mdash; and 160 comes included "
               "with the droplet"],
              ["Where do we buy storage?", "<b>Cloudflare R2</b>, 3 TB &mdash; $45/month, "
                                           "$540/year, and nothing to read it back"],
              ["Do we keep WAV or FLAC?", "<b>FLAC only.</b> Identical audio, 3 TB lasts twice "
                                          "as long"],
              ["Do we need Glacier?", "<b>No.</b> Everything stays instantly readable"],
              ["<b>Cheapest overall</b>", "<b>Partial cloud &mdash; $46/month, $552/year</b>, if "
                                          "somebody at AIMS LAB owns the machine"],
              ["<b>Best rented option</b>", "<b>DigitalOcean + R2 &mdash; $104/month, "
                                            "$1,243/year</b>"],
              ["Amazon?", "$152/month with S3. <b>$168 with R2 &mdash; avoid</b>, Amazon charges "
                          "$549/year to send data to Cloudflare"],
              ["How long does 3 TB last?", "The whole 18,000 study uses 1.3 TB. 3 TB is about "
                                           "six months of recording"],
              ["What must be built first?", "Upload to R2 and delete locally. About a week"],
              ["The one step never to skip", "<b>Restore a recording on day one and prove it "
                                             "opens</b>"]],
             [56 * mm, 100 * mm], highlight=[7, 8]))
S.append(Spacer(1, 6))
S.append(Paragraph("Prepared by AIMS LAB. Prices are indicative, October 2026, and should be "
                   "confirmed on each provider's own pricing page before purchase. Server and "
                   "storage figures are measured on this project's own test bench.", SUB))


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7.3)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 9.5 * mm,
                      "AIMScribe v3 - the complete cloud plan - AIMS LAB, October 2026")
    canvas.drawRightString(A4[0] - 18 * mm, 9.5 * mm, "page %d" % doc.page)
    canvas.restoreState()


ap = argparse.ArgumentParser()
ap.add_argument("--out", default="CLOUD_HOSTING_PLAN.pdf")
args = ap.parse_args()
OUT = Path(__file__).resolve().parent / args.out
SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                  topMargin=16 * mm, bottomMargin=20 * mm,
                  title="AIMScribe v3 - The Complete Cloud Plan",
                  author="AIMS LAB").build(S, onFirstPage=footer, onLaterPages=footer)
print("wrote", OUT)
