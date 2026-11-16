"""Invoice field extraction.

Three extractors produce the same shape:
  * Claude or Gemini (vision + text): read the PDF directly, so they handle scans,
    odd layouts, tax-inclusive pricing and implied PO references. Whichever has a
    key configured is used (EXTRACTOR=claude|gemini|rules forces one).
  * Rules (regex over the PDF's text layer): deterministic fallback used when
    no API key is configured or the API call fails. It cannot read scans.

The model only *reads* the document. Every decision is made later by
deterministic rules in pipeline.py, so outcomes are explainable and auditable.
"""
import base64
import json
import os
import re
from datetime import datetime

from pathlib import Path


def _load_dotenv():
    """Read KEY=value lines from a .env file in the project root (keys stay out of the code)."""
    f = Path(__file__).resolve().parent.parent / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
# Tried in order; the first that answers is used. GEMINI_MODEL=a,b,c overrides.
GEMINI_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODEL", "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash,gemini-3.5-flash-lite").split(",") if m.strip()]
GEMINI_MODEL = GEMINI_MODELS[0]
GEMINI_TIMEOUT_MS = int(os.environ.get("GEMINI_TIMEOUT_MS", "30000"))

_NUM = {"type": "number"}
_STR_N = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NUM_N = {"anyOf": [{"type": "number"}, {"type": "null"}]}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["vendor_name", "vendor_tax_id", "invoice_number", "invoice_date", "due_date", "po_reference",
                 "currency", "line_items", "subtotal", "tax_amount", "tax_inclusive", "total", "notes",
                 "field_confidence"],
    "properties": {
        "vendor_name": _STR_N,
        "vendor_tax_id": _STR_N,
        "invoice_number": _STR_N,
        "invoice_date": _STR_N,
        "due_date": _STR_N,
        "po_reference": _STR_N,
        "currency": _STR_N,
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["sku", "description", "quantity", "unit_price", "amount"],
                "properties": {"sku": _STR_N, "description": {"type": "string"},
                               "quantity": _NUM, "unit_price": _NUM, "amount": _NUM},
            },
        },
        "subtotal": _NUM_N,
        "tax_amount": _NUM_N,
        "tax_inclusive": {"type": "boolean"},
        "total": _NUM_N,
        "notes": {"type": "array", "items": {"type": "string"}},
        "field_confidence": {
            "type": "object",
            "additionalProperties": False,
            "required": ["vendor_name", "invoice_number", "invoice_date", "po_reference", "total", "line_items"],
            "properties": {k: {"type": "string", "enum": ["high", "medium", "low"]}
                           for k in ["vendor_name", "invoice_number", "invoice_date", "po_reference", "total",
                                     "line_items"]},
        },
    },
}

SYSTEM = """You extract data from vendor invoices for an accounts-payable team.
Report only what is printed on the document. Never invent values: if a field is absent, blank, or illegible, return null
and mention it in notes. Blank placeholders such as "____" mean the field is missing.
- Dates: ISO format YYYY-MM-DD.
- po_reference: the purchase order number exactly as written (e.g. "PO 1005"), or null if none appears.
- line_items: one entry per billed line, amounts as printed. Put an item code / SKU in `sku` when one is shown
  (including codes in parentheses inside the description).
- If printed amounts already include tax, set tax_inclusive=true, set tax_amount to the stated tax, and set subtotal
  to the pre-tax amount if it can be derived from what is printed.
- notes: short factual observations an AP clerk would want (missing fields, "reminder copy", handwriting, tax treatment).
- field_confidence: how sure you are that each value was read correctly from the page."""


def claude_available():
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def gemini_available():
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def providers():
    """AI extractors to try, in order. Rules are always the last resort (handled by the pipeline)."""
    forced = os.environ.get("EXTRACTOR", "").lower()
    avail = [p for p, ok in (("claude", claude_available()), ("gemini", gemini_available())) if ok]
    if forced == "rules":
        return []
    if forced in avail:
        return [forced] + [p for p in avail if p != forced]
    return avail


def model_name(provider):
    return {"claude": MODEL, "gemini": GEMINI_MODEL}.get(provider, "rules")


def extract_with_gemini(pdf_bytes):
    from google import genai
    from google.genai import types

    # Short timeout and one quick retry per model, then the next model: a busy model
    # must never stall a live run for minutes.
    client = genai.Client(
        api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"),
        http_options=types.HttpOptions(
            timeout=GEMINI_TIMEOUT_MS,
            retry_options=types.HttpRetryOptions(attempts=1, initial_delay=1.0, max_delay=2.0,
                                                 http_status_codes=[429, 500, 503, 504]),
        ),
    )
    errors = []
    for model in GEMINI_MODELS:
        try:
            resp = client.models.generate_content(
                model=model,
                contents=[types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), "Extract this invoice."],
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM,
                    response_mime_type="application/json",
                    response_json_schema=SCHEMA,
                    temperature=0,
                ),
            )
            if not resp.text:
                reason = resp.candidates[0].finish_reason if resp.candidates else "no candidates"
                raise RuntimeError(f"empty response ({reason})")
            data = json.loads(resp.text)
        except Exception as e:
            errors.append(f"{model}: {type(e).__name__}: {str(e)[:120]}")
            continue
        data["invoice_date"] = norm_date(data.get("invoice_date"))
        data["due_date"] = norm_date(data.get("due_date"))
        u = resp.usage_metadata
        meta = {"model": model, "input_tokens": getattr(u, "prompt_token_count", None),
                "output_tokens": getattr(u, "candidates_token_count", None)}
        if errors:
            meta["skipped_models"] = errors
        return data, meta
    raise RuntimeError("all Gemini models failed: " + " | ".join(errors))


