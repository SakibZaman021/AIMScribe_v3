"""
Vector workflow diagrams for the CMED integration document.

Each function returns a reportlab Drawing sized to the text column, so it drops
straight into a platypus story. Geometry is laid out on a simple grid and every
diagram is rendered to PNG by tools/preview_diagrams.py so it can be looked at
before it ships.
"""
from reportlab.graphics.shapes import (Drawing, Rect, String, Line, Polygon,
                                       Group, PolyLine)
from reportlab.lib import colors
from reportlab.lib.units import mm

W = 490.0                      # drawing width, fits the 174 mm text column

INK = colors.HexColor("#0b0b0b")
TEAL = colors.HexColor("#17836f")
MUTED = colors.HexColor("#6f6d68")
RULE = colors.HexColor("#c9c7c0")
BAND = colors.HexColor("#eef3f2")
PALE = colors.HexColor("#f7f8f7")
WARN = colors.HexColor("#7a5200")
WARNBG = colors.HexColor("#fdf3dc")
CRIT = colors.HexColor("#97292a")
CRITBG = colors.HexColor("#fbeceb")
GOOD = colors.HexColor("#006300")
GOODBG = colors.HexColor("#e7f6e7")
WHITE = colors.white

F = "Helvetica"
FB = "Helvetica-Bold"
FM = "Courier"
FMB = "Courier-Bold"


# ----------------------------------------------------------------- primitives

def box(x, y, w, h, *, fill=WHITE, stroke=RULE, sw=0.8, radius=0):
    r = Rect(x, y, w, h, fillColor=fill, strokeColor=stroke, strokeWidth=sw)
    if radius:
        r.rx = r.ry = radius
    return r


def text(x, y, s, *, size=8, font=F, fill=INK, anchor="start"):
    t = String(x, y, s, fontName=font, fontSize=size, fillColor=fill)
    t.textAnchor = anchor
    return t


def centred(g, x, y, w, lines, *, size=8, font=F, fill=INK, lead=None):
    """Lines centred in a box of width w whose left edge is x, top text at y."""
    lead = lead or size + 2.4
    for i, ln in enumerate(lines):
        g.add(text(x + w / 2.0, y - i * lead, ln, size=size, font=font,
                   fill=fill, anchor="middle"))


def arrow(g, x1, y1, x2, y2, *, colour=TEAL, sw=1.1, head=5.0, dash=None):
    """A straight arrow from (x1,y1) to (x2,y2) with a solid head at the end."""
    ln = Line(x1, y1, x2, y2, strokeColor=colour, strokeWidth=sw)
    if dash:
        ln.strokeDashArray = dash
    g.add(ln)
    dx, dy = x2 - x1, y2 - y1
    n = (dx * dx + dy * dy) ** 0.5 or 1.0
    ux, uy = dx / n, dy / n
    px, py = -uy, ux
    g.add(Polygon([x2, y2,
                   x2 - ux * head - px * head * 0.45,
                   y2 - uy * head - py * head * 0.45,
                   x2 - ux * head + px * head * 0.45,
                   y2 - uy * head + py * head * 0.45],
                  fillColor=colour, strokeColor=colour))


def elbow(g, pts, *, colour=TEAL, sw=1.1, head=5.0, dash=None):
    """A multi-segment line with an arrowhead on the final segment."""
    flat = []
    for p in pts:
        flat.extend(p)
    pl = PolyLine(flat, strokeColor=colour, strokeWidth=sw)
    if dash:
        pl.strokeDashArray = dash
    g.add(pl)
    (x1, y1), (x2, y2) = pts[-2], pts[-1]
    arrow(g, x1, y1, x2, y2, colour=colour, sw=sw, head=head, dash=dash)


def label(g, x, y, s, *, size=7.2, font=F, fill=MUTED, anchor="middle", bg=None):
    if bg is not None:
        wid = len(s) * size * 0.50 + 6
        left = {"middle": x - wid / 2.0, "start": x - 3, "end": x - wid + 3}[anchor]
        g.add(Rect(left, y - 2.4, wid, size + 2.2, fillColor=bg,
                   strokeColor=None))
    g.add(text(x, y, s, size=size, font=font, fill=fill, anchor=anchor))


