/* Node harness for static/forecast-laya.js — run by
 * tests/test_forecast_laya_page.py::test_js_component_formatting_and_validation.
 *
 * Verifies the display contract: format-only rendering of supplied metrics,
 * engine banner (ready/unavailable), one row per SKU, status badge only when
 * the backend supplied one, probability chips only when valid, explicit
 * error rows for failed SKUs (missing != zero, never an invented
 * prediction), band-is-a-range footer, and HTML escaping.
 */
"use strict";
const assert = require("node:assert");
const { ForecastLaya, LayaSuggestionCell, FPL_ERRORS,
         wireLayaQtyEdit, layaQtyInputHtml, layaQtyLabel, parseLayaQty,
         setLayaQty, clearLayaQty } = require("../static/forecast-laya.js");

const CARD = {
  type: "demand_forecast",
  medicine: "Dolo 650",
  analysis_date: "2025-11-30",
  state_id: "st_test",
  status: { key: "monitor", label: "Monitor" },
  outlook: {
    timing: { label: "8\u201314 days", prob: 0.97 },
    band: { label: "1001+ units", prob: 1.0 },
    trajectory: { value: "spiking", label: "Spiking", prob: 1.0 },
    band_note: "Model-predicted range, not an exact order quantity.",
  },
  drivers: {},
  interpretation: "Dolo 650 demand is currently classified as spiking.",
  low_confidence: [],
};

const TRAJ = { type: "demand_trajectory", medicine: "Dolo 650",
               analysis_date: "2025-11-30" };

const PAYLOAD = {
  engine: { status: "ready", detail: null },
  generated_at: "2026-10-09T12:00:00",
  state: "latest",
  rows: [
    { drug_id: 1, drug: "Dolo 650", as_of: "2025-11-30",
      card: CARD, trajectory: TRAJ, error: null, error_detail: null },
    { drug_id: 2, drug: "Pan 40", as_of: null, card: null, trajectory: null,
      error: "no_data", error_detail: "No state data supports a prediction for this SKU." },
    { drug_id: 3, drug: "Telma 40", as_of: "2025-11-30",
      card: { ...CARD, medicine: "Telma 40",
              status: null,                                    /* badge unsupported */
              outlook: { ...CARD.outlook,
                         timing: { label: "4\u20137 days", prob: null } } },
      trajectory: null, error: null, error_detail: null },
    { drug_id: 4, drug: "Pan 40 ", as_of: "2025-11-30",
      card: { ...CARD, medicine: "Pan 40",
              status: { key: "reorder_soon", label: "Reorder soon" },
              outlook: { ...CARD.outlook,
                         trajectory: { value: "falling", label: "Falling", prob: 0.6 } } },
      trajectory: null, error: null, error_detail: null },
    { drug_id: 5, drug: "Glycomet 500", as_of: "2025-11-30",
      card: { ...CARD, medicine: "Glycomet 500",
              status: { key: "none", label: "No immediate reorder indicated" },
              outlook: { ...CARD.outlook,
                         trajectory: { value: "falling", label: "Falling", prob: 0.7 } } },
      trajectory: null, error: null, error_detail: null },
  ],
};

const html = ForecastLaya(PAYLOAD);

/* ---- engine banner + freshness ---- */
assert.ok(html.includes('data-status="ready"'), "engine ready chip");
assert.ok(html.includes("computed 2026-10-09T12:00:00"), "generated_at shown");
assert.ok(html.includes("latest state"), "state basis shown");

/* ---- one row per SKU, with the supplied values ---- */
assert.ok(html.includes("Dolo 650") && html.includes("Telma 40"));
assert.ok(html.includes("2025-11-30"), "actual analysis date shown");
assert.ok(html.includes('dfc-badge--monitor'), "Monitor badge (chat-card vocabulary)");
assert.ok(html.includes('dfc-badge--soon'), "Reorder soon badge (chat-card vocabulary)");

