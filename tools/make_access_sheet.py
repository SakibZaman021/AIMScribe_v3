#!/usr/bin/env python3
"""
Build ACCESS_SHEET.pdf - every address and login for this bench, filled in.

    python tools/make_access_sheet.py

It reads the values out of `deploy/local/.env` when you run it, so this file
holds no secret of its own and is safe in the repository. The sheet it writes
is not: `ACCESS_SHEET.pdf` is in .gitignore and must stay there. It is a map of
the whole system - every address, username and password in one page - and both
AIMScribe repositories are public.

The UIU server has its own .env with different values, generated there. Nothing
here applies to it.
"""
from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, HRFlowable)

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / "deploy" / "local" / ".env"
OUT = ROOT / "ACCESS_SHEET.pdf"

INK = colors.HexColor("#0b0b0b")
TEAL = colors.HexColor("#17836f")
MUTED = colors.HexColor("#898781")
RULE = colors.HexColor("#e1e0d9")
BAND = colors.HexColor("#f4f6f5")
WARNC = colors.HexColor("#7a5200")
CRIT = colors.HexColor("#97292a")

ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Title"], fontName="Helvetica-Bold",
                    fontSize=19, textColor=INK, spaceAfter=2, alignment=0)
SUB = ParagraphStyle("SUB", parent=ss["Normal"], fontSize=9.5, textColor=MUTED, spaceAfter=9)
H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontName="Helvetica-Bold",
                    fontSize=12, textColor=TEAL, spaceBefore=13, spaceAfter=4)
BODY = ParagraphStyle("BODY", parent=ss["Normal"], fontSize=9.3, textColor=INK, leading=13)
NOTE = ParagraphStyle("NOTE", parent=BODY, textColor=WARNC, fontSize=9)
BAD = ParagraphStyle("BAD", parent=BODY, textColor=CRIT, fontSize=9.6)
CELL = ParagraphStyle("CELL", parent=ss["Normal"], fontSize=8.6, textColor=INK, leading=11.4)
MONO = ParagraphStyle("MONO", parent=ss["Normal"], fontName="Courier",
                      fontSize=8.0, textColor=INK, leading=11.2)
LBL = ParagraphStyle("LBL", parent=ss["Normal"], fontName="Helvetica-Bold",
                     fontSize=8.6, textColor=INK, leading=11.4)


