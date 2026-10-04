"""Build CLOUD_HOSTING_PLAN.pdf - the full cloud plan, in plain language.

    python deploy/cloud/make_cloud_plan.py

Writes the PDF next to this file. No secrets, no project data - it is a
document generator, safe in the repository.
"""
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, HRFlowable, PageBreak)

INK = colors.HexColor("#0b0b0b")
TEAL = colors.HexColor("#17836f")
MUTED = colors.HexColor("#898781")
RULE = colors.HexColor("#e1e0d9")
BAND = colors.HexColor("#f4f6f5")
WARNC = colors.HexColor("#7a5200")
CRIT = colors.HexColor("#97292a")
GOODC = colors.HexColor("#006300")

ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Title"], fontName="Helvetica-Bold",
                    fontSize=18, textColor=INK, spaceAfter=2, alignment=0, leading=22)
SUB = ParagraphStyle("SUB", parent=ss["Normal"], fontSize=9.5, textColor=MUTED, spaceAfter=9)
H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontName="Helvetica-Bold",
                    fontSize=12.5, textColor=TEAL, spaceBefore=14, spaceAfter=5)
H3 = ParagraphStyle("H3", parent=ss["Heading3"], fontName="Helvetica-Bold",
                    fontSize=10.5, textColor=INK, spaceBefore=9, spaceAfter=3)
BODY = ParagraphStyle("BODY", parent=ss["Normal"], fontSize=9.4, textColor=INK, leading=13.4)
NOTE = ParagraphStyle("NOTE", parent=BODY, textColor=WARNC)
BAD = ParagraphStyle("BAD", parent=BODY, textColor=CRIT)
GOOD = ParagraphStyle("GOOD", parent=BODY, textColor=GOODC)
CELL = ParagraphStyle("CELL", parent=ss["Normal"], fontSize=8.4, textColor=INK, leading=11.0)
MONO = ParagraphStyle("MONO", parent=ss["Normal"], fontName="Courier",
                      fontSize=7.9, textColor=INK, leading=11.0)
LBL = ParagraphStyle("LBL", parent=ss["Normal"], fontName="Helvetica-Bold",
                     fontSize=8.4, textColor=INK, leading=11.0)


def tbl(rows, widths, mono=(), bold_first=True):
    data = [[Paragraph("<b>%s</b>" % h, CELL) for h in rows[0]]]
    for r in rows[1:]:
        data.append([Paragraph(str(c), MONO if i in mono else (LBL if (i == 0 and bold_first) else CELL))
                     for i, c in enumerate(r)])
    t = Table(data, colWidths=widths, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("TEXTCOLOR", (0, 0), (-1, 0), TEAL),
        ("GRID", (0, 0), (-1, -1), 0.35, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafbfa")]),
    ]))
    return t


S = []
S.append(Paragraph("AIMScribe v3 &mdash; The Cloud Plan", H1))
S.append(Paragraph("Everything in the cloud, explained without jargon &middot; "
                   "AIMS LAB &middot; 5 October 2026", SUB))
S.append(HRFlowable(width="100%", thickness=1, color=RULE, spaceAfter=8))
S.append(Paragraph("This replaces the earlier version. The UIU server option is gone, the "
                   "\"extra disk\" is gone, and the storage design is simpler because you were "
                   "right about the FLAC copies. Every number is justified where it appears.", BODY))

