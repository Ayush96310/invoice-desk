"""The invoice-to-decision pipeline.

Stages: ingest -> extract -> validate -> vendor -> duplicates -> po_match -> decide -> action.
Every stage writes its status, timing, summary and details to the run record as it
goes, which is what the live run view polls.

Decision policy (deterministic, see POLICY):
  * any check with outcome "reject" -> REJECT
  * else any check with outcome "hold" -> HOLD (routed to vendor / buyer / AP)
  * else -> APPROVE (posted to the PO ledger, payment scheduled)
"""
import hashlib
import io
import os
import re
import time
import traceback
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

from pypdf import PdfReader

from . import db, extract, master

POLICY = {
    "unit_price_tolerance_pct": 1.5,    # line price may drift this much above PO without a hold
    "header_tolerance_pct": 1.0,        # invoice net may exceed remaining PO value by max(pct, abs)
    "header_tolerance_abs": 25.00,
    "arith_tolerance": 0.05,            # rounding slack for sums
    "max_invoice_age_days": 180,
    "vendor_match_threshold": 0.86,     # fuzzy name match to accept vendor
    "vendor_review_threshold": 0.70,    # between review and match -> hold for confirmation
    "duplicate_amount_pct": 0.5,
    "duplicate_window_days": 10,
}
STAGE_DELAY = float(os.environ.get("STAGE_DELAY", "0.6"))

STAGES = [
    ("ingest", "Ingest document"),
    ("extract", "Extract fields"),
    ("validate", "Validate invoice data"),
    ("vendor", "Vendor master check"),
    ("duplicates", "Duplicate detection"),
    ("po_match", "PO matching"),
    ("decide", "Decision"),
    ("action", "Next action"),
]

ROUTE_LABEL = {"vendor": "Vendor (info request)", "buyer": "Buyer / requester", "ap": "AP clerk"}


def money(x):
    return f"${x:,.2f}" if x is not None else "—"


def norm_name(s):
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(inc|llc|ltd|limited|co|corp|corporation|company|pvt|the)\b", " ", s)
    return " ".join(s.split())


