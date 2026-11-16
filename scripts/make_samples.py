"""Generate the demo invoice PDFs in ./samples.

Three different vendor layouts (so extraction can't rely on one template)
plus one image-only "scanned" invoice with no text layer.

    python scripts/make_samples.py
"""
from pathlib import Path
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent.parent / "samples"
OUT.mkdir(exist_ok=True)
W, H = LETTER


def money(x):
    return f"{x:,.2f}"


# ---------------------------------------------------------------- layouts
def classic(path, d):
    """Plain corporate layout: vendor top-left, meta block top-right, ruled table."""
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, H - 60, d["vendor"])
    c.setFont("Helvetica", 9)
    y = H - 76
    for line in d["vendor_addr"]:
        c.drawString(50, y, line)
        y -= 12
    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(W - 50, H - 60, "INVOICE")
    c.setFont("Helvetica", 10)
    meta = [("Invoice No:", d["number"]), ("Invoice Date:", d["date"]), ("Due Date:", d["due"])]
    if d.get("po"):
        meta.append(("PO Number:", d["po"]))
    y = H - 82
    for k, v in meta:
        c.drawRightString(W - 150, y, k)
        c.drawRightString(W - 50, y, v)
        y -= 14
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, H - 170, "Bill To:")
    c.setFont("Helvetica", 10)
    c.drawString(50, H - 184, "Halcyon Manufacturing Co. - Accounts Payable")
    c.drawString(50, H - 198, "2200 Riverside Ave, Columbus, OH 43215")

    y = H - 240
    c.setFillColor(colors.HexColor("#eeeeee"))
    c.rect(45, y - 5, W - 90, 18, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(50, y, "Item")
    c.drawString(130, y, "Description")
    c.drawRightString(400, y, "Qty")
    c.drawRightString(480, y, "Unit Price")
    c.drawRightString(W - 50, y, "Amount")
    c.setFont("Helvetica", 9)
    for ln in d["lines"]:
        y -= 20
        c.drawString(50, y, ln[0])
        c.drawString(130, y, ln[1])
        c.drawRightString(400, y, str(ln[2]))
        c.drawRightString(480, y, money(ln[3]))
        c.drawRightString(W - 50, y, money(ln[2] * ln[3]))
    y -= 14
    c.line(45, y, W - 45, y)
    totals(c, d, y - 20)
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(50, 60, d.get("footer", f"Payment terms: Net {d['terms']}. Please quote the invoice number on remittance."))
    c.save()


def modern(path, d):
    """Colour band header, different labels ("Bill No.", "Order Ref", "Issued")."""
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFillColor(colors.HexColor(d.get("accent", "#1f4e79")))
    c.rect(0, H - 110, W, 110, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(40, H - 55, d["vendor"])
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 72, "  |  ".join(d["vendor_addr"]))
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(W - 40, H - 55, "TAX INVOICE")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 10)
    y = H - 140
    rows = [("Bill No.", d["number"]), ("Issued", d["date"]), ("Payment due", d["due"])]
    if d.get("po"):
        rows.append(("Order Ref", d["po"]))
    for k, v in rows:
        c.setFont("Helvetica-Bold", 10)
        c.drawString(40, y, k)
        c.setFont("Helvetica", 10)
        c.drawString(130, y, v)
        y -= 15
    c.setFont("Helvetica-Bold", 10)
    c.drawString(330, H - 140, "Customer")
    c.setFont("Helvetica", 10)
    c.drawString(330, H - 155, "Halcyon Manufacturing Co.")
    c.drawString(330, H - 170, "Attn: Accounts Payable")

    y = H - 230
    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(colors.HexColor(d.get("accent", "#1f4e79")))
    for x, t, right in [(40, "Description", False), (380, "Qty", True), (470, "Rate", True), (W - 40, "Line Total", True)]:
        (c.drawRightString if right else c.drawString)(x, y, t)
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 9)
    for ln in d["lines"]:
        y -= 20
        desc = f"{ln[1]} ({ln[0]})" if ln[0] else ln[1]
        c.drawString(40, y, desc)
        c.drawRightString(380, y, str(ln[2]))
        c.drawRightString(470, y, money(ln[3]))
        c.drawRightString(W - 40, y, money(ln[2] * ln[3]))
    y -= 14
    c.setStrokeColor(colors.HexColor("#cccccc"))
    c.line(40, y, W - 40, y)
    totals(c, d, y - 20, right=W - 40)
    if d.get("note"):
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(40, 90, d["note"])
    c.setFont("Helvetica", 8)
    c.drawString(40, 60, f"Terms: Net {d['terms']} days. Remit to: {d['remit']}")
    c.save()


