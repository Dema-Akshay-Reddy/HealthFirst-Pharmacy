/* DemandForecastCard — AI demand forecast card for the chatbot.
 *
 * Renders the backend's validated structured payload (card type
 * "demand_forecast"): medicine + analysis date, reorder status badge,
 * reorder outlook (timing / quantity band / trajectory with model
 * probabilities when returned), supporting drivers, and the backend's
 * plain-English interpretation.
 *
 * Division of labour: the BACKEND does every calculation (percentages,
 * status, wording); this component only formats and displays supplied
 * values. Missing metrics render as "Unavailable" (never zero), missing
 * weather renders as "Weather signal unavailable" (never assumed normal),
 * a status badge appears only when the backend supplied one, and an
 * unusable payload renders an explicit unavailable state - no value is
 * ever invented or substituted from another state.
 *
 * Standalone on purpose: no dependencies on app.js, so it loads before
 * app.js and unit-tests under Node (module.exports).
 */
"use strict";

const DFC_STATUS_TONE = { reorder_soon: "soon", monitor: "monitor", none: "none" };

const DFC_ICONS = {
  up: `<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M3 17l6-6 4 4 8-8" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/><path d="M15 7h6v6" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  flat: `<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M3 12h15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/><path d="M14 7l5 5-5 5" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  down: `<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M3 7l6 6 4-4 8 8" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/><path d="M15 17h6v-6" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
};
const DFC_TRAJ_ICON = { spiking: "up", rising: "up", stable: "flat", falling: "down" };

const DFC_WEATHER_UNAVAILABLE = "Weather signal unavailable";
const DFC_FOOTER =
  "Model prediction based on historical sales data. Quantity bands are ranges, " +
  "not exact orders, and no reorder is placed automatically.";

/* Same escaping as app.js's esc(); inlined so the component is standalone. */
const dfcEsc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* Strict numeric validation: finite numbers only, anything else is
 * "unavailable" - never coerced, never defaulted to 0. */
const dfcNum = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

/* Display helper: up to 2 decimals, trailing zeros trimmed (5.57, 4.5). */
const dfcFmt = (v) => String(Math.round(v * 100) / 100);

/* Probability chip: shown only when the backend actually returned a valid
 * one - never a confidence claim Laya did not make. */
function dfcProb(p) {
  if (dfcNum(p) === null || p < 0 || p > 1) return "";
  return `<span class="dfc-prob" title="Model probability returned by Laya">p=${p.toFixed(2)}</span>`;
}

/* Explicit unavailable state for an unusable payload: no numbers shown. */
function dfcInvalidCard() {
  return `<div class="dfc dfc--invalid" role="note">
    <div class="dfc-head">
      <span class="dfc-ico dfc-ico--none" aria-hidden="true"><svg viewBox="0 0 24 24" width="16" height="16"><path d="M12 3a9 9 0 100 18 9 9 0 000-18zm0 4v6m0 3.5v.1" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></span>
      <div class="dfc-head-main">
        <div class="dfc-title">Demand forecast</div>
        <div class="dfc-date">Prediction unavailable</div>
      </div>
    </div>
    <div class="dfc-sec">
      <p class="dfc-p dfc-muted">This forecast can't be displayed because the
      structured payload is missing or invalid. No values are substituted from
      another historical state.</p>
    </div>
  </div>`;
}

function dfcMetricRow(label, valueHtml) {
  return `<div class="dfc-row"><span>${dfcEsc(label)}</span><b>${valueHtml}</b></div>`;
}

/* Main component: validated structured payload -> HTML string. */
function DemandForecastCard(c) {
  const data = (c && typeof c === "object") ? c : null;
  const medicine = (data && typeof data.medicine === "string")
    ? data.medicine.trim() : "";
  const outlook = (data && data.outlook && typeof data.outlook === "object")
    ? data.outlook : null;
  const traj = (outlook && outlook.trajectory && typeof outlook.trajectory === "object")
    ? outlook.trajectory : null;
  const trajVal = (traj && typeof traj.value === "string")
    ? traj.value.toLowerCase() : "";
  if (!medicine || !outlook || !Object.prototype.hasOwnProperty.call(DFC_TRAJ_ICON, trajVal)) {
    return dfcInvalidCard();   // never fabricate a title or classification
  }

  const analysisDate = (data && typeof data.analysis_date === "string")
    ? data.analysis_date.trim() : "";
  const dateLine = analysisDate
    ? `Historical analysis date: ${dfcEsc(analysisDate)}`
    : "Historical analysis date unavailable";

  // Status badge only when the backend supplied a supported one.
  const status = (data.status && typeof data.status === "object")
    ? data.status : null;
  const tone = status && typeof status.key === "string"
    ? DFC_STATUS_TONE[status.key] : null;
  const statusLabel = (status && typeof status.label === "string"
    && status.label.trim()) ? status.label.trim() : "";
  const badge = (tone && statusLabel)
    ? `<span class="dfc-badge dfc-badge--${tone}">${dfcEsc(statusLabel)}</span>` : "";

  const timing = outlook.timing && typeof outlook.timing === "object" ? outlook.timing : {};
  const band = outlook.band && typeof outlook.band === "object" ? outlook.band : {};
  const trajLabel = typeof traj.label === "string" && traj.label.trim()
    ? traj.label.trim() : null;

  const cell = (label, valueHtml, prob) => `<div class="dfc-o">
      <span class="dfc-o-l">${dfcEsc(label)}</span>
      <b class="dfc-o-v">${valueHtml}</b>${dfcProb(prob)}
    </div>`;
  const outlookHtml = `<div class="dfc-outlook">
      ${cell("Expected reorder timing",
        (typeof timing.label === "string" && timing.label)
          ? dfcEsc(timing.label) : `<span class="dfc-na">Unavailable</span>`, timing.prob)}
      ${cell("Predicted quantity band",
        (typeof band.label === "string" && band.label)
          ? dfcEsc(band.label) : `<span class="dfc-na">Unavailable</span>`, band.prob)}
      ${cell("Demand trajectory",
        (trajLabel ? dfcEsc(trajLabel) : `<span class="dfc-na">Unavailable</span>`), traj.prob)}
    </div>
    <div class="dfc-note">${dfcEsc(outlook.band_note || "")}</div>`;

  // --- drivers: display supplied metrics only, no recalculation ---
  const d = (data.drivers && typeof data.drivers === "object") ? data.drivers : {};
  const recent = dfcNum(d.recent_daily_demand);
  const baseline = dfcNum(d.baseline_daily_demand);
  const change = typeof d.baseline_change === "string" ? d.baseline_change : "unavailable";
  const pct = dfcNum(d.baseline_change_pct);

  const recentRow = dfcMetricRow("Recent average daily sales",
    recent === null ? `<span class="dfc-na">Unavailable</span>`
                    : `${dfcFmt(recent)} units/day`);
  const baselineRow = dfcMetricRow("Historical baseline daily demand",
    baseline === null ? `<span class="dfc-na">Unavailable</span>`
                      : `${dfcFmt(baseline)} units/day`);
  let changeValue;
  if (change === "above" && pct !== null) {
    changeValue = `approximately ${Math.abs(pct)}% above baseline`;
  } else if (change === "below" && pct !== null) {
    changeValue = `approximately ${Math.abs(pct)}% below baseline`;
  } else if (change === "in_line") {
    changeValue = "in line with baseline (within 2%)";
  } else {
    changeValue = `<span class="dfc-na">Unavailable</span>`;
  }
  const changeRow = dfcMetricRow("Baseline change", changeValue);

  const seasonal = (d.seasonal && typeof d.seasonal === "object") ? d.seasonal : null;
  const seasonalRow = dfcMetricRow("Seasonal demand signal",
    (seasonal && typeof seasonal.label === "string" && seasonal.label)
      ? dfcEsc(seasonal.label) : `<span class="dfc-na">Unavailable</span>`);

  const weather = (d.weather && typeof d.weather === "object"
    && typeof d.weather.label === "string" && d.weather.label) ? d.weather : null;
  const weatherRow = dfcMetricRow("Weather signal",
    weather
      ? dfcEsc(weather.label)
      : `<span class="dfc-na">${dfcEsc(DFC_WEATHER_UNAVAILABLE)}</span>`);

  let prescriptionsRow = "";
  if (d.prescriptions_available === true) {
    prescriptionsRow = dfcMetricRow("Prescription demand trend",
      (typeof d.prescription_trend === "string" && d.prescription_trend)
        ? dfcEsc(d.prescription_trend) : `<span class="dfc-na">Unavailable</span>`);
  }

  const subLines = [];
  if (seasonal && typeof seasonal.explanation === "string" && seasonal.explanation) {
    subLines.push(`<div class="dfc-sub">${dfcEsc(seasonal.explanation)}${dfcProb(seasonal.prob)}</div>`);
  }
  if (weather && (weather.detail || weather.source)) {
    const parts = [];
    if (weather.detail) parts.push(dfcEsc(weather.detail));
    if (weather.source) parts.push(`source: ${dfcEsc(weather.source)}`);
    subLines.push(`<div class="dfc-sub">${parts.join(" · ")}</div>`);
  }
  if (d.prescriptions_available !== true && typeof d.demand_proxy_note === "string"
      && d.demand_proxy_note) {
    subLines.push(`<div class="dfc-note">${dfcEsc(d.demand_proxy_note)}</div>`);
  }

  const lowConf = (Array.isArray(data.low_confidence) ? data.low_confidence : [])
    .filter((q) => typeof q === "string" && q.trim());
  if (lowConf.length) {
    subLines.push(`<div class="dfc-note dfc-warn">Low model confidence: ${dfcEsc(lowConf.join(", "))}.
      Treat these as probabilistic signals, not guarantees.</div>`);
  }

  const interpretation = (typeof data.interpretation === "string"
    && data.interpretation.trim()) ? data.interpretation.trim() : null;

  return `<div class="dfc" role="group" aria-label="AI demand forecast">
    <div class="dfc-head">
      <span class="dfc-ico dfc-ico--${DFC_TRAJ_ICON[trajVal]}" aria-hidden="true">${DFC_ICONS[DFC_TRAJ_ICON[trajVal]]}</span>
      <div class="dfc-head-main">
        <div class="dfc-title">${dfcEsc(medicine)}</div>
        <div class="dfc-date${analysisDate ? "" : " dfc-na"}">${dateLine}</div>
      </div>
      ${badge}
    </div>
    <div class="dfc-sec">
      <div class="dfc-h">Reorder outlook</div>
      ${outlookHtml}
    </div>
    <div class="dfc-sec">
      <div class="dfc-h">What is driving the prediction?</div>
      <div class="dfc-rows">
        ${recentRow}${baselineRow}${changeRow}${seasonalRow}${weatherRow}${prescriptionsRow}
      </div>
      ${subLines.join("\n      ")}
    </div>
    <div class="dfc-sec">
      <div class="dfc-h">What does this mean?</div>
      <p class="dfc-p">${interpretation ? dfcEsc(interpretation)
        : `<span class="dfc-na">Interpretation unavailable for this state.</span>`}</p>
    </div>
    <div class="dfc-foot">${dfcEsc(DFC_FOOTER)}</div>
  </div>`;
}

/* Node (unit tests) export; browsers get the global function declaration. */
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    DemandForecastCard,
    dfcInvalidCard,
    dfcProb,
    DFC_STATUS_TONE,
    DFC_TRAJ_ICON,
    DFC_WEATHER_UNAVAILABLE,
    DFC_FOOTER,
  };
}