def read_env(path: Path) -> dict:
    """KEY=value pairs, quotes stripped. Comments and blanks ignored."""
    if not path.is_file():
        sys.exit(f"{path} not found.\nRun deploy/local/bootstrap.py first - it writes the .env.")
    values = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def tbl(rows, widths, mono_cols=(1,)):
    data = [[Paragraph("<b>%s</b>" % h, CELL) for h in rows[0]]]
    for r in rows[1:]:
        data.append([Paragraph(str(c), MONO if i in mono_cols else (LBL if i == 0 else CELL))
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


def build(env: dict) -> None:
    def v(key, fallback="&mdash; not in .env &mdash;"):
        return env.get(key) or fallback

    port = env.get("AIMS_PORT", "6060")
    pg = env.get("POSTGRES_PORT", "5433")

    S = []
    S.append(Paragraph("AIMScribe v3 &mdash; Access Sheet", H1))
    S.append(Paragraph("AIMS LAB bench, this laptop. Generated from deploy/local/.env.", SUB))
    S.append(HRFlowable(width="100%", thickness=1, color=RULE, spaceAfter=7))
    S.append(Paragraph(
        "<b>This page carries live passwords. Keep it on this machine.</b> It is in .gitignore "
        "and must stay there &mdash; both AIMScribe repositories are public. Do not email it, and "
        "do not carry it to the UIU server: that machine generates its own values.", BAD))

    S.append(Paragraph("1 &middot; Every page, in one place", H2))
    S.append(tbl([["Page", "Address", "Needs"],
                  ["Operations dashboard", f"http://localhost:{port}/api/v2/dashboard",
                   "the administrator key, section 2"],
                  ["<b>CMED page</b>", "<b>http://localhost:3000</b>",
                   "nothing &mdash; this is the page a doctor uses"],
                  ["CMED protocol bench", "http://localhost:3000/protocol-test",
                   "nothing; every message and reply, side by side"],
                  ["Database viewer", "http://localhost:8080",
                   "section 2, and <b>Server = postgres</b>"],
                  ["Object storage console", "http://localhost:9001", "section 2"],
                  ["Server health", f"http://localhost:{port}/health", "nothing"],
                  ["The recorder", "ws://127.0.0.1:5050/ws",
                   "loopback only; the page connects, not a person"]],
                 [36 * mm, 72 * mm, 48 * mm]))
    S.append(Spacer(1, 3))
    S.append(Paragraph("The CMED page runs only when the stack was started with "
                       "<font face='Courier'>--cmed</font>. It is the one to open to record a "
                       "real consultation.", BODY))

    S.append(Paragraph("2 &middot; Every login", H2))
    S.append(tbl([["What it opens", "Username", "Password or key"],
                  ["Dashboard", "&mdash;", v("AIMS_ADMIN_KEY")],
                  ["Adminer, superuser", "aims_admin", v("POSTGRES_SUPERUSER_PASSWORD")],
                  ["<b>aims_recordings</b>", "aims_recordings", v("AIMS_RECORDINGS_PASSWORD")],
                  ["<b>aims_clinical</b>", "aims_clinical_writer", v("AIMS_CLINICAL_PASSWORD")],
                  ["Monitor, read-only", "aims_monitor", v("AIMS_MONITOR_PASSWORD")],
                  ["Object storage", v("MINIO_ACCESS_KEY", "aimslocal"), v("MINIO_SECRET_KEY")],
                  ["Redis", "&mdash;", v("REDIS_PASSWORD")],
                  ["CMED Channel B", "&mdash;", v("AIMS_CMED_KEY")],
                  ["Archive worker", "&mdash;", v("AIMS_WORKER_KEY")]],
                 [30 * mm, 40 * mm, 86 * mm], mono_cols=(1, 2)))
    S.append(Spacer(1, 3))
    S.append(Paragraph(f"All three database roles are on "
                       f"<font face='Courier'>localhost : {pg}</font>. "
                       "<b>aims_recordings</b> holds sessions, integrity chains and receipts and "
                       "contains no patient names by design; <b>aims_clinical</b> holds the "
                       "patient data. Neither role can reach the other's database.", BODY))
    S.append(Spacer(1, 3))
    S.append(Paragraph("<b>In Adminer the Server field must be</b> "
                       "<font face='Courier'>postgres</font>, never "
                       "<font face='Courier'>localhost</font>. Adminer runs inside a container, "
                       "so localhost there is Adminer itself: the connection is refused and the "
                       "page returns empty with no useful error. Start it with "
                       "<font face='Courier'>docker compose --profile db up -d dbviewer</font>.", NOTE))

    S.append(Paragraph("3 &middot; The database without a login form", H2))
    S.append(tbl([["Command", "Shows"],
                  ["python tools/db.py tables", "every table in both databases, real counts"],
                  ["python tools/db.py sessions", "the most recent recordings"],
                  ["python tools/db.py patient &lt;id&gt;", "one patient across both databases"],
                  ["python tools/db.py sql &quot;select ...&quot;",
                   "any read; --db clinical for the other"],
                  ["python tools/db.py analyze", "refresh row estimates after a crash"]],
                 [62 * mm, 94 * mm], mono_cols=(0,)))
    S.append(Spacer(1, 3))
    S.append(Paragraph("It goes through the database container directly, so there is no hostname "
                       "to get wrong and no password to enter. Reads only, unless "
                       "<font face='Courier'>--write</font> is passed.", BODY))

    S.append(Paragraph("4 &middot; The archive on disk", H2))
    S.append(tbl([["What", "Path"],
                  ["On this bench", "deploy/local/archive/"],
                  ["On the UIU server", "/srv/aims/archive   (AIMS_ARCHIVE_PATH)"],
                  ["One consultation", "&lt;clinic&gt;/&lt;doctor&gt;/&lt;date&gt;/&lt;name&gt;/"],
                  ["Inside that folder", "the .wav, its .manifest.json, its clinical .json"]],
                 [34 * mm, 122 * mm]))
    S.append(Spacer(1, 3))
    S.append(Paragraph("The dashboard's recordings table shows this folder per recording with a "
                       "copy button, so removing one by hand is a copy and a paste.", BODY))

    S.append(Paragraph("5 &middot; Ports", H2))
    S.append(tbl([["Port", "Service"],
                  [port, "AIMS LAB server &mdash; API and dashboard"],
                  ["3000", "CMED page, only with --cmed"],
                  ["8080", "Adminer, this machine only, --profile db"],
                  [pg, "PostgreSQL"],
                  ["9001", "Object storage console"],
                  ["5050", "The recorder on a doctor's PC, loopback only"]],
                 [18 * mm, 138 * mm], mono_cols=(0,)))
    S.append(Spacer(1, 4))
    S.append(Paragraph("<b>Not port 6000.</b> Browsers and Node both refuse it &mdash; it belongs "
                       "to X11.", NOTE))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
        canvas.setFont("Helvetica-Bold", 7.3)
        canvas.setFillColor(CRIT)
        canvas.drawString(18 * mm, 9.5 * mm,
                          "CONFIDENTIAL - live passwords. This machine only; never commit, never email.")
        canvas.setFont("Helvetica", 7.3)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(A4[0] - 18 * mm, 9.5 * mm, "page %d" % doc.page)
        canvas.restoreState()

    SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                      topMargin=15 * mm, bottomMargin=20 * mm,
                      title="AIMScribe v3 Access Sheet",
                      author="AIMS LAB").build(S, onFirstPage=footer, onLaterPages=footer)


def main() -> int:
    env = read_env(ENV)
    build(env)
    missing = [k for k in ("AIMS_ADMIN_KEY", "POSTGRES_SUPERUSER_PASSWORD",
                           "AIMS_RECORDINGS_PASSWORD", "AIMS_CLINICAL_PASSWORD")
               if not env.get(k)]
    print(f"wrote {OUT}")
    if missing:
        print("  missing from .env, shown as a dash: " + ", ".join(missing))
    print("  this file holds live passwords - it is gitignored, keep it that way")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
