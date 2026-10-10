/* ForecastLaya — Laya AI outlook section for the forecast page.
 *
 * Renders GET /api/forecast/laya: one row per catalogue SKU with the SAME
 * validated card payloads the chatbot uses (medicine, analysis date, status
 * badge, expected timing, predicted quantity band, trajectory, model
 * probabilities when the backend returned valid ones).
 *
 * Division of labour: the BACKEND runs every prediction and every calculation
 * and reports engine/row failures honestly; this component only formats and
 * displays supplied values. Missing values render as an explicit dash (never
 * zero), a status badge appears only when the backend supplied one,
 * probabilities render only when returned, and an errored SKU shows the
 * backend's reason — never an invented prediction.
 *
 * Standalone on purpose: no dependencies on app.js, so it loads before
 * app.js and unit-tests under Node (module.exports).
 */
"use strict";

const FPL_ERRORS = {
  engine_unavailable: "Laya engine unavailable",
  timeout: "Prediction timed out",
  malformed: "Laya returned malformed output",
  no_data: "No prediction for this state",
  state_unavailable: "State unavailable",
  prediction_invalid: "Prediction could not be presented",
  error: "Prediction failed",
};

const FPL_TRAJ_GLYPH = { spiking: "⚡", rising: "↗", stable: "→", falling: "↘" };
const FPL_TONE = { reorder_soon: "soon", monitor: "monitor", none: "none" };
/* Compact status wording for the outlook TABLE column: the chat card keeps
 * the full sentence, but a table chip must fit its column. The backend's
 * full label stays available on the badge's title attribute, so the exact
 * wording is never lost - only shortened for the column. */
const FPL_STATUS_SHORT = { reorder_soon: "Reorder soon", monitor: "Monitor", none: "No action" };

const FPL_FOOTER =
  "Predicted quantity bands are model-predicted ranges — never an exact order " +
  "quantity. Every prediction is recomputed from the latest state when this " +
  "page loads, and no reorder is placed automatically.";