def minimal(path, d):
    """Sparse statement-style layout with labels inline in a single column."""
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(60, H - 70, d["vendor"].upper())
    c.setFont("Helvetica", 9)
    c.drawString(60, H - 85, ", ".join(d["vendor_addr"]))
    c.setFont("Helvetica", 10)
    y = H - 130
    lines = [f"Invoice # {d['number']}", f"Date: {d['date']}", f"Due: {d['due']}"]
    if d.get("po"):
        lines.append(f"Purchase Order: {d['po']}")
    lines.append("Customer: Halcyon Manufacturing Co.")
    for t in lines:
        c.drawString(60, y, t)
        y -= 15
    y -= 20
    c.setFont("Helvetica-Bold", 9)
    c.drawString(60, y, "Code")
    c.drawString(140, y, "Service")
    c.drawRightString(400, y, "Units")
    c.drawRightString(480, y, "Price")
    c.drawRightString(W - 60, y, "Total")
    c.setFont("Helvetica", 9)
    for ln in d["lines"]:
        y -= 18
        c.drawString(60, y, ln[0] or "-")
        c.drawString(140, y, ln[1])
        c.drawRightString(400, y, str(ln[2]))
        c.drawRightString(480, y, money(ln[3]))
        c.drawRightString(W - 60, y, money(ln[2] * ln[3]))
    totals(c, d, y - 35, right=W - 60)
    c.setFont("Helvetica", 8)
    c.drawString(60, 60, f"Net {d['terms']}. Questions: {d['remit']}")
    c.save()


def totals(c, d, y, right=W - 50):
    sub = round(sum(q * p for _, _, q, p in d["lines"]), 2)
    tax = round(sub * d["tax_rate"], 2)
    rows = [("Subtotal", sub), (d.get("tax_label", f"Sales Tax ({d['tax_rate']*100:.0f}%)"), tax), ("Total Due (USD)", sub + tax)]
    for i, (k, v) in enumerate(rows):
        c.setFont("Helvetica-Bold" if i == 2 else "Helvetica", 10)
        c.drawRightString(right - 110, y, k)
        c.drawRightString(right, y, "$" + money(v))
        y -= 16


def scanned(path, d):
    """Image-only invoice (no text layer): handwritten-ish fields, tax-inclusive total,
    slight skew and noise, as if it came off an office scanner."""
    scale = 2
    img = Image.new("L", (850 * scale, 1100 * scale), 250)
    dr = ImageDraw.Draw(img)

    def font(size, bold=False):
        for name in (["arialbd.ttf"] if bold else []) + ["arial.ttf", "DejaVuSans.ttf"]:
            try:
                return ImageFont.truetype(name, size * scale)
            except OSError:
                continue
        return ImageFont.load_default()

    def t(x, y, s, size=14, bold=False):
        dr.text((x * scale, y * scale), s, fill=35, font=font(size, bold))

    t(60, 50, d["vendor"], 26, True)
    t(60, 88, " | ".join(d["vendor_addr"]), 12)
    t(600, 50, "INVOICE", 26, True)
    t(60, 150, f"Date:  {d['date']}", 15)
    t(60, 175, f"P.O.:  {d['po_raw']}", 15)
    t(60, 200, "Invoice No:  ________", 15)
    t(470, 150, "Bill to: Halcyon Manufacturing Co.", 14)
    t(470, 172, "2200 Riverside Ave, Columbus OH", 14)
    y = 270
    dr.line((60 * scale, (y - 8) * scale, 790 * scale, (y - 8) * scale), fill=60, width=2 * scale)
    t(60, y, "Description", 14, True)
    t(560, y, "Amount", 14, True)
    for desc, amt in d["scan_lines"]:
        y += 34
        t(60, y, desc, 15)
        t(560, y, f"$ {money(amt)}", 15)
    y += 50
    dr.line((60 * scale, y * scale, 790 * scale, y * scale), fill=60, width=2 * scale)
    t(380, y + 20, d["scan_total_label"], 15, True)
    t(380, y + 48, d["scan_tax_note"], 12)
    t(60, 900, "Thank you for your business. Payment within 30 days.", 12)
    img = img.rotate(0.9, expand=False, fillcolor=250, resample=Image.BICUBIC)
    rnd = random.Random(7)
    px = img.load()
    for _ in range(9000):
        x, yy = rnd.randrange(img.width), rnd.randrange(img.height)
        px[x, yy] = rnd.randrange(150, 230)
    img = img.filter(ImageFilter.GaussianBlur(0.6)).resize((850, 1100))
    img.convert("RGB").save(path, "PDF", resolution=100)