/* ---- status column: compact chip wording, full label kept on title= ---- */
assert.ok(html.includes(">Monitor<"), "compact Monitor chip as column text");
assert.ok(html.includes(">Reorder soon<"), "compact Reorder soon chip");
assert.ok(html.includes(">No action<"), "compact No action chip");
assert.ok(!html.includes(">No immediate reorder indicated<"),
          "long wording never renders as column text");
assert.ok(html.includes('title="No immediate reorder indicated"'),
          "full backend label preserved as the chip's tooltip");
assert.ok(html.includes('class="btn sm"'), "Details uses the app's standard button");
assert.ok(html.includes("\u26a1 Spiking") || html.includes("\u26a1"), "trajectory glyph");
assert.ok(html.includes("Falling"), "trajectory label");
assert.ok(html.includes("8\u201314 days") && html.includes("4\u20137 days"));
assert.ok(html.includes("1001+ units"), "band shown as range");

/* ---- probability chips only when valid ---- */
assert.ok(html.includes("p=0.97"), "timing probability shown");
assert.ok(html.includes("p=1"), "valid probability shown");
const telmaCell = html.split("Telma 40")[1].split("</tr>")[0];
assert.ok(!telmaCell.includes("p=null") && !telmaCell.includes("p=NaN"),
          "null probability never rendered");

/* ---- status badge only when the backend supplied one ---- */
assert.strictEqual((html.match(/dfc-badge--/g) || []).length, 3,
  "exactly the rows with a backend status get a badge (Telma has none)");

/* ---- error rows: explicit reason, no invented prediction ---- */
assert.ok(html.includes(FPL_ERRORS.no_data), "human error code");
assert.ok(html.includes("No state data supports a prediction for this SKU."),
          "backend detail preserved");
assert.strictEqual((html.match(/data-fpl=/g) || []).length, 4,
  "only successful rows get a Details button");

/* ---- footer transparency ---- */
assert.ok(html.includes("never an exact order"), "band-is-a-range footer");
assert.ok(html.includes("no reorder is placed automatically"),
          "no automatic ordering footer");
assert.ok(html.includes("recomputed from the latest state"), "up-to-date note");

/* ---- null payload -> explicit unavailable state ---- */
const missing = ForecastLaya(null);
assert.ok(missing.includes("could not be loaded"), "null payload handled");
assert.ok(missing.includes("unavailable"));

/* ---- engine unavailable keeps every SKU listed ---- */
const down = ForecastLaya({ engine: { status: "unavailable", detail: "no model" },
                            generated_at: null, state: "latest",
                            rows: [{ drug_id: 9, drug: "X", as_of: null,
                                     card: null, trajectory: null,
                                     error: "engine_unavailable",
                                     error_detail: "no model" }] });
assert.ok(down.includes('data-status="unavailable"'), "engine chip reflects outage");
assert.ok(down.includes("no model"), "engine detail shown");
assert.ok(down.includes(FPL_ERRORS.engine_unavailable), "row error shown");
assert.ok(down.includes(">X<"), "SKU still listed during outage");

/* ---- raw model slugs never surface ---- */
for (const slug of ["4_7_days", "801_1000", "1001_plus", "reorder_due_within_7d"]) {
  assert.ok(!html.includes(slug), `raw slug leaked: ${slug}`);
}

/* ---- HTML escaping: payload text never becomes markup ---- */
const xss = ForecastLaya({ engine: { status: "ready", detail: null },
  generated_at: "t", state: "latest",
  rows: [{ drug_id: 1, drug: `<img src=x onerror="alert(1)">Dolo`,
           as_of: "2025-11-30",
           card: { ...CARD, status: null,
                   outlook: { ...CARD.outlook, band: { label: `<b>1001+</b>`, prob: null } } },
           trajectory: null, error: null, error_detail: null }] });
assert.ok(!xss.includes("<img") && !xss.includes("<b>"), "payload escaped");
assert.ok(xss.includes("&lt;img") && xss.includes("&lt;b&gt;"));

/* ---- no undefined/NaN ever reaches the page ---- */
assert.ok(!html.includes("undefined") && !html.includes("NaN"),
          "no undefined/NaN in output");