# ---------------------------------------------------------------- 1
S.append(Paragraph("1 &middot; The words, first", H2))
S.append(Paragraph("A rented cloud server is described by three numbers. Here is what each one "
                   "means, without jargon.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["The word", "What it really means", "Think of it as"],
              ["<b>vCPU</b><br/>(also \"cores\")",
               "How many jobs the computer can do at the same moment. 4 vCPU means four jobs "
               "run side by side instead of queueing",
               "<b>How many workers you employ.</b> One worker can only do one thing at a time"],
              ["<b>GB RAM</b><br/>(memory)",
               "The desk space those workers have. Programs must be loaded into memory to run. "
               "Run out and the machine slows to a crawl or stops",
               "<b>The size of the desk.</b> Too small and work falls on the floor"],
              ["<b>GB disk</b><br/>(SSD, storage)",
               "Where things are kept when nothing is using them: the operating system, the "
               "databases, files waiting to be uploaded",
               "<b>The filing cabinet.</b> It keeps things; it does not do work"]],
             [28 * mm, 76 * mm, 52 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("So <b>\"4 vCPU / 8 GB / 160 GB\"</b> means: four workers, a desk big enough "
                   "for all of them, and a filing cabinet that holds 160 GB. It is one rented "
                   "computer, described by its three limits.", GOOD))
S.append(Spacer(1, 4))
S.append(H3 and Paragraph("Hard disk or cloud disk?", H3))
S.append(tbl([["", "What it is", "In this plan"],
              ["The server's own disk",
               "Fast SSD storage that comes with the rented server, already included in its "
               "price. You do not buy it separately and you cannot touch it &mdash; it is in "
               "the provider's data centre",
               "<b>This is what we use.</b> 160 GB, included"],
              ["An \"extra disk\" or volume",
               "More storage you bolt on to the server afterwards, billed per gigabyte per "
               "month. Useful when the built-in disk is too small",
               "<b>Not needed.</b> See section 4"],
              ["A physical hard disk",
               "A drive you buy and screw into a machine you own",
               "Not applicable &mdash; nothing here is a machine you own"]],
             [34 * mm, 82 * mm, 40 * mm]))
S.append(Spacer(1, 3))
S.append(Paragraph("The \"extra disk\" in the previous plan was for the UIU machine, whose "
                   "built-in 100 GB was too small. <b>You have ruled that option out, so the "
                   "extra disk goes with it.</b> A rented server simply comes with a bigger disk "
                   "from the start.", BODY))

S.append(PageBreak())

# ---------------------------------------------------------------- 2
S.append(Paragraph("2 &middot; You were right about FLAC &mdash; and it makes this simpler", H2))
S.append(Paragraph("The earlier plan had two copies of every consultation: the raw WAV in fast "
                   "storage, and a FLAC copy in Amazon's deep archive. <b>On a cloud setup that "
                   "is keeping the same audio twice.</b>", BODY))
S.append(Spacer(1, 4))
S.append(Paragraph("The thing to understand about FLAC: <b>it is not a different recording and "
                   "it is not a backup. It is the same audio, stored more efficiently.</b> It is "
                   "lossless &mdash; it unpacks back to exactly the same numbers, every sample "
                   "identical. The system already proves this on every file before accepting the "
                   "copy. Think of it as a zip file that any audio tool can open directly.", GOOD))
S.append(Spacer(1, 4))
S.append(Paragraph("So the question is not \"WAV or FLAC plus archive\". It is simply: "
                   "<b>which format do we store in?</b>", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["Choice", "Storage for the 18,000-patient phase", "Per month", "Quality"],
              ["Keep WAV and FLAC (the old plan)", "3,456 GB", "~$79", "identical"],
              ["Keep WAV only", "2,160 GB", "~$50", "identical"],
              ["<b>Keep FLAC only</b>", "<b>1,296 GB</b>", "<b>~$30</b>", "<b>identical</b>"]],
             [56 * mm, 48 * mm, 26 * mm, 26 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Store FLAC only.</b> Same audio, 40% less storage, 40% less cost, one "
                   "copy to manage instead of two. Researchers open it directly &mdash; every "
                   "common audio library reads FLAC natively, and anything that insists on WAV "
                   "can unpack it in a second.", GOOD))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>And yes: no Glacier.</b> Deep archive exists to make rarely-read data "
                   "cheap, at the price of waiting 12 to 48 hours to read it. Your researchers "
                   "will read this dataset constantly for the next year. Keep it all in normal "
                   "cloud storage where it opens instantly. Revisit in a few years, when old "
                   "recordings genuinely are not being touched.", BODY))

# ---------------------------------------------------------------- 3
S.append(Paragraph("3 &middot; Why this server, number by number", H2))
S.append(Paragraph("You asked why 4 vCPU. Here is the honest working &mdash; and it corrects an "
                   "over-specification in the previous plan.", BODY))
S.append(Spacer(1, 4))
S.append(H3 and Paragraph("Workers (vCPU): 2 is enough, 4 is comfortable", H3))
S.append(tbl([["Job", "How much work", "Workers needed"],
              ["Compressing audio to FLAC", "The only heavy job. About 2 minutes of one worker "
                                            "per consultation; 277 a day is about 9 hours of "
                                            "one worker, spread across the day",
               "under 1"],
              ["The website and the API", "About 1.2 requests a second across all 14 rooms. "
                                          "That is very light", "well under 1"],
              ["The databases", "10 GB of data, simple queries", "well under 1"],
              ["Everything else", "Queue, gateway, housekeeping", "negligible"],
              ["<b>Total</b>", "", "<b>about 1.5</b>"]],
             [44 * mm, 82 * mm, 30 * mm]))
