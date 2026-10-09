#!/usr/bin/env python3
"""
Build DIGITALOCEAN_DEPLOYMENT_RUNBOOK.pdf - the end-to-end procedure for moving
the whole AIMScribe server to DigitalOcean, and the endpoint status table to
show CMED.

    python deploy/cloud/make_deployment_runbook.py [--out NAME.pdf]

Every port, path, service and variable is taken from the stack itself:
  deploy/uiu/docker-compose.yml   the nine services and the required variables
  deploy/uiu/Caddyfile            TLS, the only two open ports, the 2 MB cap
  deploy/uiu/CHANGING_THE_SERVER_URL.md   the four places the address lives
  backend/src/main_fastapi.py     /health and the front page
  backend/src/clinical.py         the two CMED endpoints
No secrets: every credential below is a placeholder or generated on the server.
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
S.append(Paragraph("Deploying the AIMScribe Server to DigitalOcean", H1))
S.append(Paragraph("End to end: the droplet, the stack, the endpoints, and what to tell CMED "
                   "&middot; AIMS LAB &middot; 9 October 2026", SUB))
S.append(HRFlowable(width="100%", thickness=1, color=RULE, spaceAfter=8))
S.append(Paragraph("Everything below is taken from the stack as it stands. The whole server "
                   "&mdash; nine services, both databases, the gateway, the archive worker "
                   "&mdash; moves to one DigitalOcean droplet. <b>The recorders and CMED's page "
                   "do not change in any way other than one address each.</b>", BODY))
S.append(Spacer(1, 5))
S.append(tbl([["", "Stage", "Time", "Who"],
              ["<b>A</b>", "<b>The endpoint status table</b> &mdash; what works before and after. "
                           "Give this to CMED", "read it now", "you &rarr; CMED"],
              ["<b>B</b>", "What to have in hand before starting", "15 min", "AIMS LAB"],
              ["<b>C</b>", "<b>The deployment, twelve steps</b>", "<b>2&ndash;3 hours</b>",
               "AIMS LAB"],
              ["<b>D</b>", "Proving every endpoint answers", "15 min", "AIMS LAB"],
              ["<b>E</b>", "Moving the recorders onto the new address", "30 min", "AIMS LAB"],
              ["<b>F</b>", "Backups, monitoring, rollback", "30 min", "AIMS LAB"]],
             [10 * mm, 90 * mm, 26 * mm, 30 * mm], highlight=[1, 3]))
S.append(Spacer(1, 5))
S.append(tbl([["The one thing to understand before starting"],
              ["<b>CMED's page never learns the server address.</b> It talks to the recorder on "
               "the same PC, over loopback. Only <b>CMED's own server</b> and <b>each recorder</b> "
               "hold the AIMS LAB address, which is why this deployment changes exactly two "
               "things on the outside &mdash; one line in CMED's environment, and one line in each "
               "recorder's configuration. Nothing is rebuilt and no code changes."]],
             [156 * mm], bold_first=False))

S.append(PageBreak())

# ============================================================ PART A
S.append(part("PART A", "The endpoint status table",
              "What works today, what works after deployment - the page to send CMED"))

S.append(Paragraph("1 &middot; Every endpoint, before and after", H2))
S.append(Paragraph("<b>This is the table to put in front of CMED.</b> \"Today\" means the system "
                   "as it runs now, on a PC on a private network. \"After deployment\" means once "
                   "Part C is done.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["", "Endpoint", "Today", "After deployment", "Why"],
              ["<b>1</b>", "<font face='Courier'>GET https://&lt;host&gt;/health</font>",
               "<b>NO</b>", "<b>YES</b>",
               "Needs a public name and a certificate. Both arrive with the droplet"],
              ["<b>2</b>", "<font face='Courier'>GET https://&lt;host&gt;/</font>",
               "<b>NO</b>", "<b>YES</b>", "same"],
              ["<b>3</b>", "<font face='Courier'>POST https://&lt;host&gt;/api/v2/clinical/"
                           "patient-information</font><br/><i>API 2</i>",
               "<b>NO</b>", "<b>YES</b>",
               "<b>This is the one CMED is waiting for.</b> The code is finished and tested; it "
               "has had no address to answer on"],
              ["<b>4</b>", "<font face='Courier'>POST https://&lt;host&gt;/api/v2/clinical/"
                           "prescription</font><br/><i>API 3, Channel B</i>",
               "<b>NO</b>", "<b>YES</b>", "same as 3"],
              ["<b>5</b>", "<font face='Courier'>ws://127.0.0.1:5050/ws</font> &mdash; "
                           "<font face='Courier'>start</font><br/><i>API 1</i>",
               "<b>YES</b>", "<b>YES</b>",
               "<b>Unaffected.</b> Loopback on the clinic PC &mdash; it never involved our server"],
              ["<b>6</b>", "<font face='Courier'>ws://127.0.0.1:5050/ws</font> &mdash; "
                           "<font face='Courier'>prescription_built</font><br/><i>API 3, "
                           "Channel A</i>", "<b>YES</b>", "<b>YES</b>", "Unaffected"],
              ["<b>7</b>", "<font face='Courier'>ws://127.0.0.1:5050/ws</font> &mdash; "
                           "<font face='Courier'>doctors</font>", "<b>YES</b>", "<b>YES</b>",
               "Unaffected"],
              ["<b>8</b>", "<font face='Courier'>ws://127.0.0.1:5050/ws</font> &mdash; "
                           "<font face='Courier'>status</font>", "<b>YES</b>", "<b>YES</b>",
               "Unaffected"]],
             [8 * mm, 56 * mm, 16 * mm, 24 * mm, 52 * mm], highlight=[5, 6, 7, 8], req=[1, 2, 3, 4]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Read the shape of that table.</b> The four local ones already work and are "
                   "untouched by the deployment. The four server ones are all blocked by the same "
                   "single cause &mdash; no public address &mdash; and all four are unblocked by "
                   "the same single act. There is no code to write on our side.", GOOD))

S.append(Paragraph("2 &middot; What to say to CMED, in writing", H2))
S.append(Paragraph("A note you can send as it stands:", BODY))
S.append(Spacer(1, 2))
S.append(Preformatted(
    'The four endpoints your page uses on the clinic PC work today and are not\n'
    'affected by our deployment. Your front-end team can build and test against\n'
    'them now - install the recorder on one PC and they are live:\n'
    '\n'
    '    ws://127.0.0.1:5050/ws   commands: start, prescription_built,\n'
    '                                       doctors, status\n'
    '\n'
    'The two endpoints your backend uses are finished and tested but have no\n'
    'public address yet, because our server is currently on a private network.\n'
    'They will answer as soon as our DigitalOcean deployment is done:\n'
    '\n'
    '    POST  https://<host>/api/v2/clinical/patient-information    (API 2)\n'
    '    POST  https://<host>/api/v2/clinical/prescription           (API 3b)\n'
    '    GET   https://<host>/health                 - no key, to check we are up\n'
    '\n'
    'Nothing about the message format, the reply codes, the key or the limits\n'
    'changes between now and then. Your backend can be written against the\n'
    'specification today and pointed at the host when we send it.\n'
    '\n'
    'Please hold the host as configuration, not a compiled-in constant: we will\n'
    'give you a staging host first and a production host later.', CODE))

S.append(PageBreak())

# ============================================================ PART B
S.append(part("PART B", "Before you start",
              "Fifteen minutes of preparation that saves an afternoon"))

S.append(Paragraph("3 &middot; What you need in hand", H2))
S.append(tbl([["", "What", "Where from", "Note"],
              ["&#9744;", "A DigitalOcean account with billing", "digitalocean.com",
               "about $115/month for droplet and backups"],
              ["&#9744;", "<b>A domain name you control</b>", "a UIU subdomain, or a registrar",
               "<b>Required.</b> A certificate cannot be issued for an IP address"],
              ["&#9744;", "A Cloudflare account with an <b>R2 bucket</b>", "cloudflare.com",
               "3 TB of audio. Create it before the server needs it"],
              ["&#9744;", "The R2 access key and secret", "Cloudflare &rarr; R2 &rarr; Manage API "
                                                          "tokens", "Shown once"],
              ["&#9744;", "An SSH key pair", "your own machine", "Password login will be turned off"],
              ["&#9744;", "The project code", "the AIMScribe_v3 repository", "cloned on the droplet"],
              ["&#9744;", "CMED's clinic codes", "CMED", "<b>needed at step 10</b>, not before"],
              ["&#9744;", "An email for the certificate", "yours", "Let's Encrypt expiry notices"]],
             [10 * mm, 48 * mm, 48 * mm, 48 * mm], req=[2]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>The domain is the only hard prerequisite.</b> Everything else can be done "
                   "as you go, but the gateway asks Let's Encrypt for a certificate the first time "
                   "it starts, and that requires a name already pointing at the droplet. Decide "
                   "the name before you begin.", BAD))

S.append(Paragraph("4 &middot; What will be running when you are done", H2))
S.append(tbl([["Service", "What it does", "Exposed?"],
              ["<b>gateway</b> (Caddy)", "HTTPS, the certificate, security headers, a 2 MB body cap",
               "<b>yes &mdash; 80 and 443</b>"],
              ["<b>api</b>", "every endpoint, on internal port 6000", "no"],
              ["postgres", "both databases", "no"],
              ["pgbouncer", "connection pooling", "no"],
              ["redis", "the work queue", "no"],
              ["worker", "background processing", "no"],
              ["<b>archive-worker</b>", "joins recordings, uploads to R2, verifies", "no"],
              ["backup", "nightly encrypted database dump", "no"],
              ["monitor", "watches disk, queue depth, unconfirmed recordings", "no"]],
             [34 * mm, 90 * mm, 32 * mm], highlight=[1]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Only the gateway is reachable from outside, on ports 80 and 443 only.</b> "
                   "Every other service talks on a private Docker network. The API itself is never "
                   "published &mdash; which is why there is no way to reach the recorder endpoints "
                   "or the dashboard except through the gateway.", GOOD))

S.append(PageBreak())

# ============================================================ PART C
S.append(part("PART C", "The deployment",
              "Twelve steps, two to three hours, done once"))

S.append(Paragraph("Step 1 &middot; Create the droplet", H2))
S.append(tbl([["Field", "Choose"],
              ["Region", "<b>Bangalore / BLR1</b> &mdash; lowest latency to Dhaka, 45&ndash;70 ms"],
              ["Image", "<b>Ubuntu 24.04 (LTS) x64</b>"],
              ["Type", "<b>Basic &rarr; Regular (SSD)</b>"],
              ["Size", "<b>8 vCPU / 16 GB / 320 GB &mdash; <font face='Courier'>s-8vcpu-16gb"
                       "</font> &mdash; $96/mo</b>"],
              ["Authentication", "<b>SSH key.</b> Not a password"],
              ["Backups", "<b>Enable</b> &mdash; +20%, about $19/mo"],
              ["Monitoring", "Enable &mdash; free"],
              ["Hostname", "<font face='Courier'>aimscribe-prod-blr1</font>"]],
             [34 * mm, 122 * mm], highlight=[4]))
S.append(Spacer(1, 3))
S.append(Paragraph("Note the public IPv4 address when it finishes.", BODY))

S.append(Paragraph("Step 2 &middot; Point the name at it", H2))
S.append(Preformatted(
    'A     aimscribe.uiu.ac.bd        -> <the droplet IPv4>      TTL 300\n'
    '\n'
    '# then, from your own machine, wait until this answers with that address:\n'
    'nslookup aimscribe.uiu.ac.bd', CODE))
S.append(Paragraph("<b>Do not go further until the name resolves.</b> Caddy will ask Let's Encrypt "
                   "for a certificate on first start, and if the name does not yet point here the "
                   "request fails and is rate-limited for a while.", BAD))

S.append(Paragraph("Step 3 &middot; The firewall", H2))
S.append(Paragraph("In the DigitalOcean control panel, Networking &rarr; Firewalls, create one and "
                   "attach it to the droplet:", BODY))
S.append(Spacer(1, 3))
S.append(tbl([["Direction", "Protocol", "Port", "Source / destination", "Why"],
              ["Inbound", "TCP", "<b>22</b>", "<b>your own IP only</b>", "administration"],
              ["Inbound", "TCP", "<b>80</b>", "all IPv4, all IPv6", "certificate issue and renewal"],
              ["Inbound", "TCP", "<b>443</b>", "all IPv4, all IPv6", "<b>the API and the gateway</b>"],
              ["Inbound", "UDP", "<b>443</b>", "all IPv4, all IPv6", "HTTP/3"],
              ["Outbound", "all", "all", "all", "R2 uploads, updates, certificates"]],
             [22 * mm, 20 * mm, 16 * mm, 46 * mm, 52 * mm], req=[1], highlight=[3]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Nothing else.</b> No database port, no Redis port, no API port. If you ever "
                   "find yourself opening 5432 or 6379 to the internet, stop &mdash; use an SSH "
                   "tunnel instead.", NOTE))

S.append(PageBreak())
S.append(Paragraph("Step 4 &middot; Harden the machine and install Docker", H2))
S.append(Preformatted(
    'ssh root@<droplet-ip>\n'
    '\n'
    '# a working user\n'
    'adduser --disabled-password --gecos "" aims\n'
    'usermod -aG sudo aims\n'
    'rsync --archive --chown=aims:aims ~/.ssh /home/aims\n'
    '\n'
    '# no root login, no passwords\n'
    'sed -i "s/^#*PermitRootLogin.*/PermitRootLogin no/" /etc/ssh/sshd_config\n'
    'sed -i "s/^#*PasswordAuthentication.*/PasswordAuthentication no/" \\\n'
    '    /etc/ssh/sshd_config\n'
    'systemctl restart ssh\n'
    '\n'
    '# updates, and automatic security patches\n'
    'apt update && apt upgrade -y\n'
    'apt install -y unattended-upgrades fail2ban git curl\n'
    'dpkg-reconfigure -plow unattended-upgrades\n'
    '\n'
    '# the clock - recordings are filed by the clinic\'s local date\n'
    'timedatectl set-timezone Asia/Dhaka\n'
    'timedatectl set-ntp true\n'
    '\n'
    '# Docker\n'
    'curl -fsSL https://get.docker.com | sh\n'
    'usermod -aG docker aims\n'
    'systemctl enable --now docker', CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>The timezone line is not cosmetic.</b> A recording is filed under the "
                   "clinic's local date, decided once when the session closes. A server on UTC "
                   "files the evening's consultations under tomorrow.", NOTE))

S.append(Paragraph("Step 5 &middot; Get the code", H2))
S.append(Preformatted(
    'exit                     # back out of root\n'
    'ssh aims@<droplet-ip>\n'
    '\n'
    'git clone https://github.com/SakibZaman021/AIMScribe_v3.git\n'
    'cd AIMScribe_v3/deploy/uiu\n'
    '\n'
    '# the folder is named uiu for historical reasons; it is the SERVER stack,\n'
    '# and it is the right one for this droplet. deploy/local is the dev stack.\n'
    'ls   # docker-compose.yml  Caddyfile  .env.example  postgres/  backup/ ...', CODE))

S.append(Paragraph("Step 6 &middot; The settings file", H2))
S.append(Paragraph("<b>Eleven values have no default and the stack refuses to start without "
                   "them</b>, plus <font face='Courier'>AIMS_ADMIN_KEY</font>, which you need in "
                   "step 10. <b>Generate the secrets on the server; do not invent them and do not "
                   "reuse any.</b>", BODY))
S.append(Spacer(1, 2))
S.append(Preformatted(
    'cp .env.example .env\n'
    'chmod 600 .env\n'
    '\n'
    '# five passwords and two service keys\n'
    'for v in POSTGRES_SUPERUSER_PASSWORD AIMS_RECORDINGS_PASSWORD \\\n'
    '         AIMS_CLINICAL_PASSWORD AIMS_MONITOR_PASSWORD REDIS_PASSWORD \\\n'
    '         AIMS_WORKER_KEY AIMS_ADMIN_KEY; do\n'
    '  echo "$v=$(openssl rand -base64 36 | tr -d \'/+=\' | head -c 40)" >> .env\n'
    'done\n'
    '\n'
    '# and the two encryption keys (32 bytes each)\n'
    'echo "AIMS_BACKUP_KEY=$(openssl rand -base64 32)" >> .env\n'
    'echo "AIMS_COPY_KEY=$(openssl rand -base64 32)"   >> .env', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["The eleven the stack enforces", "What it is"],
              ["<font face='Courier'>POSTGRES_SUPERUSER_PASSWORD</font>", "generated above"],
              ["<font face='Courier'>AIMS_RECORDINGS_PASSWORD</font>",
               "the <font face='Courier'>aims_recordings</font> database"],
              ["<font face='Courier'>AIMS_CLINICAL_PASSWORD</font>",
               "the <font face='Courier'>aims_clinical</font> database"],
              ["<font face='Courier'>AIMS_MONITOR_PASSWORD</font>", "read-only, for the monitor"],
              ["<font face='Courier'>REDIS_PASSWORD</font>", "the queue"],
              ["<font face='Courier'>AIMS_WORKER_KEY</font>", "the archive worker's own key"],
              ["<b><font face='Courier'>AIMS_PUBLIC_HOST</font></b>",
               "<b>the name from step 2</b> &mdash; the certificate is issued for it"],
              ["<b><font face='Courier'>AIMS_ACME_EMAIL</font></b>",
               "<b>yours</b> &mdash; Let's Encrypt expiry notices"],
              ["<font face='Courier'>AIMS_DB_PATH</font>", "<font face='Courier'>/srv/aimscribe/db"
                                                           "</font>"],
              ["<font face='Courier'>AIMS_ARCHIVE_PATH</font>",
               "<font face='Courier'>/srv/aimscribe/archive</font>"],
              ["<font face='Courier'>AIMS_BACKUP_PATH</font>",
               "<font face='Courier'>/srv/aimscribe/backup</font>"]],
             [62 * mm, 94 * mm], req=[7, 8]))
S.append(Spacer(1, 3))
S.append(Paragraph("Then edit <font face='Courier'>.env</font> and set the rest by hand:", BODY))
S.append(Spacer(1, 2))
S.append(Preformatted(
    'AIMS_PUBLIC_HOST=aimscribe.uiu.ac.bd        # the name from step 2\n'
    'AIMS_ACME_EMAIL=you@uiu.ac.bd               # certificate notices\n'
    'TZ=Asia/Dhaka\n'
    '\n'
    'AIMS_DB_PATH=/srv/aimscribe/db              # the databases\n'
    'AIMS_ARCHIVE_PATH=/srv/aimscribe/archive    # recordings in transit\n'
    'AIMS_BACKUP_PATH=/srv/aimscribe/backup      # nightly dumps\n'
    '\n'
    '# Cloudflare R2 - where the audio actually lives\n'
    'AIMS_COPY_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com\n'
    'AIMS_COPY_BUCKET=aimscribe-audio\n'
    'AIMS_COPY_ACCESS_KEY=<from Cloudflare>\n'
    'AIMS_COPY_SECRET_KEY=<from Cloudflare>', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["<b>Write AIMS_BACKUP_KEY and AIMS_COPY_KEY down somewhere safe, off this "
               "machine, now</b>"],
              ["These two keys encrypt the database backups and every audio file before it leaves "
               "for Cloudflare. <b>If the droplet is lost and these keys were only on it, the "
               "backups and the entire cloud archive are permanently unreadable.</b> Put them in "
               "the access sheet, and keep a copy somewhere that is not this server."]],
             [156 * mm], bold_first=False))

S.append(PageBreak())
S.append(Paragraph("Step 7 &middot; Create the folders and the R2 bucket", H2))
S.append(Preformatted(
    'sudo mkdir -p /srv/aimscribe/{db,archive,backup}\n'
    'sudo chown -R aims:aims /srv/aimscribe\n'
    'df -h /srv            # confirm the 320 GB is there', CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("In Cloudflare, R2 &rarr; Create bucket:", BODY))
S.append(Spacer(1, 2))
S.append(tbl([["Setting", "Value"],
              ["Name", "<font face='Courier'>aimscribe-audio</font>"],
              ["Location", "Asia-Pacific"],
              ["<b>Object versioning</b>", "<b>On</b> &mdash; so a mistaken delete can be undone"],
              ["Public access", "<b>Off.</b> Never public"]],
             [40 * mm, 116 * mm], highlight=[3]))

S.append(Paragraph("Step 8 &middot; Start it", H2))
S.append(Preformatted(
    'docker compose up -d\n'
    '\n'
    '# the databases, their roles and all eleven migrations run themselves\n'
    'docker compose logs -f api | head -60\n'
    '\n'
    '# all nine should be healthy or running\n'
    'docker compose ps', CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Give the gateway a minute.</b> On first start it asks Let's Encrypt for the "
                   "certificate; until that completes HTTPS will refuse connections. Watch it with "
                   "<font face='Courier'>docker compose logs -f gateway</font> &mdash; you are "
                   "looking for <font face='Courier'>certificate obtained successfully</font>.", NOTE))

S.append(Paragraph("Step 9 &middot; Prove it answers &mdash; from your own machine, not the server",
                   H2))
S.append(Preformatted(
    'curl -s https://aimscribe.uiu.ac.bd/health | jq .\n'
    '\n'
    '# expect:\n'
    '#   "status":   "healthy"\n'
    '#   "database": "connected"\n'
    '#   "redis":    "connected"\n'
    '#   "system":   "AIMScribe v3"      <-- check this one', CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Run it from somewhere else, not from the droplet.</b> Testing from the "
                   "server proves the program is running; it does not prove DNS, the firewall, the "
                   "certificate or the route work &mdash; which is the whole point of this step.", BAD))

S.append(Paragraph("Step 10 &middot; Register the clinics, the doctors and CMED's key", H2))
S.append(Paragraph("These are admin API calls, authenticated with the "
                   "<font face='Courier'>X-Admin-Key</font> you generated in step 6. Run them from "
                   "the droplet.", BODY))
S.append(Spacer(1, 2))
S.append(Preformatted(
    'A=$(grep ^AIMS_ADMIN_KEY= .env | cut -d= -f2)\n'
    'H=https://aimscribe.uiu.ac.bd/api/v2\n'
    '\n'
    '# one clinic. Note cmed_hospital_id - THIS is the mapping to CMED\'s own\n'
    '# code, and it is what stops a visit being orphaned (see below).\n'
    'curl -s -X POST $H/admin/hospital -H "X-Admin-Key: $A" \\\n'
    '  -H "Content-Type: application/json" \\\n'
    '  -d \'{ "hospital_id": "AALO_DHOLPUR",\n'
    '        "name": "Aalo Clinic, Dholpur",\n'
    '        "timezone": "Asia/Dhaka",\n'
    '        "cmed_hospital_id": "<CMED\'s code for this clinic>" }\'\n'
    '\n'
    '# repeat for AALO_KARAIL, AALO_MIRPUR, AALO_SHYAMPUR,\n'
    '#            AALO_NARAYANGANJ, AALO_ERSHADNAGAR, AMADER_SUSASTHO\n'
    '\n'
    '# the doctors at each, so CMED\'s selector can be filled from /doctors\n'
    'curl -s -X POST $H/admin/doctor -H "X-Admin-Key: $A" \\\n'
    '  -H "Content-Type: application/json" \\\n'
    '  -d \'{ "doctor_id": "DR0042", "hospital_id": "AALO_DHOLPUR",\n'
    '        "full_name": "Dr Rahman", "active": true }\'\n'
    '\n'
    '# CMED\'s Channel B key - PRINTED ONCE, NEVER RECOVERABLE\n'
    'curl -s -X POST $H/admin/cmed-key -H "X-Admin-Key: $A"', CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Capture that key the moment it prints</b> and send it to CMED over a "
                   "channel that is not email if you can. It cannot be retrieved afterwards &mdash; "
                   "only revoked and reissued, with "
                   "<font face='Courier'>POST /admin/cmed-key/revoke</font>.", BAD))
S.append(Spacer(1, 4))
S.append(tbl([["<b><font face='Courier'>cmed_hospital_id</font> is the blocking prerequisite, and "
               "this is where it goes</b>"],
              ["It is the field that maps <b>CMED's clinic code</b> to ours. Without it a visit "
               "CMED describes cannot be tied to the clinic it happened in: the record is stored, "
               "<b>CMED receives a normal 202</b>, and the recording is erased after twenty-four "
               "hours &mdash; silently, from their side.<br/><br/>"
               "<b>So get CMED's seven codes before this step.</b> If they are not available yet, "
               "register the clinics anyway and fill the field in later with the same call; it is "
               "an update, not a re-registration."]],
             [156 * mm], bold_first=False))

S.append(PageBreak())
S.append(Paragraph("Step 11 &middot; Enrol the clinic PCs against the new address", H2))
S.append(Paragraph("This is the step people underestimate. <b>A recorder is enrolled to one clinic "
                   "and one server address, and the enrolment is signed</b> &mdash; so pointing a "
                   "recorder at a new server is not simply an edit to a settings file.", BAD))
S.append(Spacer(1, 4))
S.append(tbl([["", "The four places the address lives", "Changes?"],
              ["<b>1</b>", "<b>The server:</b> <font face='Courier'>AIMS_PUBLIC_HOST</font> in "
                           "<font face='Courier'>deploy/uiu/.env</font>",
               "<b>yes</b> &mdash; step 6"],
              ["<b>2</b>", "The archive worker", "<b>no</b> &mdash; it reaches the API inside the "
                                                 "machine"],
              ["<b>3</b>", "<b>CMED:</b> <font face='Courier'>AIMS_SERVER_URL</font> in CMED's own "
                            "server environment", "<b>yes</b> &mdash; tell them"],
              ["<b>4</b>", "<b>Every recorder:</b> <font face='Courier'>AIMS_BACKEND_URL</font> in "
                            "the <font face='Courier'>.env</font> beside the exe",
               "<b>yes</b> &mdash; per PC"],
              ["&mdash;", "<b>CMED's web page</b>", "<b>no &mdash; it never knew the address</b>"]],
             [8 * mm, 108 * mm, 40 * mm], highlight=[5]))
S.append(Spacer(1, 4))
S.append(Paragraph("For each clinic PC: issue a fresh single-use enrolment token on the server, "
                   "then run the installer or the enrolment script on that PC with the new "
                   "address. <b>Do one PC first and record a full test consultation on it before "
                   "touching the other thirteen.</b>", BODY))
S.append(Spacer(1, 3))
S.append(Preformatted(
    '# The fleet is minted from a CSV - one row per LAPTOP, not per doctor.\n'
    '# It creates any hospital that does not exist, mints one single-use token\n'
    '# per machine, and writes a one-page instruction sheet for each.\n'
    '\n'
    '#   backend/scripts/laptops.csv\n'
    '#   hospital_id,hospital_name,room,doctor_id,doctor_name\n'
    '#   AALO_DHOLPUR,"Aalo Clinic, Dholpur",Room 3,,\n'
    '#   AALO_DHOLPUR,"Aalo Clinic, Dholpur",Room 4,,\n'
    '\n'
    'docker compose exec api python scripts/mint_enrolment_tokens.py \\\n'
    '    scripts/laptops.csv\n'
    '\n'
    '# then on each clinic PC, in the folder beside AIMScribe_Agent.exe:\n'
    '#   AIMS_BACKEND_URL=https://aimscribe.uiu.ac.bd\n'
    '# enrol with that machine\'s token, and restart the agent', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Two things about <font face='Courier'>laptops.csv</font>"],
              ["<b>Add rows, never rewrite the file.</b> The script creates any hospital it finds "
               "and the register is cumulative &mdash; a trimmed CSV reads as clinics that no "
               "longer exist.<br/>"
               "<b>One row per laptop, not per doctor.</b> A token binds a <i>machine</i> to a "
               "<i>clinic</i>, and that is all it decides. The doctor arrives with each "
               "consultation from CMED, so one laptop serves the morning and afternoon shifts "
               "untouched."]],
             [156 * mm], bold_first=False))

S.append(Paragraph("Step 12 &middot; Backups and the restore drill", H2))
S.append(Preformatted(
    '# the backup service already runs nightly. Prove it works TODAY:\n'
    'docker compose exec backup /backup/backup.sh\n'
    'ls -lh /srv/aimscribe/backup\n'
    '\n'
    '# then put it back into a scratch database and count the rows\n'
    'docker compose exec backup /backup/restore.sh --help\n'
    '\n'
    '# the full drill, both databases and a day of audio, is written up in\n'
    '#   deploy/uiu/README.md, section 7', CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>A backup nobody has restored is not a backup.</b> Do the restore drill on "
                   "day one, then once a month. This is the single most valuable half hour in the "
                   "whole deployment.", BAD))

S.append(PageBreak())

# ============================================================ PART D
S.append(part("PART D", "Proving every endpoint",
              "The checks to run after Part C, and to send CMED as evidence"))

S.append(Paragraph("5 &middot; The eight checks", H2))
S.append(tbl([["", "Check", "Command or action", "Pass"],
              ["<b>1</b>", "The name resolves", "<font face='Courier'>nslookup &lt;host&gt;</font>",
               "the droplet's IP"],
              ["<b>2</b>", "The certificate is real",
               "<font face='Courier'>curl -sI https://&lt;host&gt;/health</font>",
               "no TLS warning"],
              ["<b>3</b>", "<b>Health, from outside</b>",
               "<font face='Courier'>curl -s https://&lt;host&gt;/health</font>",
               "<b><font face='Courier'>status: healthy</font></b>"],
              ["<b>4</b>", "It is the right system", "the same reply",
               "<b><font face='Courier'>system: AIMScribe v3</font></b>"],
              ["<b>5</b>", "The key is enforced", "POST API 2 with no key", "<b>401</b>"],
              ["<b>6</b>", "<b>API 2 accepts</b>", "POST API 2 with the key", "<b>202</b>"],
              ["<b>7</b>", "Retries are safe", "POST the identical body again",
               "<b>202</b> ALREADY_RECEIVED"],
              ["<b>8</b>", "<b>API 3 Channel B accepts</b>", "POST the prescription", "<b>202</b>"]],
             [8 * mm, 42 * mm, 60 * mm, 46 * mm], highlight=[3, 6, 8]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Our script does checks 3 to 8 in one command:</b> "
                   "<font face='Courier'>python tools/channel_b_test.py --server "
                   "https://&lt;host&gt; --key &lt;key&gt;</font>. Run it, keep the output, and "
                   "send it to CMED &mdash; it is the evidence that the gateway is ready for them.",
                   GOOD))
S.append(Spacer(1, 4))
S.append(tbl([["And then the one check that actually matters"],
              ["Checks 1 to 8 prove the gateway works. <b>They do not prove the integration "
               "works.</b> Record one real consultation on one clinic PC, with CMED sending all "
               "four messages, and then <b>confirm on the dashboard that the recording was matched "
               "and archived</b>. Every check above can pass while the five identifying fields "
               "silently fail to match &mdash; in which case the recording is deleted twenty-four "
               "hours later and nobody notices."]],
             [156 * mm], bold_first=False))

S.append(Paragraph("6 &middot; Where things are on the droplet", H2))
S.append(tbl([["What", "Path", "Note"],
              ["The code and the stack", "<font face='Courier'>/home/aims/AIMScribe_v3/deploy/uiu"
                                         "</font>", "where you run docker compose"],
              ["<b>The settings and the keys</b>", "<font face='Courier'>&hellip;/deploy/uiu/.env"
                                                   "</font>", "<b>mode 600. Never commit it</b>"],
              ["The databases", "<font face='Courier'>/srv/aimscribe/db</font>", "about 10 GB, "
                                                                                "growing"],
              ["<b>Recordings in transit</b>", "<font face='Courier'>/srv/aimscribe/archive</font>",
               "<b>clinic / doctor / date / consultation folder</b>"],
              ["Nightly backups", "<font face='Courier'>/srv/aimscribe/backup</font>", "encrypted"],
              ["<b>The permanent audio</b>", "<b>Cloudflare R2, not this machine</b>",
               "<font face='Courier'>copies/clinic/doctor/date/</font>"]],
             [40 * mm, 62 * mm, 54 * mm], highlight=[4, 6]))

S.append(PageBreak())

# ============================================================ PART E
S.append(part("PART F", "Keeping it running, and going back",
              "What to watch, and what to do if the deployment has to be undone"))

S.append(Paragraph("7 &middot; What to watch", H2))
S.append(tbl([["What", "How", "Act when"],
              ["<b>The system is up</b>", "<font face='Courier'>/health</font>, every minute, from "
                                          "outside", "<b>not healthy twice in a row</b>"],
              ["Disk", "DigitalOcean monitoring, or <font face='Courier'>df -h</font>",
               "<b>above 80%</b>"],
              ["<b>Unconfirmed recordings</b>", "the dashboard, every morning",
               "<b>anything above zero</b>"],
              ["The upload buffer", "the monitor service", "above half full"],
              ["Backups", "that last night's file exists", "any night it does not"],
              ["The certificate", "renews itself, every 60 days", "only if /health goes unreachable"]],
             [44 * mm, 62 * mm, 50 * mm], highlight=[1, 3]))

S.append(Paragraph("8 &middot; Routine operations", H2))
S.append(Preformatted(
    'cd ~/AIMScribe_v3/deploy/uiu\n'
    '\n'
    'docker compose ps                      # what is running\n'
    'docker compose logs -f api             # follow the API\n'
    'docker compose logs --tail 200 gateway # the certificate, the requests\n'
    'docker compose restart api             # restart one service\n'
    '\n'
    '# update to a new release, outside clinic hours\n'
    'git pull && docker compose up -d --build\n'
    '\n'
    '# a consultation in progress survives an API restart: the recorder holds\n'
    '# its pieces and retries. Do it between patients anyway.', CODE))

S.append(Paragraph("9 &middot; If it has to be undone", H2))
S.append(tbl([["", "Situation", "What to do"],
              ["<b>1</b>", "The deployment fails before any clinic uses it",
               "<b>Nothing to undo.</b> Destroy the droplet. The old PC stack is untouched and "
               "still has every recording"],
              ["<b>2</b>", "It works, but a problem appears on day one",
               "Point the recorders' <font face='Courier'>AIMS_BACKEND_URL</font> back, and tell "
               "CMED to revert <font face='Courier'>AIMS_SERVER_URL</font>. <b>Keep the droplet "
               "running</b> &mdash; it holds recordings the old one does not"],
              ["<b>3</b>", "The droplet is lost entirely",
               "Rebuild from the DigitalOcean backup, or from scratch with this runbook plus the "
               "nightly database backup. <b>The audio is in R2 and is unaffected</b>"]],
             [8 * mm, 50 * mm, 98 * mm], req=[3]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Which is the real reason to put the audio in R2 rather than on the "
                   "droplet.</b> Losing the server costs you an afternoon of rebuilding. Losing "
                   "the server with 2 TB of consultations on its disk would cost the study.", GOOD))

S.append(Paragraph("10 &middot; One page to remember", H2))
S.append(tbl([["Question", "Answer"],
              ["Which compose file?", "<b><font face='Courier'>deploy/uiu/docker-compose.yml"
                                      "</font></b> &mdash; the server stack, despite the folder "
                                      "name"],
              ["Which droplet?", "<b><font face='Courier'>s-8vcpu-16gb</font>, Bangalore</b>, "
                                 "Ubuntu 24.04, backups on"],
              ["What must exist first?", "<b>A domain name pointing at the droplet.</b> The "
                                         "certificate depends on it"],
              ["Which ports open?", "<b>80 and 443 only</b>, plus SSH from your own IP"],
              ["How many settings have no default?", "<b>Eleven</b>, plus "
                                      "<font face='Courier'>AIMS_ADMIN_KEY</font> for step 10"],
              ["<b>Where do CMED's clinic codes go?</b>", "<b><font face='Courier'>cmed_hospital_id</font></b> on each "
                                                        "hospital registration. Step 10"],
              ["Which two secrets must leave the machine?", "<b><font face='Courier'>AIMS_BACKUP_KEY"
                                                            "</font> and <font face='Courier'>"
                                                            "AIMS_COPY_KEY</font></b> &mdash; "
                                                            "without them the backups and the whole "
                                                            "R2 archive are unreadable"],
              ["Where does the audio live?", "<b>Cloudflare R2</b>, as encrypted WAV. Not on the "
                                             "droplet"],
              ["What does CMED change?", "<b>One line</b> &mdash; "
                                         "<font face='Courier'>AIMS_SERVER_URL</font>. Their page "
                                         "changes nothing"],
              ["What does each recorder change?", "<b>One line</b> &mdash; "
                                                  "<font face='Courier'>AIMS_BACKEND_URL</font> "
                                                  "&mdash; plus a fresh enrolment token"],
              ["Which endpoints start working?", "<b>All four server ones.</b> The four local ones "
                                                 "already worked. Part A"],
              ["First thing after it is up?", "<font face='Courier'>curl https://&lt;host&gt;/health"
                                              "</font> <b>from somewhere else</b>"],
              ["Most valuable half hour?", "<b>The restore drill.</b> Step 12"]],
             [54 * mm, 102 * mm], highlight=[6, 12]))
S.append(Spacer(1, 6))
S.append(Paragraph("Prepared by AIMS LAB, United International University. Companions: the "
                   "Integration &amp; Test Guide for CMED DevOps, and the Complete Cloud Plan. "
                   "Every port, path, service and variable here is taken from the stack itself.",
                   SUB))


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7.3)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 9.5 * mm,
                      "AIMScribe v3 - DigitalOcean deployment runbook - AIMS LAB, October 2026")
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
