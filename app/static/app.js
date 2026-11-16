// Invoice Desk front-end: hash router, dashboard, new run, live run view, master data.
const $view = document.getElementById("view");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (x) => (x == null ? "—" : "$" + Number(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const api = async (path, opts = {}) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
};
const ago = (iso) => {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return new Date(iso).toLocaleDateString();
};
const toast = (msg) => {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2200);
};
const MODE_LABEL = { claude: "Claude", gemini: "Gemini", rules: "Rule-based" };
const ROUTE = { vendor: "Vendor", buyer: "Buyer", ap: "AP clerk" };
const DECISION_LABEL = { APPROVE: "Approved", HOLD: "On hold", REJECT: "Rejected" };
const decisionPill = (r, big) => {
  if (r.status !== "done") return `<span class="pill ${r.status} ${big ? "big" : ""}">${r.status === "error" ? "Error" : "Running…"}</span>`;
  return `<span class="pill ${r.decision} ${big ? "big" : ""}">${DECISION_LABEL[r.decision] || r.decision}</span>`;
};

let pollTimer = null;
let dashFilter = "all";
const runTab = {};

// ------------------------------------------------------------------ router
async function route() {
  clearTimeout(pollTimer);
  const h = location.hash || "#/";
  const [, page, id] = h.split("/");
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active",
    a.dataset.nav === (page === "" ? "dashboard" : page === "run" ? "dashboard" : page)));
  try {
    if (page === "new") await renderNew();
    else if (page === "run" && id) await renderRun(id, true);
    else if (page === "data") await renderData();
    else await renderDashboard();
  } catch (e) {
    $view.innerHTML = `<div class="card empty">Something went wrong: ${esc(e.message)}</div>`;
  }
}
window.addEventListener("hashchange", route);

// ------------------------------------------------------------------ status
async function loadStatus() {
  const s = await api("/api/status");
  const name = MODE_LABEL[s.provider];
  document.getElementById("mode-badge").innerHTML = s.provider !== "rules"
    ? `<b><span class="dot" style="background:var(--ok)"></span>AI extraction on (${name})</b>${esc(s.model)} reads each PDF; rules decide.`
    : `<b><span class="dot" style="background:var(--warn)"></span>Rules-only mode</b>Add GEMINI_API_KEY or ANTHROPIC_API_KEY to .env to enable AI extraction (needed for scans).`;
}
document.getElementById("reset-btn").onclick = async () => {
  if (!confirm("Clear all runs and restore the seed PO ledger?")) return;
  await api("/api/reset", { method: "POST" });
  toast("Demo data reset");
  location.hash = "#/";
  route();
};

