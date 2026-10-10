/* Node harness for static/demand-forecast-card.js — run by
 * tests/test_forecast_card.py::test_js_component_formatting_and_validation.
 *
 * Verifies the display contract: format-only rendering of supplied metrics,
 * explicit unavailable states (missing ≠ zero, weather absence, no status
 * badge when unsupported), band-is-a-range wording, probability chips only
 * when valid, interpretation from the backend, and HTML escaping.
 */
"use strict";
const assert = require("node:assert");
const {
  DemandForecastCard,
  dfcProb,
  DFC_WEATHER_UNAVAILABLE,
  DFC_FOOTER,
} = require("../static/demand-forecast-card.js");

const SAMPLE = {
  type: "demand_forecast",
  medicine: "Dolo 650",
  analysis_date: "2026-09-30",
  state_id: "st_4033f3b2063d",
  status: { key: "monitor", label: "Monitor" },
  outlook: {
    timing: { label: "8\u201314 days", prob: 0.9647 },
    band: { label: "1001+ units", prob: 1.0 },
    trajectory: { value: "spiking", label: "Spiking", prob: 0.9999 },
    band_note: "Model-predicted range, not an exact order quantity. " +
      "Exact quantities come from the inventory engine.",
  },
  drivers: {
    recent_daily_demand: 4.64,
    baseline_daily_demand: 4.38,
    baseline_change: "above",
    baseline_change_pct: 6,
    seasonal: { label: "Seasonal down",
      explanation: "Demand is seasonally subdued for this period.", prob: 0.9996 },
    prescriptions_available: false,
    prescription_trend: null,
    demand_proxy_note: "Sales transactions are used as a demand proxy: " +
      "Prescription records are absent; observed sales volume and " +
      "transaction frequency are the demand signal used here.",
    weather: { label: "Elevated rainfall", detail: "27.2C avg, 8.1mm rain (7d)",
      source: "open-meteo" },
  },
  interpretation: "Dolo 650 demand is currently classified as spiking, with " +
    "recent average sales approximately 6% above the historical baseline. " +
    "Monitor the trend and review available stock before making " +
    "replenishment decisions.",
  low_confidence: [],
};

/* ---- probability chips: only valid, returned probabilities ---- */
assert.ok(dfcProb(0.9647).includes("p=0.96"));
assert.strictEqual(dfcProb(null), "");
assert.strictEqual(dfcProb("0.9"), "");   // not a number
assert.strictEqual(dfcProb(1.5), "");     // out of range
assert.strictEqual(dfcProb(undefined), "");

/* ---- full payload: header, outlook, drivers, meaning, footer ---- */
const html = DemandForecastCard(SAMPLE);
assert.ok(html.includes("dfc-title") && html.includes("Dolo 650"), "medicine title");
assert.ok(html.includes("Historical analysis date: 2026-09-30"), "actual analysis date");
assert.ok(html.includes("dfc-badge--monitor") && html.includes(">Monitor<"),
  "status badge from backend data");
assert.ok(html.includes("dfc-ico--up"), "trend icon follows trajectory");

// section 1: reorder outlook, prominent, three cells
assert.ok(html.includes("Reorder outlook"));
const cells = html.split("dfc-o-l").length - 1;
assert.strictEqual(cells, 3, "three outlook cells");
assert.ok(html.includes("8\u201314 days"), "timing label");
assert.ok(html.includes("1001+ units"), "quantity band label");
assert.ok(html.includes("Spiking"), "trajectory label");
assert.ok(html.includes("p=0.96") && html.includes("p=1.00"), "probability chips");
const bandNote = html.split("dfc-note")[1] || "";
assert.ok(bandNote.includes("not an exact order quantity") &&
  bandNote.includes("inventory engine"), "band kept separate from exact order");

// section 2: drivers, displayed from supplied values only
assert.ok(html.includes("What is driving the prediction?"));
assert.ok(html.includes("4.64 units/day"), "recent average daily sales");
assert.ok(html.includes("4.38 units/day"), "historical baseline");
assert.ok(html.includes("approximately 6% above baseline"), "backend percentage");
assert.ok(html.includes("Seasonal down"), "seasonal signal");
assert.ok(html.includes("Demand is seasonally subdued for this period."),
  "seasonal human explanation");
assert.ok(html.includes("Elevated rainfall") && html.includes("27.2C avg"),
  "weather signal with detail");
assert.ok(html.includes("demand proxy"), "sales labelled as demand proxy");
assert.ok(!html.includes("Prescription demand trend"),
  "prescription row only when data is available");