def caption(g, y, s):
    g.add(text(0, y, s, size=7.2, font=F, fill=MUTED))


# ----------------------------------------------------------------- 1. the map

def architecture():
    """Who talks to whom, and over what."""
    H = 338.0
    d = Drawing(W, H)
    g = Group()

    # ---- the laptop, full width, containing the page and the recorder
    lx, lw, lh = 4, 486, 104
    ly = H - lh - 4
    g.add(box(lx, ly, lw, lh, fill=PALE, stroke=TEAL, sw=1.3, radius=4))
    g.add(text(lx + 10, ly + lh - 15, "THE DOCTOR'S LAPTOP", size=7.8, font=FB,
               fill=TEAL))
    g.add(text(lx + 10, ly + lh - 26, "one per consulting room, fourteen in all",
               size=7, fill=MUTED))

    px, py, pw, ph = lx + 14, ly + 14, 150, 52
    g.add(box(px, py, pw, ph, fill=WHITE, stroke=RULE, sw=1.0, radius=3))
    centred(g, px, py + ph - 17, pw, ["CMED's page"], size=8.8, font=FB)
    centred(g, px, py + ph - 30, pw, ["in the doctor's browser"], size=7,
            fill=MUTED)

    rx, ry, rw, rh = lx + lw - 164, ly + 14, 150, 52
    g.add(box(rx, ry, rw, rh, fill=WHITE, stroke=TEAL, sw=1.2, radius=3))
    centred(g, rx, ry + rh - 17, rw, ["AIMScribe.exe"], size=8.8, font=FB,
            fill=TEAL)
    centred(g, rx, ry + rh - 30, rw, ["the recorder"], size=7, fill=MUTED)

    midx = (px + pw + rx) / 2.0
    arrow(g, px + pw + 4, py + 38, rx - 4, py + 38, colour=TEAL)
    label(g, midx, py + 43, "API 1, API 3a", size=7.4, font=FB, fill=TEAL)
    arrow(g, rx - 4, py + 19, px + pw + 4, py + 19, colour=MUTED, sw=0.9,
          dash=[2, 2])
    label(g, midx, py + 24, "status, doctors", size=7, fill=MUTED)
    label(g, midx, py + 6, "ws://127.0.0.1:5050", size=6.8, font=FM, fill=MUTED)

    # ---- the two servers, side by side
    cx, cy, cw, ch = 4, 128, 176, 56
    g.add(box(cx, cy, cw, ch, fill=WHITE, stroke=RULE, sw=1.1, radius=3))
    centred(g, cx, cy + ch - 18, cw, ["CMED's server"], size=8.8, font=FB)
    centred(g, cx, cy + ch - 32, cw, ["holds the X-CMED-Key"], size=7,
            fill=MUTED)

    ax, ay, aw, ah = 300, 128, 190, 56
    g.add(box(ax, ay, aw, ah, fill=BAND, stroke=TEAL, sw=1.3, radius=3))
    centred(g, ax, ay + ah - 18, aw, ["AIMS LAB server"], size=8.8, font=FB,
            fill=TEAL)
    centred(g, ax, ay + ah - 32, aw, ["DigitalOcean, Bangalore"], size=7,
            fill=MUTED)

    # ---- Cloudflare
    fx, fy, fw2, fh = 300, 64, 190, 42
    g.add(box(fx, fy, fw2, fh, fill=WHITE, stroke=RULE, sw=1.0, radius=3))
    centred(g, fx, fy + fh - 16, fw2, ["Cloudflare R2"], size=8.4, font=FB)
    centred(g, fx, fy + fh - 29, fw2, ["every recording, encrypted"], size=7,
            fill=MUTED)

    # page -> CMED's server, straight down the left
    arrow(g, px + 40, py - 4, cx + 68, cy + ch + 4, colour=MUTED, sw=0.9)
    label(g, px + 66, cy + ch + 14, "the doctor's actions", size=7, fill=MUTED,
          anchor="start")

    # recorder -> AIMS LAB, straight down the right
    arrow(g, rx + rw - 44, ry - 4, ax + aw - 60, ay + ah + 4, colour=TEAL,
          sw=1.3)
    label(g, ax + aw - 54, ay + ah + 14, "audio, in 30-90 second pieces",
          size=7, font=FB, fill=TEAL, anchor="end")

    # CMED's server -> AIMS LAB
    arrow(g, cx + cw + 4, cy + 28, ax - 4, cy + 28, colour=TEAL, sw=1.4)
    label(g, (cx + cw + ax) / 2.0, cy + 34, "API 2, API 3b", size=7.6, font=FB,
          fill=TEAL)
    label(g, (cx + cw + ax) / 2.0, cy + 19, "HTTPS 443", size=6.8, font=FM,
          fill=MUTED)

    # AIMS LAB -> Cloudflare
    arrow(g, ax + aw / 2.0, ay - 4, ax + aw / 2.0, fy + fh + 4, colour=TEAL,
          sw=1.3)
    label(g, ax + aw / 2.0 - 8, (ay + fy + fh) / 2.0 - 2,
          "uploaded, verified, then removed from the server", size=7,
          fill=MUTED, anchor="end")

    # ---- the rule, along the bottom where nothing crosses it
    g.add(box(4, 4, 486, 48, fill=CRITBG, stroke=CRIT, sw=1.0, radius=3))
    g.add(text(13, 40, "Audio never reaches CMED. Nothing clinical is ever served "
                       "back to CMED.", size=8.2, font=FB, fill=CRIT))
    g.add(text(13, 28, "The two downward paths never meet: the recorder sends audio to "
                       "us, and CMED's server sends us the clinical", size=7.4,
               fill=CRIT))
    g.add(text(13, 18, "record. Neither party ever receives the other's data, and there "
                       "is no endpoint by which it could.", size=7.4, fill=CRIT))
    g.add(text(13, 8, "CMED's page never learns the AIMS LAB address either - only "
                      "CMED's own server holds it.", size=7.4, font=FB, fill=CRIT))

    d.add(g)
    return d