// ------------------------------------------------------------------ dashboard
async function renderDashboard() {
  const runs = await api("/api/runs");
  const done = runs.filter((r) => r.status === "done");
  const by = (d) => done.filter((r) => r.decision === d);
  const sum = (arr) => arr.reduce((a, r) => a + (r.total || 0), 0);
  const auto = done.filter((r) => r.decision === "APPROVE" && !r.review).length;
  const queue = done.filter((r) => r.decision === "HOLD" && !r.review);
  const shown = runs.filter((r) => dashFilter === "all" || (dashFilter === "queue" ? r.decision === "HOLD" && !r.review : r.decision === dashFilter));

  $view.innerHTML = `
    <div class="page-head">
      <div><h1>Dashboard</h1><p>Every invoice processed, what was decided, and why.</p></div>
      <a class="btn primary" href="#/new">＋ Process an invoice</a>
    </div>
    <div class="grid kpis">
      ${kpi("Invoices processed", runs.length, `${runs.length - done.length} in progress`)}
      ${kpi("Approved", by("APPROVE").length, money(sum(by("APPROVE"))))}
      ${kpi("Waiting for review", queue.length, money(sum(queue)) + " held")}
      ${kpi("Rejected", by("REJECT").length, money(sum(by("REJECT"))) + " stopped")}
      ${kpi("Straight-through rate", done.length ? Math.round((auto / done.length) * 100) + "%" : "—", "approved with no human touch")}
    </div>
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px">
        <h2 style="margin:0">Run history</h2>
        <div class="chips">
          ${[["all", "All"], ["queue", `Review queue (${queue.length})`], ["APPROVE", "Approved"], ["HOLD", "On hold"], ["REJECT", "Rejected"]]
            .map(([k, l]) => `<span class="chip ${dashFilter === k ? "active" : ""}" data-f="${k}">${l}</span>`).join("")}
        </div>
      </div>
      ${shown.length ? `<div class="table-wrap"><table>
        <thead><tr><th>When</th><th>Document</th><th>Vendor</th><th>Invoice #</th><th>PO</th><th class="num">Total</th><th>Decision</th><th>Routed to</th><th>Extractor</th></tr></thead>
        <tbody>${shown.map((r) => `
          <tr class="click" data-id="${r.id}">
            <td class="muted small">${ago(r.created_at)}</td>
            <td><div style="font-weight:550">${esc(r.filename)}</div></td>
            <td>${esc(r.vendor || "—")}</td>
            <td class="mono">${esc(r.invoice_number || "—")}</td>
            <td class="mono">${esc(r.po_number || "—")}</td>
            <td class="num">${money(r.total)}</td>
            <td>${decisionPill(r)}${r.review ? ` <span class="faint small">reviewed</span>` : ""}</td>
            <td class="small">${r.decision === "HOLD" && !r.review ? (r.route || []).map((x) => ROUTE[x]).join(", ") : "<span class='faint'>—</span>"}</td>
            <td class="small muted">${MODE_LABEL[r.mode] || "—"}</td>
          </tr>`).join("")}</tbody></table></div>`
      : `<div class="empty">${runs.length ? "Nothing matches this filter." : `No invoices yet. <a href="#/new">Process your first one →</a>`}</div>`}
    </div>`;
  $view.querySelectorAll(".chip").forEach((c) => (c.onclick = () => { dashFilter = c.dataset.f; renderDashboard(); }));
  $view.querySelectorAll("tr[data-id]").forEach((tr) => (tr.onclick = () => (location.hash = "#/run/" + tr.dataset.id)));
  if (runs.some((r) => r.status === "running" || r.status === "queued")) pollTimer = setTimeout(renderDashboard, 1000);
}
const kpi = (label, value, sub) => `<div class="card kpi"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;

// ------------------------------------------------------------------ new run
async function renderNew() {
  const samples = await api("/api/samples");
  $view.innerHTML = `
    <div class="page-head"><div><h1>Process an invoice</h1><p>Upload a vendor PDF, or run one of the prepared demo scenarios.</p></div></div>
    <label class="drop" id="drop">
      <input type="file" id="file" accept="application/pdf" hidden>
      <div class="big">Drop an invoice PDF here or click to choose</div>
      <div class="muted small" style="margin-top:6px">Text PDFs and scanned images are both accepted. The run starts immediately.</div>
    </label>
    <h2 style="margin:28px 0 12px">Demo scenarios</h2>
    <div class="grid samples">
      ${samples.map((s) => `
        <div class="card sample">
          <div class="row"><span class="t">${esc(s.title)}</span><a class="tag" href="/api/samples/${s.name}" target="_blank" title="Open PDF">${esc(s.name.slice(0, 2))} ↗</a></div>
          <div class="d">${esc(s.description)}</div>
          <div class="row"><span class="small faint">Expected: ${esc(s.expected)}</span><button class="btn small primary" data-s="${s.name}">Run ▸</button></div>
        </div>`).join("")}
    </div>`;
  const drop = document.getElementById("drop");
  const input = document.getElementById("file");
  const send = async (f) => {
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    try {
      const { id } = await api("/api/runs", { method: "POST", body: fd });
      location.hash = "#/run/" + id;
    } catch (e) { toast(e.message); }
  };
  input.onchange = () => send(input.files[0]);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); send(e.dataTransfer.files[0]); };
  $view.querySelectorAll("[data-s]").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    const { id } = await api("/api/runs/sample/" + b.dataset.s, { method: "POST" });
    location.hash = "#/run/" + id;
  }));
}

// ------------------------------------------------------------------ run view
async function renderRun(id, first) {
  const r = await api("/api/runs/" + id);
  if (location.hash !== "#/run/" + id) return;
  const live = r.status === "queued" || r.status === "running";
  const res = r.result || {};
  const stages = r.stages || [];
  const extractStage = stages.find((s) => s.key === "extract");
  const poStage = stages.find((s) => s.key === "po_match");
  const inv = res.invoice || extractStage?.details?.fields;
  if (!runTab[id]) runTab[id] = "decision";
  const tab = runTab[id];

  const scrollY = window.scrollY;
  $view.innerHTML = `
    <div class="run-head">
      <div>
        <div class="muted small"><a href="#/">Dashboard</a> / Run <span class="mono">${esc(r.id)}</span> · ${ago(r.created_at)} · ${MODE_LABEL[r.mode] ? MODE_LABEL[r.mode] + " extraction" : ""}</div>
        <h1 style="margin-top:6px">${esc(r.filename)}</h1>
        <div class="summary">${live ? `<span class="muted">Processing… each stage appears as it runs.</span>` : esc(r.summary || "")}</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center">${decisionPill(r, true)}</div>
    </div>
    ${!live && r.decision === "HOLD" && !r.review && res.vendor && res.po ? reviewBox() : ""}
    ${!live && r.decision === "HOLD" && !r.review && !(res.vendor && res.po) ? `<div class="review-box"><span>Held for manual handling. It can't be approved here until vendor and PO are resolved.</span><button class="btn small bad" data-rev="reject">Reject invoice</button></div>` : ""}
    ${r.review ? `<div class="reviewed"><b>${r.review.action === "approve" ? "Approved" : "Rejected"} by ${esc(r.review.reviewer)}</b> · ${ago(r.review.at)}${r.review.note ? ` · “${esc(r.review.note)}”` : ""}</div>` : ""}
    <div class="run-grid">
      <div class="card">
        <h3>Pipeline</h3>
        <ol class="steps">${stages.map(stepHtml).join("")}</ol>
      </div>
      <div class="card">
        <div class="tabs">
          ${[["decision", "Decision & actions"], ["fields", "Extracted data"], ["po", "PO match"], ["doc", "Document"]]
            .map(([k, l]) => `<div class="tab ${tab === k ? "active" : ""}" data-tab="${k}">${l}</div>`).join("")}
        </div>
        <div id="tab-body">${
          tab === "fields" ? fieldsTab(inv, extractStage)
          : tab === "po" ? poTab(poStage)
          : tab === "doc" ? `<iframe class="doc" src="/api/runs/${r.id}/file"></iframe>`
          : decisionTab(r, res, live)}</div>
      </div>
    </div>`;
  if (!first) window.scrollTo(0, scrollY);
  $view.querySelectorAll(".tab").forEach((t) => (t.onclick = () => { runTab[id] = t.dataset.tab; renderRun(id); }));
  $view.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = () => {
    navigator.clipboard.writeText(b.closest(".action").querySelector("pre").innerText).then(() => toast("Copied"));
  }));
  $view.querySelectorAll("[data-rev]").forEach((b) => (b.onclick = async () => {
    const note = document.getElementById("rev-note")?.value || "";
    try {
      await api(`/api/runs/${id}/review`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: b.dataset.rev, note }) });
      toast(b.dataset.rev === "approve" ? "Approved and posted to PO ledger" : "Rejected");
      renderRun(id);
    } catch (e) { toast(e.message); }
  }));
  if (live) pollTimer = setTimeout(() => renderRun(id), 450);
}

const reviewBox = () => `
  <div class="review-box">
    <b>Reviewer decision:</b>
    <input id="rev-note" placeholder="Note for the audit trail (e.g. 'surcharge agreed with buyer')">
    <button class="btn small ok" data-rev="approve">Approve anyway</button>
    <button class="btn small bad" data-rev="reject">Reject</button>
  </div>`;

function stepHtml(s, i) {
  const icon = s.status === "done" ? ({ ok: "✓", warn: "!", hold: "!", reject: "✕" }[s.flag] || "✓")
    : s.status === "skipped" ? "–" : s.status === "error" ? "✕" : i + 1;
  const checks = (s.checks || []).filter((c) => c.status !== "pass" || (s.checks || []).length <= 6);
  return `<li class="step ${s.status} flag-${s.flag || ""}">
    <div class="icon">${icon}</div>
    <div class="name"><span>${esc(s.label)}</span>${s.duration_ms != null ? `<span class="dur">${s.duration_ms < 1000 ? s.duration_ms + "ms" : (s.duration_ms / 1000).toFixed(1) + "s"}</span>` : s.status === "running" ? `<span class="dur">running</span>` : ""}</div>
    ${s.summary ? `<div class="sum">${esc(s.summary)}</div>` : ""}
    ${checks.length ? `<ul class="checks">${checks.map((c) => `
      <li class="c-${c.status} o-${c.outcome}"><span class="ci">${{ pass: "✓", warn: "!", fail: c.outcome === "reject" ? "✕" : "!", info: "i" }[c.status]}</span>
      <span><b>${esc(c.label)}</b> · ${esc(c.detail)}${c.outcome !== "none" ? ` <span class="pill ${c.outcome === "reject" ? "bad" : "warn"}" style="font-size:10.5px;padding:0 6px">${c.outcome === "reject" ? "reject" : "hold → " + (ROUTE[c.route] || "review")}</span>` : ""}</span></li>`).join("")}</ul>` : ""}
  </li>`;
}

function decisionTab(r, res, live) {
  if (live) return `<div class="empty">The decision and drafted next steps appear here when the run finishes.</div>`;
  if (r.status === "error") return `<div class="empty">The pipeline hit an error.<pre class="json" style="text-align:left">${esc(res.error || r.summary)}</pre></div>`;
  const reasons = (res.checks || []).filter((c) => c.outcome !== "none");
  const warns = (res.checks || []).filter((c) => c.outcome === "none" && (c.status === "warn" || c.status === "info"));
  return `
    <h3>Why</h3>
    ${reasons.length ? `<ul class="checks" style="margin-bottom:14px">${reasons.map((c) => `<li class="c-fail o-${c.outcome}"><span class="ci">${c.outcome === "reject" ? "✕" : "!"}</span><span><b>${esc(c.label)}</b> · ${esc(c.detail)}</span></li>`).join("")}</ul>`
      : `<p class="muted" style="margin-top:0">All ${res.checks?.length || 0} checks passed: vendor approved, not a duplicate, arithmetic correct, every line within PO price and quantity.</p>`}
    ${warns.length ? `<ul class="checks" style="margin-bottom:14px">${warns.map((c) => `<li class="c-${c.status}"><span class="ci">${c.status === "warn" ? "!" : "i"}</span><span><b>${esc(c.label)}</b> · ${esc(c.detail)}</span></li>`).join("")}</ul>` : ""}
    <h3 style="margin-top:18px">Next actions</h3>
    ${(res.actions || []).map((a) => `
      <div class="action">
        <div class="at"><span>${{ email: "✉", task: "☐", payment: "＄", post: "▤" }[a.type] || "•"} ${esc(a.title)}</span>${a.body && (a.type === "email" || a.type === "task") ? `<button class="btn small ghost" data-copy>Copy</button>` : ""}</div>
        ${a.to ? `<div class="meta">To: ${esc(a.to)} · Subject: ${esc(a.subject)}</div>` : ""}
        ${a.type === "email" || a.type === "task" ? `<pre>${esc(a.body)}</pre>` : `<div class="meta">${esc(a.body)}</div>`}
      </div>`).join("") || `<p class="muted">None.</p>`}`;
}

function fieldsTab(inv, st) {
  if (!inv) return `<div class="empty">Waiting for extraction…</div>`;
  const conf = inv.field_confidence || {};
  const c = (k) => (conf[k] ? `<span class="conf ${conf[k]}">${conf[k]}</span>` : "");
  const v = (x) => (x == null || x === "" ? `<span class="faint">not found</span>` : esc(x));
  const det = st?.details || {};
  return `
    <dl class="kv">
      <dt>Vendor</dt><dd>${v(inv.vendor_name)}${c("vendor_name")}</dd>
      <dt>Vendor tax ID</dt><dd class="mono">${v(inv.vendor_tax_id)}</dd>
      <dt>Invoice number</dt><dd class="mono">${v(inv.invoice_number)}${c("invoice_number")}</dd>
      <dt>Invoice date</dt><dd>${v(inv.invoice_date)}${c("invoice_date")}</dd>
      <dt>Due date</dt><dd>${v(inv.due_date)}</dd>
      <dt>PO reference</dt><dd class="mono">${v(inv.po_reference)}${c("po_reference")}</dd>
      <dt>Subtotal / tax</dt><dd>${money(inv.subtotal)} / ${money(inv.tax_amount)}${inv.tax_inclusive ? ` <span class="pill neutral">tax included in prices</span>` : ""}</dd>
      <dt>Total</dt><dd><b>${money(inv.total)}</b> ${esc(inv.currency || "")}${c("total")}</dd>
    </dl>
    <h3 style="margin-top:20px">Line items ${c("line_items")}</h3>
    ${(inv.line_items || []).length ? `<div class="table-wrap"><table><thead><tr><th>SKU</th><th>Description</th><th class="num">Qty</th><th class="num">Unit</th><th class="num">Amount</th></tr></thead><tbody>
      ${inv.line_items.map((l) => `<tr><td class="mono">${esc(l.sku || "—")}</td><td>${esc(l.description)}</td><td class="num">${l.quantity}</td><td class="num">${money(l.unit_price)}</td><td class="num">${money(l.amount)}</td></tr>`).join("")}
    </tbody></table></div>` : `<p class="muted">No line items extracted.</p>`}
    ${(inv.notes || []).length ? `<h3 style="margin-top:20px">Extractor notes</h3><ul class="note-list">${inv.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
    ${(det.ai_errors || []).map((e) => `<p class="small" style="color:var(--warn)">AI call failed, fell back: ${esc(e)}</p>`).join("")}
    ${det.input_tokens ? `<p class="small faint">Model ${esc(det.model)} · ${det.input_tokens} in / ${det.output_tokens} out tokens</p>` : ""}
    <details><summary>Raw extraction JSON</summary><pre class="json">${esc(JSON.stringify(inv, null, 2))}</pre></details>`;
}