# ---------------------------------------------------------------- scenarios
SAMPLES = [
    ("01_happy_northwind.pdf", classic, dict(
        vendor="Northwind Office Supplies Inc.", vendor_addr=["480 Market St, Suite 12", "San Jose, CA 95113", "Tax ID 94-2817736"],
        number="NW-58213", date="2026-11-10", due="2026-12-10", po="PO-1001", terms=30, tax_rate=0.08,
        lines=[("PPR-A4-500", "A4 copy paper (box of 5 reams)", 50, 42.00),
               ("TNR-26A", "Toner cartridge HP 26A", 10, 89.50),
               ("ORG-DSK-3", "Desk organizer 3-tier", 20, 15.25)])),
    ("02_duplicate_cobalt.pdf", modern, dict(
        vendor="Cobalt Cloud Services", vendor_addr=["1 Harbour Exchange", "Seattle WA 98101", "Tax ID 88-1203347"],
        number="INV-2210", date="Nov 1, 2026", due="Nov 16, 2026", po="PO-1004", terms=15, tax_rate=0.08,
        remit="finance@cobaltcloud.example", accent="#0b6e6e",
        note="Reminder copy - our records show this invoice as outstanding.",
        lines=[("CLD-HOST-OCT", "Cloud hosting - October", 1, 2450.00),
               ("CLD-SUP-OCT", "Premium support - October", 1, 300.00)])),
    ("03_split_overbill_brightline.pdf", minimal, dict(
        vendor="Brightline Logistics LLC", vendor_addr=["77 Freight Way", "Memphis TN 38118"],
        number="BL-1003", date="11/08/2026", due="12/23/2026", po="PO-1002", terms=45, tax_rate=0.0,
        tax_label="Tax (exempt)", remit="billing@brightline.example",
        lines=[("FRT-LH-MO", "Monthly linehaul freight service - Oct", 1, 4000.00),
               ("", "Fuel surcharge - Oct", 1, 650.00)])),
    ("04_no_po_price_drift_apex.pdf", classic, dict(
        vendor="Apex Industrial Components Inc.", vendor_addr=["9 Foundry Rd", "Gary, IN 46402", "Tax ID 37-9902114"],
        number="AIC-77310", date="2026-11-12", due="2026-12-12", po=None, terms=30, tax_rate=0.08,
        footer="Prices reflect our 2026-Q4 price list. Payment terms: Net 30.",
        lines=[("HF-220", "Hydraulic fitting HF-220", 400, 7.10),
               ("GK-15", "Gasket kit GK-15", 120, 12.40),
               ("BR-6204", "Ball bearing 6204-2RS", 200, 4.15)])),
    ("06_unapproved_vertex.pdf", modern, dict(
        vendor="Vertex Marketing Group", vendor_addr=["300 Lakeshore Dr", "Chicago IL 60601"],
        number="VMG-0042", date="2026-11-11", due="2026-11-25", po=None, terms=14, tax_rate=0.0,
        tax_label="Tax", remit="pay@vertexmg.example", accent="#7a2e8c",
        lines=[("", "Campaign strategy retainer - November", 1, 6500.00)])),
]

SCANNED = ("05_scanned_meridian.pdf", dict(
    vendor="Meridian Facilities Management", vendor_addr=["14 Commerce Park", "Columbus OH 43219"],
    date="November 14, 2026", po_raw="PO 1005",
    scan_lines=[("Facility cleaning - November (FM-CLEAN-NOV)", 3888.00),
                ("Cleaning consumables - November (FM-CONS-NOV)", 453.60)],
    scan_total_label="TOTAL DUE:  $ 4,341.60",
    scan_tax_note="Amounts include 8% sales tax ($321.60)"))

if __name__ == "__main__":
    for name, fn, data in SAMPLES:
        fn(OUT / name, data)
        print("wrote", name)
    scanned(OUT / SCANNED[0], SCANNED[1])
    print("wrote", SCANNED[0])