def norm_inv(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def po_digits(s):
    m = re.findall(r"\d+", s or "")
    return m[-1].lstrip("0") if m else None


def sim(a, b):
    return SequenceMatcher(None, a, b).ratio()


class Run:
    def __init__(self, run_id):
        self.id = run_id
        self.stages = [{"key": k, "label": l, "status": "pending"} for k, l in STAGES]
        self.checks = []
        self.ctx = {}

    # -- stage bookkeeping -------------------------------------------------
    def _stage(self, key):
        return next(s for s in self.stages if s["key"] == key)

    def start(self, key):
        s = self._stage(key)
        s.update(status="running", started_at=time.time())
        db.update_run(self.id, stages=self.stages)

    def finish(self, key, summary, details=None, status="done"):
        s = self._stage(key)
        s.update(status=status, summary=summary, details=details or {},
                 duration_ms=int((time.time() - s["started_at"]) * 1000))
        stage_checks = [c for c in self.checks if c["stage"] == key]
        if any(c["outcome"] == "reject" for c in stage_checks):
            s["flag"] = "reject"
        elif any(c["outcome"] == "hold" for c in stage_checks):
            s["flag"] = "hold"
        elif any(c["status"] == "warn" for c in stage_checks):
            s["flag"] = "warn"
        else:
            s["flag"] = "ok"
        s["checks"] = stage_checks
        db.update_run(self.id, stages=self.stages)
        if STAGE_DELAY:
            time.sleep(STAGE_DELAY)

    def check(self, stage, cid, label, status, detail, outcome="none", route=None):
        """status: pass | warn | fail | info ; outcome: none | hold | reject"""
        self.checks.append(dict(stage=stage, id=cid, label=label, status=status, detail=detail,
                                outcome=outcome, route=route))


# ======================================================================
def execute(run_id, pdf_path):
    run = Run(run_id)
    db.update_run(run_id, status="running", stages=run.stages)
    try:
        _ingest(run, pdf_path)
        _extract(run)
        _validate(run)
        _vendor(run)
        _duplicates(run)
        _po_match(run)
        _decide(run)
        _action(run)
        db.update_run(run_id, status="done", finished_at=db.now())
    except Exception as e:  # never leave a run hanging in "running"
        for s in run.stages:
            if s["status"] == "running":
                s.update(status="error", summary=str(e))
        db.update_run(run_id, status="error", finished_at=db.now(), stages=run.stages,
                      summary=f"Pipeline error: {e}", result={"error": traceback.format_exc()})


# ---------------------------------------------------------------- 1 ingest
def _ingest(run, pdf_path):
    run.start("ingest")
    raw = open(pdf_path, "rb").read()
    reader = PdfReader(io.BytesIO(raw))
    text = "\n".join((p.extract_text(extraction_mode="layout") or "") for p in reader.pages)
    chars = len(re.sub(r"\s", "", text))
    has_text = chars > 40
    run.ctx.update(raw=raw, text=text, has_text=has_text, file_hash=hashlib.sha256(raw).hexdigest())
    run.finish("ingest",
               f"{len(reader.pages)} page(s), " + (f"text layer found ({chars} chars)" if has_text
                                                   else "no text layer: scanned/image document"),
               {"pages": len(reader.pages), "bytes": len(raw), "text_chars": chars, "scanned": not has_text,
                "sha256": run.ctx["file_hash"][:16] + "…"})


# ---------------------------------------------------------------- 2 extract
def _extract(run):
    run.start("extract")
    meta, data, mode, errors = {}, None, "rules", []
    for provider in extract.providers():
        try:
            data, meta = extract.extract_with(provider, run.ctx["raw"])
            mode = provider
            break
        except Exception as e:  # try the next provider, then rules
            errors.append(f"{provider}: {type(e).__name__}: {e}"[:300])
    if errors:
        meta["ai_errors"] = errors
    if data is None:
        if run.ctx["has_text"]:
            data = extract.extract_with_rules(run.ctx["text"])
        else:
            data = {k: None for k in extract.SCHEMA["required"]}
            data.update(line_items=[], notes=["Scanned image with no text layer; rule-based extractor cannot read it."],
                        tax_inclusive=False, field_confidence={})
            run.ctx["unreadable"] = True
    run.ctx.update(inv=data, mode=mode)
    db.update_run(run.id, mode=mode, vendor=data.get("vendor_name"), invoice_number=data.get("invoice_number"),
                  total=data.get("total"), currency=data.get("currency"))
    found = sum(1 for k in ("vendor_name", "invoice_number", "invoice_date", "po_reference", "total") if data.get(k))
    how = {"claude": "Claude", "gemini": "Gemini"}.get(mode)
    how = f"{how} ({meta.get('model', extract.model_name(mode))})" if how else "rule-based parser"
    if errors:
        how += f" ({', '.join(e.split(':')[0].title() for e in errors)} call failed; fell back)"
    run.finish("extract", f"{how}: {found}/5 key fields, {len(data.get('line_items') or [])} line item(s)",
               {"method": mode, "fields": data, **meta})


# ---------------------------------------------------------------- 3 validate
def _validate(run):
    run.start("validate")
    inv, P = run.ctx["inv"], POLICY
    S = "validate"
    if run.ctx.get("unreadable"):
        run.check(S, "readable", "Document readable", "fail",
                  "Scanned image with no text layer, and no AI extractor was available (not configured or call failed). Needs manual keying.",
                  "hold", "ap")
        run.finish(S, "Cannot validate: nothing extracted")
        return

    for f, label, route in [("invoice_number", "Invoice number", "vendor"), ("invoice_date", "Invoice date", "vendor"),
                            ("total", "Invoice total", "vendor"), ("vendor_name", "Vendor name", "ap")]:
        if inv.get(f) in (None, ""):
            run.check(S, f"missing_{f}", f"{label} present", "fail", f"{label} is missing from the invoice.", "hold", route)
        else:
            run.check(S, f"missing_{f}", f"{label} present", "pass", money(inv[f]) if f == "total" else f"{inv[f]}")

    low = [k for k, v in (inv.get("field_confidence") or {}).items() if v == "low" and inv.get(k) not in (None, "", [])]
    if low and run.ctx["mode"] != "rules":
        run.check(S, "confidence", "Extraction confidence", "warn",
                  f"Low read confidence on: {', '.join(low)}. Verify against the image.", "hold", "ap")

    items = inv.get("line_items") or []
    total, sub, tax = inv.get("total"), inv.get("subtotal"), inv.get("tax_amount") or 0.0
    line_sum = round(sum(i["amount"] for i in items), 2)
    for i in items:
        exp = round(i["quantity"] * i["unit_price"], 2)
        if abs(exp - i["amount"]) > P["arith_tolerance"]:
            run.check(S, "line_math", "Line arithmetic", "fail",
                      f"'{i['description']}': {i['quantity']} × {money(i['unit_price'])} = {money(exp)}, invoice shows {money(i['amount'])}.",
                      "hold", "vendor")
    if items and total is not None:
        if inv.get("tax_inclusive"):
            ok = abs(line_sum - total) <= P["arith_tolerance"]
            run.check(S, "sum", "Lines add up to total", "pass" if ok else "fail",
                      f"Tax-inclusive lines {money(line_sum)} vs total {money(total)}.", "none" if ok else "hold", "vendor")
            if sub is None and tax:
                inv["subtotal"] = round(total - tax, 2)
            run.check(S, "tax_inclusive", "Tax treatment", "info",
                      f"Prices include tax ({money(tax)}). Net amount for PO matching: {money(inv.get('subtotal'))}.")
        else:
            base = sub if sub is not None else line_sum
            ok1 = sub is None or abs(line_sum - sub) <= P["arith_tolerance"]
            ok2 = abs(base + tax - total) <= P["arith_tolerance"]
            run.check(S, "sum", "Lines + tax = total", "pass" if ok1 and ok2 else "fail",
                      f"Lines {money(line_sum)} · subtotal {money(sub)} · tax {money(tax)} · total {money(total)}.",
                      "none" if ok1 and ok2 else "hold", "vendor")
            if inv.get("subtotal") is None:
                inv["subtotal"] = line_sum
        net = inv.get("subtotal") or 0
        if net and tax / net > 0.2:
            run.check(S, "tax_rate", "Tax rate plausible", "fail", f"Effective tax rate {tax / net:.1%} looks wrong.", "hold", "ap")
    elif not items:
        run.check(S, "lines", "Line items present", "warn", "No line items could be read; matching on header totals only.")
        if inv.get("subtotal") is None and total is not None:
            inv["subtotal"] = round(total - tax, 2)

    d = inv.get("invoice_date")
    if d:
        dd = date.fromisoformat(d)
        if dd > date.today():
            run.check(S, "date", "Invoice date", "fail", f"Invoice is dated in the future ({d}).", "hold", "vendor")
        elif (date.today() - dd).days > P["max_invoice_age_days"]:
            run.check(S, "date", "Invoice date", "fail", f"Invoice is {(date.today() - dd).days} days old.", "hold", "ap")
        else:
            run.check(S, "date", "Invoice date", "pass", f"{d} ({(date.today() - dd).days} days ago)")

    fails = [c for c in run.checks if c["stage"] == S and c["outcome"] != "none"]
    run.finish(S, "All data checks passed" if not fails else f"{len(fails)} issue(s): " + "; ".join(c["label"] for c in fails))


# ---------------------------------------------------------------- 4 vendor
def _vendor(run):
    run.start("vendor")
    inv, P, S = run.ctx["inv"], POLICY, "vendor"
    run.ctx["vendor"] = None
    if run.ctx.get("unreadable"):
        run.finish(S, "Skipped: nothing extracted to check", status="skipped")
        return
    name = inv.get("vendor_name")
    best, score = None, 0.0
    if name:
        n = norm_name(name)
        for v in master.vendors():
            for cand in [v["name"], *v["aliases"]]:
                s = sim(n, norm_name(cand))
                if s > score:
                    best, score = v, s
    details = {"extracted_name": name, "best_match": best and best["name"], "similarity": round(score, 3)}
    run.ctx["vendor"] = None
    if not name:
        run.check(S, "vendor_found", "Vendor identified", "fail", "No vendor name to look up.", "hold", "ap")
        run.finish(S, "No vendor name extracted", details)
        return
    if score < P["vendor_review_threshold"]:
        run.check(S, "vendor_found", "Approved vendor", "fail",
                  f"'{name}' is not in the vendor master (closest: {best['name'] if best else 'none'}, {score:.0%}). "
                  "Payments only go to onboarded vendors.", "reject")
        run.finish(S, f"'{name}' is not an approved vendor", details)
        return
    run.ctx["vendor"] = best
    db.update_run(run.id, vendor=best["name"], vendor_id=best["vendor_id"])
    if score < P["vendor_match_threshold"]:
        run.check(S, "vendor_found", "Approved vendor", "warn",
                  f"'{name}' only loosely matches '{best['name']}' ({score:.0%}). Confirm it is the same entity.", "hold", "ap")
    else:
        run.check(S, "vendor_found", "Approved vendor", "pass", f"Matched {best['vendor_id']} · {best['name']} ({score:.0%})")
    if best["status"] != "approved":
        run.check(S, "vendor_status", "Vendor status", "fail", f"{best['name']} is {best['status'].upper()} in the vendor master.", "reject")
    tid = inv.get("vendor_tax_id")
    if tid:
        if re.sub(r"\D", "", tid) != re.sub(r"\D", "", best["tax_id"]):
            run.check(S, "tax_id", "Tax ID matches master", "fail",
                      f"Invoice tax ID {tid} ≠ master {best['tax_id']}. Possible impersonation or changed entity.", "hold", "ap")
        else:
            run.check(S, "tax_id", "Tax ID matches master", "pass", tid)
    if inv.get("currency") and inv["currency"] != best["currency"]:
        run.check(S, "currency", "Currency", "fail", f"Invoice in {inv['currency']}, vendor contracted in {best['currency']}.", "hold", "buyer")
    details.update(vendor_id=best["vendor_id"], status=best["status"], terms=f"Net {best['payment_terms_days']}")
    run.finish(S, f"{best['vendor_id']} · {best['name']} ({best['status']})", details)


# ---------------------------------------------------------------- 5 duplicates
def _duplicates(run):
    run.start("duplicates")
    inv, P, S = run.ctx["inv"], POLICY, "duplicates"
    v = run.ctx.get("vendor")
    hits = []
    for r in db.prior_runs(run.id):
        if r["file_hash"] == run.ctx["file_hash"]:
            hits.append(("exact_file", f"Identical file already processed in run {r['id'][:8]} ({r['decision']}).", "reject"))
    if v:
        num = norm_inv(inv.get("invoice_number"))
        total = inv.get("total")
        idate = inv.get("invoice_date")
        candidates = [dict(source="ledger", ref=l["invoice_number"], date=l["invoice_date"], total=l["total"],
                           po=l["po_number"], status=l["status"]) for l in db.ledger(vendor_id=v["vendor_id"])]
        for r in db.prior_runs(run.id):
            if r.get("vendor_id") == v["vendor_id"] and r.get("invoice_number"):
                res = r.get("result") or {}
                candidates.append(dict(source="run", ref=r["invoice_number"], date=res.get("invoice_date"), total=r["total"],
                                       po=r["po_number"], status=("in review" if r["decision"] == "HOLD" else "approved")))
        seen = set()
        for c in candidates:
            key = norm_inv(c["ref"])
            if key in seen:
                continue
            seen.add(key)
            cn = norm_inv(c["ref"])
            same_amt = total is not None and c["total"] and abs(c["total"] - total) <= c["total"] * P["duplicate_amount_pct"] / 100
            num_exact = num and cn == num
            num_fuzzy = num and cn and not num_exact and len(min(cn, num, key=len)) >= 4 and \
                (cn.endswith(num) or num.endswith(cn)) and re.sub(r"\D", "", cn) == re.sub(r"\D", "", num)
            near_date = False
            if idate and c["date"]:
                near_date = abs((date.fromisoformat(idate) - date.fromisoformat(c["date"])).days) <= P["duplicate_window_days"]
            if num_exact:
                hits.append(("same_number", f"Invoice {c['ref']} from this vendor is already {c['status']} "
                             f"({money(c['total'])}, {c['date']}).", "reject"))
            elif num_fuzzy and same_amt:
                hits.append(("reformatted_number", f"'{inv.get('invoice_number')}' is the same invoice as {c['ref']} "
                             f"(already {c['status']}): same number once formatting is stripped, same amount "
                             f"{money(total)}{', same date' if near_date else ''}.", "reject"))
            elif same_amt and near_date:
                hits.append(("same_amount_date", f"Same amount {money(total)} and date within {P['duplicate_window_days']} days "
                             f"as {c['ref']} ({c['status']}), different number. Possible duplicate.", "hold"))
    if not hits:
        run.check(S, "dup", "Not previously processed", "pass", "No matching invoice number, amount/date, or file hash.")
    for cid, detail, outcome in hits:
        run.check(S, cid, "Duplicate check", "fail", detail, outcome, "ap" if outcome == "hold" else None)
    run.finish(S, "No duplicates found" if not hits else hits[0][1], {"matches": len(hits)})


# ---------------------------------------------------------------- 6 PO match
def _billed(po_number):
    qty, net = {}, 0.0
    for l in db.ledger(po_number=po_number):
        net += l["net_amount"] or 0
        for ln, q in l["po_line_qty"].items():
            qty[int(ln)] = qty.get(int(ln), 0) + q
    return qty, round(net, 2)


def _line_match(item, po_lines):
    sku = (item.get("sku") or "").upper()
    for pl in po_lines:
        if sku and pl["sku"].upper() == sku:
            return pl, 1.0
    best, score = None, 0.0
    for pl in po_lines:
        s = sim(norm_name(item["description"]), norm_name(pl["description"]))
        if s > score:
            best, score = pl, s
    return (best, score) if score >= 0.72 else (None, score)


def _po_match(run):
    run.start("po_match")
    inv, P, S = run.ctx["inv"], POLICY, "po_match"
    v = run.ctx.get("vendor")
    pos = master.purchase_orders()
    run.ctx["po"] = None
    if not v:
        run.finish(S, "Skipped: no approved vendor to match against", status="skipped")
        return
    ref = inv.get("po_reference")
    po, how = None, None
    if ref:
        dg = po_digits(ref)
        po = next((p for p in pos if po_digits(p["po_number"]) == dg), None)
        if po and po["vendor_id"] != v["vendor_id"]:
            run.check(S, "po_vendor", "PO belongs to vendor", "fail",
                      f"{po['po_number']} was issued to {master.vendor_by_id(po['vendor_id'])['name']}, not {v['name']}.", "hold", "buyer")
            run.finish(S, f"{po['po_number']} belongs to a different vendor")
            return
        how = "explicit" if po else None
        if not po:
            run.check(S, "po_exists", "PO exists", "fail", f"Referenced PO '{ref}' was not found.", "hold", "buyer")
    if not po:
        # Infer: score this vendor's open POs by SKU/description overlap with the invoice lines.
        best, best_score = None, 0.0
        items = inv.get("line_items") or []
        for p in pos:
            if p["vendor_id"] != v["vendor_id"] or p["status"] != "open":
                continue
            matched = sum(1 for it in items if _line_match(it, p["lines"])[0])
            score = matched / max(len(items), 1)
            if not items and inv.get("subtotal"):
                score = 1 - min(abs(p["total"] - inv["subtotal"]) / p["total"], 1)
            if score > best_score:
                best, best_score = p, score
        if best and best_score >= 0.6:
            po, how = best, "inferred"
            run.check(S, "po_inferred", "PO reference", "warn",
                      f"No PO on invoice{f' (or {ref!r} not found)' if ref else ''}. Inferred {best['po_number']} from vendor + "
                      f"{best_score:.0%} line overlap ({best['description']}).")
        elif not ref:
            run.check(S, "po_exists", "PO reference", "fail",
                      f"No PO on the invoice and none of {v['name']}'s open POs match the billed items.", "hold", "buyer")
    if not po:
        run.finish(S, "No purchase order could be matched")
        return

    run.ctx["po"] = po
    db.update_run(run.id, po_number=po["po_number"])
    if how == "explicit":
        run.check(S, "po_exists", "PO reference", "pass", f"{po['po_number']} · {po['description']} (buyer {po['buyer']})")

    billed_qty, billed_net = _billed(po["po_number"])
    remaining_value = round(po["total"] - billed_net, 2)
    items = inv.get("line_items") or []
    tax_factor = 1.0
    if inv.get("tax_inclusive") and inv.get("total") and inv.get("subtotal"):
        tax_factor = inv["subtotal"] / inv["total"]

    rows, clean_net, posting = [], 0.0, {}
    for it in items:
        pl, conf = _line_match(it, po["lines"])
        unit_net = round(it["unit_price"] * tax_factor, 4)
        amt_net = round(it["amount"] * tax_factor, 2)
        row = {"invoice_line": it["description"], "sku": it.get("sku"), "qty": it["quantity"], "unit_price": unit_net,
               "amount": amt_net, "po_line": None, "issues": []}
        if not pl:
            row["issues"].append("not on PO")
            run.check(S, "unmatched_line", "Charge is on the PO", "fail",
                      f"'{it['description']}' ({money(amt_net)}) has no matching line on {po['po_number']}.", "hold", "buyer")
            rows.append(row)
            continue
        prev = billed_qty.get(pl["line_no"], 0)
        var = (unit_net - pl["unit_price"]) / pl["unit_price"] * 100
        row.update(po_line=pl["line_no"], po_desc=pl["description"], po_unit_price=pl["unit_price"], po_qty=pl["qty"],
                   billed_before=prev, price_var_pct=round(var, 2), match_conf=round(conf, 2))
        line_ok = True
        if var > P["unit_price_tolerance_pct"]:
            over = round((unit_net - pl["unit_price"]) * it["quantity"], 2)
            row["issues"].append(f"price +{var:.1f}%")
            run.check(S, "price_var", "Unit price within tolerance", "fail",
                      f"{pl['sku']}: billed {money(unit_net)} vs PO {money(pl['unit_price'])} (+{var:.1f}%, tolerance "
                      f"{P['unit_price_tolerance_pct']}%) = {money(over)} over PO.", "hold", "buyer")
            line_ok = False
        elif var < -P["unit_price_tolerance_pct"]:
            row["issues"].append(f"price {var:.1f}% (under)")
            run.check(S, "price_under", "Unit price", "info", f"{pl['sku']} billed {abs(var):.1f}% below PO price.")
        if prev + it["quantity"] > pl["qty"] + 1e-9:
            row["issues"].append("qty exceeds PO")
            run.check(S, "qty_over", "Quantity within PO", "fail",
                      f"{pl['sku']}: {prev:g} already billed + {it['quantity']:g} now = {prev + it['quantity']:g} > PO qty {pl['qty']:g}.",
                      "hold", "buyer")
            line_ok = False
        if line_ok:
            clean_net += amt_net
        posting[str(pl["line_no"])] = posting.get(str(pl["line_no"]), 0) + it["quantity"]
        rows.append(row)

    net = inv.get("subtotal") or round(sum(r["amount"] for r in rows), 2)
    allowed = max(remaining_value * P["header_tolerance_pct"] / 100, P["header_tolerance_abs"])
    if net > remaining_value + allowed:
        run.check(S, "po_balance", "Within remaining PO value", "fail",
                  f"Invoice net {money(net)} exceeds remaining PO value {money(remaining_value)} by {money(net - remaining_value)} "
                  f"({money(billed_net)} of {money(po['total'])} already billed).", "hold", "buyer")
    else:
        run.check(S, "po_balance", "Within remaining PO value", "pass",
                  f"Net {money(net)} of {money(remaining_value)} remaining ({money(billed_net)} billed before).")
    if 0 < billed_net < po["total"]:
        run.check(S, "split", "Partial billing", "info",
                  f"{po['po_number']} is being billed in parts: {money(billed_net)} previously approved.")
    run.ctx.update(posting=posting, net=net, clean_net=round(clean_net, 2))
    flagged = [c for c in run.checks if c["stage"] == S and c["outcome"] == "hold"]
    run.finish(S, f"{po['po_number']} ({how}) · " + ("all lines match" if not flagged else f"{len(flagged)} variance(s)"),
               {"po": {k: po[k] for k in ("po_number", "description", "buyer", "total", "issue_date")}, "match": how,
                "billed_before": billed_net, "remaining_before": remaining_value, "lines": rows,
                "po_lines": po["lines"]})


# ---------------------------------------------------------------- 7 decide
def _decide(run):
    run.start("decide")
    rejects = [c for c in run.checks if c["outcome"] == "reject"]
    holds = [c for c in run.checks if c["outcome"] == "hold"]
    if rejects:
        decision, reasons = "REJECT", rejects
    elif holds:
        decision, reasons = "HOLD", holds
    else:
        decision, reasons = "APPROVE", []
    routes = sorted({c["route"] for c in holds if c["route"]}, key=["vendor", "buyer", "ap"].index) if decision == "HOLD" else []
    inv, po, v = run.ctx["inv"], run.ctx.get("po"), run.ctx.get("vendor")
    if decision == "APPROVE":
        summary = (f"Approved: {money(inv.get('total'))} from {v['name']} matches {po['po_number']} on every line"
                   f"{' (PO inferred from line items)' if any(c['id'] == 'po_inferred' for c in run.checks) else ''}.")
    elif decision == "REJECT":
        summary = "Rejected: " + reasons[0]["detail"]
    else:
        summary = f"On hold ({', '.join(ROUTE_LABEL[r] for r in routes) or 'review'}): " + " ".join(c["detail"] for c in reasons[:2])
        if len(reasons) > 2:
            summary += f" (+{len(reasons) - 2} more)"
    run.ctx.update(decision=decision, routes=routes, summary=summary)
    db.update_run(run.id, decision=decision, summary=summary)
    run.finish("decide", summary, {"decision": decision, "reasons": [c["detail"] for c in reasons], "routes": routes,
                                    "policy": POLICY})


# ---------------------------------------------------------------- 8 action
def _action(run):
    run.start("action")
    inv, po, v = run.ctx["inv"], run.ctx.get("po"), run.ctx.get("vendor")
    decision, checks = run.ctx["decision"], run.checks
    actions = []
    num = inv.get("invoice_number") or "(no number)"
    if decision == "APPROVE":
        due = inv.get("due_date")
        if not due and inv.get("invoice_date"):
            due = (date.fromisoformat(inv["invoice_date"]) + timedelta(days=v["payment_terms_days"])).isoformat()
        db.add_ledger(v["vendor_id"], inv.get("invoice_number"), inv.get("invoice_date"), po["po_number"],
                      run.ctx.get("net"), inv.get("tax_amount") or 0, inv.get("total"), run.ctx.get("posting", {}), "run", run.id)
        actions.append({"type": "post", "title": "Posted to PO ledger",
                        "body": f"{money(run.ctx.get('net'))} net booked against {po['po_number']}; PO balance updated."})
        actions.append({"type": "payment", "title": f"Payment scheduled for {due}",
                        "body": f"{money(inv.get('total'))} to {v['name']} (Net {v['payment_terms_days']}), remit {v['remit_email']}."})
    elif decision == "REJECT":
        rej = [c for c in checks if c["outcome"] == "reject"]
        to = v["remit_email"] if v else "(sender of the invoice email)"
        reason_lines = "\n".join(f"  • {c['detail']}" for c in rej)
        actions.append({"type": "email", "title": "Rejection notice to vendor (draft)", "to": to,
                        "subject": f"Invoice {num}: not processed",
                        "body": f"Hello,\n\nWe received invoice {num} for {money(inv.get('total'))} but cannot process it:\n\n"
                                f"{reason_lines}\n\nNo payment will be made against this document. If you believe this is in error, "
                                "reply to this email and reference the original invoice.\n\nAccounts Payable\nHalcyon Manufacturing Co."})
    else:
        by_route = {}
        for c in checks:
            if c["outcome"] == "hold":
                by_route.setdefault(c["route"] or "ap", []).append(c)
        if "vendor" in by_route:
            asks = "\n".join(f"  • {c['detail']}" for c in by_route["vendor"])
            actions.append({"type": "email", "title": "Information request to vendor (draft)",
                            "to": v["remit_email"] if v else "(sender)", "subject": f"Action needed: invoice for {money(inv.get('total'))}",
                            "body": f"Hello,\n\nWe're processing your invoice dated {inv.get('invoice_date') or '(undated)'} for "
                                    f"{money(inv.get('total'))}{' against ' + po['po_number'] if po else ''}, but need the following "
                                    f"before it can be approved:\n\n{asks}\n\nPlease reply with a corrected invoice. The rest of the invoice "
                                    "checks out, so we'll release it as soon as we hear back.\n\nAccounts Payable\nHalcyon Manufacturing Co."})
        if "buyer" in by_route:
            asks = "\n".join(f"  • {c['detail']}" for c in by_route["buyer"])
            clean = run.ctx.get("clean_net")
            partial = (f"\n\nOption: approve the {money(clean)} that matches the PO now and dispute the rest."
                       if clean and run.ctx.get("net") and clean < run.ctx["net"] else "")
            actions.append({"type": "task", "title": f"Review task for buyer{(' ' + po['buyer']) if po else ''}",
                            "body": f"Invoice {num} from {v['name'] if v else 'unknown vendor'} needs a purchasing decision:\n\n{asks}"
                                    f"{partial}\n\nApprove the variance (and amend the PO) or ask the vendor for a credit note."})
        if "ap" in by_route:
            asks = "\n".join(f"  • {c['detail']}" for c in by_route["ap"])
            actions.append({"type": "task", "title": "AP clerk review", "body": f"Check before releasing:\n\n{asks}"})
    result = {
        "decision": decision, "summary": run.ctx["summary"], "route": run.ctx.get("routes"), "checks": checks,
        "actions": actions, "invoice": inv, "invoice_date": inv.get("invoice_date"),
        "vendor": v and {k: v[k] for k in ("vendor_id", "name", "status", "payment_terms_days")},
        "po": po and {k: po[k] for k in ("po_number", "description", "buyer", "total")}, "mode": run.ctx["mode"],
        "net": run.ctx.get("net"), "clean_net": run.ctx.get("clean_net"), "posting": run.ctx.get("posting"),
    }
    db.update_run(run.id, result=result)
    run.finish("action", "; ".join(a["title"] for a in actions) or "No action")