/* Same escaping as app.js's esc(); inlined so the component is standalone. */
const fplEsc = (v) => String(v ?? "").replace(/[&<>\"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* Probability chip: display only a finite 0..1 number (the backend has
 * already validated Laya's output; this is a second safety net). */
const fplProb = (p) =>
  (typeof p === "number" && Number.isFinite(p) && p >= 0 && p <= 1) ? p : null;

const fplChip = (p) => {
  const v = fplProb(p);
  return v === null ? "" : `<span class="fpl-prob">p=${v}</span>`;
};

const fplNa = '<span class="fpl-na">—</span>';

function ForecastLaya(payload) {
  if (!payload || typeof payload !== "object") {
    return `<div class="fpl"><div class="fpl-bar">
      <span class="fpl-engine" data-status="unknown">Laya outlook</span>
      <span class="fpl-meta">response unavailable</span></div>
      <div class="fpl-empty">Laya outlook could not be loaded for this page.</div></div>`;
  }
  const eng = payload.engine || {};
  const meta = [];
  if (payload.generated_at) meta.push(`computed ${fplEsc(payload.generated_at)}`);
  meta.push("latest state");
  const rows = Array.isArray(payload.rows) ? payload.rows : [];

  const body = rows.length ? rows.map((row, i) => {
    const med = fplEsc(row.drug || "");
    const when = row.as_of ? fplEsc(row.as_of) : fplNa;
    if (row.error) {
      let msg = FPL_ERRORS[row.error] || fplEsc(row.error);
      const detail = String(row.error_detail || "").trim();
      if (detail && detail !== msg) msg += ` — ${fplEsc(detail)}`;
      return `<tr><td class="fpl-med">${med}</td><td class="fpl-date">${when}</td>
        <td colspan="5" class="fpl-err">${msg}</td></tr>`;
    }
    const c = row.card || {};
    const o = c.outlook || {};
    const st = c.status;
    /* Same badge vocabulary as the chat's DemandForecastCard (dfc-badge--<tone>),
       shortened for the column; the full backend label rides on title=. */
    const stFull = st && typeof st.label === "string" ? st.label.trim() : "";
    const stShort = st ? (FPL_STATUS_SHORT[st.key] || stFull) : "";
    const badge = st && FPL_TONE[st.key] && stShort
      ? `<span class="dfc-badge dfc-badge--${FPL_TONE[st.key]}"` +
        (stFull ? ` title="${fplEsc(stFull)}"` : "") +
        `>${fplEsc(stShort)}</span>`
      : fplNa;
    const t = o.trajectory || {};
    const traj = t.value
      ? `<span class="fpl-traj" data-traj="${fplEsc(t.value)}">${FPL_TRAJ_GLYPH[t.value] || ""} ${fplEsc(t.label || t.value)}</span>`
      : fplNa;
    const hasDetail = Boolean(row.card || row.trajectory);
    return `<tr>
      <td class="fpl-med">${med}</td>
      <td class="fpl-date">${when}</td>
      <td class="fpl-st">${badge}</td>
      <td>${traj}</td>
      <td>${o.timing && o.timing.label ? fplEsc(o.timing.label) : fplNa}${fplChip(o.timing && o.timing.prob)}</td>
      <td>${o.band && o.band.label ? fplEsc(o.band.label) : fplNa}${fplChip(o.band && o.band.prob)}</td>
      <td class="fpl-act">${hasDetail ? `<button class="btn sm" data-fpl="${i}">Details</button>` : ""}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="7" class="fpl-empty">No SKUs in the catalogue.</td></tr>`;

  return `<div class="fpl">
    <div class="fpl-bar">
      <span class="fpl-engine" data-status="${fplEsc(eng.status || "unknown")}">Laya ${fplEsc(eng.status || "unknown")}</span>
      ${eng.status !== "ready" && eng.detail ? `<span class="fpl-eng-detail">${fplEsc(eng.detail)}</span>` : ""}
      <span class="fpl-meta">${meta.join(" · ")}</span>
    </div>
    <div class="fpl-twrap"><table>
      <thead><tr><th>Medicine</th><th>Analysis date</th><th class="fpl-st">Status</th><th>Trajectory</th>
        <th>Expected timing</th><th>Predicted band</th><th></th></tr></thead>
      <tbody>${body}</tbody>
    </table></div>
    <div class="fpl-foot">${FPL_FOOTER}</div>
  </div>`;
}

/* ------------------------------------------------------------------ *
 * Editable order quantity (the "Order qty" line of the cell).
 *
 * Laya's band floor is the PRE-FILLED HINT, not a constraint: the operator
 * can click the line and type any quantity to order (free number entry,
 * 0 allowed, rounded to whole units). Overrides live in this module's Map
 * for the lifetime of the page - transient by design, never posted to the
 * backend by this component, so a reload starts from Laya's number again.
 *
 * The renderer stays pure HTML (Node-testable, no DOM at load time);
 * app.js calls wireLayaQtyEdit(view) once per render to attach the editor.
 * ------------------------------------------------------------------ */
const LAYA_QTY_SET = new Map(); // drug_id -> whole units typed by the operator

/* Override keys are always finite numbers as strings, so a payload id can
 * never leak into a data- attribute unescaped (and junk ids just disable
 * editing instead of rendering). */
const qtyKey = (id) => {
  if (id === null || id === undefined || id === "") return null;
  const n = Number(id);
  return Number.isFinite(n) ? String(n) : null;
};

/* Free entry: any non-negative number, rounded to whole units. Invalid or
 * negative input yields null and the caller keeps the previous value. */
const parseLayaQty = (raw) => {
  if (raw === null || raw === undefined) return null;
  const s = String(raw).trim();
  if (!s) return null;
  const n = Number(s);
  return Number.isFinite(n) && n >= 0 ? Math.round(n) : null;
};

const setLayaQty = (id, raw) => {
  const key = qtyKey(id), n = parseLayaQty(raw);
  if (key === null || n === null) return false;
  LAYA_QTY_SET.set(key, n);
  return true;
};

const clearLayaQty = (id) => {
  if (id === undefined) { LAYA_QTY_SET.clear(); return; }
  const key = qtyKey(id);
  if (key !== null) LAYA_QTY_SET.delete(key);
};

const lqv = (id) => {
  const key = qtyKey(id);
  return key !== null && LAYA_QTY_SET.has(key) ? LAYA_QTY_SET.get(key) : null;
};

const lqFmt = (n) => Math.round(n).toLocaleString("en-IN");

/* One label for both render paths (component and inline editor), so a
 * re-render can never disagree with what the editor just wrote. */
const layaQtyLabel = (id, suggest, open) => {
  const over = lqv(id);
  if (over !== null) return `Order qty ${lqFmt(over)}`;
  const s = typeof suggest === "number" && Number.isFinite(suggest) && suggest > 0
    ? Math.round(suggest) : null;
  if (s === null) return `Order qty \u2014`;
  return `Order qty ${open === true ? "\u2265" : ""}${lqFmt(s)}`;
};

const layaQtyTitle = (id, suggest, open) => {
  const over = lqv(id);
  const s = typeof suggest === "number" && Number.isFinite(suggest) && suggest > 0
    ? Math.round(suggest) : null;
  if (over !== null) {
    return s === null
      ? "Quantity you set for this page \u2014 Laya had no suggestion"
      : `Quantity you set for this page \u2014 Laya's floor was ${open === true ? "\u2265" : ""}${lqFmt(s)}`;
  }
  if (s === null) return "No Laya suggestion \u2014 click to type the quantity to order";
  return "Floor of Laya's predicted band \u2014 click to edit the quantity to order";
};

/* The in-place editor: a plain number input replaces the qty line while
 * editing (band, probabilities, trajectory and act-by stay visible around
 * it); Enter/blur commits, Escape reverts. */
const layaQtyInputHtml = (value) =>
  `<input class="lsg-qty-input" type="number" inputmode="numeric" min="0" step="1"` +
  (value === null || value === undefined ? "" : ` value="${Math.round(value)}"`) +
  ` aria-label="Order quantity to order" />`;

/* Repaint one qty line from the store + the row's suggestion, so the line
 * after an edit is EXACTLY what a fresh render would produce (label,
 * tooltip and the set/empty markers can never drift apart). */
function lqPaint(el, key, suggest, open) {
  const hasValue = lqv(key) !== null ||
    (typeof suggest === "number" && Number.isFinite(suggest) && suggest > 0);
  el.classList.toggle("lsg-qty-set", lqv(key) !== null);
  el.classList.toggle("lsg-qty-empty", !hasValue);
  el.textContent = layaQtyLabel(key, suggest, open);
  el.setAttribute("title", layaQtyTitle(key, suggest, open));
}

function lqStartEdit(el) {
  if (!el || el.dataset.lsgEditing === "1") return;
  const key = qtyKey(el.dataset.lsgQty);
  const suggest = el.dataset.lsgSuggest ? Number(el.dataset.lsgSuggest) : null;
  const open = el.dataset.lsgOpen === "1";
  const over = lqv(key);
  const prefill = over !== null ? over
    : (suggest !== null && Number.isFinite(suggest) && suggest > 0 ? Math.round(suggest) : null);

  el.dataset.lsgEditing = "1";
  el.classList.add("lsg-qty-editing");
  el.innerHTML = layaQtyInputHtml(prefill);
  const input = el.querySelector("input");
  if (!input) { // no editor possible: restore the line untouched
    delete el.dataset.lsgEditing;
    el.classList.remove("lsg-qty-editing");
    lqPaint(el, key, suggest, open);
    return;
  }
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    if (save) setLayaQty(key, input.value); // junk input keeps the old value
    delete el.dataset.lsgEditing;
    el.classList.remove("lsg-qty-editing");
    lqPaint(el, key, suggest, open);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); finish(true); el.focus(); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); el.focus(); }
  });
  input.addEventListener("blur", () => finish(true));
  input.focus();
  if (prefill !== null) input.select();
}

