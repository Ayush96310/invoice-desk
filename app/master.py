"""Procurement master data: vendor master and purchase orders (CSV-backed)."""
import csv
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"


def _read(name):
    with open(DATA / name, newline="") as f:
        return list(csv.DictReader(f))


def vendors():
    out = []
    for r in _read("vendors.csv"):
        r["aliases"] = [a for a in r["aliases"].split("|") if a]
        r["payment_terms_days"] = int(r["payment_terms_days"])
        out.append(r)
    return out


def purchase_orders():
    lines = {}
    for r in _read("po_lines.csv"):
        r["line_no"] = int(r["line_no"])
        r["qty"] = float(r["qty"])
        r["unit_price"] = float(r["unit_price"])
        r["amount"] = round(r["qty"] * r["unit_price"], 2)
        lines.setdefault(r["po_number"], []).append(r)
    pos = []
    for r in _read("purchase_orders.csv"):
        r["lines"] = lines.get(r["po_number"], [])
        r["total"] = round(sum(l["amount"] for l in r["lines"]), 2)
        pos.append(r)
    return pos


def vendor_by_id(vid):
    return next((v for v in vendors() if v["vendor_id"] == vid), None)