# ------------------------------------------------------------- 2. the sequence

def sequence():
    """One consultation, top to bottom, across five lanes."""
    # Heights are declared, then summed, so nothing can silently overflow.
    PAD_TOP, HEAD, GAP, PAD_BOT = 8.0, 22.0, 11.0, 8.0
    M1, MATCH, CONSULT, M2, STILL, M3, CLOSE = 90.0, 34.0, 62.0, 78.0, 46.0, 44.0, 32.0
    H = (PAD_TOP + HEAD + M1 + GAP + MATCH + GAP + CONSULT + GAP + M2 + GAP
         + STILL + GAP + M3 + GAP + CLOSE + PAD_BOT)

    d = Drawing(W, H)
    g = Group()

    lanes = [("The doctor", 38), ("CMED's page", 138), ("CMED's server", 240),
             ("AIMScribe.exe", 342), ("AIMS LAB", 442)]
    D, P, C, R, A = (x for _, x in lanes)

    head_y = H - PAD_TOP - HEAD
    for name, x in lanes:
        g.add(box(x - 42, head_y, 84, HEAD - 3, fill=BAND, stroke=TEAL, sw=0.9,
                  radius=2))
        g.add(text(x, head_y + 6.5, name, size=7.6, font=FB, fill=TEAL,
                   anchor="middle"))
        ln = Line(x, PAD_BOT, x, head_y - 2, strokeColor=RULE, strokeWidth=0.8)
        ln.strokeDashArray = [2, 3]
        g.add(ln)

    def band(top, h, fill, stroke, sw=0.8):
        g.add(box(-2, top - h, 492, h, fill=fill, stroke=stroke, sw=sw, radius=3))

    y = head_y - 2                      # cursor: the top of the next block

    # ---------------------------------------------------- moment 1
    band(y, M1, PALE, RULE, 0.7)
    g.add(text(4, y - 13, "MOMENT 1   the doctor opens a patient", size=7.6,
               font=FB, fill=TEAL))
    arrow(g, D, y - 30, P - 4, y - 30, colour=MUTED, sw=0.9)
    label(g, (D + P) / 2.0, y - 26, "opens patient A", size=6.8, fill=MUTED)

    arrow(g, P, y - 46, R - 4, y - 46, colour=TEAL, sw=1.3)
    label(g, (P + R) / 2.0, y - 42, "API 1   start", size=7.2, font=FB, fill=TEAL)
    arrow(g, R, y - 60, P + 4, y - 60, colour=GOOD, sw=1.0)
    label(g, (P + R) / 2.0, y - 56, "RECORDING_STARTED + session_id", size=6.4,
          font=FM, fill=GOOD)

    arrow(g, P, y - 76, C + 4, y - 76, colour=MUTED, sw=0.9)
    arrow(g, C, y - 76, A - 4, y - 76, colour=TEAL, sw=1.3)
    label(g, (C + A) / 2.0, y - 72, "API 2   patient information", size=7.2,
          font=FB, fill=TEAL)
    label(g, (C + A) / 2.0, y - 85, "202 ACCEPTED", size=6.4, font=FM, fill=GOOD)
    y -= M1 + GAP

    # ---------------------------------------------------- the match
    band(y, MATCH, GOODBG, GOOD, 0.9)
    g.add(text(7, y - 13, "THE MATCH   we compare the five fields the recorder "
                          "reported with the five CMED's server sent.", size=7.4,
               font=FB, fill=GOOD))
    g.add(text(7, y - 24, "Identical: the recording is CONFIRMED and joins the "
                          "dataset.   Different: it is deleted after 24 hours.",
               size=7.2, fill=GOOD))
    y -= MATCH + GAP

    # ---------------------------------------------------- the consultation
    g.add(text(0, y - 10, "the consultation runs", size=7.4, font=FB, fill=INK))
    g.add(Rect(R - 5, y - CONSULT + 6, 10, CONSULT - 12, fillColor=BAND,
               strokeColor=TEAL, strokeWidth=0.8))
    for k in range(4):
        arrow(g, R + 7, y - 16 - k * 12, A - 4, y - 16 - k * 12, colour=RULE,
              sw=0.8, head=3.4)
    g.add(text(0, y - 24, "audio is written continuously and cut into",
               size=7, fill=MUTED))
    g.add(text(0, y - 34, "30-90 second pieces, each uploaded as soon",
               size=7, fill=MUTED))
    g.add(text(0, y - 44, "as it is sealed", size=7, fill=MUTED))
    y -= CONSULT + GAP

    # ---------------------------------------------------- moment 2
    band(y, M2, PALE, RULE, 0.7)
    g.add(text(4, y - 13, "MOMENT 2   the doctor presses Build Prescription",
               size=7.6, font=FB, fill=TEAL))
    arrow(g, D, y - 30, P - 4, y - 30, colour=MUTED, sw=0.9)
    label(g, (D + P) / 2.0, y - 26, "Build Prescription", size=6.8, fill=MUTED)

    arrow(g, P, y - 46, R - 4, y - 46, colour=TEAL, sw=1.3)
    label(g, (P + R) / 2.0, y - 42, "API 3a   prescription_built", size=7.2,
          font=FB, fill=TEAL)
    arrow(g, R, y - 59, P + 4, y - 59, colour=GOOD, sw=1.0)
    label(g, (P + R) / 2.0, y - 55, "GATE_ARMED", size=6.4, font=FM, fill=GOOD)

    arrow(g, P, y - 72, C + 4, y - 72, colour=MUTED, sw=0.9)
    arrow(g, C, y - 72, A - 4, y - 72, colour=TEAL, sw=1.3)
    label(g, (C + A) / 2.0, y - 68, "API 3b   the prescription", size=7.2,
          font=FB, fill=TEAL)
    y -= M2 + GAP

    # ---------------------------------------------------- still recording
    band(y, STILL, WARNBG, WARN, 1.0)
    g.add(text(7, y - 13, "STILL RECORDING   the prescription is printed and handed "
                          "over, and the doctor keeps talking.", size=7.6, font=FB,
               fill=WARN))
    g.add(text(7, y - 24, "That counselling is some of the most valuable audio in the "
                          "study, which is why API 3a stops nothing.", size=7.2,
               fill=WARN))
    g.add(text(7, y - 35, "It only ARMS a gate, meaning: this consultation may now be "
                          "closed by the next one.", size=7.2, fill=WARN))
    y -= STILL + GAP

    # ---------------------------------------------------- moment 3
    band(y, M3, PALE, RULE, 0.7)
    g.add(text(4, y - 13, "MOMENT 3   the doctor opens the next patient", size=7.6,
               font=FB, fill=TEAL))
    arrow(g, D, y - 29, P - 4, y - 29, colour=MUTED, sw=0.9)
    label(g, (D + P) / 2.0, y - 25, "opens patient B", size=6.8, fill=MUTED)
    arrow(g, P, y - 29, R - 4, y - 29, colour=TEAL, sw=1.3)
    label(g, (P + R) / 2.0, y - 25, "API 1   start   (patient B)", size=7.2,
          font=FB, fill=TEAL)
    y -= M3 + GAP

    # ---------------------------------------------------- the close
    band(y, CLOSE, GOODBG, GOOD, 0.9)
    g.add(text(7, y - 13, "Patient A's recording CLOSES here, and patient B's opens "
                          "in the same instant. No gap.", size=7.6, font=FB,
               fill=GOOD))
    g.add(text(7, y - 24, "A's pieces are then joined into one file, named, and "
                          "uploaded to Cloudflare R2 by the archive worker.",
               size=7.2, fill=GOOD))

    d.add(g)
    return d