def extract_with(provider, pdf_bytes):
    return {"claude": extract_with_claude, "gemini": extract_with_gemini}[provider](pdf_bytes)


def extract_with_claude(pdf_bytes):
    import anthropic

    client = anthropic.Anthropic()
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=8000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.standard_b64encode(pdf_bytes).decode()}},
                {"type": "text", "text": "Extract this invoice."},
            ],
        }],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("extraction output was truncated")
    text = next(b.text for b in resp.content if b.type == "text")
    data = json.loads(text)
    data["invoice_date"] = norm_date(data.get("invoice_date"))
    data["due_date"] = norm_date(data.get("due_date"))
    meta = {"model": resp.model, "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    return data, meta


# ---------------------------------------------------------------- rules
DATE = r"(\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|[A-Z][a-z]{2,8}\.? \d{1,2},? \d{4}|\d{1,2} [A-Z][a-z]{2,8} \d{4})"
AMT = r"\$?\s*([\d,]+\.\d{2})"


def norm_date(s):
    if not s:
        return None
    s = s.strip().replace(".", "") if not re.match(r"\d", s) else s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%d.%m.%Y", "%b %d, %Y", "%b %d %Y", "%B %d, %Y",
                "%B %d %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _f(s):
    return float(s.replace(",", "")) if s else None


def _first(pattern, text, flags=re.I | re.M):
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


LINE_RE = re.compile(
    r"^\s*(?:(?P<sku>[A-Z0-9][A-Z0-9\-]{2,}|-)\s{2,})?(?P<desc>\S.*?)\s{2,}(?P<qty>\d+(?:\.\d+)?)\s{2,}"
    r"\$?(?P<price>[\d,]+\.\d{2})\s{2,}\$?(?P<amt>[\d,]+\.\d{2})\s*$")


def extract_with_rules(text):
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    notes = []
    vendor = re.split(r"\s{3,}", lines[0].strip())[0] if lines else None

    inv = _first(r"(?:Invoice\s*(?:No\.?|Number|#)|Bill\s*No\.?|Inv\.?\s*#)\s*[:#]?\s*([A-Z0-9][A-Z0-9/-]*\d[A-Z0-9/-]*)", text)
    inv_date = _first(rf"(?<!Due )(?<!due )(?:Invoice\s*Date|Issued|Date\s*of\s*issue|(?<![A-Za-z])Date)\s*:?\s+{DATE}", text)
    due = _first(rf"(?:Due\s*Date|Payment\s*due|(?<![A-Za-z])Due)\s*:?\s+{DATE}", text)
    po = _first(r"(?:P\.?O\.?\s*(?:Number|No\.?|#|Ref)?|Purchase\s*Order(?:\s*(?:No\.?|Number|#))?|Order\s*Ref(?:erence)?)"
                r"\s*[:#]?\s+(PO[-\s]?\d{3,}|\d{4,})", text)
    tax_id = _first(r"Tax\s*ID\s*:?\s*(\d{2}-\d{7})", text)
    subtotal = _f(_first(rf"Sub\s*-?\s*total\s*:?\s+{AMT}", text))
    tax = None
    for l in lines:
        if re.search(r"\b(tax|vat|gst)\b", l, re.I) and not re.search(r"total|tax\s*id", l, re.I):
            m = re.search(rf"{AMT}\s*$", l)
            if m:
                tax = _f(m.group(1))
    total = None
    for l in lines:
        if re.search(r"(total\s*due|amount\s*due|balance\s*due|grand\s*total|^\s*total\b)", l, re.I) and \
                not re.search(r"sub\s*-?\s*total", l, re.I):
            m = re.search(rf"{AMT}\s*$", l)
            if m:
                total = _f(m.group(1))

    items = []
    for l in lines:
        m = LINE_RE.match(l)
        if not m or re.match(r"\s*(sub)?total", m.group("desc"), re.I):
            continue
        desc, sku = m.group("desc").strip(), m.group("sku")
        if sku == "-":
            sku = None
        paren = re.search(r"\(([A-Z0-9][A-Z0-9-]{2,})\)\s*$", desc)
        if paren and not sku:
            sku, desc = paren.group(1), desc[:paren.start()].strip()
        items.append({"sku": sku, "description": desc, "quantity": float(m.group("qty")),
                      "unit_price": _f(m.group("price")), "amount": _f(m.group("amt"))})

    if not inv:
        notes.append("No invoice number found on the document.")
    if not po:
        notes.append("No purchase order reference found.")
    found = lambda v: "medium" if v else "low"
    return {
        "vendor_name": vendor.title() if vendor and vendor.isupper() else vendor,
        "vendor_tax_id": tax_id,
        "invoice_number": inv,
        "invoice_date": norm_date(inv_date),
        "due_date": norm_date(due),
        "po_reference": po,
        "currency": "USD" if ("$" in text or "USD" in text) else None,
        "line_items": items,
        "subtotal": subtotal,
        "tax_amount": tax,
        "tax_inclusive": bool(re.search(r"incl(\.|uding|ude)?\s*(\d+%\s*)?(sales\s*)?tax", text, re.I)),
        "total": total,
        "notes": notes,
        "field_confidence": {"vendor_name": found(vendor), "invoice_number": found(inv),
                             "invoice_date": found(inv_date), "po_reference": found(po), "total": found(total),
                             "line_items": found(items)},
    }