/* Called by app.js after each render of the suggestions table. */
function wireLayaQtyEdit(root) {
  if (!root || typeof root.querySelectorAll !== "function") return;
  root.querySelectorAll("[data-lsg-qty]").forEach((el) => {
    el.addEventListener("click", () => lqStartEdit(el));
    el.addEventListener("keydown", (e) => {
      if (e.target !== el) return; // the input owns its own keys
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); lqStartEdit(el); }
    });
  });
}

/* Laya units + EDITABLE order quantity + act-by deadline for ONE row of
 * "AI reorder suggestions".
 *
 * The forecast page already loads both the reorder suggestions and the Laya
 * outlook, so app.js joins them by drug_id and renders this cell next to the
 * inventory engine's Order qty / Due columns. The cell states Laya's own
 * numbers automatically: the predicted quantity BAND (a range), the order
 * quantity derived from that band's floor (flagged as a minimum when the
 * top band is open-ended) - pre-filled as the starting hint for the
 * operator's editable quantity - and the act-by deadline derived from
 * Laya's reorder-timing window. The line is editable for EVERY row: with a
 * band (hint pre-filled) or without one (explicit dash, typed from
 * scratch), so a failed prediction never blocks ordering that SKU. A
 * quantity is only ever shown when the backend supplied it or the operator
 * typed it - never parsed, never invented here.
 *
 * Optional second argument: the drug id to key the override on when the
 * Laya row itself is missing (app.js always passes it).
 */
