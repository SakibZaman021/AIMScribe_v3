"""
Shared look and helpers for the two CMED documents.

Imported by make_cmed_integration_doc.py and make_cmed_server_doc.py so the two
teams receive documents that look like one family. No content lives here.
"""
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Table, TableStyle, SimpleDocTemplate

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

_ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=_ss["Title"], fontName="Helvetica-Bold",
                    fontSize=19, textColor=INK, spaceAfter=2, alignment=0, leading=23)
SUB = ParagraphStyle("SUB", parent=_ss["Normal"], fontSize=9.5, textColor=MUTED, spaceAfter=9)
H2 = ParagraphStyle("H2", parent=_ss["Heading2"], fontName="Helvetica-Bold",
                    fontSize=13, textColor=TEAL, spaceBefore=15, spaceAfter=5, leading=16)
H3 = ParagraphStyle("H3", parent=_ss["Heading3"], fontName="Helvetica-Bold",
                    fontSize=10.5, textColor=INK, spaceBefore=10, spaceAfter=3)
BODY = ParagraphStyle("BODY", parent=_ss["Normal"], fontSize=9.4, textColor=INK, leading=13.6)
NOTE = ParagraphStyle("NOTE", parent=BODY, textColor=WARNC)
BAD = ParagraphStyle("BAD", parent=BODY, textColor=CRIT)
GOOD = ParagraphStyle("GOOD", parent=BODY, textColor=GOODC)
CELL = ParagraphStyle("CELL", parent=_ss["Normal"], fontSize=8.3, textColor=INK, leading=11.0)
MONO = ParagraphStyle("MONO", parent=_ss["Normal"], fontName="Courier",
                      fontSize=7.8, textColor=INK, leading=10.8)
LBL = ParagraphStyle("LBL", parent=_ss["Normal"], fontName="Helvetica-Bold",
                     fontSize=8.3, textColor=INK, leading=11.0)
CODE = ParagraphStyle("CODE", parent=_ss["Code"], fontName="Courier", fontSize=7.4,
                      textColor=INK, leading=9.9, backColor=CODEBG,
                      borderPadding=6, leftIndent=0, spaceBefore=3, spaceAfter=3)
PARTT = ParagraphStyle("PARTT", parent=_ss["Normal"], fontName="Helvetica-Bold",
                       fontSize=15, textColor=colors.white, leading=19)
PARTS = ParagraphStyle("PARTS", parent=_ss["Normal"], fontSize=9,
                       textColor=colors.HexColor("#cfe6e0"), leading=12)


def tbl(rows, widths, mono=(), bold_first=True, highlight=None, req=None):
    """A header row plus body rows. `highlight` greens a row, `req` ambers one."""
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
    """A full-width banner opening one part of a document."""
    inner = Table([[Paragraph(label, PARTS)], [Paragraph(title, PARTT)],
                   [Paragraph(blurb, PARTS)]], colWidths=[150 * mm], hAlign="LEFT")
    inner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PARTBG),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (0, 0), 8), ("BOTTOMPADDING", (0, -1), (-1, -1), 9),
        ("TOPPADDING", (0, 1), (-1, -1), 1),
    ]))
    return inner


def build(story, out_path, title, footer_text):
    """Render one document, with a ruled footer carrying its own caption."""
    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
        canvas.setFont("Helvetica", 7.3)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 9.5 * mm, footer_text)
        canvas.drawRightString(A4[0] - 18 * mm, 9.5 * mm, "page %d" % doc.page)
        canvas.restoreState()

    SimpleDocTemplate(str(out_path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                      topMargin=16 * mm, bottomMargin=20 * mm,
                      title=title, author="AIMS LAB").build(
        story, onFirstPage=footer, onLaterPages=footer)
    print("wrote", out_path)