function poTab(st) {
  const d = st?.details;
  if (!st || st.status === "pending" || st.status === "running") return `<div class="empty">Waiting for PO matching…</div>`;
  if (!d?.po) return `<div class="empty">${esc(st.summary || "No PO matched.")}</div>`;
  const pct = d.po.total ? Math.min(100, (d.billed_before / d.po.total) * 100) : 0;
  return `
    <dl class="kv">
      <dt>Purchase order</dt><dd><b class="mono">${esc(d.po.po_number)}</b> · ${esc(d.po.description)} <span class="pill neutral">${d.match === "inferred" ? "inferred from lines" : "referenced on invoice"}</span></dd>
      <dt>Buyer</dt><dd>${esc(d.po.buyer)}</dd>
      <dt>PO value</dt><dd>${money(d.po.total)} · ${money(d.billed_before)} billed before this invoice · <b>${money(d.remaining_before)}</b> remaining
        <div class="bar" style="margin-top:6px;max-width:260px"><i style="width:${pct}%"></i></div></dd>
    </dl>
    <h3 style="margin-top:20px">Line-by-line (net of tax)</h3>
    <div class="table-wrap"><table>
      <thead><tr><th>Invoice line</th><th>PO line</th><th class="num">Qty (billed before / PO)</th><th class="num">Unit vs PO</th><th class="num">Amount</th><th>Result</th></tr></thead>
      <tbody>${d.lines.map((l) => `<tr>
        <td>${esc(l.invoice_line)}<div class="mono faint">${esc(l.sku || "")}</div></td>
        <td>${l.po_line ? `#${l.po_line} ${esc(l.po_desc)}` : `<span class="faint">no match</span>`}</td>
        <td class="num">${l.qty}${l.po_line ? ` <span class="faint">(${l.billed_before} / ${l.po_qty})</span>` : ""}</td>
        <td class="num">${money(l.unit_price)}${l.po_line ? `<div class="faint">PO ${money(l.po_unit_price)}${l.price_var_pct ? ` (${l.price_var_pct > 0 ? "+" : ""}${l.price_var_pct}%)` : ""}</div>` : ""}</td>
        <td class="num">${money(l.amount)}</td>
        <td>${l.issues.length ? l.issues.map((x) => `<div class="issue">${esc(x)}</div>`).join("") : `<span class="okc">✓ match</span>`}</td>
      </tr>`).join("") || `<tr><td colspan="6" class="muted">No line items; matched on header amount.</td></tr>`}</tbody>
    </table></div>`;
}