/* ================= LayaSuggestionCell (AI reorder suggestions) ================= */
const spiky = LayaSuggestionCell({ ...PAYLOAD.rows[0], reorder_by: "2026-10-23" });
assert.ok(spiky.includes("1001+ units"), "Laya band shown as a range");
assert.ok(spiky.includes("Act by 2026-10-23"), "act-by deadline shown");
assert.ok(spiky.includes('data-traj="spiking"'), "trajectory carried for tone");
assert.ok(spiky.includes("p=1"), "band probability chip when valid");
assert.ok(!/order_qty|exact order quantity\./.test(spiky),
          "raw field names never leak into the cell");
/* the qty line is the editable control: key for the transient override,
   button semantics for keyboard users */
assert.ok(spiky.includes('data-lsg-qty="1"') && spiky.includes('role="button"') &&
          spiky.includes('tabindex="0"'), "qty line is editable");
assert.ok(spiky.includes("Order qty \u2014"),
          "no band floor yet -> explicit dash, still editable");
assert.ok(typeof wireLayaQtyEdit === "function", "editor wiring exported for app.js");

/* ---- order quantity: only what the backend supplied (Laya band floor) ---- */
const withQty = LayaSuggestionCell({ ...PAYLOAD.rows[0], reorder_by: "2026-10-23",
                                     order_qty: 1001, order_qty_open: true });
assert.ok(withQty.includes("Order qty ≥1,001"),
          "open band shown automatically as a minimum");
assert.ok(withQty.includes("1001+ units") && withQty.includes("Act by 2026-10-23"),
          "band + deadline stay in the cell next to the qty");
assert.ok(withQty.includes("lsg-qty"), "qty gets its own line");
assert.ok(withQty.includes('data-lsg-qty="1"'), "qty line editable even with a suggestion");
assert.ok(withQty.includes('data-lsg-suggest="1001"') && withQty.includes('data-lsg-open="1"'),
          "band floor + open flag carried for the editor's prefill/tooltip");
const closedQty = LayaSuggestionCell({ ...PAYLOAD.rows[0], order_qty: 601,
                                       order_qty_open: false });
assert.ok(closedQty.includes("Order qty 601") && !closedQty.includes("≥"),
          "closed band shows its floor plainly");
/* no qty supplied -> dash only, never an invented number (still editable) */
const noQty = LayaSuggestionCell(PAYLOAD.rows[0]);
assert.ok(noQty.includes("Order qty \u2014"), "missing qty renders a dash, not a guess");
assert.ok(!/Order qty (?:\u2265)?\d/.test(noQty), "no number appears without a backend qty");
assert.ok(noQty.includes('data-lsg-qty="1"'), "dash cell is still editable from scratch");
/* junk never renders as a quantity */
for (const junk of ["601 evil", NaN, 0, -5, null, undefined, {}, "1001_plus"]) {
  const h = LayaSuggestionCell({ ...PAYLOAD.rows[0], order_qty: junk });
  assert.ok(!/Order qty (?:\u2265)?\d/.test(h) && !h.includes("undefined") && !h.includes("NaN"),
            `junk qty never renders: ${String(junk)}`);
  assert.ok(h.includes('data-lsg-qty="1"'), `junk qty cell stays editable: ${String(junk)}`);
}

/* no valid prediction -> no invented number or date, but the operator can
   still type a quantity to order for that SKU */
const errCell = LayaSuggestionCell(PAYLOAD.rows[1]);
assert.ok(!/Order qty (?:\u2265)?\d/.test(errCell), "errored SKU never gets an invented number");
assert.ok(errCell.includes('data-lsg-qty="2"') && errCell.includes("Order qty \u2014"),
          "errored SKU keeps an editable, empty order qty");
/* no row and no drug id -> nothing to key an override on: plain dash */
assert.strictEqual(LayaSuggestionCell(null), '<span class="fpl-na">—</span>');
assert.strictEqual(LayaSuggestionCell(undefined), '<span class="fpl-na">—</span>');

