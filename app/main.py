"""HTTP API + static UI.

    uvicorn app.main:app --reload
"""
import hashlib
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, extract, master, pipeline

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT.parent / "samples"

SAMPLE_INFO = {
    "01_happy_northwind.pdf": ("Happy path", "Clean PDF, explicit PO, every line matches PO-1001.", "APPROVE"),
    "02_duplicate_cobalt.pdf": ("Duplicate in disguise", "'Reminder copy' of an invoice already paid as CCS-INV-2210, re-numbered INV-2210 on a different template.", "REJECT"),
    "03_split_overbill_brightline.pdf": ("Split PO, over-billed", "Third invoice against a 3-month freight PO; adds a fuel surcharge that isn't on the PO.", "HOLD → buyer"),
    "04_no_po_price_drift_apex.pdf": ("No PO + price drift", "No PO number; PO is inferred from line items, but one unit price is 4.4% above contract.", "HOLD → buyer"),
    "05_scanned_meridian.pdf": ("Scanned, tax-inclusive, no invoice #", "Image-only scan, tax baked into line prices, blank invoice number.", "HOLD → vendor (AI) / AP (rules only)"),
    "06_unapproved_vertex.pdf": ("Unknown vendor", "Clean invoice from a company that was never onboarded, no PO.", "REJECT"),
}

app = FastAPI(title="Invoice Desk")
db.init()


def _start(filename, data):
    run_id = uuid.uuid4().hex[:12]
    path = db.UPLOADS / f"{run_id}.pdf"
    path.write_bytes(data)
    db.create_run(run_id, filename, path, hashlib.sha256(data).hexdigest())
    threading.Thread(target=pipeline.execute, args=(run_id, path), daemon=True).start()
    return {"id": run_id}


@app.post("/api/runs")
async def create_run(file: UploadFile = File(...)):
    data = await file.read()
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "Please upload a PDF invoice.")
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "File too large (20 MB max).")
    return _start(file.filename or "invoice.pdf", data)


@app.post("/api/runs/sample/{name}")
def run_sample(name: str):
    if name not in SAMPLE_INFO:
        raise HTTPException(404, "Unknown sample")
    return _start(name, (SAMPLES / name).read_bytes())


@app.get("/api/runs")
def runs():
    return db.list_runs()


@app.get("/api/runs/{run_id}")
def run(run_id: str):
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(404, "Run not found")
    r.pop("file_path", None)
    return r


@app.get("/api/runs/{run_id}/file")
def run_file(run_id: str):
    r = db.get_run(run_id)
    if not r or not Path(r["file_path"]).exists():
        raise HTTPException(404)
    return FileResponse(r["file_path"], media_type="application/pdf")


class Review(BaseModel):
    action: str  # approve | reject
    note: str = ""
    reviewer: str = "AP reviewer"


@app.post("/api/runs/{run_id}/review")
def review(run_id: str, body: Review):
    r = db.get_run(run_id)
    if not r or r["decision"] != "HOLD" or r.get("review"):
        raise HTTPException(400, "Only un-reviewed HOLD runs can be reviewed.")
    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    res = r["result"] or {}
    inv = res.get("invoice") or {}
    if body.action == "approve":
        if not (res.get("vendor") and res.get("po")):
            raise HTTPException(400, "Cannot approve without a matched vendor and PO.")
        db.add_ledger(res["vendor"]["vendor_id"], inv.get("invoice_number"), inv.get("invoice_date"), res["po"]["po_number"],
                      res.get("net"), inv.get("tax_amount") or 0, inv.get("total"), res.get("posting") or {}, "review", run_id)
    rev = {"action": body.action, "note": body.note, "reviewer": body.reviewer, "at": db.now()}
    db.update_run(run_id, review=rev, decision="APPROVE" if body.action == "approve" else "REJECT",
                  summary=f"{'Approved' if body.action == 'approve' else 'Rejected'} by {body.reviewer} after review"
                          f"{': ' + body.note if body.note else ''}. Originally: {r['summary']}")
    return {"ok": True}


@app.get("/api/samples")
def samples():
    return [{"name": n, "title": t, "description": d, "expected": e} for n, (t, d, e) in SAMPLE_INFO.items()]


@app.get("/api/samples/{name}")
def sample_file(name: str):
    if name not in SAMPLE_INFO:
        raise HTTPException(404)
    return FileResponse(SAMPLES / name, media_type="application/pdf")


@app.get("/api/master")
def master_data():
    pos = master.purchase_orders()
    for p in pos:
        qty, net = pipeline._billed(p["po_number"])
        p["billed_net"] = net
        p["remaining"] = round(p["total"] - net, 2)
        for l in p["lines"]:
            l["billed_qty"] = qty.get(l["line_no"], 0)
    return {"vendors": master.vendors(), "purchase_orders": pos, "ledger": db.ledger(), "policy": pipeline.POLICY}


@app.get("/api/status")
def status():
    prov = extract.providers()
    return {"provider": prov[0] if prov else "rules", "providers": prov,
            "model": extract.model_name(prov[0]) if prov else None, "stage_delay": pipeline.STAGE_DELAY}


@app.post("/api/reset")
def reset():
    db.reset()
    return {"ok": True}


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")
