# Invoice Desk: PS-1 invoice processing, from PDF to decision

A working AP automation: drop in a vendor invoice PDF and it extracts the data, checks it against the vendor master,
the invoice ledger and the purchase orders, then decides **Approve / Hold / Reject**, explains why, and drafts the
next action (payment schedule, vendor email, or buyer review task). Every stage is visible live as it runs, and a
dashboard keeps the history.

## Run it

```bash
run.bat          # Windows
./run.sh         # macOS / Linux
```

Then open http://localhost:8000.

**AI extraction (recommended):** put a key in a `.env` file in the project folder (see `.env.example`):
`GEMINI_API_KEY=...` (Google Gemini) or `ANTHROPIC_API_KEY=...` (Claude). The model reads each PDF directly (works on
scans, odd layouts, tax-inclusive pricing). If both keys are set, Claude is tried first; `EXTRACTOR=gemini` changes
that. If the AI call fails, or there's no key, the app runs in **rules-only mode**: a regex parser reads the PDF text
layer and can't read scanned images. The sidebar shows which mode is on.

Use **Reset demo data** in the sidebar before a demo. It clears runs and restores the seed PO ledger, so every
scenario behaves the same each time.

Regenerate the sample PDFs with `python scripts/make_samples.py`.

## How it works

```
PDF ─► Ingest ─► Extract ─► Validate ─► Vendor ─► Duplicates ─► PO match ─► Decide ─► Action
       hash,     AI or       fields,     master,   number/amount/  explicit or  rules     ledger + payment,
       text      rules       math,       status,   date/file hash  inferred PO,           vendor email,
       layer?                dates       tax ID                    line prices,            buyer/AP task
                                                                   qty, balance
```

**Design principle: the LLM reads, the rules decide.** The model (Gemini or Claude, swappable behind one function in
[app/extract.py](app/extract.py)) only turns a messy document into structured fields
(with per-field confidence). Every decision is made by deterministic, auditable rules in
[app/pipeline.py](app/pipeline.py) (`POLICY` holds the tolerances), so each outcome can be explained line by line
and the same invoice always gets the same decision.

Every check produces `pass / warn / fail / info` and an outcome of `none / hold / reject`. A hold is routed to the
person who can fix it: **vendor** (missing or wrong data), **buyer** (price or PO variance), or **AP clerk**
(unreadable scan, low-confidence read, possible duplicate).

| Area | Rule |
|---|---|
| Completeness | invoice number, date, total, vendor must be present; else hold → vendor |
| Arithmetic | qty × price = line; lines (+ tax) = total, ±$0.05 |
| Vendor | fuzzy name match ≥ 86% to master or aliases; 70–86% holds; below rejects; blocked vendors reject; tax ID mismatch holds |
| Duplicates | same normalised invoice number → reject; reformatted number + same amount → reject; same amount + date within 10 days → hold; identical file → reject |
| PO match | explicit PO (any format: "PO-1005", "PO 1005", "1005") or inferred from the vendor's open POs by SKU/description overlap |
| Lines | unit price ≤ +1.5% over PO; cumulative qty ≤ PO qty; charges not on the PO hold |
| Balance | invoice net ≤ remaining PO value (after earlier invoices) + max(1%, $25) |

Approved invoices are posted to the PO ledger, so later invoices on the same PO are checked against what's left.
Held invoices can be approved or rejected by a reviewer in the UI, with a note kept for the audit trail.

## Demo scenarios (in `samples/`)

| # | Scenario | Why it matters | Result |
|---|---|---|---|
| 01 | **Happy path**: clean PDF, PO-1001, all lines match | Baseline straight-through processing | APPROVE, payment scheduled for due date |
| 02 | **Duplicate in disguise**: "reminder copy" of paid invoice CCS-INV-2210, renumbered INV-2210 on a different template | Duplicate payments are the most expensive AP error; exact-match checks miss them | REJECT, rejection email drafted |
| 03 | **Split PO, over-billed**: third invoice on a 3-month freight PO adds a $650 fuel surcharge not on the PO | Partial billing needs a running PO balance, not a one-off comparison | HOLD → buyer, offers partial approval of the $4,000 that matches |
| 04 | **No PO + price drift**: no PO number; PO inferred from SKUs; HF-220 billed 4.4% above contract | Real invoices often omit the PO; price creep hides inside "close enough" totals | HOLD → buyer, shows $120 over PO |
| 05 | **Scanned, tax-inclusive, no invoice #**: image-only PDF, tax baked into line prices, blank number | Scans and tax treatment break template parsers | With AI: HOLD → vendor (asks for invoice number; net-of-tax matches PO-1005). Rules-only: HOLD → AP for manual keying |
| 06 | **Unknown vendor**: clean invoice from a company never onboarded | Fraud control: don't pay vendors who aren't in the master | REJECT |

Bonus live moment: run scenario 01 a second time. It is rejected as a duplicate, and the PO page shows PO-1001 fully billed.

Suggested 5-minute demo order: Reset → 01 (walk the live run view) → 04 (inferred PO, PO-match tab) → 02 →
03 (approve anyway with a note, then show the PO page) → dashboard.

## Assumptions

- One invoice per PDF; USD; sales tax ~8% is passed through, not validated against jurisdiction tables.
- PO prices are pre-tax, so tax-inclusive invoices are converted to net before matching.
- Any charge not on the PO (freight, surcharges) needs buyer approval. There's no automatic allowance.
- Unit prices *below* PO are noted but not held.
- Vendor master, POs and history are CSVs in `app/data/`; in production these would come from the ERP.
- Email and task drafts are generated, not sent. A human stays in the loop for anything not auto-approved.
- "Today" is the server's date, used for the invoice-age check.

## Stack

Python · FastAPI · SQLite · pypdf · Google Gemini API (`gemini-3.8-flash`, PDF input, JSON-schema output) or Claude API
(`claude-opus-5-5`, structured JSON output, PDF input, server-side
refusal fallback) · vanilla JS front end. No build step.

## Files

- [app/pipeline.py](app/pipeline.py): stages, checks, decision policy, action drafting
- [app/extract.py](app/extract.py): Gemini + Claude extractors, rule-based fallback, shared schema
- [app/main.py](app/main.py): API (runs, samples, review, master data, reset)
- [app/static/](app/static/): live run view, dashboard, PO & vendor pages
- [app/data/](app/data/): vendor master, POs, PO lines, invoice history
- [scripts/make_samples.py](scripts/make_samples.py): generates the six demo invoices (three vendor layouts + a scan)
