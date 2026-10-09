#!/usr/bin/env python3
"""
Build CMED_2_SERVER_DEPLOYMENT.pdf - for CMED's server and infrastructure team.

    python tools/make_cmed_server_doc.py [--out NAME.pdf]

The machine, the region, DNS, TLS, the ports, the nine services, the storage
architecture, health monitoring, and the endpoint status table. Nothing about
field formats - that is the integration team's document.

Sources, all read rather than remembered:
  deploy/uiu/docker-compose.yml   the nine services, the eleven required vars
  deploy/uiu/Caddyfile            TLS, the two open ports, the body cap
  deploy/uiu/CHANGING_THE_SERVER_URL.md   the four places the address lives
  backend/src/main_fastapi.py     /health and its fields
  backend/src/api_v2.py           copy_object_key - the R2 layout
No secrets: every credential below is a placeholder or generated on the server.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reportlab.lib.units import mm
from reportlab.platypus import Spacer, Paragraph, HRFlowable, PageBreak, Preformatted

import cmed_doc_style as st
from cmed_doc_style import (H1, SUB, H2, H3, BODY, NOTE, BAD, GOOD, CODE,
                            tbl, part, build)

S = []

# ============================================================ TITLE
S.append(Paragraph("AIMScribe v3 &mdash; Server Deployment", H1))
S.append(Paragraph("<b>Document 2 of 2 &mdash; for CMED's server and infrastructure team</b> "
                   "&middot; The machine, the gateway, the storage and the monitoring &middot; "
                   "AIMS LAB, United International University &middot; 9 October 2026", SUB))
S.append(HRFlowable(width="100%", thickness=1, color=st.RULE, spaceAfter=8))
S.append(tbl([["", "The two documents"],
              ["1. The other", "<b>Software integration</b> &mdash; the four APIs, the key, the "
                               "fields, the data model. <b>For the team writing the code</b>"],
              ["<b>2. This one</b>", "<b>Server deployment</b> &mdash; where the server runs, how "
                                     "it is exposed, how to tell it is healthy, where the audio "
                                     "lives, and what changes on CMED's side. <b>For the team "
                                     "working on infrastructure</b>"]],
             [26 * mm, 130 * mm], highlight=[2]))
S.append(Spacer(1, 5))
S.append(tbl([["What is being deployed, in one paragraph"],
              ["The whole AIMS LAB server &mdash; nine services, two databases, the HTTPS gateway "
               "and the archive worker &mdash; moves onto <b>one DigitalOcean droplet in "
               "Bangalore</b>. The recordings themselves are not kept on it: they are uploaded to "
               "<b>Cloudflare R2</b> and removed. <b>Nothing CMED runs needs to change except one "
               "configuration line</b>, and the clinic PCs need no firewall rule at all."]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 5))
S.append(tbl([["The three things this document exists to tell you"],
              ["<b>1.</b> Which endpoints work <b>today</b> and which only work <b>after</b> "
               "deployment &mdash; section 1, so nobody waits on the wrong thing."],
              ["<b>2.</b> <b>CMED changes exactly one line</b> when we go live: "
               "<font face='Courier'>AIMS_SERVER_URL</font> in your own server environment. Your "
               "web page changes nothing, because it never knew our address &mdash; section 10."],
              ["<b>3.</b> <b>The clinic PCs need no inbound port, no port forward and no firewall "
               "change</b>, on CMED's side or the clinic's &mdash; section 9."]],
             [156 * mm], bold_first=False))

S.append(H3 and Paragraph("Contents", H3))
S.append(tbl([["Part", "Covers", "Sections"],
              ["<b>A</b>", "<b>Endpoint status &mdash; before and after deployment</b>", "1&ndash;2"],
              ["<b>B</b>", "The machine, the region and the latency", "3&ndash;5"],
              ["<b>C</b>", "How it is exposed &mdash; DNS, TLS, ports, the services", "6&ndash;9"],
              ["<b>D</b>", "Where the recordings live", "10&ndash;11"],
              ["<b>E</b>", "What changes on CMED's side, and when", "12&ndash;13"],
              ["<b>F</b>", "Health, monitoring and what to do when it is down", "14&ndash;16"],
              ["<b>G</b>", "The deployment timeline, and one page to remember", "17&ndash;18"]],
             [14 * mm, 116 * mm, 26 * mm], highlight=[1]))

S.append(PageBreak())

# ============================================================ PART A
S.append(part("PART A", "Endpoint status",
              "What works today, what works after deployment"))

S.append(Paragraph("1 &middot; Every endpoint, before and after", H2))
S.append(Paragraph("\"Today\" means the system as it runs now, on a development machine on a "
                   "private network. \"After\" means once the droplet is live.", BODY))
S.append(Spacer(1, 4))
S.append(tbl([["", "Endpoint", "Today", "After", "Why"],
              ["<b>1</b>", "<font face='Courier'>GET https://&lt;host&gt;/health</font>",
               "<b>NO</b>", "<b>YES</b>",
               "Needs a public name and a certificate. Both arrive with the droplet"],
              ["<b>2</b>", "<font face='Courier'>GET https://&lt;host&gt;/</font>",
               "<b>NO</b>", "<b>YES</b>", "same"],
              ["<b>3</b>", "<font face='Courier'>POST &hellip;/api/v2/clinical/patient-information"
                           "</font><br/><i>API 2</i>", "<b>NO</b>", "<b>YES</b>",
               "<b>The code is finished and tested.</b> It has had no address to answer on"],
              ["<b>4</b>", "<font face='Courier'>POST &hellip;/api/v2/clinical/prescription</font>"
                           "<br/><i>API 3, Channel B</i>", "<b>NO</b>", "<b>YES</b>", "same as 3"],
              ["<b>5</b>", "<font face='Courier'>ws://127.0.0.1:5050/ws</font> &rarr; "
                           "<font face='Courier'>start</font><br/><i>API 1</i>",
               "<b>YES</b>", "<b>YES</b>",
               "<b>Unaffected.</b> Loopback on the clinic PC &mdash; our server was never involved"],
              ["<b>6</b>", "<font face='Courier'>ws://&hellip;</font> &rarr; "
                           "<font face='Courier'>prescription_built</font>",
               "<b>YES</b>", "<b>YES</b>", "unaffected"],
              ["<b>7</b>", "<font face='Courier'>ws://&hellip;</font> &rarr; "
                           "<font face='Courier'>doctors</font>", "<b>YES</b>", "<b>YES</b>",
               "unaffected"],
              ["<b>8</b>", "<font face='Courier'>ws://&hellip;</font> &rarr; "
                           "<font face='Courier'>status</font>", "<b>YES</b>", "<b>YES</b>",
               "unaffected"]],
             [8 * mm, 56 * mm, 16 * mm, 16 * mm, 60 * mm], highlight=[5, 6, 7, 8], req=[1, 2, 3, 4]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Read the shape of that table.</b> The four local ones already work and the "
                   "deployment does not touch them. The four server ones are blocked by <b>one "
                   "cause</b> &mdash; no public address &mdash; and unblocked by <b>one act</b>. "
                   "There is no code to write on either side.", GOOD))

S.append(Paragraph("2 &middot; So what should each CMED team do now?", H2))
S.append(tbl([["Team", "Can start", "Blocked until"],
              ["<b>Front-end</b> (the page)", "<b>today, on everything</b> &mdash; endpoints "
                                              "5&ndash;8 are live on any PC with the recorder "
                                              "installed", "<b>nothing</b>"],
              ["<b>Back-end</b> (the two POSTs)", "<b>today, writing against document 1</b>",
               "<b>testing</b> needs a hostname from us"],
              ["<b>Infrastructure</b>", "reading this document, and preparing the one config line "
                                        "in section 12", "nothing"]],
             [40 * mm, 72 * mm, 44 * mm], highlight=[1]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>The browser half is the one with the awkward detail</b> &mdash; the gate, "
                   "the session id, the provisional reply, reading the clinic code off the laptop "
                   "&mdash; and it is completely unblocked. <b>Nobody on CMED's side needs to wait "
                   "for our deployment to begin work.</b>", GOOD))
S.append(Spacer(1, 4))
S.append(tbl([["If CMED's backend team needs to test sooner than our droplet is ready"],
              ["We can publish a <b>Cloudflare Tunnel</b> from the development machine in about "
               "fifteen minutes. It gives a real public HTTPS hostname, needs no inbound firewall "
               "rule, and costs nothing.<br/><br/>"
               "<b>Your code is byte-for-byte identical against the tunnel and against the final "
               "droplet</b> &mdash; every endpoint, reply code, key and limit is the same. Only "
               "the hostname differs, which is why document 1 asks you to hold it in "
               "configuration. <b>Ask us and we will stand one up.</b>"]],
             [156 * mm], bold_first=False))

S.append(PageBreak())

# ============================================================ PART B
S.append(part("PART B", "The machine",
              "What we are renting, where, and how fast it answers"))

S.append(Paragraph("3 &middot; The droplet", H2))
S.append(tbl([["", "Specification", "Note"],
              ["Provider and region", "<b>DigitalOcean, Bangalore (BLR1)</b>",
               "the lowest-latency region available to Dhaka"],
              ["Droplet", "<b>Basic / Regular &mdash; <font face='Courier'>s-8vcpu-16gb</font></b>",
               "the smallest standard size meeting every requirement"],
              ["Processors", "<b>8 vCPU</b>", "the measured load needs about 1.5; the rest is for "
                                              "the busy hour"],
              ["Memory", "<b>16 GB</b>", "measured use is about 1 GB, capped at 3.4 GB"],
              ["Disk", "<b>320 GB SSD, included</b>", "<b>working space only</b> &mdash; see part D"],
              ["Outbound transfer", "<b>6 TB/month included</b>",
               "<b>so uploads to Cloudflare R2 cost nothing</b>"],
              ["Operating system", "Ubuntu Server 24.04 LTS", "supported to 2029"],
              ["Backups", "DigitalOcean droplet backups, enabled", "+20%"],
              ["Price", "<b>$96/month + $19 backups</b>", "plus $45 for storage &mdash; part D"]],
             [34 * mm, 58 * mm, 64 * mm], highlight=[6]))

S.append(Paragraph("4 &middot; Why Bangalore", H2))
S.append(tbl([["Region", "From Dhaka", "Round trip", "Verdict"],
              ["<b>Bangalore &mdash; BLR1</b>", "~2,400 km", "<b>45&ndash;70 ms</b>",
               "<b>Chosen</b>"],
              ["Singapore &mdash; SGP1", "~3,000 km", "60&ndash;95 ms", "second choice"],
              ["Frankfurt &mdash; FRA1", "~7,400 km", "130&ndash;170 ms", "too far"],
              ["New York &mdash; NYC3", "~12,900 km", "230&ndash;280 ms", "too far"]],
             [44 * mm, 32 * mm, 38 * mm, 36 * mm], highlight=[1]))
S.append(Spacer(1, 4))
S.append(tbl([["<b>Latency does not affect recording, and this is worth understanding</b>"],
              ["The microphone is driven by the program on the clinic PC, over loopback. <b>It "
               "starts in under a millisecond regardless of where our server is, or whether it is "
               "reachable at all.</b><br/><br/>"
               "The round trip to Bangalore only affects how quickly your two backend messages are "
               "acknowledged &mdash; and those should be queued on your side anyway, so a slow "
               "reply is never visible to a doctor. <b>If our server is down for an hour, every "
               "clinic keeps recording</b>; each laptop holds about thirteen hours."]],
             [156 * mm], bold_first=False))

S.append(Paragraph("5 &middot; What the load actually is", H2))
S.append(tbl([["Measure", "Value", "Note"],
              ["Clinics", "7", "fourteen consulting rooms in total"],
              ["Consultations per clinic day", "277", "across all rooms"],
              ["Requests per second, all rooms", "<b>about 1.2</b>", "measured. This is very light"],
              ["<b>Messages per second from CMED, at peak</b>", "<b>about 0.5</b>",
               "<b>no rate limit applies to CMED</b>"],
              ["Audio uploaded per clinic day", "32.5 GB", "277 recordings at ~120 MB"],
              ["Audio uploaded per month", "<b>844 GB</b>",
               "<b>inside the droplet's 6 TB allowance, so free</b>"]],
             [56 * mm, 32 * mm, 68 * mm], highlight=[6]))

S.append(PageBreak())

# ============================================================ PART C
S.append(part("PART C", "How it is exposed",
              "DNS, the certificate, the ports, and the nine services"))

S.append(Paragraph("6 &middot; The name and the certificate", H2))
S.append(tbl([["", "Detail"],
              ["The address", "a DNS <font face='Courier'>A</font> record pointing at the droplet "
                              "&mdash; for example <font face='Courier'>aimscribe.uiu.ac.bd</font>"],
              ["<b>Certificate</b>", "<b>Let's Encrypt, issued and renewed automatically</b> by "
                                     "the gateway. No manual step, no expiry to diarise"],
              ["TLS", "<b>1.2 and 1.3 only.</b> Older versions are refused"],
              ["HTTP", "port 80 redirects to HTTPS. It exists only for certificate renewal"],
              ["HTTP/3", "supported, on UDP 443"],
              ["<b>An IP address will not work</b>", "<b>A certificate cannot be issued for an IP.</b> "
                                                     "The name is a hard prerequisite"]],
             [40 * mm, 116 * mm], highlight=[2], req=[6]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Please always use the hostname, never the IP address.</b> Calling the IP "
                   "directly will fail the certificate check, and the droplet's address can change "
                   "if it is ever rebuilt.", NOTE))

S.append(Paragraph("7 &middot; The security headers on every reply", H2))
S.append(Preformatted(
    'Strict-Transport-Security   max-age=31536000; includeSubDomains\n'
    'X-Content-Type-Options     nosniff\n'
    'X-Frame-Options            DENY\n'
    'Referrer-Policy            no-referrer\n'
    'Content-Security-Policy    default-src \'none\'; frame-ancestors \'none\'\n'
    'Server                     (removed)\n'
    '\n'
    'Request body cap at the gateway: 2 MB\n'
    'Application body cap for the clinical endpoints: 1 MB', CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Note the two body caps.</b> The gateway refuses anything over 2 MB outright; "
                   "the application refuses over 1 MB with a readable "
                   "<font face='Courier'>413 TOO_LARGE</font>. A normal message is a few kilobytes, "
                   "so hitting either usually means a file or an image has got into the JSON.", BODY))

S.append(Paragraph("8 &middot; The only two open ports", H2))
S.append(tbl([["Direction", "Protocol", "Port", "Source", "Why"],
              ["Inbound", "TCP", "<b>443</b>", "all", "<b>the API &mdash; this is the one that "
                                                      "matters</b>"],
              ["Inbound", "UDP", "<b>443</b>", "all", "HTTP/3"],
              ["Inbound", "TCP", "<b>80</b>", "all", "certificate issue and renewal only"],
              ["Inbound", "TCP", "22", "<b>AIMS LAB admin IPs only</b>", "administration"],
              ["Outbound", "all", "all", "&mdash;", "R2 uploads, OS updates, certificates"]],
             [22 * mm, 20 * mm, 16 * mm, 46 * mm, 52 * mm], highlight=[1], req=[4]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>No database port, no queue port, no API port is ever exposed.</b> "
                   "PostgreSQL, Redis and the API itself are reachable only on a private Docker "
                   "network inside the machine. Administrative access to the databases is by SSH "
                   "tunnel, never by an open port.", GOOD))
S.append(Spacer(1, 4))
S.append(tbl([["Does CMED need to allow anything outbound?"],
              ["Only ordinary HTTPS on port 443 to our hostname, from <b>CMED's server</b>. If "
               "your egress is restricted by destination, that is the one entry to add. "
               "<b>Nothing needs to be opened inbound to CMED</b>, because we never call you "
               "&mdash; the traffic is one-directional, CMED to AIMS LAB."]],
             [156 * mm], bold_first=False))

S.append(PageBreak())
S.append(Paragraph("9 &middot; The nine services, and the clinic PCs", H2))
S.append(tbl([["Service", "What it does", "Exposed?"],
              ["<b>gateway</b> (Caddy)", "HTTPS, the certificate, the security headers, the 2 MB cap",
               "<b>yes &mdash; 80 and 443</b>"],
              ["<b>api</b>", "every endpoint, on internal port 6000", "no"],
              ["postgres", "both databases", "no"],
              ["pgbouncer", "connection pooling", "no"],
              ["redis", "the work queue", "no"],
              ["worker", "background processing", "no"],
              ["<b>archive-worker</b>", "joins each recording, uploads it to R2, verifies the copy",
               "no"],
              ["backup", "nightly encrypted database dump", "no"],
              ["monitor", "watches disk, queue depth and unconfirmed recordings", "no"]],
             [34 * mm, 90 * mm, 32 * mm], highlight=[1]))
S.append(Spacer(1, 4))
S.append(H3 and Paragraph("9.1 &nbsp; The clinic PCs need nothing from either infrastructure team",
                          H3))
S.append(tbl([["", "Detail"],
              ["What runs there", "<b>AIMScribe.exe</b>, one per consulting room, installed by "
                                  "AIMS LAB"],
              ["Listens on", "<font face='Courier'>127.0.0.1:5050</font> &mdash; <b>loopback only</b>"],
              ["Reachable from", "<b>that PC alone.</b> Not the LAN, not the internet, by design"],
              ["<b>Firewall changes needed</b>", "<b>none</b> &mdash; on CMED's side or the "
                                                 "clinic's"],
              ["Port forwarding", "<b>none</b>"],
              ["Authentication", "<b>no key.</b> The recorder checks your page's registered web "
                                 "address instead"],
              ["Outbound from the clinic", "HTTPS 443 to our hostname, for uploading recordings"]],
             [40 * mm, 116 * mm], highlight=[4, 5]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Why no key on the local connection:</b> a secret placed in a web page is "
                   "readable by anyone who opens the page, so it would protect nothing. The "
                   "recorder instead accepts connections only from web addresses AIMS LAB has "
                   "registered, only on loopback, and only from the same machine. <b>That is why "
                   "CMED's exact page addresses are a blocking prerequisite</b> &mdash; they are "
                   "the actual access control.", GOOD))

S.append(PageBreak())

# ============================================================ PART D
S.append(part("PART D", "Where the recordings live",
              "The droplet disk is working space - the audio is in object storage"))

S.append(Paragraph("10 &middot; Two storages, both needed", H2))
S.append(Paragraph("<b>The droplet's 320 GB is not the audio archive and is nowhere near enough to "
                   "be one.</b> Every recording is uploaded to Cloudflare R2 and then removed from "
                   "the server.", BAD))
S.append(Spacer(1, 4))
S.append(tbl([["", "<b>Droplet disk &mdash; 320 GB</b>", "<b>Cloudflare R2 &mdash; 3 TB</b>"],
              ["Holds", "the OS, the nine services, both databases, logs, and recordings <b>in "
                        "transit</b>",
               "<b>every recording, permanently, as the original WAV</b>"],
              ["How long", "hours &mdash; a file is removed once the cloud copy is verified",
               "<b>permanently.</b> This is the research dataset"],
              ["Capacity", "about a day and a half of recording, as a buffer",
               "<b>26,200 consultations.</b> The 18,000-patient study uses 2.06 TB"],
              ["Cost", "included in the droplet", "<b>$45/month</b>"],
              ["If it fills", "archiving stalls; <b>clinics keep recording locally</b>",
               "buy more &mdash; $15 per TB per month, no limit"]],
             [24 * mm, 64 * mm, 68 * mm], highlight=[1]))
S.append(Spacer(1, 4))
S.append(tbl([["Total monthly cost of the deployment", "Monthly", "Yearly"],
              ["Droplet &mdash; 8 vCPU / 16 GB / 320 GB, Bangalore", "$96", "$1,152"],
              ["Droplet backups", "$19", "$230"],
              ["<b>Cloudflare R2 &mdash; 3 TB</b>", "<b>$45</b>", "<b>$540</b>"],
              ["Transfer to R2 (844 GB/mo, inside the 6 TB allowance)", "$0", "$0"],
              ["<b>Total</b>", "<b>$160</b>", "<b>$1,922</b>"]],
             [100 * mm, 28 * mm, 28 * mm], highlight=[3, 5]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>R2 charges nothing to read data back</b>, which is why it was chosen over "
                   "S3 or Spaces: a research dataset is downloaded many times, and on Amazon each "
                   "full read of 3 TB would cost roughly $250.", GOOD))

S.append(Paragraph("11 &middot; The layout, and what Cloudflare can see", H2))
S.append(Preformatted(
    'ON THE DROPLET, in transit\n'
    '  /srv/aimscribe/archive/catalogue.sqlite3    an index of every session\n'
    '  /srv/aimscribe/archive/AALO_DHOLPUR/DR0042/2026-10-09/<stem>/\n'
    '      <stem>.wav             the audio\n'
    '      <stem>.json            the clinical record CMED sent\n'
    '      <stem>.manifest.json   proof the audio is intact\n'
    '      _index.json            what is in this folder\n'
    '\n'
    'IN CLOUDFLARE R2, permanently\n'
    '  copies/AALO_DHOLPUR/DR0042/2026-10-09/<stem>.v1.wav.enc\n'
    '  copies/AALO_DHOLPUR/DR0042/2026-10-09/<stem>.v1.json.enc\n'
    '\n'
    '  <stem> = P0012345_DR0042_AALO_DHOLPUR_101432_102755_20261009\n'
    '           patient _ doctor _ clinic _ start _ end _ date', CODE))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>The cloud layout mirrors the server layout deliberately</b> &mdash; clinic, "
                   "then doctor, then date &mdash; so a recording can be found, or a whole clinic "
                   "or day restored, without consulting any database.", GOOD))
S.append(Spacer(1, 4))
S.append(tbl([["<b>Every file is encrypted before it leaves our server</b>"],
              ["AES-GCM, and <b>the key never leaves AIMS LAB</b>. Cloudflare can see the folder "
               "names and the file names; <b>it cannot read the audio or the clinical record</b>. "
               "Cloudflare stores bytes it cannot interpret, and so would any future provider "
               "&mdash; which is what makes putting patient recordings on commodity object storage "
               "acceptable at all.<br/><br/>"
               "<b>Nothing in R2 is publicly accessible.</b> There is no public bucket, no signed "
               "public URL, and no path by which audio reaches CMED or anyone else."]],
             [156 * mm], bold_first=False))

S.append(PageBreak())

# ============================================================ PART E
S.append(part("PART E", "What changes on CMED's side",
              "One line, and the timing of it"))

S.append(Paragraph("12 &middot; The four places the address lives", H2))
S.append(tbl([["", "Who", "Setting", "Changes?"],
              ["<b>1</b>", "Our server", "<font face='Courier'>AIMS_PUBLIC_HOST</font>",
               "<b>yes</b> &mdash; ours to do"],
              ["<b>2</b>", "Our archive worker", "nothing &mdash; it reaches the API inside the "
                                                 "machine", "<b>no</b>"],
              ["<b>3</b>", "<b>CMED's server</b>", "<b><font face='Courier'>AIMS_SERVER_URL</font>"
                                                   "</b>", "<b>YES &mdash; this is your one line</b>"],
              ["<b>4</b>", "Every clinic recorder", "<font face='Courier'>AIMS_BACKEND_URL</font>",
               "<b>yes</b> &mdash; ours to do, per PC"],
              ["&mdash;", "<b>CMED's web page</b>", "<b>nothing &mdash; it never knew our address</b>",
               "<b>no</b>"]],
             [8 * mm, 40 * mm, 68 * mm, 40 * mm], req=[3], highlight=[5]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>That is the whole of CMED's change: one environment variable on your "
                   "server.</b> Your page is unaffected because it talks to the recorder on "
                   "<font face='Courier'>localhost</font> and to your own backend &mdash; the AIMS "
                   "LAB address lives only in your backend's configuration, which is exactly where "
                   "it should be.", GOOD))

S.append(Paragraph("13 &middot; The sequence, so nothing is sent into a void", H2))
S.append(tbl([["", "Step", "Who", "Note"],
              ["<b>1</b>", "We deploy the droplet and verify it from outside", "AIMS LAB",
               "nothing of CMED's is pointed at it yet"],
              ["<b>2</b>", "<b>We send you the hostname and the key</b>", "AIMS LAB",
               "staging first, production later"],
              ["<b>3</b>", "<b>You set <font face='Courier'>AIMS_SERVER_URL</font> to staging and "
                           "run the test script</b>", "<b>CMED</b>",
               "section 15. Send us the output"],
              ["<b>4</b>", "We move the clinic recorders onto the new address, one first",
               "AIMS LAB", "a test consultation on that PC before the other thirteen"],
              ["<b>5</b>", "<b>Joint end-to-end test</b>, and we confirm the recording matched and "
                           "archived", "both", "<b>the only check that proves the integration</b>"],
              ["<b>6</b>", "You switch <font face='Courier'>AIMS_SERVER_URL</font> to production",
               "CMED", "go live"]],
             [8 * mm, 72 * mm, 24 * mm, 52 * mm], highlight=[3], req=[5]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Messages sent before step 2 are not lost on your side if you queue them</b> "
                   "&mdash; which document 1 asks you to do anyway. But there is no point sending "
                   "them: nothing will answer. Keep "
                   "<font face='Courier'>AIMS_SERVER_URL</font> unset or pointed at your own stub "
                   "until we send you the hostname.", NOTE))

S.append(PageBreak())

# ============================================================ PART F
S.append(part("PART F", "Health and monitoring",
              "How to tell it is up, and what to do when it is not"))

S.append(Paragraph("14 &middot; The health endpoint", H2))
S.append(Paragraph("<b>This is the endpoint to monitor.</b> No key, no body, no side effects. It "
                   "checks the database and the queue on every call.", BODY))
S.append(Spacer(1, 2))
S.append(Preformatted(
    'curl -s https://<aims-host>/health\n'
    '\n'
    '200 {\n'
    '      "status":   "healthy",\n'
    '      "database": "connected",\n'
    '      "redis":    "connected",\n'
    '      "minio":    "connected",\n'
    '      "system":   "AIMScribe v3",      <-- ASSERT ON THIS\n'
    '      "srs":      "3.x",\n'
    '      "version":  "3.x.x",\n'
    '      "mode":     "FastAPI Async"\n'
    '    }', CODE))
S.append(Spacer(1, 3))
S.append(tbl([["Field", "Watch for", "Meaning"],
              ["<b>status</b>", "<font face='Courier'>healthy</font>",
               "<b>everything is up.</b> Anything else is "
               "<font face='Courier'>degraded</font>"],
              ["database", "<font face='Courier'>connected</font>",
               "if disconnected, nothing can be stored. <b>Alert on this</b>"],
              ["redis", "<font face='Courier'>connected</font>",
               "the queue. If disconnected, archiving stalls"],
              ["<b>system</b>", "<b><font face='Courier'>AIMScribe v3</font></b>",
               "<b>see the warning below</b>"],
              ["version", "changes after a deploy", "useful to confirm a release landed"]],
             [26 * mm, 46 * mm, 84 * mm], highlight=[1], req=[4]))
S.append(Spacer(1, 4))
S.append(tbl([["<b>Please assert on the <font face='Courier'>system</font> field, not just the "
               "status code</b>"],
              ["There is an older <b>version 1</b> AIMScribe backend still running elsewhere, and "
               "it answers a <font face='Courier'>/health</font> path that looks almost identical. "
               "<b>Pointing at the wrong one costs an afternoon</b>, because every request succeeds "
               "and nothing is ever recorded. The <font face='Courier'>system</font> field is first "
               "in the response for exactly this reason."]],
             [156 * mm], bold_first=False))
S.append(Spacer(1, 4))
S.append(tbl([["Monitoring guidance", "Recommendation"],
              ["How often", "every 30&ndash;60 seconds is ample"],
              ["Alert when", "<font face='Courier'>status != \"healthy\"</font> twice in a row, or "
                             "no answer for 2 minutes"],
              ["Timeout", "5 seconds"],
              ["<b>Do not</b>", "<b>do not alert the clinics.</b> A doctor cannot act on it, and "
                                "recording continues regardless"]],
             [40 * mm, 116 * mm], req=[4]))

S.append(Paragraph("15 &middot; Proving the gateway, once it is up", H2))
S.append(tbl([["", "Check", "Expected", "Proves"],
              ["<b>1</b>", "<font face='Courier'>nslookup &lt;host&gt;</font>", "the droplet's IP",
               "DNS"],
              ["<b>2</b>", "<font face='Courier'>curl -sI https://&lt;host&gt;/health</font>",
               "no TLS warning", "the certificate"],
              ["<b>3</b>", "<b><font face='Courier'>GET /health</font></b>",
               "<b><font face='Courier'>status: healthy</font></b>",
               "<b>reachable from CMED's network</b>"],
              ["<b>4</b>", "the same reply", "<font face='Courier'>system: AIMScribe v3</font>",
               "the right system"],
              ["<b>5</b>", "POST API 2 with no key", "<b>401</b>", "auth is enforced"],
              ["<b>6</b>", "POST API 2 with the key", "<b>202</b>", "<b>the key works</b>"],
              ["<b>7</b>", "POST the identical body again", "<b>202</b> ALREADY_RECEIVED",
               "<b>retries are safe</b>"],
              ["<b>8</b>", "POST a deliberately invalid body", "<b>422</b>, naming the bad fields",
               "validation works, and a bad record is <b>quarantined, not lost</b>"],
              ["<b>9</b>", "POST a body over 1 MB", "<b>413</b>", "the limit is real"]],
             [8 * mm, 54 * mm, 46 * mm, 44 * mm], highlight=[3, 6, 7]))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>Our script does checks 3 to 8 in one command</b> &mdash; "
                   "<font face='Courier'>python tools/channel_b_test.py --server https://&lt;host&gt; "
                   "--key &lt;key&gt;</font>. We will send it with the key. <b>Run it and send us "
                   "the output</b> &mdash; it is the evidence both sides need that the gateway is "
                   "ready.", GOOD))
S.append(Spacer(1, 3))
S.append(Paragraph("<b>And then the one check that actually matters.</b> Checks 1 to 8 prove the "
                   "gateway works; <b>they do not prove the integration works</b>. A 202 means "
                   "stored, not matched. Record one real consultation with all four messages, then "
                   "<b>ask us to confirm it was matched and archived</b>.", BAD))

S.append(PageBreak())
S.append(Paragraph("16 &middot; When it is down", H2))
S.append(tbl([["How long", "What happens in the clinics", "What CMED should do"],
              ["minutes", "<b>Nothing.</b> Recording continues; laptops hold their pieces and "
                          "retry", "<b>Nothing.</b> Your queue retries"],
              ["an hour", "<b>Still nothing.</b> Each laptop holds about thirteen hours",
               "Nothing. Keep queueing"],
              ["most of a day", "Laptops begin to fill; clinics eventually stop",
               "Keep queueing. <b>Do not drop messages</b>"],
              ["<b>a message is never sent</b>", "<b>The recording is uploaded but unconfirmed, "
                                                 "and deleted after 24 hours</b>",
               "<b>This is the only real failure.</b> Queue durably and retry with backoff"]],
             [30 * mm, 66 * mm, 60 * mm], req=[4]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Notice what never happens: our server being down does not stop a doctor and "
                   "does not cut a recording.</b> The system is built so the clinic keeps working "
                   "whatever we are doing. A message that arrives an hour late is completely fine.",
                   GOOD))
S.append(Spacer(1, 4))
S.append(tbl([["What AIMS LAB watches, so you know what we will notice"],
              ["<b>Every minute:</b> <font face='Courier'>/health</font>, from outside the droplet."
               "<br/>"
               "<b>Every morning:</b> the count of unconfirmed recordings &mdash; <b>it should be "
               "zero</b>; anything above it means a message did not match.<br/>"
               "<b>Continuously:</b> free disk, queue depth, and the upload buffer.<br/>"
               "<b>Nightly:</b> an encrypted database backup, with a restore drill monthly."]],
             [156 * mm], bold_first=False))

S.append(PageBreak())

# ============================================================ PART G
S.append(part("PART G", "Timeline and summary",
              "What happens when, and the page to keep"))

S.append(Paragraph("17 &middot; The deployment timeline", H2))
S.append(tbl([["", "What", "Who", "How long", "Blocks CMED?"],
              ["<b>1</b>", "Droplet created, DNS pointed, firewall attached", "AIMS LAB", "1 hour",
               "no"],
              ["<b>2</b>", "Machine hardened, Docker installed, stack started", "AIMS LAB",
               "1 hour", "no"],
              ["<b>3</b>", "R2 bucket created, settings filled in, services verified", "AIMS LAB",
               "1 hour", "no"],
              ["<b>4</b>", "<b>Clinics and doctors registered, CMED key issued</b>", "AIMS LAB",
               "30 min", "<b>needs CMED's clinic codes</b>"],
              ["<b>5</b>", "<b>Hostname and key sent to CMED</b>", "AIMS LAB", "&mdash;",
               "<b>unblocks your backend testing</b>"],
              ["<b>6</b>", "CMED points <font face='Courier'>AIMS_SERVER_URL</font> at staging and "
                           "runs the script", "<b>CMED</b>", "30 min", "&mdash;"],
              ["<b>7</b>", "Recorders moved onto the new address, one clinic first", "AIMS LAB",
               "2 hours", "no"],
              ["<b>8</b>", "<b>Joint end-to-end test and confirmation</b>", "both", "1 hour",
               "<b>yes &mdash; both sides needed</b>"],
              ["<b>9</b>", "Backup restore drill, alerting enabled", "AIMS LAB", "1 hour", "no"]],
             [8 * mm, 62 * mm, 22 * mm, 20 * mm, 44 * mm], highlight=[5], req=[4, 8]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>Steps 1 to 3 are a single afternoon and need nothing from CMED.</b> The two "
                   "rows that need you are step 4, which needs your clinic codes, and step 8, which "
                   "needs a developer on each side for an hour. <b>Everything else your teams do "
                   "runs in parallel and is not waiting on us.</b>", GOOD))

S.append(Paragraph("17A &middot; Go-live checklist", H2))
S.append(tbl([["", "Item", "Owner"],
              ["&#9744;", "Production hostname and key issued, and stored in CMED's secret store",
               "both"],
              ["&#9744;", "<b>CMED's page addresses registered</b> (production, staging, preview)",
               "<b>CMED &rarr; AIMS LAB</b>"],
              ["&#9744;", "<b>Clinic code mapping agreed and loaded</b>",
               "<b>CMED &rarr; AIMS LAB</b>"],
              ["&#9744;", "<font face='Courier'>/health</font> in CMED's monitoring, asserting on "
                          "<font face='Courier'>system</font> <b>and</b> "
                          "<font face='Courier'>status</font>", "CMED"],
              ["&#9744;", "Both POSTs sent from a <b>durable queue with retry</b>, never inline in "
                          "the request serving the doctor", "CMED"],
              ["&#9744;", "<b>The five fields built once per consultation and reused by all four "
                          "messages</b>", "CMED"],
              ["&#9744;", "Clinic code read from the <font face='Courier'>doctors</font> command, "
                          "never hardcoded", "CMED"],
              ["&#9744;", "Microphone <font face='Courier'>level</font> shown on the page", "CMED"],
              ["&#9744;", "<font face='Courier'>channel_b_test.py</font> run, output sent to AIMS "
                          "LAB", "CMED"],
              ["&#9744;", "<b>End-to-end test done, and the recording confirmed matched and "
                          "archived by AIMS LAB</b>", "both"],
              ["&#9744;", "Droplet live in BLR1, backups enabled, R2 bucket created with "
                          "versioning", "AIMS LAB"],
              ["&#9744;", "Database backup <b>restored once</b> and row counts checked",
               "AIMS LAB"],
              ["&#9744;", "Alerting on unconfirmed recordings and on disk above 80%", "AIMS LAB"],
              ["&#9744;", "All fourteen clinic PCs enrolled against the production hostname",
               "AIMS LAB"]],
             [10 * mm, 112 * mm, 34 * mm], req=[2, 3], highlight=[10]))
S.append(Spacer(1, 4))
S.append(Paragraph("<b>The two amber rows are the ones that block us</b>, and both are CMED's to "
                   "send. The green row is the one that cannot be signed off by either side alone.",
                   NOTE))

S.append(PageBreak())
S.append(Paragraph("18 &middot; One page for the CMED infrastructure engineer", H2))
S.append(tbl([["Question", "Answer"],
              ["Where does the server run?", "<b>DigitalOcean, Bangalore (BLR1)</b>, "
                                             "<font face='Courier'>s-8vcpu-16gb</font> &mdash; "
                                             "8 vCPU / 16 GB / 320 GB, Ubuntu 24.04"],
              ["Why Bangalore?", "Lowest round trip to Dhaka, <b>45&ndash;70 ms</b>"],
              ["Does latency affect recording?", "<b>No.</b> The microphone is driven locally over "
                                                 "loopback"],
              ["How do I know it is up?", "<b><font face='Courier'>GET /health</font></b> &mdash; "
                                          "no key. Expect "
                                          "<font face='Courier'>status: healthy</font>"],
              ["<b>What else must I assert on?</b>", "<b><font face='Courier'>system == "
                                                     "\"AIMScribe v3\"</font></b> &mdash; an older "
                                                     "v1 backend has a similar health path"],
              ["Which ports are open?", "<b>443 TCP and UDP, and 80 TCP</b> for certificates. "
                                        "Nothing else"],
              ["Who issues the certificate?", "<b>Let's Encrypt, automatically.</b> No manual "
                                              "renewal"],
              ["Can I use the IP instead of the name?", "<b>No.</b> The certificate is issued for "
                                                        "the name"],
              ["Do I open a port for the clinic PCs?", "<b>No.</b> The recorder is loopback-only"],
              ["Does CMED need an inbound rule?", "<b>No.</b> We never call you"],
              ["Outbound from CMED?", "HTTPS 443 to our hostname, from your server"],
              ["<b>What changes on CMED's side?</b>", "<b>One line &mdash; "
                                                      "<font face='Courier'>AIMS_SERVER_URL</font>."
                                                      "</b> Your page changes nothing"],
              ["Where does the audio live?", "<b>Cloudflare R2, 3 TB</b>, as encrypted WAV. "
                                             "<b>Not on the droplet's 320 GB</b>, which is working "
                                             "space"],
              ["Can Cloudflare read it?", "<b>No.</b> Encrypted before upload; the key stays at "
                                          "AIMS LAB"],
              ["Does CMED ever receive audio?", "<b>No. Never</b>"],
              ["What if your server goes down?", "<b>Clinics keep recording.</b> Each laptop holds "
                                                 "~13 hours. Keep queueing"],
              ["<b>The only real failure?</b>", "<b>A message never sent.</b> The recording is "
                                                "erased after 24 hours"],
              ["<b>Does a 202 mean success?</b>", "<b>No &mdash; stored, not matched.</b> Ask us to "
                                                  "confirm a test recording archived"],
              ["Total cost?", "<b>$160/month</b> &mdash; $96 droplet, $19 backups, $45 storage"],
              ["What do you need from us?", "<b>Your clinic codes</b> (step 4) and <b>your page's "
                                            "exact web addresses</b>"]],
             [54 * mm, 102 * mm], highlight=[5, 12, 17, 18]))
S.append(Spacer(1, 6))
S.append(Paragraph("Prepared by AIMS LAB, United International University. <b>Document 2 of 2</b>; "
                   "the companion is \"Software Integration\", for the team writing the code. Every "
                   "port, service, variable and limit here is taken from the deployment stack "
                   "itself. Prices and latency figures are indicative.", SUB))


_ap = argparse.ArgumentParser()
_ap.add_argument("--out", default="CMED_2_SERVER_DEPLOYMENT.pdf")
OUT = Path(__file__).resolve().parent.parent / _ap.parse_args().out
build(S, OUT, "AIMScribe v3 - Server Deployment (Document 2 of 2)",
      "AIMScribe v3 - server deployment for CMED - document 2 of 2 - October 2026")
