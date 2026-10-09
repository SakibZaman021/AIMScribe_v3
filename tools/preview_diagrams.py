#!/usr/bin/env python3
"""
Render each workflow diagram to its own PNG so it can be looked at before it
ships in the document.

    python tools/preview_diagrams.py [--out DIR] [--only NAME]

Needs pymupdf for the rasterising step; the diagrams themselves do not.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reportlab.pdfgen import canvas
from reportlab.graphics import renderPDF

import cmed_diagrams as cd

ap = argparse.ArgumentParser()
ap.add_argument("--out", default=None, help="directory for the PNGs")
ap.add_argument("--only", default=None, help="render just one diagram")
ap.add_argument("--dpi", type=int, default=140)
args = ap.parse_args()

out = Path(args.out) if args.out else Path(__file__).resolve().parent.parent / "_diagram_preview"
out.mkdir(parents=True, exist_ok=True)

try:
    import pymupdf
except ImportError:
    sys.exit("pymupdf is needed to rasterise: pip install pymupdf")

names = [args.only] if args.only else list(cd.ALL)
for name in names:
    drawing = cd.ALL[name]()
    pdf = out / f"{name}.pdf"
    c = canvas.Canvas(str(pdf), pagesize=(drawing.width + 24, drawing.height + 24))
    renderPDF.draw(drawing, c, 12, 12)
    c.save()
    doc = pymupdf.open(pdf)
    doc[0].get_pixmap(dpi=args.dpi).save(out / f"{name}.png")
    doc.close()
    print(f"{name:14} {drawing.width:>5.0f} x {drawing.height:>5.0f} pt   -> {out / (name + '.png')}")