function LayaSuggestionCell(row, drugId) {
  const isObj = Boolean(row) && typeof row === "object";
  const id = isObj && row.drug_id !== undefined && row.drug_id !== null ? row.drug_id : drugId;
  const key = qtyKey(id);
  const hasCard = isObj && !row.error && Boolean(row.card);
  const suggest = hasCard && typeof row.order_qty === "number" &&
    Number.isFinite(row.order_qty) && row.order_qty > 0 ? Math.round(row.order_qty) : null;
  const open = hasCard && row.order_qty_open === true;
  const over = key !== null && LAYA_QTY_SET.has(key);

  const qtyAttrs = key === null ? ""
    : ` data-lsg-qty="${key}" role="button" tabindex="0"` +
      (suggest !== null ? ` data-lsg-suggest="${suggest}"` : "") +
      (open ? " data-lsg-open=\"1\"" : "");
  const qtyHtml = `<span class="lsg-qty${over ? " lsg-qty-set" : suggest === null ? " lsg-qty-empty" : ""}"` +
    `${qtyAttrs} title="${fplEsc(layaQtyTitle(key, suggest, open))}">` +
    `${fplEsc(layaQtyLabel(key, suggest, open))}</span>`;

  if (!hasCard) {
    /* Failed / missing prediction: no band and no date to show, but the
       order quantity stays typeable so the operator can still order. */
    return key === null ? fplNa : `<span class="lsg">${qtyHtml}</span>`;
  }

  const o = row.card.outlook || {};
  const band = o.band && o.band.label;
  const t = o.trajectory || {};
  const meta = [];
  if (t.value) meta.push(`${FPL_TRAJ_GLYPH[t.value] || ""} ${fplEsc(t.label || t.value)}`);
  if (row.reorder_by) meta.push(`Act by ${fplEsc(row.reorder_by)}`);
  const bandHtml = band ? `<span class="lsg-band">${fplEsc(band)}</span>${fplChip(o.band && o.band.prob)}` : fplNa;
  const metaHtml = meta.length ? `<span class="lsg-meta">${meta.join(" · ")}</span>` : "";
  return `<span class="lsg"${t.value ? ` data-traj="${fplEsc(t.value)}"` : ""}>${bandHtml}${qtyHtml}${metaHtml}</span>`;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { ForecastLaya, LayaSuggestionCell, FPL_ERRORS, FPL_TRAJ_GLYPH, fplProb,
                     wireLayaQtyEdit, layaQtyInputHtml, layaQtyLabel, parseLayaQty,
                     setLayaQty, clearLayaQty };
}