// section 3 + footer
assert.ok(html.includes("What does this mean?"));
assert.ok(html.includes(SAMPLE.interpretation.slice(0, 60)), "backend interpretation");
assert.ok(html.includes(DFC_FOOTER), "footer with band/no-auto-order note");
assert.ok(html.includes("no reorder is placed automatically"), "no auto ordering");

/* ---- status absent -> no badge (only when backend supports it) ---- */
for (const bad of [null, {}, { key: "unknown", label: "???" },
                   { key: "monitor", label: "" }]) {
  const out = DemandForecastCard({ ...SAMPLE, status: bad });
  assert.ok(!out.includes("dfc-badge"), `no badge for ${JSON.stringify(bad)}`);
}

/* ---- weather missing -> explicit unavailable, never "normal" ---- */
const noWx = DemandForecastCard({
  ...SAMPLE, drivers: { ...SAMPLE.drivers, weather: null },
});
assert.ok(noWx.includes(DFC_WEATHER_UNAVAILABLE), "weather unavailable label");
assert.ok(!noWx.includes("Normal conditions"), "absence never shown as normal");

/* ---- prescriptions available -> row shown instead of proxy note ---- */
const withRx = DemandForecastCard({
  ...SAMPLE, drivers: { ...SAMPLE.drivers,
    prescriptions_available: true, prescription_trend: "12.5 items/day",
    demand_proxy_note: null },
});
assert.ok(withRx.includes("Prescription demand trend"), "prescription row");
assert.ok(withRx.includes("12.5 items/day"), "prescription trend value");
assert.ok(!withRx.includes("demand proxy"), "no proxy note when data exists");

/* ---- missing vs zero: distinct, never fabricated ---- */
const missing = DemandForecastCard({
  ...SAMPLE,
  drivers: { ...SAMPLE.drivers, recent_daily_demand: null,
    baseline_daily_demand: null, baseline_change: "unavailable",
    baseline_change_pct: null, seasonal: null },
});
assert.strictEqual((missing.match(/Unavailable/g) || []).length >= 4, true,
  "missing metrics and signals marked unavailable");
assert.ok(!missing.includes("4.64") && !missing.includes("4.38"),
  "no fabricated values");
assert.ok(!missing.includes("units/day"), "no invented unit values");
const zeroed = DemandForecastCard({
  ...SAMPLE, drivers: { ...SAMPLE.drivers, recent_daily_demand: 0.0,
    baseline_change: "below", baseline_change_pct: -100 },
});
assert.ok(zeroed.includes("0 units/day"), "a real zero is displayed, not hidden");
assert.ok(zeroed.includes("approximately 100% below baseline"), "zero is a value");

/* ---- invalid payloads -> explicit unavailable state ---- */
for (const bad of [null, "nonsense", {}, { medicine: "", outlook: SAMPLE.outlook },
  { ...SAMPLE, outlook: null },
  { ...SAMPLE, outlook: { ...SAMPLE.outlook, trajectory: null } },
  { ...SAMPLE, outlook: { ...SAMPLE.outlook,
    trajectory: { value: "constructor", label: "X" } } }]) {
  const out = DemandForecastCard(bad);
  assert.ok(out.includes("dfc--invalid"), `invalid for ${JSON.stringify(bad)}`);
  assert.ok(out.includes("No values are substituted"), "explicit no-substitution");
  assert.ok(!out.includes("4.64") && !out.includes("1001+"),
    "no payload values leaked into an invalid card");
}

/* ---- interpretation missing -> explicit, not invented ---- */
const noInterp = DemandForecastCard({ ...SAMPLE, interpretation: "" });
assert.ok(noInterp.includes("Interpretation unavailable for this state."));

/* ---- low confidence -> explicit caution ---- */
const lowConf = DemandForecastCard({ ...SAMPLE, low_confidence: ["reorder timing"] });
assert.ok(lowConf.includes("Low model confidence: reorder timing"));
assert.ok(lowConf.includes("not guarantees"));

/* ---- raw model slugs never surface ---- */
for (const slug of ["4_7_days", "801_1000", "1001_plus", "seasonal_down",
                    "within_3_days", "reorder_due_within_7d"]) {
  assert.ok(!html.includes(slug), `raw slug leaked: ${slug}`);
}

/* ---- HTML escaping: payload text never becomes markup ---- */
const xss = DemandForecastCard({ ...SAMPLE,
  medicine: `<img src=x onerror="alert(1)">Dolo`,
  interpretation: `<script>alert(2)</script>`,
  outlook: { ...SAMPLE.outlook, band: { label: `<b>1001+</b>`, prob: null } } });
assert.ok(!xss.includes("<img") && !xss.includes("<script>"), "payload escaped");
assert.ok(xss.includes("&lt;img") && xss.includes("&lt;script&gt;"));

console.log("demand-forecast-card: all assertions passed");