# ------------------------------------------------------------------ 3. the gate

def gate():
    """The recorder's state machine, which is the idea most often misread."""
    H = 214.0
    d = Drawing(W, H)
    g = Group()

    bw, bh, gapw = 80.0, 50.0, 56.0
    ys = H - 80
    xs = [4 + i * (bw + gapw) for i in range(4)]          # 4*80 + 3*56 = 488
    names = [("IDLE", "nothing is", "being recorded"),
             ("RECORDING", "audio is being", "written to disk"),
             ("RECORDING", "+ armed", "still writing"),
             ("CLOSED", "joined, named,", "uploaded")]
    fills = [WHITE, GOODBG, WARNBG, BAND]
    strokes = [RULE, GOOD, WARN, TEAL]
    inks = [INK, GOOD, WARN, TEAL]

    for i, (x, (a, b, c)) in enumerate(zip(xs, names)):
        g.add(box(x, ys, bw, bh, fill=fills[i], stroke=strokes[i], sw=1.2,
                  radius=3))
        centred(g, x, ys + bh - 16, bw, [a], size=8.4, font=FB, fill=inks[i])
        centred(g, x, ys + bh - 29, bw, [b, c], size=6.6, fill=MUTED)

    for i, name in enumerate(("API 1", "API 3a", "API 1")):
        x1, x2 = xs[i] + bw, xs[i + 1]
        arrow(g, x1 + 3, ys + bh / 2.0, x2 - 3, ys + bh / 2.0, colour=TEAL,
              sw=1.3)
        label(g, (x1 + x2) / 2.0, ys + bh / 2.0 + 7, name, size=7.2, font=FB,
              fill=TEAL)

    # the long command names, as a legend rather than crammed between boxes
    g.add(text(4, ys - 16, "API 1", size=7, font=FB, fill=TEAL))
    g.add(text(34, ys - 16, "= start", size=7, font=FM, fill=MUTED))
    g.add(text(92, ys - 16, "API 3a", size=7, font=FB, fill=TEAL))
    g.add(text(126, ys - 16, "= prescription_built", size=7, font=FM, fill=MUTED))
    g.add(text(244, ys - 16, "the third arrow is the NEXT consultation's API 1",
               size=7, fill=MUTED))

    # the doctor's own stop, routed below everything
    elbow(g, [(xs[1] + bw / 2.0, ys - 2), (xs[1] + bw / 2.0, ys - 34),
              (xs[3] + bw / 2.0, ys - 34), (xs[3] + bw / 2.0, ys - 4)],
          colour=MUTED, sw=0.9, dash=[3, 2])
    label(g, (xs[1] + xs[3] + bw) / 2.0, ys - 45,
          "the doctor's own Stop button  -  never CMED", size=7, font=FB,
          fill=MUTED)

    g.add(box(4, 6, 486, 50, fill=CRITBG, stroke=CRIT, sw=1.0, radius=3))
    g.add(text(13, 44, "The third state is the one that gets missed.",
               size=8.2, font=FB, fill=CRIT))
    g.add(text(13, 32, "prescription_built does not stop the recording. It moves the "
                       "recorder from RECORDING to RECORDING + armed,",
               size=7.4, fill=CRIT))
    g.add(text(13, 22, "which means \"this consultation may now be closed by the next "
                       "one\". Audio keeps being written throughout.", size=7.4,
               fill=CRIT))
    g.add(text(13, 12, "If the next API 1 arrives without the gate being armed, it is "
                       "refused with GATE_NOT_ARMED.", size=7.4, font=FB,
               fill=CRIT))

    d.add(g)
    return d