S.append(Spacer(1, 3))
S.append(Paragraph("So <b>2 workers genuinely covers it</b>, and <b>4 gives real headroom</b> "
                   "for the busy hour when fourteen rooms finish consultations at once. "
                   "<b>The previous plan said 4 minimum and 8 recommended. That was too much, "
                   "and I should have shown this working the first time.</b> 8 workers would be "
                   "paying for idle capacity.", NOTE))
S.append(Spacer(1, 4))
S.append(H3 and Paragraph("Memory: 8 GB", H3))
S.append(Paragraph("Measured on our own test, under twice the real load: everything together "
                   "uses about <b>1 GB</b> in normal running, and we cap the parts at 3.4 GB so "
                   "no single piece can take the machine down. 8 GB leaves the rest for the "
                   "database to keep recent data in memory, which is what makes the dashboard "
                   "feel instant. <b>4 GB would be too tight</b> &mdash; the caps alone are "
                   "3.4 GB. <b>16 GB would be wasted</b> on a 10 GB database.", BODY))

S.append(PageBreak())

# ---------------------------------------------------------------- 4
S.append(Paragraph("4 &middot; Why 160 GB of disk, and why no extra disk", H2))
S.append(Paragraph("The audio does not live on the server. It is uploaded to cloud storage and "
                   "deleted locally. So the disk only holds the things that must be on the "
                   "machine itself:", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["What", "Size", "Why it must be there"],
              ["Operating system", "20 GB", "Linux, packages, updates"],
              ["The programs themselves", "15 GB", "Eight parts: the API, both databases, the "
                                                   "queue, the archive worker, the gateway, the "
                                                   "page CMED uses"],
              ["<b>Both databases</b>", "<b>10 GB</b>", "Patient records, prescriptions, "
                                                        "previous visits, and the tracking "
                                                        "record of every audio file"],
              ["Database safety log", "10 GB", "Every change written down before it is applied, "
                                               "so a power cut cannot corrupt anything"],
              ["Backup staging", "10 GB", "Last night's backup, before and after upload"],
              ["Working space", "3 GB", "One consultation being joined and compressed"],
              ["<b>Upload buffer</b>", "<b>30 GB</b>", "<b>If cloud storage is unreachable, "
                                                       "finished recordings wait here.</b> "
                                                       "30 GB is about a day and a half"],
              ["Logs", "5 GB", "What each part did, for diagnosing problems"],
              ["Kept free on purpose", "20 GB", "The worker refuses to write below this, so a "
                                                "full disk can never damage a recording"],
              ["Never-full headroom", "25 GB", "No disk should run above 80% full"],
              ["<b>Total</b>", "<b>148 GB</b>", ""]],
             [40 * mm, 20 * mm, 96 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>A rented server in this class comes with 160 GB included.</b> 148 fits "
                   "inside 160, so <b>no extra disk is needed and none is budgeted.</b> That is "
                   "the justification you asked for &mdash; the extra disk only existed because "
                   "the UIU machine had 100 GB, and that option is gone.", GOOD))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>The one line to watch over the years</b> is the databases. They grow by "
                   "roughly 20&ndash;40 GB a year and never shrink. On 160 GB that is "
                   "comfortable for about three years. After that, either move up one server "
                   "size or add a volume then &mdash; a decision for 2029, not now.", NOTE))

# ---------------------------------------------------------------- 5
S.append(Paragraph("5 &middot; Plan A &mdash; DigitalOcean", H2))
S.append(Paragraph("The simpler of the two. One company, one bill, one control panel, and a "
                   "Bangalore or Singapore location close to Dhaka.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["What to buy", "Exactly", "Price"],
              ["The server", "Droplet &mdash; Basic, Regular. <b>8 GB memory, 4 vCPU, 160 GB "
                             "SSD</b>. Region: Bangalore (BLR1)", "<b>$48/mo</b>"],
              ["Storage for audio", "Spaces Object Storage. $5 covers the first 250 GB, then "
                                    "about 2 cents per GB. At 1.3 TB", "<b>~$26/mo</b>"],
              ["Backups of the server", "Droplet Backups, weekly images", "$9.60/mo"],
              ["Database", "<b>None to buy.</b> PostgreSQL runs on the server itself", "$0"],
              ["Domain and certificate", "A name such as aimscribe.uiu.ac.bd. The certificate is "
                                         "free and renews itself", "~$1/mo"],
              ["<b>Total</b>", "", "<b>~$85/mo</b>"]],
             [34 * mm, 92 * mm, 30 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Over the 2.5-month collection phase: about $210</b>, because storage is "
                   "only half full on average while it fills. <b>Running on permanently: about "
                   "$85 a month</b>, rising slowly as the archive grows.", BODY))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Why this one is easier:</b> the disk is included, the backup is a "
                   "checkbox, and object storage is on the same bill. There is no separate "
                   "billing for data moving between your server and your storage inside the same "
                   "region.", BODY))