// ------------------------------------------------------------------ master data
async function renderData() {
  const m = await api("/api/master");
  const vname = Object.fromEntries(m.vendors.map((v) => [v.vendor_id, v.name]));
  $view.innerHTML = `
    <div class="page-head"><div><h1>Purchase orders &amp; vendors</h1><p>The procurement data every invoice is checked against. Balances update as invoices are approved.</p></div></div>
    <h2>Purchase orders</h2>
    ${m.purchase_orders.map((p) => {
      const pct = p.total ? Math.min(100, (p.billed_net / p.total) * 100) : 0;
      return `<div class="card po-card">
        <div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:center">
          <div><b class="mono">${p.po_number}</b> · ${esc(p.description)}<div class="small muted">${esc(vname[p.vendor_id])} · buyer ${esc(p.buyer)} · issued ${p.issue_date}</div></div>
          <div style="text-align:right;min-width:200px"><div class="small">${money(p.billed_net)} of ${money(p.total)} billed · <b>${money(p.remaining)}</b> left</div>
          <div class="bar ${pct >= 99.9 ? "full" : ""}" style="margin-top:6px"><i style="width:${pct}%"></i></div></div>
        </div>
        <div class="table-wrap" style="margin-top:10px"><table><thead><tr><th>#</th><th>SKU</th><th>Description</th><th class="num">Qty billed / ordered</th><th class="num">Unit price</th><th class="num">Line value</th></tr></thead><tbody>
        ${p.lines.map((l) => `<tr><td>${l.line_no}</td><td class="mono">${l.sku}</td><td>${esc(l.description)}</td><td class="num">${l.billed_qty} / ${l.qty}</td><td class="num">${money(l.unit_price)}</td><td class="num">${money(l.amount)}</td></tr>`).join("")}
        </tbody></table></div></div>`;
    }).join("")}
    <h2 style="margin-top:28px">Vendor master</h2>
    <div class="card table-wrap"><table><thead><tr><th>ID</th><th>Name</th><th>Also known as</th><th>Tax ID</th><th>Terms</th><th>Status</th></tr></thead><tbody>
      ${m.vendors.map((v) => `<tr><td class="mono">${v.vendor_id}</td><td>${esc(v.name)}</td><td class="small muted">${esc(v.aliases.join(", "))}</td><td class="mono">${v.tax_id}</td><td>Net ${v.payment_terms_days}</td><td><span class="pill ${v.status === "approved" ? "ok" : "bad"}">${v.status}</span></td></tr>`).join("")}
    </tbody></table></div>
    <h2 style="margin-top:28px">Invoice ledger <span class="muted small" style="font-weight:400">(paid / approved invoices used for duplicate and balance checks)</span></h2>
    <div class="card table-wrap"><table><thead><tr><th>Vendor</th><th>Invoice #</th><th>Date</th><th>PO</th><th class="num">Net</th><th class="num">Total</th><th>Status</th><th>Source</th></tr></thead><tbody>
      ${m.ledger.map((l) => `<tr><td>${esc(vname[l.vendor_id] || l.vendor_id)}</td><td class="mono">${esc(l.invoice_number || "—")}</td><td>${l.invoice_date || "—"}</td><td class="mono">${l.po_number}</td><td class="num">${money(l.net_amount)}</td><td class="num">${money(l.total)}</td><td>${l.status}</td><td class="small muted">${l.source === "seed" ? "history" : l.source === "review" ? `<a href="#/run/${l.run_id}">reviewer approval</a>` : `<a href="#/run/${l.run_id}">auto-approved</a>`}</td></tr>`).join("")}
    </tbody></table></div>
    <h2 style="margin-top:28px">Decision policy</h2>
    <div class="card"><dl class="kv">
      <dt>Unit price tolerance</dt><dd>+${m.policy.unit_price_tolerance_pct}% over PO price per line</dd>
      <dt>PO balance tolerance</dt><dd>greater of ${m.policy.header_tolerance_pct}% or ${money(m.policy.header_tolerance_abs)} over remaining value</dd>
      <dt>Arithmetic slack</dt><dd>${money(m.policy.arith_tolerance)}</dd>
      <dt>Duplicate window</dt><dd>same amount (±${m.policy.duplicate_amount_pct}%) within ${m.policy.duplicate_window_days} days, or same normalised invoice number</dd>
      <dt>Vendor name match</dt><dd>≥${m.policy.vendor_match_threshold * 100}% similarity accepts · ${m.policy.vendor_review_threshold * 100}–${m.policy.vendor_match_threshold * 100}% holds · below rejects</dd>
      <dt>Max invoice age</dt><dd>${m.policy.max_invoice_age_days} days</dd>
    </dl></div>`;
}

loadStatus();
route();