# ------------------------------------------------------- 4. the five fields

def five_fields():
    """One object, built once, feeding all four messages."""
    # four 40pt boxes on a 54pt pitch, then the note: 16 + 4*54 + 18 + 58 + 6
    H = 300.0
    d = Drawing(W, H)
    g = Group()

    # the source object
    ox, oy, ow, oh = 4, H - 142, 182, 104
    g.add(box(ox, oy, ow, oh, fill=GOODBG, stroke=GOOD, sw=1.3, radius=3))
    g.add(text(ox + 10, oy + oh - 15, "built ONCE, when the doctor", size=7.6,
               font=FB, fill=GOOD))
    g.add(text(ox + 10, oy + oh - 25, "opens the patient", size=7.6, font=FB,
               fill=GOOD))
    rows = ["patient_id   P0012345",
            "doctor_id    DR0042",
            "hospital_id  AALO_DHOLPUR",
            "start_time   ...T10:14:32+06:00",
            "date         2026-10-09"]
    for i, r in enumerate(rows):
        g.add(text(ox + 10, oy + oh - 42 - i * 11, r, size=6.8, font=FM,
                   fill=INK))

    # the four messages
    mx, mw, mh = 268, 218, 40
    msgs = [("API 1", "start", "to the recorder, over loopback", TEAL),
            ("API 2", "patient information", "to AIMS LAB, with the key", TEAL),
            ("API 3a", "prescription_built", "to the recorder, over loopback", TEAL),
            ("API 3b", "the prescription", "to AIMS LAB, with the key", TEAL)]
    tops = [H - 16, H - 70, H - 124, H - 178]
    for (a, b, c, col), t in zip(msgs, tops):
        yy = t - mh
        g.add(box(mx, yy, mw, mh, fill=WHITE, stroke=col, sw=1.0, radius=3))
        g.add(text(mx + 9, yy + mh - 14, a, size=8.4, font=FB, fill=col))
        g.add(text(mx + 50, yy + mh - 14, b, size=7.6, font=FM, fill=INK))
        g.add(text(mx + 9, yy + mh - 27, c, size=6.8, fill=MUTED))
        arrow(g, ox + ow + 2, oy + oh / 2.0, mx - 2, yy + mh / 2.0,
              colour=GOOD, sw=1.0, head=4.4)

    g.add(box(4, 6, 486, 58, fill=WARNBG, stroke=WARN, sw=1.0, radius=3))
    g.add(text(13, 52, "Pass the same object to all four. Never rebuild it and never "
                       "recompute the time.", size=8.2, font=FB, fill=WARN))
    g.add(text(13, 40, "In API 3a and API 3b, start_time is still the moment the "
                       "PATIENT WAS OPENED - not the moment the", size=7.4,
               fill=WARN))
    g.add(text(13, 30, "prescription was built. That single mistake is the most common "
                       "way this integration fails, and it is", size=7.4,
               fill=WARN))
    g.add(text(13, 20, "silent: every message succeeds, and the prescription simply "
                       "never finds its recording.", size=7.4, fill=WARN))
    g.add(text(13, 10, "Take hospital_id from the recorder's doctors command, not from "
                       "your own settings.", size=7.4, font=FB, fill=WARN))

    d.add(g)
    return d


ALL = {"architecture": architecture, "sequence": sequence,
       "gate": gate, "five_fields": five_fields}
