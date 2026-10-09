/* Node harness for static/demand-trajectory-card.js — run by
 * tests/test_trajectory_card.py::test_js_component_validation_and_rendering.
 *
 * Verifies: deterministic percentage maths, three aligned metric rows,
 * explicit unavailable states for missing/invalid data, badge integrity
 * (metrics never override Laya's trajectory), no invented causes and HTML
 * escaping.
 */
"use strict";
const assert = require("node:assert");
const {
  DemandTrajectoryCard,
  dtcBaselinePct,
  dtcMeaning,
  DTC_DISCLAIMER,
} = require("../static/demand-trajectory-card.js");

const SAMPLE = {
  type: "demand_trajectory",
  medicine: "Dolo 650",
  analysis_date: "2025-11-30",
  trajectory: "spiking",
  trajectory_label: "Spiking",
  recent_daily_demand: 5.57,
  baseline_daily_demand: 4.46,
  recent_vs_baseline_ratio: 1.249,
  state_id: "st_abc123",
};

/* ---- percentage maths: deterministic, null when not computable ---- */
assert.ok(Math.abs(dtcBaselinePct(5.57, 4.46) - 24.8879) < 0.001);
assert.strictEqual(dtcBaselinePct(4.46, 4.46), 0);
assert.strictEqual(dtcBaselinePct(3, 4.46) < 0, true);
assert.strictEqual(dtcBaselinePct(5.57, 0), null);   // non-positive baseline
assert.strictEqual(dtcBaselinePct(null, 4.46), null); // missing metric
assert.strictEqual(dtcBaselinePct(5.57, null), null);
assert.strictEqual(dtcBaselinePct(NaN, 4.46), null);

/* ---- full payload: header, sections, rows, meaning, disclaimer ---- */
const html = DemandTrajectoryCard(SAMPLE);
assert.ok(html.includes("dtc-badge--spike"), "orange spiking badge class");
assert.ok(html.includes(">Spiking<"), "badge label");
assert.ok(html.includes("Dolo 650"), "medicine title from payload");
assert.ok(html.includes("Historical analysis date: 2025-11-30"), "date under title");
assert.ok(html.includes("What is happening?"));
assert.ok(html.includes("Laya classifies Dolo 650&#39;s demand as spiking.") ||
          html.includes("Laya classifies Dolo 650's demand as spiking."),
          "plain-English trajectory sentence");
assert.ok(html.includes("Recent daily demand was approximately 5.57 units per day, " +
  "compared with a historical baseline of 4.46 units per day."), "metrics sentence");
assert.ok(html.includes("Supporting metrics"));
assert.ok(html.includes("Recent demand"), "row 1 label");
assert.ok(html.includes("5.57 units/day"), "row 1 value");
assert.ok(html.includes("Historical baseline"), "row 2 label");
assert.ok(html.includes("4.46 units/day"), "row 2 value");
assert.ok(html.includes("Above baseline"), "row 3 label");
assert.ok(html.includes("approximately 25%"), "row 3 value, rounded");
assert.ok(html.includes("What does this mean?"));
assert.ok(html.includes("Demand is running about 25% above its historical baseline, " +
  "which supports monitoring for a potential demand increase."), "interpretation");
assert.ok(html.includes(DTC_DISCLAIMER), "verbatim disclaimer");
assert.ok(!/weather/i.test(html), "no weather cause claimed");
assert.ok(!/reorder timing|quantity band|units to order/i.test(html),
  "reorder timing/quantity kept out of this card");

/* ---- badge integrity: metrics never override Laya's trajectory ---- */
const falling = DemandTrajectoryCard({ ...SAMPLE, trajectory: "falling",
  trajectory_label: "Falling" });
assert.ok(falling.includes("dtc-badge--fall"), "badge follows Laya, not metrics");
assert.ok(falling.includes(">Falling<"));
assert.ok(falling.includes("Above baseline"), "metrics row still factual");

/* ---- below / in-line branches ---- */
const below = DemandTrajectoryCard({ ...SAMPLE, recent_daily_demand: 3 });
assert.ok(below.includes("Below baseline"), "negative change label");
assert.ok(below.includes("approximately 33%"), "(3-4.46)/4.46 = -33%");
assert.ok(below.includes("about 33% below its historical baseline"), "below meaning");
const inline = DemandTrajectoryCard({ ...SAMPLE, recent_daily_demand: 4.5 });
assert.ok(inline.includes("In line with baseline"), "negligible change label");
assert.ok(inline.includes("within 2%"), "negligible change value");

/* ---- missing metrics: explicit, never fabricated ---- */
const noMetrics = DemandTrajectoryCard({
  ...SAMPLE, recent_daily_demand: null, baseline_daily_demand: "n/a",
});
assert.ok(noMetrics.includes("Unavailable"), "rows marked unavailable");
assert.ok(noMetrics.includes("Demand metrics for this state are incomplete"),
  "explicit missing-metric sentence");
assert.ok(noMetrics.includes("don't allow a baseline comparison"),
  "explicit missing-metric meaning");
assert.ok(noMetrics.includes(">Spiking<"), "trajectory still shown");
assert.ok(!noMetrics.includes("units/day"), "no invented metric values");

/* zero baseline: metrics display, comparison stays unavailable */
const zeroBase = DemandTrajectoryCard({ ...SAMPLE, baseline_daily_demand: 0 });
assert.ok(zeroBase.includes("0 units/day"), "actual zero is shown, not hidden");
assert.ok(zeroBase.includes("Unavailable"), "comparison not computable");

/* ---- invalid payloads: explicit unavailable state, no numbers ---- */
for (const bad of [
  null,
  "nonsense",
  { type: "demand_trajectory" },
  { ...SAMPLE, medicine: "" },
  { ...SAMPLE, medicine: "   " },
  { ...SAMPLE, trajectory: "exploding" },
  { ...SAMPLE, trajectory: "constructor" },   // prototype key must not pass
  { ...SAMPLE, medicine: undefined },
]) {
  const out = DemandTrajectoryCard(bad);
  assert.ok(out.includes("dtc--invalid"), `invalid rendered for ${JSON.stringify(bad)}`);
  assert.ok(out.includes("No values are substituted"), "explicit no-substitution note");
  assert.ok(!out.includes("5.57") && !out.includes("25%"),
    "no payload metrics leaked into an invalid card");
}

/* ---- HTML escaping: payload text never becomes markup ---- */
const xss = DemandTrajectoryCard({ ...SAMPLE,
  medicine: `<img src=x onerror="alert(1)">Dolo`,
  analysis_date: `"><script>alert(2)</script>` });
assert.ok(!xss.includes("<img"), "medicine escaped");
assert.ok(!xss.includes("<script>"), "date escaped");
assert.ok(xss.includes("&lt;img") && xss.includes("&lt;script&gt;"));

/* ---- interpretation wording: no causes, no promises, no weather ---- */
for (const pct of [24.8879, -32.7, 0.5, null]) {
  const m = dtcMeaning("spiking", pct);
  assert.ok(!/weather|because|due to|caused|will (stay|remain|keep)/i.test(m),
    `meaning must not invent causes or promise demand: ${m}`);
}
assert.ok(dtcMeaning("spiking", 24.8879).includes("about 25% above"));

console.log("demand-trajectory-card: all assertions passed");