S.append(PageBreak())

# ---------------------------------------------------------------- 6
S.append(Paragraph("6 &middot; Plan B &mdash; Amazon Web Services", H2))
S.append(Paragraph("More pieces, more control, and more ways to be surprised by a bill. Use the "
                   "<b>Mumbai region (ap-south-1)</b> &mdash; it is the closest to Dhaka, about "
                   "40 milliseconds away.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["What to buy", "Exactly", "Price"],
              ["The server", "EC2 instance, <b>t4g.large</b> &mdash; 2 vCPU, 8 GB memory. "
                             "ARM-based, which is cheaper and ample here. "
                             "Step up to t4g.xlarge (4 vCPU, 16 GB) if you want headroom",
               "<b>~$49/mo</b><br/>(xlarge ~$98)"],
              ["<b>The disk</b>", "<b>EBS gp3 volume, 200 GB.</b> On AWS the disk is billed "
                                  "separately &mdash; it does not come with the server. This is "
                                  "the main difference from DigitalOcean",
               "<b>~$18/mo</b>"],
              ["Storage for audio", "S3 Standard. At 1.3 TB of FLAC", "<b>~$33/mo</b>"],
              ["Backups", "EBS snapshots, daily, kept 7 days", "~$10/mo"],
              ["Database", "<b>None to buy.</b> PostgreSQL runs on the server itself",
               "$0<br/>(RDS would be ~$25)"],
              ["Fixed address", "Elastic IP, free while attached to a running server", "$0"],
              ["Certificate", "Free, renews itself", "$0"],
              ["<b>Total</b>", "", "<b>~$110/mo</b>"]],
             [34 * mm, 92 * mm, 30 * mm]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Over the collection phase: about $260. Running permanently: about $110 a "
                   "month.</b>", BODY))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Three things to set up on AWS that DigitalOcean does for you:</b>", BODY))