/* timing window unmappable -> band + trajectory, but NO fabricated deadline */
const noDeadline = LayaSuggestionCell(PAYLOAD.rows[0]);
assert.ok(noDeadline.includes("1001+ units") && !noDeadline.includes("Act by"),
          "deadline only when Laya supplied a timing window");

/* payload text never becomes markup */
const xssCell = LayaSuggestionCell({
  card: { ...CARD, outlook: { ...CARD.outlook,
    band: { label: `<img src=x onerror="alert(1)">1001+`, prob: null },
    trajectory: { value: "spiking", label: `<b>Spiking</b>`, prob: null } } },
  reorder_by: "<script>x</script>", error: null,
});
assert.ok(!xssCell.includes("<img") && !xssCell.includes("<b>") &&
          !xssCell.includes("<script>"), "suggestion cell escaped");
assert.ok(!xssCell.includes("undefined") && !xssCell.includes("NaN"),
          "no undefined/NaN in suggestion cell");

/* ================= editable order qty ================= */
clearLayaQty();

/* free number entry: any non-negative number, rounded to whole units */
assert.strictEqual(parseLayaQty("5000"), 5000, "any number above Laya's floor is accepted");
assert.strictEqual(parseLayaQty("12.6"), 13, "rounded to whole units");
assert.strictEqual(parseLayaQty(0), 0, "0 allowed (operator decides to order nothing)");
assert.strictEqual(parseLayaQty(" -5 "), null, "negatives rejected, previous value kept");
assert.strictEqual(parseLayaQty("601 evil"), null, "junk rejected");
assert.strictEqual(parseLayaQty(""), null, "blank rejected, previous value kept");
assert.strictEqual(parseLayaQty(undefined), null);

/* in-place editor markup: number input, pre-filled only when a value exists */
const inp = layaQtyInputHtml(801);
assert.ok(inp.includes('class="lsg-qty-input"') && inp.includes('type="number"') &&
          inp.includes('min="0"') && inp.includes('step="1"') && inp.includes('value="801"'),
          "compact number input replacing the qty line");
assert.ok(!layaQtyInputHtml(null).includes("value="), "empty cell starts blank");

/* operator override: replaces the hint in the cell, per SKU, transient */
assert.ok(setLayaQty(1, 2500), "typed quantity stored for this page");
const edited = LayaSuggestionCell({ ...PAYLOAD.rows[0], order_qty: 1001, order_qty_open: true });
assert.ok(edited.includes("Order qty 2,500") && !edited.includes("Order qty \u22651,001"),
          "operator's number replaces Laya's floor hint");
assert.ok(edited.includes("lsg-qty-set"), "operator-set value marked in the class");
assert.ok(noQty.includes("lsg-qty-empty"), "no-suggestion line carries the empty marker");
assert.ok(edited.includes("Laya&#39;s floor was \u22651,001"),
          "original suggestion kept as the tooltip context");
assert.ok(LayaSuggestionCell({ ...PAYLOAD.rows[3], order_qty: 601,
                               order_qty_open: false }).includes("Order qty 601"),
          "override is scoped to its own SKU");
/* typed from scratch where Laya has no band */
assert.ok(setLayaQty(1, 42), "quantity typed with no band at all");
assert.ok(LayaSuggestionCell(PAYLOAD.rows[0]).includes("Order qty 42"),
          "no-band cell shows what the operator typed");
clearLayaQty();
assert.ok(LayaSuggestionCell({ ...PAYLOAD.rows[0], order_qty: 1001,
                               order_qty_open: true }).includes("Order qty \u22651,001"),
          "override is transient: cleared state renders Laya's floor again");
/* junk ids never become editable (and never leak into the markup) */
const badId = LayaSuggestionCell({ drug_id: `<img src=x onerror="a(1)>`, card: CARD });
assert.ok(!badId.includes("<img") && !badId.includes("data-lsg-qty"),
          "non-numeric drug id disables editing instead of rendering");

console.log("forecast-laya: all assertions passed");
