"""SQLite persistence: run history (what the dashboard shows) and the invoice
ledger (what has already been billed against each PO - drives duplicate and
over-billing checks). `reset()` restores the seed state so demos are repeatable."""
import csv
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
DB_PATH = DATA / "runtime.db"
UPLOADS = DATA / "uploads"
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    filename TEXT,
    file_path TEXT,
    file_hash TEXT,
    status TEXT NOT NULL,            -- queued | running | done | error
    mode TEXT,                       -- claude | rules
    decision TEXT,                   -- APPROVE | HOLD | REJECT
    vendor TEXT,
    vendor_id TEXT,
    invoice_number TEXT,
    po_number TEXT,
    total REAL,
    currency TEXT,
    summary TEXT,
    stages TEXT NOT NULL DEFAULT '[]',
    result TEXT,
    review TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id TEXT,
    invoice_number TEXT,
    invoice_date TEXT,
    po_number TEXT,
    net_amount REAL,
    tax REAL,
    total REAL,
    status TEXT,                     -- paid | approved
    po_line_qty TEXT,                -- JSON {"line_no": qty}
    source TEXT,                     -- seed | run | review
    run_id TEXT,
    created_at TEXT
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init(force=False):
    UPLOADS.mkdir(parents=True, exist_ok=True)
    fresh = force or not DB_PATH.exists()
    with _lock, conn() as c:
        if force:
            c.executescript("DROP TABLE IF EXISTS runs; DROP TABLE IF EXISTS ledger;")
        c.executescript(SCHEMA)
        if fresh:
            _seed_ledger(c)


def reset():
    init(force=True)
    for f in UPLOADS.glob("*"):
        try:
            f.unlink()
        except OSError:
            pass


def _seed_ledger(c):
    with open(DATA / "invoice_history.csv", newline="") as f:
        for r in csv.DictReader(f):
            lines = {}
            for part in filter(None, (r.get("po_line_qty") or "").split("|")):
                ln, q = part.split(":")
                lines[ln] = float(q)
            c.execute(
                "INSERT INTO ledger (vendor_id, invoice_number, invoice_date, po_number, net_amount, tax, total,"
                " status, po_line_qty, source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (r["vendor_id"], r["invoice_number"], r["invoice_date"], r["po_number"], float(r["net_amount"]),
                 float(r["tax"]), float(r["total"]), r["status"], json.dumps(lines), "seed", now()),
            )


# ---------------------------------------------------------------- runs
def create_run(run_id, filename, file_path, file_hash):
    with _lock, conn() as c:
        c.execute("INSERT INTO runs (id, created_at, filename, file_path, file_hash, status) VALUES (?,?,?,?,?,?)",
                  (run_id, now(), filename, str(file_path), file_hash, "queued"))


def update_run(run_id, **fields):
    if not fields:
        return
    for k in ("stages", "result", "review"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k], default=str)
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock, conn() as c:
        c.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for k in ("stages", "result", "review"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    return d


def get_run(run_id):
    with conn() as c:
        return _row(c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())


def list_runs(limit=200):
    with conn() as c:
        rows = c.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = _row(r)
        d.pop("stages", None)
        res = d.pop("result", None) or {}
        d["route"] = res.get("route")
        d["checks_failed"] = sum(1 for ch in res.get("checks", []) if ch.get("outcome") in ("hold", "reject"))
        out.append(d)
    return out


def prior_runs(exclude_id):
    """Earlier runs that are still 'live' for duplicate purposes (approved or waiting in review)."""
    with conn() as c:
        rows = c.execute("SELECT * FROM runs WHERE id != ? AND status='done' AND decision IN ('APPROVE','HOLD')",
                         (exclude_id,)).fetchall()
    return [_row(r) for r in rows]


# ---------------------------------------------------------------- ledger
def ledger(vendor_id=None, po_number=None):
    q, args = "SELECT * FROM ledger WHERE 1=1", []
    if vendor_id:
        q += " AND vendor_id=?"
        args.append(vendor_id)
    if po_number:
        q += " AND po_number=?"
        args.append(po_number)
    with conn() as c:
        rows = c.execute(q + " ORDER BY id", args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["po_line_qty"] = json.loads(d["po_line_qty"] or "{}")
        out.append(d)
    return out


def add_ledger(vendor_id, invoice_number, invoice_date, po_number, net, tax, total, line_qty, source, run_id):
    with _lock, conn() as c:
        c.execute(
            "INSERT INTO ledger (vendor_id, invoice_number, invoice_date, po_number, net_amount, tax, total, status,"
            " po_line_qty, source, run_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (vendor_id, invoice_number, invoice_date, po_number, net, tax, total, "approved",
             json.dumps(line_qty), source, run_id, now()),
        )