S.append(Spacer(1, 3))
S.append(tbl([["", "What", "Why"],
              ["1", "A VPC endpoint for S3 (\"gateway endpoint\", free)",
               "Without it, traffic between your server and your own storage can leave the "
               "network and be billed. With it, that traffic is free. <b>This one setting can "
               "be the difference between $0 and $100 a month</b>"],
              ["2", "A billing alarm at, say, $150",
               "AWS will happily let a misconfiguration run up a bill. DigitalOcean's prices are "
               "mostly fixed; Amazon's are mostly per-use"],
              ["3", "S3 versioning on the audio bucket",
               "So a mistaken delete can be undone. Costs almost nothing at this size"]],
             [7 * mm, 54 * mm, 95 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>What AWS buys you for the extra $25 a month:</b> a managed database "
                   "later if you want one, automatic scaling if the project grows, more regions, "
                   "and the deep archive already sitting there if you ever do want it. "
                   "<b>What it costs you:</b> more moving parts, and a bill that can surprise "
                   "you.", BODY))

# ---------------------------------------------------------------- 7
S.append(Paragraph("7 &middot; Side by side", H2))
S.append(tbl([["", "DigitalOcean", "Amazon (Mumbai)"],
              ["Server", "8 GB / 4 vCPU / 160 GB &mdash; $48", "8 GB / 2 vCPU &mdash; $49"],
              ["Disk", "<b>included</b>", "separate, 200 GB &mdash; $18"],
              ["Audio storage, 1.3 TB", "$26", "$33"],
              ["Backups", "$9.60", "$10"],
              ["<b>Monthly</b>", "<b>~$85</b>", "<b>~$110</b>"],
              ["Distance from Dhaka", "Bangalore, ~50 ms", "Mumbai, ~40 ms"],
              ["Ease of setup", "<b>an afternoon</b>", "a couple of days"],
              ["Risk of a surprise bill", "<b>low &mdash; mostly fixed prices</b>",
               "real, unless you set the endpoint and the alarm"],
              ["Room to grow", "fine for this project", "effectively unlimited"]],
             [46 * mm, 55 * mm, 55 * mm]))
S.append(Spacer(1, 5))
S.append(Paragraph("<b>Take DigitalOcean.</b> It is $25 a month cheaper, the disk is included, "
                   "and it can be running this week. Nothing in this project needs what Amazon "
                   "does better. <b>Take Amazon instead if</b> UIU already has AWS credits or an "
                   "existing account with support, or if you expect to add machine-learning work "
                   "later that would sit beside the data.", GOOD))

S.append(PageBreak())

# ---------------------------------------------------------------- 8
S.append(Paragraph("8 &middot; What still has to be built", H2))
S.append(Paragraph("Neither plan works until the system can put audio in cloud storage. Today it "
                   "writes to the server's own disk and never removes it &mdash; which fills any "
                   "disk, on any provider.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["", "Work", "Why", "Size"],
              ["1", "<b>Upload the finished recording to cloud storage, check it arrived, then "
                    "delete it from the server</b>",
               "Without this the disk fills in a week and everything stops",
               "a few days"],
              ["2", "Keep the FLAC, drop the WAV",
               "Saves 40% of storage for identical audio", "half a day"],
              ["3", "Point the dashboard and the restore tool at cloud storage",
               "So a recording can still be found and fetched", "a day"],
              ["4", "Warn when the upload buffer starts filling",
               "The buffer only helps if somebody is told before it is full", "a day"]],
             [7 * mm, 56 * mm, 64 * mm, 29 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(Paragraph("About a week of work in total, and it must be finished and tested "
                   "<b>before</b> collection starts, not during it.", NOTE))

# ---------------------------------------------------------------- 9
S.append(Paragraph("9 &middot; One page to remember", H2))
S.append(tbl([["Question", "Answer"],
              ["What does \"4 vCPU / 8 GB / 160 GB\" mean?",
               "Four workers, a desk big enough for them, a 160 GB filing cabinet. One rented "
               "computer"],
              ["Why 4 workers?", "The work needs about 1.5. Four gives headroom for the busy "
                                 "hour. <b>The earlier figure of 8 was too much</b>"],
              ["Hard disk or cloud disk?", "Cloud. It comes with the rented server and is "
                                           "included in the price"],
              ["Why no extra disk?", "The audio lives in cloud storage, so the server only needs "
                                     "148 GB &mdash; and 160 GB comes included"],
              ["Do we still need FLAC?", "<b>Yes, but as the only copy.</b> It is the same audio "
                                         "stored 40% smaller, not a backup"],
              ["Do we still need Glacier?", "<b>No.</b> Keep everything in normal cloud storage "
                                            "where it opens instantly"],
              ["How much audio storage?", "1.3 TB for the 18,000-patient phase. 6.2 TB a year "
                                          "if it runs permanently"],
              ["Where do the databases live?", "On the server's own disk. About 10 GB"],
              ["DigitalOcean total", "<b>about $85 a month</b>, ~$210 for the collection phase"],
              ["Amazon total", "about $110 a month, ~$260 for the collection phase"],
              ["Which one?", "<b>DigitalOcean</b>, unless UIU already has an AWS account"],
              ["What must be built first", "Uploading recordings to cloud storage and deleting "
                                           "them locally. About a week"]],
             [56 * mm, 100 * mm]))
S.append(Spacer(1, 6))
S.append(Paragraph("Prepared by AIMS LAB. Prices are indicative, October 2026, and should be "
                   "confirmed on each provider's own pricing page before purchase.", SUB))


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7.3)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 9.5 * mm,
                      "AIMScribe v3 - the cloud plan - AIMS LAB, October 2026")
    canvas.drawRightString(A4[0] - 18 * mm, 9.5 * mm, "page %d" % doc.page)
    canvas.restoreState()


OUT = Path(__file__).resolve().parent / "CLOUD_HOSTING_PLAN.pdf"
SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                  topMargin=16 * mm, bottomMargin=20 * mm,
                  title="AIMScribe v3 - The Cloud Plan",
                  author="AIMS LAB").build(S, onFirstPage=footer, onLaterPages=footer)
print("wrote", OUT)
