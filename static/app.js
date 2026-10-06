/* Smart Pharmacy Inventory Management System — SPA
 * Vanilla JS + Chart.js (served locally). No build step.
 */
"use strict";

/* ------------------------------ utilities ------------------------------ */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const fmtN = (v) => (v === null || v === undefined || isNaN(v)) ? "–"
  : Number(v).toLocaleString("en-IN");
const fmtMoney = (v) => (v === null || v === undefined || isNaN(v)) ? "–"
  : "₹" + Number(v).toLocaleString("en-IN", { maximumFractionDigits: 0 });
const fmtMoney2 = (v) => "₹" + Number(v ?? 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtPct = (v) => (v === null || v === undefined) ? "–" : `${Number(v).toFixed(1)}%`;
const fmtDate = (d) => d ? new Date(d).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" }) : "–";
const today = () => new Date().toISOString().slice(0, 10);

const PALETTE = ["#0d47a1", "#2f80ed", "#5b9bf3", "#8fb8f5", "#279e63", "#e2a03f",
  "#e2574c", "#1565c0", "#4f46e5", "#0891b2"];
const CHART = { blue:"#0d47a1", blue2:"#2f80ed", green:"#279e63", amber:"#e2a03f", red:"#e2574c",
  indigo:"#4f46e5", slate:"#cbd5e1", fill:"rgba(47,128,237,.14)" };

const apiKey = () => localStorage.getItem("pharmacyApiKey") || "";
const session = () => { try { return JSON.parse(localStorage.getItem("pharmacySession") || "null"); } catch { return null; } };
const setSession = (s) => { if (s) localStorage.setItem("pharmacySession", JSON.stringify(s)); else localStorage.removeItem("pharmacySession"); };
const isAdmin = () => (session()?.role) === "admin";
const sessionHeaders = () => { const s = session(); return s?.token ? { "X-Session-Token": s.token } : {}; };
let USER = null;
const authQuery = () => {
  const p = new URLSearchParams();
  if (apiKey()) p.set("api_key", apiKey());
  const s = session();
  if (s?.token) p.set("token", s.token);
  return p.toString() ? `?${p}` : "";
};

async function api(path, opts = {}) {
  const key = apiKey();
  if (key) opts.headers = { ...(opts.headers || {}), "X-API-Key": key };
  opts.headers = { ...(opts.headers || {}), ...sessionHeaders() };
  const res = await fetch(path, opts);
  if (res.status === 401) {
    setSession(null);
    showLogin();
    throw new Error("Please sign in");
  }
  if (res.status === 403) throw new Error("Admin access required for this area");
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* ignore */ }
    throw new Error(detail);
  }
  return res.json();
}
const post = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: body === undefined ? "{}" : JSON.stringify(body),
});
const patch = (path, body) => api(path, {
  method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

function toast(msg, kind = "ok") {
  // kind: "ok" | "err" | "warn" (boolean true accepted as "err" for legacy callers).
  // Icon + border tone + text — never colour alone.
  if (kind === true) kind = "err";
  const icons = { ok: "✓", err: "✕", warn: "⚠" };
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<span aria-hidden="true" style="font-weight:700">${icons[kind] || "✓"}</span><span></span>`;
  el.lastElementChild.textContent = msg;
  $("#toastWrap").appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .15s"; el.style.opacity = "0";
    setTimeout(() => el.remove(), 160);
  }, 3800);
}

function download(path, name) {
  const key = apiKey();
  const s = session();
  const sep = path.includes("?") ? "&" : "?";
  const extra = [key ? `api_key=${encodeURIComponent(key)}` : "",
    s?.token ? `token=${encodeURIComponent(s.token)}` : ""].filter(Boolean).join("&");
  const a = document.createElement("a");
  a.href = extra ? `${path}${sep}${extra}` : path;
  a.download = name || ""; document.body.appendChild(a); a.click(); a.remove();
}

/* ------------------------------ charts ------------------------------ */
const charts = {};
const CHART_DEFAULTS_INJECTED = true;
function chart(id, cfg) {
  const el = document.getElementById(id);
  if (!el) return;
  if (charts[id]) charts[id].destroy();
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.color = "#5d7092";
  charts[id] = new Chart(el, cfg);
}
const gridOpts = { grid: { color: "#e9eef7" }, border: { display: false } };

/* ------------------------------ shell ------------------------------ */
const ROUTES = {
  dashboard: ["Dashboard", "Real-time inventory intelligence"],
  inventory: ["Inventory", "Stock levels, valuation & policy per SKU"],
  counter: ["Patient Counter", "Issue the medicine a patient asked for — FEFO with shelf locations"],
  shelftasks: ["Shelf Tasks", "Putaway of new batches and system-directed shifts"],
  shelf: ["SmartShelf & Expiry", "FEFO batch queue and expiry management"],
  forecast: ["Forecast & Reorder", "AI demand forecast and reorder planner"],
  alerts: ["Alerts", "Smart triggers from live data"],
  waste: ["Waste & Returns", "Expiry write-offs and vendor returns"],
  suppliers: ["Suppliers", "Scorecards and notification outbox"],
  upload: ["Upload & Data Quality", "Daily Excel/CSV/JSON ingestion and quarantine"],
  chat: ["PharmaAI Assistant", "Ask anything about stock, expiry, orders"],
  settings: ["Settings", "Forecast & alert policy"],
};
let OVERVIEW = null;

async function loadShellMeta() {
  const meta = await api("/api/meta");
  $("#brandName").textContent = meta.pharmacy || "Pharmacy";
  $("#asOfChip").textContent = `As of ${fmtDate(meta.as_of)}`;
  OVERVIEW = meta;
  return meta;
}

function renderShell(ov) {
  $("#dataRangeChip").textContent = `Feed: ${fmtDate(ov.data_from)} → ${fmtDate(ov.data_to)}`;
  const s = ov.alerts || {};
  const pill = $("#alertPill");
  pill.textContent = s.active ?? 0;
  pill.className = "pill" + ((s.critical || 0) > 0 ? "" : " ok");
}

async function navigate() {
  stopLiveRefresh();
  // Inventory Copilot lives in a floating button, not the sidebar: visible to
  // admins on every view except the copilot itself
  const fab = $("#chatFab");
  if (fab) fab.style.display = (isAdmin() && (location.hash || "").slice(1) !== "chat") ? "" : "none";
  const fallback = (USER && USER.role !== "admin") ? "counter" : "dashboard";
  const route = (location.hash || `#${fallback}`).slice(1) || fallback;
  if (!VIEWS[route]) { location.hash = `#${fallback}`; return; }
  if (!isAdmin() && ["dashboard", "inventory", "forecast", "alerts", "waste",
    "suppliers", "upload", "chat", "settings"].includes(route)) {
    location.hash = "#counter";
    return;
  }
  const [title, crumb] = ROUTES[route] || ["Dashboard", ""];
  const $pt = $("#pageTitle"), $pc = $("#pageCrumb");
  if ($pt) $pt.textContent = title;
  if ($pc) $pc.textContent = crumb;
  document.title = (crumb ? `${title} · ${crumb}` : title) + " — HealthFirst Pharmacy";
  $$("#nav a").forEach((a) => {
    const active = a.dataset.route === route;
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  closeSidebar();
  const view = $("#view");
  view.innerHTML = skeletonHTML();
  try {
    await VIEWS[route](view);
    view.focus({ preventScroll: true });
  } catch (e) {
    view.innerHTML = errorPanel(`Could not load ${title.toLowerCase()}`,
      e.message || "Something went wrong on our side.");
    const retry = $("#errRetry");
    if (retry) retry.onclick = () => navigate();
  }
  window.scrollTo(0, 0);
  if (route === "dashboard") startLiveRefresh();
}

window.addEventListener("hashchange", navigate);

/* live dashboard polling: KPIs, alert pill and alerts feed stay current
   while the dashboard tab is open (charts are left untouched). */
let liveTimer = null;
function startLiveRefresh() {
  stopLiveRefresh();
  liveTimer = setInterval(async () => {
    if (document.hidden || (location.hash || "#dashboard").slice(1) !== "dashboard") {
      stopLiveRefresh();
      return;
    }
    try {
      const [ov, alerts] = await Promise.all([api("/api/overview"), api("/api/alerts")]);
      if ((location.hash || "#dashboard").slice(1) !== "dashboard") return;
      /* keep the 8 dashboard KPI cards current without a full re-render */
      const liveMap = {
        "Total Medicines": (v) => fmtN(v.sku_count),
        "Current Stock": (v) => fmtN(v.usable_qty),
        "Low Stock": (v) => fmtN(v.low_stock),
        "Expiring Soon": (v) => fmtN(v.expiring_90d),
        "Expired Stock": (v) => fmtN(v.expired_qty),
        "Inventory Value": (v) => fmtMoney(v.usable_value),
        "Predicted Demand": (v) => fmtN(Math.round(v.sales_30d_qty || 0)),
        "Pending Reorders": (v) => fmtN(v.order_now),
      };
      $$(".grid.g4 > .kpi").forEach((el) => {
        const label = el.querySelector(".label")?.textContent?.trim();
        const fn = label && liveMap[label];
        if (!fn) return;
        const val = el.querySelector(".value");
        if (val) val.textContent = fn(ov);
      });
      renderShell(ov);
      const feed = $("#dashAlerts");
      if (feed) feed.innerHTML = alerts.items.slice(0, 6).map(alertRow).join("") ||
        `<div class="empty">No active alerts</div>`;
    } catch { /* server unreachable — keep last good values, retry next tick */ }
  }, 30000);
}
function stopLiveRefresh() {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
}
window.addEventListener("beforeunload", stopLiveRefresh);

async function refreshAll() {
  try {
    await post("/api/alerts/refresh");
    toast("Alerts recomputed from live data");
    navigate();
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------ view helpers ------------------------------ */
const kpi = (label, value, foot = "", tone = "", link = "", linkLabel = "", ico = "") => `
  <div class="kpi ${tone}">
    <div class="label">${ico ? `<span class="kico" aria-hidden="true">${ico}</span>` : ""}${label}</div>
    <div class="value">${value}</div>
    ${foot ? `<div class="foot">${foot}</div>` : ""}
    ${link ? `<button class="kpi-link" onclick="${link}">${linkLabel} →</button>` : ""}
  </div>`;

const badge = (text, tone) => `<span class="badge ${tone}">${esc(text)}</span>`;

const STATUS_TONE = {
  healthy: ["ok", "healthy"], scheduled: ["info", "scheduled"], order_now: ["bad", "order now"],
  unknown: ["mute", "—"], expired: ["bad", "expired"], critical: ["critical", "critical"],
  warning: ["warn", "expiring"], ok: ["ok", "ok"], pending: ["warn", "pending"],
  returned_to_vendor: ["ok", "credited"], return_requested: ["info", "return requested"],
  requested: ["info", "requested"], approved: ["info", "approved"], picked_up: ["info", "picked up"],
  credited: ["ok", "credited"], rejected: ["bad", "rejected"], sent: ["ok", "sent"],
  draft: ["mute", "draft"], ordered: ["info", "ordered"], received: ["ok", "received"],
  acknowledged: ["ok", "acked"], active: ["mute", "active"],
};
const ISSUE_LABELS = {
  date_out_of_range: "date out of range",
  invalid_qty: "invalid quantity",
  missing_drug_name: "missing medicine",
  unknown_drug: "unknown medicine",
  duplicate_txn_id: "duplicate txn id",
  missing_batch_no: "missing batch",
  invalid_expiry: "invalid expiry",
  invalid_price: "invalid price",
};
const issueLabel = (code) => ISSUE_LABELS[code] || code;

/* --------------------- shared UI states (skeleton / error / empty) --------------------- */
function skeletonHTML() {
  return `
    <div class="skel-kpis">${"<div class=\"skel-card\"><div class='skel skel-line w40'></div><div class='skel skel-line lg w60'></div><div class='skel skel-line w80'></div></div>".repeat(5)}</div>
    <div class="grid g23">
      <div class="skel-card"><div class='skel skel-line w40'></div><div class='skel skel-block'></div></div>
      <div class="skel-card"><div class='skel skel-line w60'></div><div class='skel skel-block' style='height:150px'></div></div>
    </div>`;
}

/* inline SVG icon set — professional glyphs, no emoji (stroke inherits color) */
const ICO = {
  box:`<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path d="M20 7L12 3 4 7v10l8 4 8-4V7zm-8-1.8L17.5 7.5 12 10.2 6.5 7.5 12 5.2zM6 9.3l5 2.5v6.9l-5-2.5V9.3zm7 9.4v-6.9l5-2.5v6.9l-5 2.5z" fill="currentColor"/></svg>`,
  alert:`<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path d="M12 2a7 7 0 00-7 7v4.6L3 17v2h18v-2l-2-3.4V9a7 7 0 00-7-7zm-2 18a2 2 0 004 0h-4z" fill="currentColor"/></svg>`,
  clock:`<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path d="M12 2a10 10 0 100 20 10 10 0 000-20zm1 5h-2v6l4.2 2.5 1-1.7-3.2-1.9V7z" fill="currentColor"/></svg>`,
  trash:`<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path d="M6 7h12l-1 13a2 2 0 01-2 2H9a2 2 0 01-2-2L6 7zm3-4h6l1 2h4v2H4V5h4l1-2z" fill="currentColor"/></svg>`,
  chart:`<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path d="M3 3h2v16h16v2H3V3zm16.7 4.3l-4 4-3-3-4.4 4.4 1.4 1.4 3-3 3 3 5.4-5.4L22 10V7h-2.3z" fill="currentColor"/></svg>`,
  cart:`<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path d="M7 4h-2L3 2H1v2h1.5l3.2 8.1-1.2 2.2A2 2 0 006.3 17H19v-2H6.6l1-1.9h7.5a2 2 0 001.8-1.1l3.2-6.5-1.8-.9-3.1 6.5H7.4L5.3 4H19V2H7V4zm0 14a2 2 0 100 4 2 2 0 000-4zm10 0a2 2 0 100 4 2 2 0 000-4z" transform="translate(1,-1)" fill="currentColor"/></svg>`,
  trend:`<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path d="M3 3h2v16h16v2H3V3zm16.7 4.3l-4 4-3-3-4.4 4.4 1.4 1.4 3-3 3 3 5.4-5.4L22 10V7h-2.3z" fill="currentColor"/></svg>`,
  spark:`<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path d="M12 2l1.8 5.6L19 9l-5.2 1.4L12 16l-1.8-5.6L5 9l5.2-1.4L12 2zm7 12l.9 2.6 2.6.9-2.6.9-.9 2.6-.9-2.6-2.6-.9 2.6-.9.9-2.6zM5 15l.7 2.1L7.8 18l-2.1.7L5 20.8l-.7-2.1L2.2 18l2.1-.9L5 15z" fill="currentColor"/></svg>`,
  up:`<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><path d="M12 4l7 8h-4v8h-6v-8H5l7-8z" fill="currentColor"/></svg>`,
  down:`<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><path d="M12 20l-7-8h4V4h6v8h4l-7 8z" fill="currentColor"/></svg>`,
  uparrow:`<svg viewBox="0 0 24 24" width="12" height="12" aria-hidden="true"><path d="M12 5l6 6h-4v8h-4v-8H6l6-6z" fill="currentColor"/></svg>`,
  downarrow:`<svg viewBox="0 0 24 24" width="12" height="12" aria-hidden="true"><path d="M12 19l-6-6h4V5h4v8h4l-6 6z" fill="currentColor"/></svg>`,
  upload:`<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><path d="M12 3l5 5h-3v6h-4V8H7l5-5zM5 18h14v2H5v-2z" fill="currentColor"/></svg>`,
  cart2:`<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><path d="M7 4h-2L3 2H1v2h1.5l3.2 8.1-1.2 2.2A2 2 0 006.3 17H19v-2H6.6l1-1.9h7.5a2 2 0 001.8-1.1l3.2-6.5-1.8-.9-3.1 6.5H7.4L5.3 4H19V2H7V4zm0 14a2 2 0 100 4 2 2 0 000-4zm10 0a2 2 0 100 4 2 2 0 000-4z" transform="translate(1,-1)" fill="currentColor"/></svg>`,
  mail:`<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><path d="M3 5h18a1 1 0 011 1v12a2 2 0 01-2 2H4a2 2 0 01-2-2V6a1 1 0 011-1zm9 7L4.4 7h15.2L12 12z" fill="currentColor"/></svg>`,
  search:`<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path d="M21 21l-4.8-4.8M17 10.5a6.5 6.5 0 11-13 0 6.5 6.5 0 0113 0z" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`,
  check:`<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path d="M9 16.2l-3.5-3.5L4 14.2 9 19.2 20 8.2l-1.5-1.5L9 16.2z" fill="currentColor"/></svg>`,
  shelf:`<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><path d="M4 4h16v5H4V4zm0 7h16v5H4v-5zm0 7h16v2H4v-2z" fill="currentColor"/></svg>`,
  pill:`<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path d="M6.5 2A5.5 5.5 0 002.6 11.4l8 8a5.5 5.5 0 007.8-7.8l-8-8A5.5 5.5 0 006.5 2zm3.6 5.4l4.5 4.5-7.8 7.8a3.5 3.5 0 01-4.9-4.9l8.2-7.4z" fill="currentColor"/></svg>`,
  refresh:`<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><path d="M12 5V2L7 6l5 4V7a5 5 0 11-5 5H5a7 7 0 107-7z" fill="currentColor"/></svg>`,
};

/* hero illustration block — healthcare-style vector scene (no external assets) */
const HERO_ART = `
  <div class="ha-cell" aria-hidden="true">
    <svg viewBox="0 0 64 64" width="54" height="54"><path d="M22 18h20a4 4 0 014 4v24a4 4 0 01-4 4H22a4 4 0 01-4-4V22a4 4 0 014-4z" fill="#fff" opacity=".92"/><path d="M30 26h4v6h6v4h-6v6h-4v-6h-6v-4h6v-6z" fill="#0d47a1"/><circle cx="47" cy="20" r="8" fill="#8fb8f5"/><path d="M47 15v10M42 20h10" stroke="#0d47a1" stroke-width="2.4" stroke-linecap="round"/></svg>
  </div>
  <div class="ha-cell" aria-hidden="true">
    <svg viewBox="0 0 64 64" width="54" height="54"><path d="M10 22h44v26a4 4 0 01-4 4H14a4 4 0 01-4-4V22z" fill="#fff" opacity=".92"/><path d="M10 22l6-10h32l6 10H10z" fill="#8fb8f5"/><path d="M26 34h12v3H26z" fill="#0d47a1"/><path d="M30.5 30.5h3V40h-3z" fill="#0d47a1"/></svg>
  </div>
  <div class="ha-cell" aria-hidden="true">
    <svg viewBox="0 0 64 64" width="54" height="54"><path d="M14 12h28a4 4 0 014 4v36a4 4 0 01-4 4H14a4 4 0 01-4-4V16a4 4 0 014-4z" fill="#fff" opacity=".92"/><path d="M18 20h14v3H18zM18 27h20v3H18zM18 34h16v3H18zM18 41h12v3H18z" fill="#5b9bf3"/><path d="M44 38l8 8-3 3-8-8v-3h3z" fill="#e2574c"/><circle cx="42" cy="36" r="7" fill="none" stroke="#e2574c" stroke-width="3"/></svg>
  </div>
  <div class="ha-cell" aria-hidden="true">
    <svg viewBox="0 0 64 64" width="54" height="54"><path d="M32 8l20 8v14c0 12-8 21-20 26C20 51 12 42 12 30V16l20-8z" fill="#fff" opacity=".92"/><path d="M32 14l14 5.6V30c0 8.6-5.6 15-14 19-8.4-4-14-10.4-14-19V19.6L32 14z" fill="#8fb8f5"/><path d="M27 30h10v3H32v10h-3V33h-7v-3h5v-10h3v10z" fill="#0d47a1" transform="translate(3,-1) scale(.9)"/></svg>
  </div>`;

/* hero scene for the auth card (similar visual family) */
const AUTH_ART = `
  <svg viewBox="0 0 220 150" width="220" height="150" role="img" aria-label="Pharmacy team illustration">
    <rect x="18" y="112" width="184" height="6" rx="3" fill="rgba(255,255,255,.35)"/>
    <rect x="40" y="38" width="64" height="76" rx="8" fill="#fff" opacity=".95"/>
    <rect x="40" y="38" width="64" height="16" rx="8" fill="#8fb8f5"/>
    <path d="M52 70h40M52 80h40M52 90h28" stroke="#c7d9f8" stroke-width="5" stroke-linecap="round"/>
    <path d="M72 56h-4v-4h-4v4h-4v4h4v4h4v-4h4z" fill="#0d47a1"/>
    <circle cx="146" cy="52" r="11" fill="#ffe3c9"/>
    <path d="M146 63c-12 0-18 8-18 18v31h36V81c0-10-6-18-18-18z" fill="#fff"/>
    <path d="M132 81h28v18h-28z" fill="#8fb8f5"/>
    <path d="M143 84h6v12h-6zM140 87h12v6h-12z" fill="#0d47a1"/>
    <circle cx="186" cy="60" r="10" fill="#ffe3c9"/>
    <path d="M186 70c-10 0-16 7-16 16v26h32V86c0-9-6-16-16-16z" fill="#dbe8fb"/>
    <rect x="176" y="92" width="20" height="26" rx="3" fill="#0d47a1"/>
    <path d="M182 99h8M182 105h8" stroke="#8fb8f5" stroke-width="2.4" stroke-linecap="round"/>
    <circle cx="102" cy="64" r="9" fill="#ffe3c9"/>
    <path d="M102 73c-9 0-14 6-14 14v25h28V87c0-8-5-14-14-14z" fill="#4da3ff"/>
    <circle cx="98" cy="61" r="4" fill="#5b9bf3"/><circle cx="106" cy="61" r="4" fill="#5b9bf3"/>
    <rect x="30" y="96" width="26" height="18" rx="3" fill="#4da3ff"/>
    <path d="M39 100v10M34 105h10" stroke="#fff" stroke-width="2.6" stroke-linecap="round"/>
  </svg>`;
/* "2026-10-06T00:30" → "Today 00:30" / "Yesterday" / "06 Oct" (full ISO kept in title tooltip) */
function whenLabel(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return String(iso); // fall back to the raw string
  const t = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const that = new Date(d); that.setHours(0, 0, 0, 0);
  const diff = Math.round((today - that) / 86400000);
  if (diff === 0) return `Today ${t}`;
 if (diff === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { day: "2-digit", month: "short" });
}

function errorPanel(title, message) {
  return `<div class="error-panel" role="alert">
    <div class="t">⚠ ${esc(title)}</div>
    <div>${esc(message || "Something went wrong on our side.")} Your data is safe — nothing was changed.</div>
    <button class="btn" id="errRetry">Try again</button>
  </div>`;
}
function emptyState(ico, title, desc, cta = "") {
  return `<div class="empty-state">
    <div class="ico" aria-hidden="true">${ico}</div>
    <div class="t">${title}</div><div class="d">${desc}</div>${cta}
  </div>`;
}

const statusBadge = (s) => {
  const [tone, label] = STATUS_TONE[s] || ["mute", s || "—"];
  return `<span class="badge ${tone}">${esc(label)}</span>`;
};

const daysLeftBadge = (d) => {
  if (d === null || d === undefined) return badge("no date", "mute");
  if (d < 0) return badge(`expired ${-d}d`, "bad");
  if (d <= 30) return badge(`${d}d left`, "critical");
  if (d <= 90) return badge(`${d}d left`, "warn");
  return badge(`${d}d left`, "ok");
};

function drugSelect(id, drugs, value) {
  return `<select id="${id}">
    <option value="">— select medicine —</option>
    ${drugs.map((d) => `<option value="${d.id}" ${String(d.id) === String(value) ? "selected" : ""}>${esc(d.name)}</option>`).join("")}
  </select>`;
}

/* ============================== DASHBOARD ============================== */
async function viewDashboard(view) {
  const [ov, charts_, acts] = await Promise.all([
    api("/api/overview"), api("/api/charts"), api("/api/activity?limit=8").catch(() => null)]);
  renderShell(ov);
  const growth = ov.sales_growth_pct;
  const gdelta = (g) => g === null || g === undefined ? `<span class="delta flat">n/a</span>`
    : `<span class="delta ${g >= 0 ? "up" : "down"}">${g >= 0 ? ICO.uparrow : ICO.downarrow} ${Math.abs(g)}%<span class="visually-hidden"> vs previous 30 days</span></span>`;
  const stockDelta = gdelta(growth);
  view.innerHTML = `
    ${ov.data_staleness_days > 5 ? `<div class="notice warn"><b>Feed gap:</b> the last sales record is
      ${ov.data_staleness_days} days old (feed ends ${fmtDate(ov.data_to)}). Demand is projected from history;
      upload a daily sales file or run the POS simulator to bring it current.</div>` : ""}
    <section class="hero" aria-label="Overview">
      <div class="hero-grid">
        <div class="hero-copy">
          <span class="hero-kicker">${ICO.spark} Smart Pharmacy Management</span>
          <h2>Manage inventory, medicines and expiry risks from one intelligent platform.</h2>
          <p class="hero-sub">Live stock levels, FEFO batch control, demand forecasting and supplier
            reorders for ${esc(ov.pharmacy || "your pharmacy")} — updated ${fmtDate(ov.as_of)}.</p>
          <div class="hero-ctas">
            <button class="btn lime" onclick="location.hash='#inventory'">Explore Inventory</button>
            <button class="btn hero-ghost" onclick="location.hash='#upload'">Upload Inventory</button>
            <button class="btn hero-ghost" onclick="location.hash='#shelf'">View Medicines</button>
          </div>
        </div>
        <div class="hero-art">${HERO_ART}</div>
      </div>
    </section>

    <div class="feats" role="list">
      <button class="feat" role="listitem" onclick="location.hash='#inventory'">
        <span class="f-ico">${ICO.box}</span>
        <span><span class="f-t">Genuine Medicines</span><br><span class="f-s">${fmtN(ov.sku_count)} SKUs across batches</span></span>
      </button>
      <button class="feat" role="listitem" onclick="location.hash='#inventory'">
        <span class="f-ico">${ICO.chart}</span>
        <span><span class="f-t">Smart Inventory</span><br><span class="f-s">${fmtMoney(ov.usable_value)} tracked at cost</span></span>
      </button>
      <button class="feat" role="listitem" onclick="location.hash='#shelf'">
        <span class="f-ico">${ICO.clock}</span>
        <span><span class="f-t">Expiry Safety</span><br><span class="f-s">${fmtN(ov.expiring_90d)} batches in the watch window</span></span>
      </button>
      <button class="feat" role="listitem" onclick="location.hash='#forecast'">
        <span class="f-ico">${ICO.trend}</span>
        <span><span class="f-t">Fast Reordering</span><br><span class="f-s">${fmtN(ov.order_now)} SKUs need orders now</span></span>
      </button>
      <button class="feat" role="listitem" onclick="location.hash='#chat'">
        <span class="f-ico">${ICO.spark}</span>
        <span><span class="f-t">AI-Powered Insights</span><br><span class="f-s">PharmaAI, grounded in live data</span></span>
      </button>
    </div>

    <div class="grid g4">
      ${kpi("Total Medicines", fmtN(ov.sku_count), `${fmtN(ov.batch_count)} batches on shelf`, "info", "location.hash='#inventory'", "Open inventory", ICO.box)}
      ${kpi("Current Stock", fmtN(ov.usable_qty), `${fmtMoney(ov.usable_value)} at cost`, "featured", "location.hash='#inventory'", "Open inventory", ICO.box)}
      ${kpi("Low Stock", fmtN(ov.low_stock), `${ov.order_now} below reorder point`, ov.low_stock ? "bad" : "good", "location.hash='#forecast'", "Review reorder plan", ICO.downarrow)}
      ${kpi("Expiring Soon", fmtN(ov.expiring_90d), `within the 90-day watch window`, "warn", "location.hash='#shelf'", "Open expiry queue", ICO.clock)}
    </div>
    <div class="grid g4" style="margin-top:12px">
      ${kpi("Expired Stock", fmtN(ov.expired_qty), `${fmtMoney(ov.expired_value)} blocked from dispensing`, ov.expired_qty ? "bad" : "good", "location.hash='#waste'", "File vendor returns", ICO.trash)}
      ${kpi("Inventory Value", fmtMoney(ov.usable_value), `${fmtMoney(ov.retail_value)} at MRP`, "", "location.hash='#inventory'", "See valuation", ICO.chart)}
      ${kpi("Predicted Demand", fmtN(Math.round(ov.sales_30d_qty || 0)), `units next 30d · revenue ${fmtMoney(ov.sales_30d_rev)} ${stockDelta}`, "", "location.hash='#forecast'", "See forecasts", ICO.trend)}
      ${kpi("Pending Reorders", fmtN(ov.order_now), ov.order_now ? "action recommended" : "all within policy", ov.order_now ? "warn" : "good", "location.hash='#forecast'", "Open reorder planner", ICO.cart)}
    </div>

    <div class="ai-banner">
      <div class="ai-head">
        <span class="ai-ico">${ICO.spark}</span>
        <span class="ai-title">AI Insights</span>
        <span class="ai-badge">Auto-generated · live data</span>
      </div>
      <div class="ai-grid" id="aiGrid"></div>
    </div>

    <div class="section-title">Priority actions</div>
    <div class="grid g23">
      <div class="card flush">
        <div style="padding:13px 16px 9px"><h3>Needs attention now</h3>
          <div class="sub" style="margin-bottom:0">Ranked by financial impact — clear these first</div></div>
        <div class="attention" id="dAttention"></div>
      </div>
      <div class="card">
        <h3>Recent activity</h3>
        <div class="sub">Latest uploads, reorders and write-offs</div>
        <div class="act-list" id="dActivity"></div>
      </div>
    </div>

    <div class="section-title">Sales &amp; demand</div>
    <div class="grid g23">
      <div class="card">
        <div class="card-head"><div><h3>Daily units sold</h3>
          <div class="sub">Rolling 90 days of feed · ${esc(ov.sales_window_90 || "—")}</div></div>
          <div class="right muted">30d revenue ${fmtMoney(ov.sales_30d_rev)} ${gdelta(growth)}</div>
        </div>
        <div class="chart-box"><canvas id="c-sales"></canvas></div>
      </div>
      <div class="card">
        <h3>Monthly revenue</h3>
        <div class="sub">Last 18 months</div>
        <div class="chart-box"><canvas id="c-monthly"></canvas></div>
      </div>
    </div>

    <div class="section-title">Inventory intelligence</div>
    <div class="grid g3">
      <div class="card"><h3>Stock value by category</h3><div class="sub">At cost price</div>
        <div class="chart-box sm"><canvas id="c-cat"></canvas></div></div>
      <div class="card"><h3>Expiry timeline</h3><div class="sub">Usable units by expiry quarter</div>
        <div class="chart-box sm"><canvas id="c-exp"></canvas></div></div>
      <div class="card"><h3>Model accuracy per SKU</h3><div class="sub">1 − weekly MAPE</div>
        <div class="chart-box sm"><canvas id="c-acc"></canvas></div></div>
    </div>
    <div class="grid g2" style="margin-top:16px">
      <div class="card flush">
        <div style="padding:14px 16px 4px"><h3>Top movers (90d)</h3><div class="sub">By revenue</div></div>
        <table><thead><tr><th>Medicine</th><th>Category</th><th class="num">Units</th><th class="num">Revenue</th></tr></thead>
        <tbody>${charts_.top_drugs.map((d) => `<tr><td><b>${esc(d.name)}</b></td><td>${esc(d.category)}</td>
          <td class="num">${fmtN(d.qty)}</td><td class="num">${fmtMoney(d.revenue)}</td></tr>`).join("") ||
          `<tr><td colspan="4" class="empty">No sales yet</td></tr>`}</tbody></table>
      </div>
      <div class="card"><h3>Supplier spend vs waste</h3><div class="sub">Purchase value against expiry waste</div>
        <div class="chart-box sm"><canvas id="c-sup"></canvas></div></div>
    </div>

    <div class="section-title">Latest alerts</div>
    <div id="dashAlerts" class="grid g2"></div>`;

  /* ---- AI insights banner: live derived, with sample-style copy for the rest ---- */
  const aiItems = [];
  if (ov.order_now > 0) aiItems.push({ sym: ICO.downarrow,
    t: `${ov.order_now} medicines may reach critical stock within 7 days.`,
    m: `Demand will outrun stock inside the lead time — review the reorder plan before it becomes a stockout.`,
    go: `<button class="btn sm" onclick="location.hash='#forecast'">Review</button>` });
  if (ov.expiring_90d > 0) aiItems.push({ sym: ICO.clock,
    t: `${fmtMoney(Math.round(ov.expired_value + ov.usable_value * 0.06))} of inventory is at risk of expiry soon.`,
    m: `Batches inside the 90-day window: plan run-down, discounts or vendor returns first.`,
    go: `<button class="btn sm" onclick="location.hash='#shelf'">Expiry queue</button>` });
  if (growth !== null && growth !== undefined) aiItems.push({ sym: growth >= 0 ? ICO.trend : ICO.downarrow,
    t: `Demand ${growth >= 0 ? "is expected to increase" : "is cooling"} — 30-day revenue ${growth >= 0 ? "up" : "down"} ${Math.abs(growth)}%.`,
    m: `Forecast-aligned purchasing: scale the next POs with the trend instead of flat averages.`,
    go: `<button class="btn sm" onclick="location.hash='#forecast'">Forecasts</button>` });
  if (ov.waste_value > 0) aiItems.push({ sym: ICO.trash,
    t: `Consider reducing purchase quantities for slow-moving medicines.`,
    m: `${fmtMoney(ov.waste_value)} has already been written off — tighten pack sizes and review periods.`,
    go: `<button class="btn sm" onclick="location.hash='#waste'">Waste analytics</button>` });
  if (!aiItems.length) aiItems.push({ sym: ICO.check,
    t: "All indicators are within policy thresholds.",
    m: "Stock cover, expiry exposure and reorder pressure look healthy right now.", go: "" });
  const aiGrid = $("#aiGrid");
  if (aiGrid) aiGrid.innerHTML = aiItems.slice(0, 3).map((a) => `
    <div class="ai-insight">
      <span class="ai-sym" aria-hidden="true">${a.sym}</span>
      <span><span class="t">${a.t}</span><span class="m" style="display:block">${a.m}</span></span>
      ${a.go ? `<span class="go">${a.go}</span>` : ""}
    </div>`).join("");

  /* ---- priority actions: derived from live data, each with why + what-next ---- */
  const actions = [];
  if (ov.order_now > 0) actions.push({ tone: "critical", ico: ICO.cart2,
    t: `${ov.order_now} SKU${ov.order_now > 1 ? "s" : ""} below reorder point`,
    m: "Demand will outrun stock within the lead time — raise POs before it becomes a stockout.",
    go: `<button class="btn sm primary" onclick="location.hash='#forecast'">Review orders</button>` });
  if (ov.waste_lots > 0) actions.push({ tone: "critical", ico: ICO.trash,
    t: `${fmtN(ov.waste_qty)} expired units (${fmtMoney(ov.waste_value)} at risk)`,
    m: "Expired stock is blocked from dispensing — recover value with a vendor return.",
    go: `<button class="btn sm" onclick="location.hash='#waste'">Start RTV</button>` });
  if (ov.expiring_90d > 0) actions.push({ tone: "warn", ico: ICO.clock,
    t: `${fmtN(ov.expiring_90d)} batches expiring within 90 days`,
    m: "Plan run-down or vendor returns before they convert into write-offs.",
    go: `<button class="btn sm" onclick="location.hash='#shelf'">Expiry queue</button>` });
  if (ov.low_stock > ov.order_now) actions.push({ tone: "warn", ico: ICO.downarrow,
    t: `${ov.low_stock - ov.order_now} SKUs approaching reorder point`,
    m: "Still above stockout, but inside the review window — watch the forecast.",
    go: `<button class="btn sm" onclick="location.hash='#forecast'">Forecasts</button>` });
  if (!actions.length) actions.push({ tone: "ok", ico: ICO.check,
    t: "All clear — nothing needs immediate action",
    m: "Stock, expiry exposure and orders are within policy thresholds.", go: "" });
  const icoTone = { critical: ["var(--red-soft)", "var(--red-700)"], warn: ["var(--amber-soft)", "var(--amber-700)"],
    info: ["var(--blue-50)", "var(--blue-700)"], ok: ["var(--green-soft)", "var(--green-700)"] };
  $("#dAttention").innerHTML = actions.slice(0, 5).map((a) => `
    <div class="attention-item ${a.tone}">
      <div class="ico" aria-hidden="true" style="background:${icoTone[a.tone][0]};color:${icoTone[a.tone][1]}">${a.ico}</div>
      <div><div class="t">${a.t}</div><div class="m">${a.m}</div></div>
      <div class="go">${a.go}</div>
    </div>`).join("");

  /* ---- recent activity timeline ---- */
  const activity = acts ? [
    ...(acts.uploads || []).map((u) => ({ when: u.created_at, ico: ICO.upload,
      t: `<b>${esc(u.filename)}</b> — ${fmtN(u.rows_accepted)}/${fmtN(u.rows_total)} rows accepted${u.rows_rejected ? `, <span style="color:var(--red-700)">${fmtN(u.rows_rejected)} quarantined</span>` : ""}` })),
    ...(acts.reorders || []).map((r) => ({ when: r.created_at, ico: ICO.cart2,
      t: `Reorder <b>${esc(r.drug)}</b> ×${fmtN(r.qty)} · ${esc(r.status)}` })),
    ...(acts.waste || []).map((w) => ({ when: w.created_at, ico: ICO.trash,
      t: `Write-off <b>${esc(w.drug)}</b> ×${fmtN(w.qty)} (${fmtMoney(w.value)}) · ${esc(w.reason)}` })),
    ...(acts.notifications || []).map((n) => ({ when: n.created_at, ico: ICO.mail,
      t: `Email <b>${esc(n.subject)}</b> · ${esc(n.status)}` })),
  ].sort((a, b) => String(b.when).localeCompare(String(a.when))).slice(0, 8) : [];
  $("#dActivity").innerHTML = activity.map((a) => `
    <div class="act-item"><div class="ico" aria-hidden="true">${a.ico}</div>
      <div class="t">${a.t}</div><span class="when" title="${esc(String(a.when || ""))}">${esc(whenLabel(a.when))}</span></div>`).join("")
    || `<div class="empty" style="padding:18px">No activity yet — uploads, reorders and write-offs will appear here.</div>`;

  // alerts preview
  const alerts = await api("/api/alerts");
  $("#dashAlerts").innerHTML = alerts.items.slice(0, 6).map(alertRow).join("") ||
    `<div class="empty">No active alerts</div>`;
  // (live refresh lives in the shell: startLiveRefresh / stopLiveRefresh)

  // charts
  const days = ov.sales_series || [];
  chart("c-sales", {
    type: "line",
    data: { labels: days.map((d) => d.date.slice(5)), datasets: [
      { label: "Units", data: days.map((d) => d.qty), borderColor: CHART.blue2, backgroundColor: CHART.fill, fill: true, tension: .3, pointRadius: 0, borderWidth: 2 }]},
    options: { maintainAspectRatio: false, plugins: { legend: { display: false } },
      scales: { x: { ...gridOpts, ticks: { maxTicksLimit: 12 } }, y: { ...gridOpts, beginAtZero: true } } },
  });
  const mo = charts_.monthly || [];
  chart("c-monthly", {
    type: "bar",
    data: { labels: mo.map((m) => m.month), datasets: [{ label: "Revenue", data: mo.map((m) => m.revenue),
      backgroundColor: CHART.blue, borderRadius: 7, maxBarThickness: 26 }]},
    options: { maintainAspectRatio: false, plugins: { legend: { display: false },
      tooltip: { callbacks: { label: (c) => fmtMoney(c.parsed.y) } } },
      scales: { x: gridOpts, y: { ...gridOpts, ticks: { callback: (v) => "₹" + (v / 1000) + "k" } } } },
  });
  const cats = charts_.categories || [];
  chart("c-cat", {
    type: "doughnut",
    data: { labels: cats.map((c) => c.category || "Other"), datasets: [{ data: cats.map((c) => c.value),
      backgroundColor: PALETTE, borderWidth: 2, borderColor: "#fff" }]},
    options: { maintainAspectRatio: false, cutout: "62%", plugins: { legend: { position: "right", labels: { boxWidth: 10, font: { size: 11 } } } } },
  });
  const tl = (charts_.expiry_timeline?.buckets) || [];
  chart("c-exp", {
    type: "bar",
    data: { labels: tl.map((b) => b.bucket), datasets: [{ label: "Units", data: tl.map((b) => b.qty),
      backgroundColor: tl.map((b) => b.bucket.endsWith("Q4") || b.bucket.endsWith("Q1") ? CHART.red : CHART.blue2),
      borderRadius: 4, maxBarThickness: 30 }]},
    options: { maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: gridOpts, y: gridOpts } },
  });
  const acc = charts_.forecast_accuracy || [];
  chart("c-acc", {
    type: "bar",
    data: { labels: acc.map((a) => a.drug), datasets: [
      { label: "Model", data: acc.map((a) => a.wmape == null ? null : Math.max(0, 100 - a.wmape)), backgroundColor: CHART.blue, borderRadius: 7 },
      { label: "Baseline", data: acc.map((a) => a.baseline == null ? null : Math.max(0, 100 - a.baseline)), backgroundColor: "#cbd5e1", borderRadius: 4 }]},
    options: { indexAxis: "y", maintainAspectRatio: false, plugins: { legend: { position: "bottom", labels: { boxWidth: 10, font: { size: 11 } } } },
      scales: { x: { ...gridOpts, max: 100, beginAtZero: true }, y: gridOpts } },
  });
  const sup = charts_.supplier_spend || [];
  chart("c-sup", {
    type: "bar",
    data: { labels: sup.map((s) => s.supplier || "—"), datasets: [
      { label: "Purchases", data: sup.map((s) => s.spend), backgroundColor: CHART.blue, borderRadius: 7, maxBarThickness: 30 },
      { label: "Waste", data: sup.map((s) => s.waste), backgroundColor: CHART.red, borderRadius: 7, maxBarThickness: 30 }]},
    options: { maintainAspectRatio: false, plugins: { legend: { position: "bottom", labels: { boxWidth: 10, font: { size: 11 } } },
      tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${fmtMoney(c.parsed.y)}` } } },
      scales: { x: gridOpts, y: { ...gridOpts, ticks: { callback: (v) => "₹" + (v / 100000) + "L" } } } },
  });
}

function alertRow(a) {
  const type = String(a.atype || "").replace(/_/g, " ");
  return `<div class="alert ${esc(a.severity)}${a.status === "acknowledged" ? " acked" : ""}">
    <div class="sev"></div>
    <div class="body">
      <div class="t">${esc(a.title)}</div>
      <div class="m">${esc(a.message)}</div>
      <div class="meta">${esc(type)} · ${esc(a.created_at)} ${a.drug ? "· " + esc(a.drug) : ""}</div>
    </div>
    <div class="side">${badge(a.severity, a.severity)}
      ${a.status === "active" ? `<button class="btn sm" data-ack="${a.id}">Ack</button>` : badge("acked", "ok")}</div>
  </div>`;
}

/* ============ reusable table: client-side sort + pagination ============ */
function sortableTable({ mount, columns, rows, rowHtml, pageSize = 12, empty = "Nothing to show", filteredEmpty = "No rows match your filters", afterRender: afterRenderOpt = null }) {
  // columns: [{key, label, num?, sortVal?(r)}] — rows: raw data array
  const state = { sortKey: null, dir: 1, page: 1, q: "", filter: "" };
  // rows may carry a __filter tag (e.g. status); rows are mutated in place by callers
  const norm = (v) => String(v ?? "").toLowerCase();
  const apply = () => {
    let out = rows;
    if (state.q) out = out.filter((r) => norm(Object.values(r).flat().join(" ")).includes(state.q));
    if (state.filter) out = out.filter((r) => String(r.__filter) === state.filter);
    if (state.sortKey) {
      const col = columns.find((c) => c.key === state.sortKey);
      const val = (r) => col.sortVal ? col.sortVal(r) : r[col.key];
      out = [...out].sort((a, b) => {
        const va = val(a), vb = val(b);
        if (typeof va === "number" && typeof vb === "number") return (va - vb) * state.dir;
        return String(va ?? "").localeCompare(String(vb ?? "")) * state.dir;
      });
    }
    return out;
  };
  const render = () => {
    // preserve focus across re-render — keyboard users tab to a header, press Enter, must not fall back to body
    const prevFocusSort = mount.contains(document.activeElement) && document.activeElement.matches?.("th.sortable")
      ? document.activeElement.dataset.sort : null;
    const data = apply();
    const pages = Math.max(1, Math.ceil(data.length / pageSize));
    if (state.page > pages) state.page = pages;
    const slice = data.slice((state.page - 1) * pageSize, state.page * pageSize);
    const ths = columns.map((c) => {
      const sortable = !!c.sortVal;
      const dir = state.sortKey === c.key ? state.dir : 0;
      return `<th class="${c.num ? "num " : ""}${sortable ? "sortable" : ""}" role="columnheader"
        ${sortable ? `aria-sort=${dir === 0 ? '"none"' : dir > 0 ? '"ascending"' : '"descending"'} tabindex="0"
        data-sort="${c.key}" aria-label="Sort by ${c.label}"` : ""}>${sortable
          ? `<span class="th-btn">${c.label}<span class="sort-ind" aria-hidden="true">${dir === 0 ? "⇅" : dir > 0 ? "▲" : "▼"}</span></span>`
          : c.label}</th>`;
    }).join("");
    const bodyHtml = slice.length ? slice.map(rowHtml).join("")
      : emptyState(ICO.search, state.q || state.filter ? filteredEmpty : empty,
        state.q || state.filter ? "Try a different search term or clear the filters." :
        "Rows will appear here as soon as data arrives.");
    mount.innerHTML = `
      <div class="table-toolbar">
        <input type="search" aria-label="Search table" placeholder="Search…" value="${esc(state.q)}" data-tq style="width:220px">
        <div class="right"><span class="result-count">${fmtN(data.length)} row${data.length === 1 ? "" : "s"}</span></div>
      </div>
      <div class="table-wrap"><table><thead><tr>${ths}</tr></thead><tbody data-tbody>${bodyHtml}</tbody></table></div>
      <div class="pager" data-pager></div>`;
    const pager = $mount("[data-pager]");
    pager.innerHTML = `
      <span>Page ${state.page} of ${pages}</span>
      <span class="pages">
        <button data-p="1" aria-label="First page" ${state.page === 1 ? "disabled" : ""}>«</button>
        <button data-p="${state.page - 1}" aria-label="Previous page" ${state.page === 1 ? "disabled" : ""}>‹</button>
        <button data-p="${state.page + 1}" aria-label="Next page" ${state.page === pages ? "disabled" : ""}>›</button>
        <button data-p="${pages}" aria-label="Last page" ${state.page === pages ? "disabled" : ""}>»</button>
      </span>`;
    pager.querySelectorAll("button").forEach((b) => b.onclick = () => { state.page = +b.dataset.p; render(); });
    mount.querySelector("[data-tq]").oninput = (e) => { state.q = e.target.value.toLowerCase(); state.page = 1; renderBodyOnly(); };
    // one handler per header cell — the inner button bubbles up to the th
    mount.querySelectorAll("th.sortable").forEach((el) => {
      el.onclick = () => {
        const k = el.dataset.sort;
        if (state.sortKey === k) state.dir = -state.dir; else { state.sortKey = k; state.dir = 1; }
        render();
      };
      el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); el.click(); } };
    });
    if (prevFocusSort) mount.querySelector(`th[data-sort="${prevFocusSort}"]`)?.focus();
    if (afterRender) afterRender(slice);
  };
  const renderBodyOnly = () => {
    // light re-render preserving the toolbar input focus
    const data = apply();
    const pages = Math.max(1, Math.ceil(data.length / pageSize));
    if (state.page > pages) state.page = pages;
    const slice = data.slice((state.page - 1) * pageSize, state.page * pageSize);
    mount.querySelector("[data-tbody]").innerHTML = slice.length ? slice.map(rowHtml).join("")
      : `<tr><td colspan="${columns.length}">${emptyState(ICO.search, filteredEmpty, "Try a different search term or clear the filters.")}</td></tr>`;
    mount.querySelector(".result-count").textContent = `${fmtN(data.length)} row${data.length === 1 ? "" : "s"}`;
    const pager = $mount("[data-pager]");
    pager.querySelectorAll("button").forEach((b) => {
      b.disabled = (+b.dataset.p < 1 || +b.dataset.p > pages);
    });
    pager.querySelector("span").textContent = `Page ${state.page} of ${pages}`;
    if (afterRender) afterRender(slice);
  };
  let afterRender = afterRenderOpt; // set in ctor so the FIRST render binds row handlers too
  const $mount = (s) => mount.querySelector(s);
  render();
  return {
    set afterRender(fn) { afterRender = fn; },
    rows,
    setFilter(v) { state.filter = v || ""; state.page = 1; render(); },
    refresh() { render(); },
  };
}

/* ============================== INVENTORY ============================== */
async function viewInventory(view) {
  const [drugs, suppliers] = await Promise.all([api("/api/drugs"), api("/api/suppliers")]);
  view.innerHTML = `
    <div class="filters">
      <select id="invFilter" aria-label="Filter by status">
        <option value="">All statuses</option>
        <option value="order_now">Order now</option>
        <option value="scheduled">Scheduled</option>
        <option value="healthy">Healthy</option>
      </select>
      <div class="right btn-row">
        <button class="btn" id="invCsv">${ICO.down} Inventory CSV</button>
        <button class="btn" id="invDispense">${ICO.down} FEFO dispense…</button>
        <button class="btn primary" id="invRx">${ICO.pill} Add medicine (Rx)</button>
      </div>
    </div>
    <div class="card flush" id="invTable"></div>
    <p class="muted" style="margin-top:10px">ROP = reorder point (lead-time demand + safety stock at the configured
      service level, or a manual override). Cover = days of stock at forecast demand.</p>`;

  const table = sortableTable({
    mount: $("#invTable"),
    columns: [
      { key: "name", label: "Medicine", sortVal: (r) => r.name },
      { key: "category", label: "Category", sortVal: (r) => r.category || "" },
      { key: "usable", label: "Usable", num: true, sortVal: (r) => r.usable },
      { key: "expired", label: "Expired", num: true, sortVal: (r) => r.expired },
      { key: "usable_value", label: "Value", num: true, sortVal: (r) => r.usable_value },
      { key: "cover", label: "Cover (d)", num: true, sortVal: (r) => r.plan.cover_days },
      { key: "avg", label: "Avg/day", num: true, sortVal: (r) => r.plan.avg_daily ?? 0 },
      { key: "rop", label: "ROP", num: true, sortVal: (r) => r.plan.reorder_point ?? 0 },
      { key: "status", label: "Status", sortVal: (r) => r.plan.status },
      { key: "actions", label: "", sortVal: null },
    ],
    rows: drugs.map((d) => ({ ...d, __filter: d.plan.status })),
    pageSize: 10,
    empty: "No medicines in the catalogue yet",
    filteredEmpty: "No medicines match your search or filter",
    rowHtml: (d) => `
      <tr class="clickable" data-drug="${d.id}" tabindex="0" aria-label="Open ${esc(d.name)} details">
        <td class="primary-cell"><b>${esc(d.name)}</b><div class="muted">${esc(d.generic || "—")} · ${esc(d.form || "")} · ${esc(d.schedule || "")}</div></td>
        <td>${badge(d.category || "Other", "info")}</td>
        <td class="num">${fmtN(d.usable)}</td>
        <td class="num">${d.expired ? `<span style="color:var(--red-700)" data-tip="${fmtN(d.expired)} units past expiry — blocked from dispensing">${fmtN(d.expired)}</span>` : "0"}</td>
        <td class="num">${fmtMoney(d.usable_value)}</td>
        <td class="num">${d.plan.cover_days > 9998 ? "∞" : fmtN(Math.round(d.plan.cover_days))}</td>
        <td class="num">${d.plan.avg_daily ?? "–"}</td>
        <td class="num">${d.plan.reorder_point ?? "–"}${d.plan.manual_override ? " ✎" : ""}</td>
        <td>${statusBadge(d.plan.status)}</td>
        <td class="row-actions"><button class="btn sm" data-detail="${d.id}">Open</button></td>
      </tr>`,
    afterRender: () => {
      $$("#invTable [data-detail]").forEach((b) => b.onclick = (e) => {
        e.stopPropagation();
        openDrugModal(drugs, suppliers, +b.dataset.detail);
      });
      $$("#invTable tr[data-drug]").forEach((tr) => {
        tr.onclick = () => openDrugModal(drugs, suppliers, +tr.dataset.drug);
        tr.onkeydown = (e) => { if (e.key === "Enter") openDrugModal(drugs, suppliers, +tr.dataset.drug); };
      });
    },
  });
  // wire the status filter into the component's __filter channel
  $("#invFilter").onchange = (e) => {
    table.rows.forEach((r) => { r.__filter = r.plan.status; });
    table.setFilter(e.target.value);
  };
  $("#invCsv").onclick = () => download("/api/reports/inventory.csv");
  $("#invDispense").onclick = () => dispenseModal(drugs);
  $("#invRx").onclick = () => prescriptionModal();
}

function dispenseModal(drugs) {
  openModal("FEFO dispense (issue stock)", `
    <div class="field"><label class="f">Medicine</label>${drugSelect("dpDrug", drugs)}</div>
    <div class="field"><label class="f">Quantity</label><input id="dpQty" type="number" min="1" value="10"></div>
    <div class="field"><label class="f">Note</label><input id="dpNote" placeholder="e.g. counter sale / ward issue"></div>
    <div class="btn-row"><button class="btn primary" id="dpGo">Dispense (FEFO)</button></div>
    <div id="dpOut"></div>`);
  $("#dpGo").onclick = async () => {
    try {
      const r = await post("/api/dispense", { drug_id: +$("#dpDrug").value, qty: +$("#dpQty").value, note: $("#dpNote").value });
      $("#dpOut").innerHTML = `<div class="notice" style="margin-top:10px">Dispensed <b>${r.dispensed}</b> of ${r.requested}
        units across ${r.batches.length} batch(es): ${r.batches.map((b) => `${esc(b.batch_no)} (${b.qty})`).join(", ")}</div>`;
      navigate();
    } catch (e) { $("#dpOut").innerHTML = `<div class="notice bad" style="margin-top:10px">${esc(e.message)}</div>`; }
  };
}

/* Add Medicine (doctor prescribed): type the medicine exactly as written on the
   prescription slip; it is FEFO-issued and deducted from total stock. */
async function prescriptionModal() {
  const drugs = await api("/api/drugs");
  const norm = (s) => String(s || "").toLowerCase().replace(/[-_/.,]/g, " ").replace(/\s+/g, " ").trim();
  const findDrug = (text) => {
    const n = norm(text);
    if (!n) return null;
    return drugs.find((d) => norm(d.name) === n)
      || drugs.find((d) => norm(d.generic) === n) || null;
  };
  openModal("Add medicine (doctor prescribed)", `
    <div class="notice">Enter the medicine exactly as written on the prescription. It is issued
      <b>FEFO</b> — earliest-expiry usable batch first — and <b>deducted from stock immediately</b>.</div>
    <div class="field">
      <label class="f" for="rxName">Prescribed medicine <span class="req" aria-hidden="true">*</span></label>
      <input id="rxName" list="rxNames" placeholder="e.g. Dolo 650" autocomplete="off" style="width:100%"
        aria-describedby="rxStock" aria-required="true">
      <datalist id="rxNames">${drugs.map((d) => `<option value="${esc(d.name)}">`).join("")}</datalist>
      <div class="hint" id="rxStock" aria-live="polite">Type a medicine name…</div>
    </div>
    <div class="form-grid">
      <div class="field">
        <label class="f" for="rxQty">Quantity <span class="req" aria-hidden="true">*</span></label>
        <input id="rxQty" type="number" min="1" value="1" aria-describedby="rxQtyHint" required>
        <div class="hint" id="rxQtyHint">Whole units to issue now.</div>
      </div>
      <div class="field">
        <label class="f" for="rxDoctor">Prescribed by</label>
        <input id="rxDoctor" placeholder="Dr. name" autocomplete="off">
      </div>
      <div class="field">
        <label class="f" for="rxPatient">Patient / reference</label>
        <input id="rxPatient" placeholder="e.g. OPD-42">
        <div class="hint">Optional — helps trace the issue later.</div>
      </div>
      <div class="field">
        <label class="f" for="rxNote">Note</label>
        <input id="rxNote" placeholder="optional">
      </div>
    </div>
    <div class="btn-row" style="margin-top:6px">
      <button class="btn primary" id="rxGo">Add &amp; deduct from stock</button>
      <button class="btn ghost" id="rxCancel">Cancel</button>
    </div>
    <div id="rxOut" aria-live="polite"></div>`);
  $("#rxCancel").onclick = closeModal;
  const showStock = () => {
    const d = findDrug($("#rxName").value);
    $("#rxStock").innerHTML = !$("#rxName").value.trim() ? "Type a medicine name…"
      : d ? `✓ <b>${esc(d.name)}</b> (${esc(d.generic || "—")}) — <b>${fmtN(d.usable)}</b> usable units in stock`
            + (d.expired ? `, ${fmtN(d.expired)} expired/blocked` : "")
          : `✗ Not in catalogue — add it via a purchase upload first`;
    return d;
  };
  $("#rxName").oninput = showStock;
  showStock();
  const submit = async () => {
    const d = findDrug($("#rxName").value);
    if (!d) {
      $("#rxName").setAttribute("aria-invalid", "true");
      $("#rxOut").innerHTML = errorPanel("Medicine not found", "Pick a name from the suggestions list, or ask an admin to upload a purchase for this medicine first.");
      return;
    }
    $("#rxName").removeAttribute("aria-invalid");
    const q = Math.floor(+$("#rxQty").value);
    if (!q || q <= 0) {
      $("#rxQty").setAttribute("aria-invalid", "true");
      $("#rxOut").innerHTML = `<div class="notice bad">Enter a quantity of at least 1.</div>`;
      return;
    }
    $("#rxQty").removeAttribute("aria-invalid");
    const go = $("#rxGo"); go.classList.add("loading");
    try {
      const r = await post("/api/prescriptions", {
        drug_id: d.id, qty: +$("#rxQty").value,
        doctor: $("#rxDoctor").value, patient: $("#rxPatient").value, note: $("#rxNote").value,
      });
      let msg = `Added <b>${r.dispensed}</b> of ${r.requested} units of <b>${esc(r.drug)}</b> (as prescribed) from ` +
        `${r.batches.length} batch(es): ${r.batches.map((b) => `${esc(b.batch_no)} ×${b.qty}`).join(", ")}. ` +
        `Usable stock: <b>${fmtN(r.usable_after)}</b> units.`;
      if (r.shortfall > 0) msg += ` ⚠ Shortfall ${r.shortfall} — not enough usable stock for the full prescription.`;
      if (r.blocked_expired > 0) msg += ` ⚠ ${fmtN(r.blocked_expired)} expired units exist and are blocked (never issued).`;
      $("#rxOut").innerHTML = `<div class="notice ${r.shortfall ? "warn" : ""}" style="margin-top:10px">${msg}</div>`;
      toast(`Prescription added — ${r.dispensed} × ${r.drug} issued, stock −${r.dispensed}`);
      navigate();
    } catch (e) {
      $("#rxOut").innerHTML = errorPanel("Could not add the medicine", e.message);
    } finally { go.classList.remove("loading"); }
  };
  $("#rxGo").onclick = submit;
  $("#rxQty").onkeydown = (e) => { if (e.key === "Enter") submit(); };
  $("#rxName").focus();
}

/* ============================== PATIENT COUNTER ============================== */
async function viewCounter(view) {
  const drugs = await api("/api/drugs");
  const norm = (s) => String(s || "").toLowerCase().replace(/[-_/.,]/g, " ").replace(/\s+/g, " ").trim();
  const findDrug = (text) => {
    const n = norm(text);
    if (!n) return null;
    return drugs.find((d) => norm(d.name) === n)
      || drugs.find((d) => norm(d.generic) === n) || null;
  };
  view.innerHTML = `
    <div class="notice">Patient asks for a medicine → the system picks the batch <b>nearest expiry</b>
      (FEFO) and tells you the <b>shelf</b> to take it from. Issuing immediately deducts total stock.</div>
    <div class="card">
      <h3>What did the patient ask for?</h3>
      <div class="form-grid" style="margin-top:8px">
        <div class="field">
          <label class="f" for="ctName">Medicine <span class="req" aria-hidden="true">*</span></label>
          <input id="ctName" list="ctNames" placeholder="e.g. Dolo 650" autocomplete="off" style="width:100%" aria-required="true">
          <datalist id="ctNames">${drugs.map((d) => `<option value="${esc(d.name)}">`).join("")}</datalist>
          <div class="hint">Brand or generic — e.g. “Dolo-650” or “Paracetamol”.</div>
        </div>
        <div class="field">
          <label class="f" for="ctQty">Quantity <span class="req" aria-hidden="true">*</span></label>
          <input id="ctQty" type="number" min="1" value="1" required>
        </div>
        <div class="field">
          <label class="f" for="ctPatient">Patient / Rx reference</label>
          <input id="ctPatient" placeholder="optional">
        </div>
        <div class="field" style="display:flex;align-items:flex-end;gap:8px">
          <button class="btn" id="ctFind">${ICO.search} Find shelf</button>
          <button class="btn primary" id="ctIssue">${ICO.pill} Issue (FEFO)</button></div>
      </div>
      <div id="ctOut" aria-live="polite" style="margin-top:12px"></div>
    </div>`;
  const qty = () => Math.max(1, +$("#ctQty").value || 1);
  const shelfBadge = (code, zone) => code
    ? `<span class="badge ${zone === "pick" ? "ok" : zone === "quarantine" ? "bad" : "info"}"
        style="font-size:13.5px;padding:5px 11px">${ICO.shelf} Shelf ${esc(code)}</span>`
    : `<span class="badge warn">not yet placed — see Shelf Tasks</span>`;
  const find = async () => {
    try {
      const r = await api(`/api/counter/lookup?name=${encodeURIComponent($("#ctName").value)}&qty=${qty()}`);
      const p = r.pick;
      $("#ctOut").innerHTML = `
        <div class="notice ${r.enough ? "" : "warn"}">
          <b>${esc(r.drug)}</b> (${esc(r.generic || "—")}) · <b>${fmtN(r.usable)}</b> usable units in stock
          ${p ? `<div style="margin-top:8px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
            ${shelfBadge(p.shelf_code, p.shelf_zone)}
            <span>Take batch <b class="mono">${esc(p.batch_no)}</b> — nearest expiry ${fmtDate(p.expiry_date)}
            ${daysLeftBadge(p.days_to_expiry)} — ${fmtN(p.qty_remaining)} units on that batch</span></div>` : ""}
          ${!r.enough ? `<div style="margin-top:6px">⚠ ${p ? "Not enough usable stock for this quantity — raise a reorder." : "No usable (unexpired) stock at all."}</div>` : ""}
        </div>`;
    } catch (e) { $("#ctOut").innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; }
  };
  const issue = async () => {
    const d = findDrug($("#ctName").value);
    if (!d) { $("#ctOut").innerHTML = `<div class="notice bad">Medicine not in catalogue — ask admin to upload a purchase first.</div>`; return; }
    try {
      const r = await post("/api/prescriptions", { drug_id: d.id, qty: qty(),
        patient: $("#ctPatient").value, note: "patient counter issue" });
      $("#ctOut").innerHTML = `<div class="notice ${r.shortfall ? "warn" : ""}">
        ${ICO.check} Issued <b>${r.dispensed}</b> of ${r.requested} units of <b>${esc(r.drug)}</b>:
        ${r.batches.map((b) => `batch <b class="mono">${esc(b.batch_no)}</b> ×${b.qty} from ${shelfBadge(b.shelf, b.shelf_zone)}`).join(" + ") || "—"}
        <div style="margin-top:6px">Usable stock now <b>${fmtN(r.usable_after)}</b>${r.shortfall ? ` · ⚠ shortfall ${r.shortfall}` : ""}${r.blocked_expired ? ` · ${fmtN(r.blocked_expired)} expired units blocked` : ""}</div></div>`;
      toast(`Issued ${r.dispensed} × ${r.drug} — stock −${r.dispensed}`);
      updateTaskBadge();
    } catch (e) { $("#ctOut").innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; }
  };
  $("#ctFind").onclick = find;
  $("#ctIssue").onclick = issue;
  $("#ctName").onkeydown = (e) => { if (e.key === "Enter") find(); };
  $("#ctName").focus();
}

/* ============================== SHELF TASKS ============================== */
async function viewShelfTasks(view) {
  const [data, done] = await Promise.all([
    api("/api/shelf/tasks?status=pending"), api("/api/shelf/tasks?status=done&limit=15")]);
  const c = data.counts;
  const row = (t) => `<tr>
    <td><b>${esc(t.drug || "—")}</b>${t.batch_no ? `<div class="muted mono">${esc(t.batch_no)}</div>` : ""}</td>
    <td>${t.kind === "putaway" ? badge("put new batch", "info") : badge("shift stock", "warn")}</td>
    <td>${t.from_code ? badge(`from ${esc(t.from_code)}`, "mute") : `<span class="muted">receiving</span>`}</td>
    <td>${badge(`to ${esc(t.to_code || "?")}`, t.kind === "putaway" ? "ok" : "info")}</td>
    <td class="muted">${esc(t.reason || "")}</td>
    <td class="row-actions"><button class="btn sm primary" data-done="${t.id}">${t.kind === "putaway" ? "Placed ✓" : "Moved ✓"}</button></td>
  </tr>`;
  view.innerHTML = `
    <div class="grid g3">
      ${kpi("Put new batches", fmtN(c.putaway), "where each received batch goes", "info")}
      ${kpi("Shift stock", fmtN(c.shift), "nearest-expiry → pick shelf, expired → quarantine", "warn")}
      ${kpi("Pending total", fmtN(c.pending), "directives for the pharmacist", c.pending ? "bad" : "good")}
    </div>
    <div class="filters"><div class="right btn-row">
      <button class="btn" id="tkRefresh">${ICO.refresh} Recompute directives</button></div></div>
    <div class="section-title">Where to place each new batch</div>
    <div class="card flush"><div class="table-wrap"><table>
      <thead><tr><th>Medicine</th><th>Task</th><th>From</th><th>To shelf</th><th>Reason</th><th></th></tr></thead>
      <tbody>${data.items.filter((t) => t.kind === "putaway").map(row).join("") ||
        `<tr><td colspan="6" class="empty">No new batches waiting — upload a purchase file</td></tr>`}</tbody>
    </table></div></div>
    <div class="section-title">Shift medicine from shelf to shelf <span class="muted">— as directed by the website</span></div>
    <div class="card flush"><div class="table-wrap"><table>
      <thead><tr><th>Medicine</th><th>Task</th><th>From</th><th>To shelf</th><th>Reason</th><th></th></tr></thead>
      <tbody>${data.items.filter((t) => t.kind === "shift").map(row).join("") ||
        `<tr><td colspan="6" class="empty">Shelves are correctly arranged — nothing to move</td></tr>`}</tbody>
    </table></div></div>
    <div class="section-title">Completed (latest)</div>
    <div class="card flush"><div class="table-wrap"><table>
      <thead><tr><th>#</th><th>Medicine</th><th>Task</th><th>To shelf</th><th>Done at</th></tr></thead>
      <tbody>${done.items.map((t) => `<tr><td class="muted">${t.id}</td><td>${esc(t.drug || "—")}</td>
        <td>${esc(t.kind)}</td><td>${badge(t.to_code || "—", "mute")}</td><td class="muted">${esc(t.done_at || "")}</td></tr>`).join("") ||
        `<tr><td colspan="5" class="empty">Nothing completed yet</td></tr>`}</tbody>
    </table></div></div>`;
  $$("[data-done]").forEach((b) => b.onclick = async () => {
    try { await post(`/api/shelf/tasks/${b.dataset.done}/done`);
      toast("Task completed — shelf map updated"); viewShelfTasks(view); updateTaskBadge(); }
    catch (e) { toast(e.message, true); }
  });
  $("#tkRefresh").onclick = async () => {
    try { const r = await post("/api/shelf/tasks/refresh");
      toast(`Directives recomputed — ${r.created} shift rule(s), ${r.pending} pending`);
      viewShelfTasks(view); }
    catch (e) { toast(e.message, true); }
  };
}

async function openDrugModal(drugs, suppliers, drugId) {
  const d = drugs.find((x) => x.id === drugId);
  if (!d) return;
  const [batches, fva, fc] = await Promise.all([
    api(`/api/batches?drug_id=${d.id}`),
    api(`/api/forecast-vs-actual?drug_id=${d.id}`),
    api(`/api/sales?drug_id=${d.id}&days=90`),
  ]);
  openModal(`${esc(d.name)} — ${esc(d.generic || "")}`, `
    <div class="grid g4" style="margin-bottom:12px">
      ${kpi("Usable stock", fmtN(d.usable), fmtMoney(d.usable_value), "info", false)}
      ${kpi("Expired / blocked", fmtN(d.expired), fmtMoney(d.expired_value), d.expired ? "bad" : "", false)}
      ${kpi("Days of cover", d.plan.cover_days > 9998 ? "∞" : fmtN(Math.round(d.plan.cover_days)), `avg ${d.plan.avg_daily}/day`, "", false)}
      ${kpi("Suggested order", fmtN(d.plan.order_qty), `${statusBadge(d.plan.status)}`, "", false)}
    </div>
    <div class="grid g2">
      <div class="card"><h3>Daily sales (90d)</h3><div class="chart-box sm"><canvas id="m-sales"></canvas></div></div>
      <div class="card"><h3>Model back-test</h3><div class="sub">${esc(fva.model || "")} vs actuals on holdout</div>
        <div class="chart-box sm"><canvas id="m-fva"></canvas></div></div>
    </div>
    <div class="card" style="margin-top:14px"><div class="card-head"><h3>Batches (FEFO order)</h3>
      <span class="muted">${batches.length} batches</span></div>
      <div class="table-wrap"><table>
        <thead><tr><th>#</th><th>Batch</th><th>Expiry</th><th>Days left</th><th class="num">Remaining</th>
          <th class="num">Cost</th><th>Supplier</th><th>Received</th></tr></thead>
        <tbody>${batches.map((b) => `<tr>
          <td class="muted">${b.fefo_rank}</td><td class="mono">${esc(b.batch_no)}</td>
          <td>${fmtDate(b.expiry_date)}</td><td>${daysLeftBadge(b.days_to_expiry)}</td>
          <td class="num">${fmtN(b.qty_remaining)}</td><td class="num">${fmtMoney2(b.unit_cost)}</td>
          <td>${esc(b.supplier || "—")}</td><td>${fmtDate(b.received_date)}</td></tr>`).join("") ||
          `<tr><td colspan="8" class="empty">No open batches</td></tr>`}</tbody>
      </table></div></div>
    <div class="divider"></div>
    <div class="form-grid">
      <div class="field"><label class="f">Lead time (days)</label>
        <input id="mLead" type="number" min="1" value="${d.lead_time_days || 7}"></div>
      <div class="field"><label class="f">Reorder point override (blank = AI-computed)</label>
        <input id="mRop" type="number" min="0" placeholder="${d.plan.reorder_point_auto ?? ""}" value="${d.reorder_point ?? ""}"></div>
      <div class="field"><label class="f">Supplier</label>
        <select id="mSup">${suppliers.items.map((s) => `<option value="${s.id}" ${d.supplier_id === s.id ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></div>
      <div class="field" style="display:flex;align-items:flex-end"><button class="btn primary" id="mSave">Save policy</button></div>
    </div>`);
  chart("m-sales", {
    type: "line", data: { labels: (fc || []).map((r) => r.date.slice(5)),
      datasets: [      { label: "Units", data: fc.map((r) => r.qty), borderColor: CHART.blue2, tension: .3, pointRadius: 0, fill: true, backgroundColor: CHART.fill }]},
    options: { maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { ...gridOpts, ticks: { maxTicksLimit: 10 } }, y: gridOpts } },
  });
  const pts = fva.points || [];
  chart("m-fva", {
    type: "line", data: { labels: pts.map((p) => p.date.slice(5)), datasets: [
      { label: "Actual", data: pts.map((p) => p.actual), borderColor: CHART.indigo, pointRadius: 0, borderWidth: 2 },
      { label: "Forecast", data: pts.map((p) => p.predicted), borderColor: CHART.amber, borderDash: [5, 4], pointRadius: 0, borderWidth: 2 }]},
    options: { maintainAspectRatio: false, plugins: { legend: { position: "bottom", labels: { boxWidth: 10 } } },
      scales: { x: { ...gridOpts, ticks: { maxTicksLimit: 10 } }, y: gridOpts } },
  });
  $("#mSave").onclick = async () => {
    const rop = $("#mRop").value === "" ? null : +$("#mRop").value;
    try {
      await patch(`/api/drugs/${d.id}`, { lead_time_days: +$("#mLead").value, reorder_point: rop, supplier_id: +$("#mSup").value });
      toast(`${d.name} policy saved — alerts refreshed`);
      closeModal(); navigate();
    } catch (e) { toast(e.message, true); }
  };
}

/* ============================== SMARTSHELF / EXPIRY ============================== */
async function viewShelf(view) {
  const [data, drugs] = await Promise.all([api("/api/expiry"), api("/api/drugs")]);
  const B = data.buckets;
  view.innerHTML = `
    <div class="grid g5">
      ${kpi("Expired", fmtN(B.expired.qty), fmtMoney(B.expired.value), "bad")}
      ${kpi("≤30 days", fmtN(B["0-30"].qty), fmtMoney(B["0-30"].value), "warn")}
      ${kpi("31–90 days", fmtN(B["31-90"].qty), fmtMoney(B["31-90"].value), "warn")}
      ${kpi("91–180 days", fmtN(B["91-180"].qty), fmtMoney(B["91-180"].value), "info")}
      ${kpi(">180 days", fmtN(B["180+"].qty), fmtMoney(B["180+"].value), "good")}
    </div>
    <div class="section-title">FEFO queue — dispense in this order</div>
    <div class="notice">FEFO = <b>First Expiry, First Out</b>. The SmartShelf ranks every open batch by expiry date;
      dispensing and returns always consume rank&nbsp;1 first so nothing lapses on the shelf.</div>
    <div class="filters">
      <select id="shStatus">
        <option value="">All batches</option>
        <option value="expired">Expired</option>
        <option value="critical">≤30 days</option>
        <option value="warning">31–90 days</option>
        <option value="ok">&gt;90 days</option>
      </select>
      ${drugSelect("shDrug", drugs)}
      <div class="right btn-row">
        <button class="btn" id="shExpiry">${ICO.down} Expiry report</button>
        <button class="btn" id="shReturn">${ICO.refresh} Draft vendor returns…</button>
      </div>
    </div>
    <div class="card flush"><div class="table-wrap tall"><table>
      <thead><tr><th>FEFO</th><th>Medicine</th><th>Batch</th><th>Expiry</th><th>Days</th>
        <th class="num">Qty</th><th class="num">Value</th><th>Supplier</th><th>Status</th></tr></thead>
      <tbody id="shBody"></tbody>
    </table></div></div>`;

  const draw = () => {
    const st = $("#shStatus").value, dn = $("#shDrug").value;
    const rows = data.shelf.filter((b) => (!st || b.status === st) && (!dn || String(b.drug_id) === dn));
    $("#shBody").innerHTML = rows.map((b) => `<tr>
      <td class="muted">${b.fefo_rank}</td>
      <td><b>${esc(b.drug)}</b></td><td class="mono">${esc(b.batch_no)}</td>
      <td>${fmtDate(b.expiry_date)}</td><td>${daysLeftBadge(b.days_to_expiry)}</td>
      <td class="num">${fmtN(b.qty_remaining)}</td><td class="num">${fmtMoney(b.value)}</td>
      <td>${esc(b.supplier || "—")}</td><td>${statusBadge(b.status)}</td></tr>`).join("")
      || `<tr><td colspan="9" class="empty">Nothing matches</td></tr>`;
  };
  $("#shStatus").onchange = draw; $("#shDrug").onchange = draw; draw();
  $("#shReturn").onclick = () => viewWasteReturnModal();
  $("#shExpiry").onclick = () => download("/api/reports/expiry.csv");
  if (!isAdmin()) { // pharmacists get the FEFO queue, not admin-only actions
    ["#shReturn", "#shExpiry"].forEach((sel) => { const b = $(sel); if (b) b.style.display = "none"; });
  }
}

/* ============================== FORECAST & REORDER ============================== */
async function viewForecast(view) {
  const [fc, ro] = await Promise.all([api("/api/forecast"), api("/api/reorders")]);
  view.innerHTML = `
    <div class="btn-row" style="margin-bottom:14px">
      <button class="btn primary" id="fcRecompute">⟳ Recompute forecasts</button>
      <button class="btn" id="fcDraftPOs">${ICO.mail} Draft supplier POs</button>
      <button class="btn" id="fcCsv">${ICO.down} Forecast CSV</button>
      <button class="btn" id="fcEval">Model evaluation</button>
      <button class="btn" id="fcEvalCsv">${ICO.down} Eval CSV</button>
      <div class="right muted">Models: damped Holt-Winters (weekly seasonality), SES, seasonal-naive, MA —
        picked by rolling back-test</div>
    </div>
    <div class="section-title">AI reorder suggestions</div>
    <div class="card flush"><div class="table-wrap"><table>
      <thead><tr><th>Medicine</th><th>Supplier</th><th class="num">On hand</th><th class="num">Avg/day</th>
        <th class="num">LT demand</th><th class="num">Safety</th><th class="num">ROP</th>
        <th class="num">Order qty</th><th>Due</th><th>Status</th><th></th></tr></thead>
      <tbody>${ro.suggestions.map((s) => `<tr>
        <td><b>${esc(s.drug)}</b></td><td>${esc(s.supplier || "—")}</td>
        <td class="num">${fmtN(s.available)}</td><td class="num">${s.avg_daily}</td>
        <td class="num">${fmtN(s.demand)}</td><td class="num">${fmtN(s.safety)}</td>
        <td class="num">${fmtN(s.reorder_point)}</td>
        <td class="num"><b>${fmtN(s.order_qty)}</b></td>
        <td>${s.due_date ? fmtDate(s.due_date) : "—"}</td><td>${statusBadge(s.status)}</td>
        <td class="row-actions">${s.status === "order_now"
          ? `<button class="btn sm primary" data-po="${s.drug_id}" data-qty="${s.order_qty}">Create PO</button>`
          : `<button class="btn sm" data-po="${s.drug_id}" data-qty="${s.order_qty}">Create PO</button>`}</td></tr>`).join("")
        || `<tr><td colspan="11" class="empty">No suggestions yet</td></tr>`}</tbody>
    </table></div></div>

    <div class="section-title">Per-SKU forecast (30 days, 80% interval)</div>
    <div class="grid g2">
      ${fc.map((p) => `
      <div class="card">
        <div class="card-head"><div><h3>${esc(p.drug)}</h3>
          <div class="sub">${esc(p.model)} · avg ${p.avg_daily}/day · wMAPE ${fmtPct(p.metrics?.wmape)} (weekly MAPE ${fmtPct(p.metrics?.mape_weekly)})
          · trend ${p.trend_vs_prev > 0 ? "+" : ""}${p.trend_vs_prev}%</div></div>
          ${badge(`${Math.round(p.daily.reduce((a, d) => a + d.qty, 0))} units / 30d`, "info")}
        </div>
        <div class="chart-box"><canvas id="fc-${p.drug_id}"></canvas></div>
        <div class="legend">
          <span><i style="background:#0d47a1"></i>forecast</span>
          <span><i style="background:rgba(47,128,237,.25)"></i>80% interval</span>
          <span class="right muted">history ${fmtDate(p.history_from)} → ${fmtDate(p.history_to)}</span>
        </div>
      </div>`).join("")}
    </div>

    <div class="section-title">Model evaluation <span class="muted">— 56-day back-test of every candidate model (click “Model evaluation” to load)</span></div>
    <div class="card flush" id="evalWrap" style="display:none">
      <div class="table-wrap"><table>
        <thead><tr><th>Medicine</th><th>#</th><th>Model</th><th class="num">wMAPE</th>
          <th class="num">Weekly MAPE</th><th class="num">Accuracy</th><th class="num">MAE</th>
          <th class="num">RMSE</th><th class="num">Fit wMAPE</th><th>Status</th></tr></thead>
        <tbody id="evalBody"></tbody>
      </table></div>
    </div>

    <div class="section-title">Reorder history</div>
    <div class="card flush"><div class="table-wrap"><table>
      <thead><tr><th>#</th><th>Medicine</th><th class="num">Qty</th><th>Due</th><th>Reason</th>
        <th>Status</th><th>Created</th><th></th></tr></thead>
      <tbody>${ro.history.map((r) => `<tr>
        <td class="muted">${r.id}</td><td><b>${esc(r.drug)}</b></td><td class="num">${fmtN(r.qty)}</td>
        <td>${r.due_date ? fmtDate(r.due_date) : "—"}</td><td class="muted">${esc(r.reason || "")}</td>
        <td>${statusBadge(r.status)}</td><td>${esc(r.created_at)}</td>
        <td class="row-actions">
          ${r.status !== "received" ? `<button class="btn sm" data-rost="${r.id}" data-st="received">Mark received</button>` : ""}
          ${r.status !== "cancelled" ? `<button class="btn sm" data-rost="${r.id}" data-st="cancelled">Cancel</button>` : ""}
        </td></tr>`).join("") || `<tr><td colspan="8" class="empty">No reorders yet</td></tr>`}</tbody>
    </table></div></div>`;

  fc.forEach((p) => {
    chart(`fc-${p.drug_id}`, {
      type: "line",
      data: { labels: p.daily.map((d) => d.date.slice(5)), datasets: [
        { label: "Upper", data: p.daily.map((d) => d.hi), borderColor: "transparent", backgroundColor: "rgba(47,128,237,.18)", fill: "+1", pointRadius: 0 },
        { label: "Lower", data: p.daily.map((d) => d.lo), borderColor: "transparent", backgroundColor: "rgba(47,128,237,.18)", fill: false, pointRadius: 0 },
        { label: "Forecast", data: p.daily.map((d) => d.qty), borderColor: CHART.blue, borderWidth: 2, pointRadius: 0, tension: .25 }]},
      options: { maintainAspectRatio: false, plugins: { legend: { display: false } },
        scales: { x: { ...gridOpts, ticks: { maxTicksLimit: 10 } }, y: { ...gridOpts, beginAtZero: true } } },
    });
  });
  $$("[data-po]").forEach((b) => b.onclick = async () => {
    try {
      const r = await post("/api/reorders", { drug_id: +b.dataset.po, qty: +b.dataset.qty || null });
      toast(`PO #${r.id} created${r.notification ? " + supplier email drafted" : ""}`);
      viewForecast(view);
    } catch (e) { toast(e.message, true); }
  });
  $$("[data-rost]").forEach((b) => b.onclick = async () => {
    try { await patch(`/api/reorders/${b.dataset.rost}`, { status: b.dataset.st }); viewForecast(view); }
    catch (e) { toast(e.message, true); }
  });
  $("#fcRecompute").onclick = async () => {
    try { await api("/api/forecast?recompute=1"); toast("Forecasts recomputed"); viewForecast(view); }
    catch (e) { toast(e.message, true); }
  };
  $("#fcDraftPOs").onclick = async () => {
    try { const r = await post("/api/notifications/build-reorder-drafts");
      toast(`${r.created} supplier draft(s) created — see Suppliers → outbox`); }
    catch (e) { toast(e.message, true); }
  };
  $("#fcCsv").onclick = () => download("/api/reports/forecast.csv");
  $("#fcEvalCsv").onclick = () => download("/api/reports/forecast-evaluation.csv");
  $("#fcEval").onclick = async () => {
    try {
      const rows = await api("/api/forecast/evaluation");
      const flat = rows.flatMap((p) => p.models.map((m) => ({ drug: p.drug, holdout: p.holdout_days, ...m })));
      $("#evalWrap").style.display = "";
      $("#evalBody").innerHTML = flat.map((m) => `<tr>
        <td><b>${esc(m.drug)}</b></td><td class="muted">${m.rank}</td><td>${esc(m.model)}</td>
        <td class="num">${m.wmape}</td><td class="num">${m.mape}</td>
        <td class="num">${m.accuracy}%</td><td class="num">${m.mae}</td><td class="num">${m.rmse}</td>
        <td class="num">${m.fit_wmape}</td>
        <td>${m.in_production ? badge("in production", "ok") : badge("candidate", "mute")}</td></tr>`).join("");
      const nModels = Math.max(...rows.map((p) => p.models.length), 0);
      toast(`Back-tested ${nModels} candidate models per SKU across ${rows.length} SKUs`);
    } catch (e) { toast(e.message, true); }
  };
}

/* ============================== ALERTS ============================== */
async function viewAlerts(view) {
  const data = await api("/api/alerts");
  const s = data.summary;
  view.innerHTML = `
    <div class="grid g5" style="margin-bottom:16px">
      ${kpi("Active", fmtN(s.active), "triggers across rules", "info")}
      ${kpi("Critical", fmtN(s.critical), "act immediately", s.critical ? "bad" : "")}
      ${kpi("High", fmtN(s.high), "this week", s.high ? "warn" : "")}
      ${kpi("Medium", fmtN(s.medium), "monitor", "")}
      ${kpi("Acknowledged", fmtN(s.acknowledged), "handled", "good")}
    </div>
    <div class="filters">
      <div class="tabs" id="alTabs">
        <button data-sev="">All</button><button data-sev="critical">Critical</button>
        <button data-sev="high">High</button><button data-sev="medium">Medium</button>
      </div>
      <div class="right btn-row">
        <button class="btn" id="alRefresh">${ICO.refresh} Recompute</button>
        <button class="btn primary" id="alAckAll">Acknowledge all</button>
      </div>
    </div>
    <div id="alList"></div>`;
  const draw = (sev) => {
    const items = data.items.filter((a) => !sev || a.severity === sev);
    $("#alList").innerHTML = items.map(alertRow).join("") ||
      `<div class="empty">No active alerts in this bucket</div>`;
    $$("#alList [data-ack]").forEach((b) => b.onclick = async () => {
      try { await post(`/api/alerts/${b.dataset.ack}/ack`); viewAlerts(view); } catch (e) { toast(e.message, true); }
    });
  };
  $$("#alTabs button").forEach((b) => b.onclick = () => {
    $$("#alTabs button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active"); draw(b.dataset.sev);
  });
  $("#alRefresh").onclick = refreshAll;
  $("#alAckAll").onclick = async () => {
    try { await post("/api/alerts/ack-all"); toast("All alerts acknowledged"); viewAlerts(view); }
    catch (e) { toast(e.message, true); }
  };
  $$("#alTabs button")[0].classList.add("active");
  draw("");
}

/* ============================== WASTE & RETURNS ============================== */
async function viewWaste(view) {
  const [data, drugs] = await Promise.all([api("/api/waste"), api("/api/drugs")]);
  const s = data.summary;
  const brMap = Object.fromEntries((s.by_reason || []).map((r) => [r.reason, Number(r.v) || 0]));
  const brTot = Object.values(brMap).reduce((a, b) => a + b, 0) || 1;
  const brPct = (k) => Math.round((brMap[k] || 0) / brTot * 100);
  const tips = [];
  if (brPct("expired") >= 50) tips.push(`<b>${brPct("expired")}% of waste value is expiry-driven</b> → tighten reorder quantities (pack size / review period), enforce FEFO dispensing, and start run-down or vendor return at the 90-day warning window.`);
  if (brMap.damaged > 0) tips.push(`<b>${fmtMoney(brMap.damaged)} recorded as damaged</b> → review storage, cold-chain and handling with the supplier; keep batch references for claims.`);
  if (brMap.recalled > 0) tips.push(`<b>${fmtMoney(brMap.recalled)} recalled</b> → quarantine any remaining stock of those batches and raise a QA query with the supplier.`);
  if ((s.by_month || []).length > 1) tips.push(`Watch the monthly trend — a rising line means safety stock and lead times need re-tuning.`);
  const tipsHtml = tips.length
    ? `<div class="notice" style="margin-top:16px"><b>Prevention insights</b>
        <ul style="margin:8px 0 0 18px">${tips.map((t) => `<li style="margin:4px 0">${t}</li>`).join("")}</ul></div>`
    : "";
  view.innerHTML = `
    <div class="grid g5">
      ${kpi("Waste value", fmtMoney(s.total_value), `${fmtN(s.total_qty)} units`, "bad")}
      ${kpi("Lots", fmtN(s.total_lots), "written off", "bad")}
      ${kpi("% of purchases", fmtPct(s.waste_pct_of_purchases), "expired value / PO value", "warn")}
      ${kpi("Awaiting write-off", fmtMoney(s.value_at_risk), "expired, not yet booked", s.value_at_risk ? "warn" : "good")}
      ${kpi("Sales (feed)", fmtMoney(s.sales_value), "for context", "info")}
    </div>
    ${tipsHtml}
    <div class="grid g3" style="margin-top:16px">
      <div class="card"><h3>Waste by reason</h3><div class="chart-box sm"><canvas id="w-reason"></canvas></div></div>
      <div class="card"><h3>Waste trend</h3><div class="sub">By month, value ₹</div><div class="chart-box sm"><canvas id="w-monthly"></canvas></div></div>
      <div class="card"><h3>Waste by supplier</h3><div class="chart-box sm"><canvas id="w-sup"></canvas></div></div>
    </div>
    <div class="section-title">Waste ledger</div>
    <div class="filters">
      <div class="right btn-row">
        <button class="btn" id="wCsv">${ICO.down} Waste CSV</button>
        <button class="btn" id="wAdd">${ICO.pill} Record damaged / recalled…</button>
        <button class="btn primary" id="wReturn">${ICO.refresh} Return expired to vendor…</button>
      </div>
    </div>
    <div class="card flush"><div class="table-wrap tall"><table>
      <thead><tr><th>Medicine</th><th>Batch</th><th>Expiry</th><th class="num">Qty</th><th>Reason</th>
        <th class="num">Value</th><th>Status</th><th>Supplier</th><th>Note</th><th>Recorded</th></tr></thead>
      <tbody>${data.items.map((w) => `<tr>
        <td><b>${esc(w.drug)}</b></td><td class="mono">${esc(w.batch_no || "—")}</td>
        <td>${fmtDate(w.expiry_date)}</td><td class="num">${fmtN(w.qty)}</td>
        <td>${badge(w.reason, w.reason === "expired" ? "bad" : "warn")}</td>
        <td class="num">${fmtMoney(w.value)}</td><td>${statusBadge(w.status)}</td>
        <td>${esc(w.supplier || "—")}</td><td class="muted">${esc(w.note || "")}</td>
        <td>${fmtDate(w.created_at)}</td></tr>`).join("") ||
        `<tr><td colspan="10" class="empty">No waste recorded</td></tr>`}</tbody>
    </table></div></div>

    <div class="section-title">Return-to-vendor workflow</div>
    <div class="card flush"><div class="table-wrap"><table>
      <thead><tr><th>Ref</th><th>Supplier</th><th class="num">Units</th><th class="num">Value</th>
        <th>Lots</th><th>Status</th><th>Created</th><th></th></tr></thead>
      <tbody id="retBody"></tbody>
    </table></div></div>
    <p class="muted" style="margin-top:8px">Flow: <b>requested → approved → picked up → credited</b>
      (a credit updates the lots to “credited”; rejected re-opens them as pending).</p>`;

  const reasons = s.by_reason || [];
  chart("w-reason", {
    type: "doughnut",
    data: { labels: reasons.map((r) => r.reason), datasets: [{ data: reasons.map((r) => r.v),
      backgroundColor: [CHART.red, CHART.amber, "#8b5cf6", "#0ea5e9", CHART.green], borderWidth: 2, borderColor: "#fff" }]},
    options: { maintainAspectRatio: false, cutout: "60%", plugins: { legend: { position: "right", labels: { boxWidth: 10 } },
      tooltip: { callbacks: { label: (c) => fmtMoney(c.parsed) } } } },
  });
  const sups = s.by_supplier || [];
  chart("w-sup", {
    type: "bar",
    data: { labels: sups.map((x) => x.supplier || "—"), datasets: [{ label: "Waste", data: sups.map((x) => x.v),
      backgroundColor: CHART.red, borderRadius: 7, maxBarThickness: 34 }]},
    options: { indexAxis: "y", maintainAspectRatio: false, plugins: { legend: { display: false },
      tooltip: { callbacks: { label: (c) => fmtMoney(c.parsed.x) } } }, scales: { x: gridOpts, y: gridOpts } },
  });
  const months = s.by_month || [];
  chart("w-monthly", {
    type: "line",
    data: { labels: months.map((m) => m.month), datasets: [{ label: "Waste ₹", data: months.map((m) => m.v),
      borderColor: CHART.red, backgroundColor: "rgba(226,87,76,.12)", fill: true, tension: .3,
      pointRadius: 3, borderWidth: 2 }]},
    options: { maintainAspectRatio: false, plugins: { legend: { display: false },
      tooltip: { callbacks: { label: (c) => fmtMoney(c.parsed.y) } } },
      scales: { x: { ...gridOpts, ticks: { maxTicksLimit: 8 } }, y: { ...gridOpts, beginAtZero: true } } },
  });

  const returns = await api("/api/returns");
  $("#retBody").innerHTML = returns.map((r) => `<tr>
    <td class="mono">${esc(r.reference)}</td><td><b>${esc(r.supplier || "—")}</b></td>
    <td class="num">${fmtN(r.qty)}</td><td class="num">${fmtMoney(r.value)}</td>
    <td>${r.lots.map((l) => `<span class="badge mute">${esc(l.batch_no || l.drug)} ×${l.qty}</span>`).join(" ")}</td>
    <td>${statusBadge(r.status)}</td><td>${fmtDate(r.created_at)}</td>
    <td class="row-actions">${["approved", "picked_up", "credited", "rejected"].filter((x) => x !== r.status).map((st) =>
      `<button class="btn sm" data-ret="${r.id}" data-st="${st}">${st.replace("_", " ")}</button>`).join("")}</td>
  </tr>`).join("") || `<tr><td colspan="8" class="empty">No vendor returns filed yet</td></tr>`;
  $$("[data-ret]").forEach((b) => b.onclick = async () => {
    try { await patch(`/api/returns/${b.dataset.ret}`, { status: b.dataset.st }); toast("Return updated"); viewWaste(view); }
    catch (e) { toast(e.message, true); }
  });

  $("#wCsv").onclick = () => download("/api/reports/waste.csv");
  $("#wAdd").onclick = () => wasteModal(drugs);
  $("#wReturn").onclick = () => viewWasteReturnModal();
}

function wasteModal(drugs) {
  openModal("Record damaged / recalled stock", `
    <div class="field"><label class="f">Medicine</label>${drugSelect("wmDrug", drugs)}</div>
    <div class="field"><label class="f">Batch (blank = FEFO pick)</label><input id="wmBatch" placeholder="e.g. DOL-2301-95"></div>
    <div class="field"><label class="f">Quantity</label><input id="wmQty" type="number" min="1" value="1"></div>
    <div class="field"><label class="f">Reason</label><select id="wmReason">
      <option value="damaged">damaged</option><option value="recalled">recalled</option>
      <option value="theft">theft</option><option value="other">other</option></select></div>
    <div class="field"><label class="f">Note</label><input id="wmNote" placeholder="details"></div>
    <div class="btn-row"><button class="btn primary" id="wmGo">Record write-off</button></div><div id="wmOut"></div>`);
  $("#wmGo").onclick = async () => {
    try {
      const r = await post("/api/waste", { drug_id: +$("#wmDrug").value, batch_no: $("#wmBatch").value || null,
        qty: +$("#wmQty").value, reason: $("#wmReason").value, note: $("#wmNote").value });
      $("#wmOut").innerHTML = `<div class="notice" style="margin-top:10px">Wrote off <b>${r.qty}</b> units from
        batch ${esc(r.batch)} (value ${fmtMoney(r.value)}).</div>`;
      setTimeout(() => { closeModal(); navigate(); }, 900);
    } catch (e) { $("#wmOut").innerHTML = `<div class="notice bad" style="margin-top:10px">${esc(e.message)}</div>`; }
  };
}

async function viewWasteReturnModal() {
  const [waste, suppliers] = await Promise.all([api("/api/waste"), api("/api/suppliers")]);
  const pending = waste.items.filter((w) => w.status === "pending");
  openModal("Return expired stock to vendor", `
    <div class="field"><label class="f">Supplier</label>
      <select id="rtSup">${suppliers.items.map((s) => `<option value="${s.id}">${esc(s.name)} — ${fmtMoney(s.waste_value)} waste</option>`).join("")}</select></div>
    <div class="field"><label class="f">Lots (pending waste)</label>
      <div style="max-height:260px;overflow:auto;border:1px solid var(--line);border-radius:10px;padding:8px">
        ${pending.length ? pending.map((w) => `
          <label style="display:flex;gap:8px;align-items:center;padding:5px 4px;font-size:12.5px">
            <input type="checkbox" class="rtLot" value="${w.id}" checked style="width:auto">
            <span><b>${esc(w.drug)}</b> ${esc(w.batch_no || "")} · ${w.qty} units · ${fmtMoney(w.value)} · ${esc(w.reason)}</span>
          </label>`).join("") : `<div class="empty">No pending lots</div>`}
      </div></div>
    <div class="field"><label class="f">Note</label><input id="rtNote" placeholder="credit note reference, pick-up date…"></div>
    <div class="btn-row"><button class="btn primary" id="rtGo">Create RTV request + email draft</button></div>
    <div id="rtOut"></div>`);
  $("#rtGo").onclick = async () => {
    try {
      const ids = $$(".rtLot:checked").map((c) => +c.value);
      const r = await post("/api/returns", { supplier_id: +$("#rtSup").value, waste_ids: ids, note: $("#rtNote").value });
      $("#rtOut").innerHTML = `<div class="notice" style="margin-top:10px">Return <b>${esc(r.reference)}</b> filed:
        ${r.lots} lots, ${fmtN(r.qty)} units, ${fmtMoney(r.value)}. A notification email was drafted in the outbox.</div>`;
      setTimeout(() => { closeModal(); navigate(); }, 1200);
    } catch (e) { $("#rtOut").innerHTML = `<div class="notice bad" style="margin-top:10px">${esc(e.message)}</div>`; }
  };
}

/* ============================== SUPPLIERS ============================== */
async function viewSuppliers(view) {
  const data = await api("/api/suppliers");
  view.innerHTML = `
    <div class="notice">Supplier score penalises expiry waste and expiring batches, and rewards shelf life at receipt.
      Use the outbox to draft and “send” reorder/return emails (simulated send — copy it into your mail client).</div>
    <div class="grid g3">
      ${data.items.map((s) => `
      <div class="card">
        <div class="card-head"><div><h3>${esc(s.name)}</h3>
          <div class="sub">${esc(s.email || "")} · lead time ${s.lead_time_days}d</div></div>
          ${badge(`score ${s.score}`, s.score >= 70 ? "ok" : s.score >= 45 ? "warn" : "bad")}</div>
        <div class="grid g3" style="gap:8px">
          <div><div class="muted">Orders</div><b>${fmtN(s.orders)}</b></div>
          <div><div class="muted">Spend</div><b>${fmtMoney(s.spend)}</b></div>
          <div><div class="muted">Units</div><b>${fmtN(s.units)}</b></div>
          <div><div class="muted">Waste</div><b style="color:var(--red)">${fmtMoney(s.waste_value)}</b></div>
          <div><div class="muted">Waste %</div><b>${fmtPct(s.waste_pct)}</b></div>
          <div><div class="muted">Shelf life</div><b>${Math.round(s.avg_shelf_life_days)}d</b></div>
        </div>
        <div class="divider"></div>
        <button class="btn sm" data-notify="${s.id}">${ICO.mail} Compose email</button>
      </div>`).join("")}
    </div>
    <div class="section-title">Notification outbox</div>
    <div class="card flush"><div class="table-wrap"><table>
      <thead><tr><th>Subject</th><th>Supplier</th><th>Status</th><th>Created</th><th></th></tr></thead>
      <tbody id="outBody"></tbody></table></div></div>
    <div id="notifDetail"></div>`;

  const drawOutbox = (items) => {
    $("#outBody").innerHTML = items.map((n) => `<tr>
      <td><b>${esc(n.subject)}</b></td><td>${esc(n.supplier || "—")}<div class="muted">${esc(n.email || "")}</div></td>
      <td>${statusBadge(n.status)}</td><td>${esc(n.created_at)}</td>
      <td class="row-actions">
        <button class="btn sm" data-view="${n.id}">Read</button>
        ${n.status !== "sent" ? `<button class="btn sm primary" data-send="${n.id}">Send</button>` : ""}
      </td></tr>`).join("") || `<tr><td colspan="5" class="empty">Outbox empty — create POs or returns first</td></tr>`;
    $$("[data-send]").forEach((b) => b.onclick = async () => {
      try { await patch(`/api/notifications/${b.dataset.send}`, { status: "sent" });
        toast("Marked as sent"); viewSuppliers(view); } catch (e) { toast(e.message, true); }
    });
    $$("[data-view]").forEach((b) => b.onclick = () => {
      const n = items.find((x) => String(x.id) === b.dataset.view);
      $("#notifDetail").innerHTML = `<div class="card" style="margin-top:14px">
        <div class="card-head"><h3>${esc(n.subject)}</h3><span class="muted">to ${esc(n.supplier)} &lt;${esc(n.email)}&gt;</span></div>
        <pre style="white-space:pre-wrap;font:13px/1.6 var(--mono);background:#f8fafc;padding:14px;border-radius:10px">${esc(n.body)}</pre>
        <div class="btn-row">
          ${n.status !== "sent" ? `<button class="btn primary" data-send2="${n.id}">Mark as sent</button>` : badge("sent", "ok")}
          <a class="btn" href="mailto:${esc(n.email)}?subject=${encodeURIComponent(n.subject)}&body=${encodeURIComponent(n.body)}">Open in mail client</a>
        </div></div>`;
      $$("[data-send2]").forEach((x) => x.onclick = async () => {
        await patch(`/api/notifications/${x.dataset.send2}`, { status: "sent" });
        toast("Marked as sent"); viewSuppliers(view);
      });
    });
  };
  drawOutbox(data.notifications || []);
  $$("[data-notify]").forEach((b) => b.onclick = () => {
    const s = data.items.find((x) => String(x.id) === b.dataset.notify);
    openModal(`Email ${esc(s.name)}`, `
      <div class="field"><label class="f">Subject</label><input id="ntSub" value="Purchase request — ${esc(s.name)}"></div>
      <div class="field"><label class="f">Body</label><textarea id="ntBody" rows="7">Hello ${esc(s.name)},

Please share availability and pricing for our next replenishment.

Regards,
HealthFirst Pharmacy</textarea></div>
      <div class="btn-row">
        <button class="btn" id="ntDraft">Save draft</button>
        <button class="btn primary" id="ntSend">Send</button>
      </div>`);
    const save = async (send) => {
      try {
        await post("/api/notifications", { supplier_id: s.id, subject: $("#ntSub").value,
          body: $("#ntBody").value, send });
        toast(send ? "Email sent (logged in outbox)" : "Draft saved");
        closeModal(); viewSuppliers(view);
      } catch (e) { toast(e.message, true); }
    };
    $("#ntDraft").onclick = () => save(false);
    $("#ntSend").onclick = () => save(true);
  });
}

/* ============================== UPLOAD & DATA QUALITY ============================== */
async function viewUpload(view) {
  const [dq, ups] = await Promise.all([api("/api/data-quality"), api("/api/uploads")]);
  view.innerHTML = `
    <div class="grid g23">
      <div>
        <div class="card">
          <h3>Daily data upload</h3>
          <div class="sub">Excel (.xlsx/.xls), CSV or JSON — sales and/or purchases. Columns are auto-detected,
            drug names normalised, noise quarantined.</div>
          <div class="drop" id="drop">
            <div class="big">Drop your file here</div>
            <div class="small">or click to browse · <a href="/api/upload/template${authQuery()}" onclick="event.stopPropagation()">download template</a></div>
            <input type="file" id="file" accept=".xlsx,.xls,.csv,.json" style="display:none">
          </div>
          <div class="field" style="margin-top:12px"><label class="f">File type</label>
            <select id="upKind"><option value="auto">Auto-detect</option>
              <option value="sales">Sales</option><option value="purchases">Purchases</option></select></div>
          <div id="upOut"></div>
        </div>
        <div class="section-title">Upload history</div>
        <div class="card flush"><div class="table-wrap"><table>
          <thead><tr><th>File</th><th>Kind</th><th class="num">Rows</th><th class="num">Accepted</th>
            <th class="num">Quarantined</th><th>When</th></tr></thead>
          <tbody>${ups.map((u) => `<tr><td><b>${esc(u.filename)}</b></td><td>${badge(u.kind, u.kind === "sales" ? "info" : "mute")}</td>
            <td class="num">${fmtN(u.rows_total)}</td><td class="num">${fmtN(u.rows_accepted)}</td>
            <td class="num">${u.rows_rejected ? `<span style="color:var(--red)">${fmtN(u.rows_rejected)}</span>` : "0"}</td>
            <td>${esc(u.created_at)}</td></tr>`).join("") || `<tr><td colspan="6" class="empty">No uploads yet</td></tr>`}</tbody>
        </table></div></div>
      </div>
      <div>
        <div class="card">
          <h3>POS simulator</h3>
          <div class="sub">Generate a day of point-of-sale traffic from the AI demand model and push it through the
            normal pipeline (labelled <span class="mono">pos_sim</span>). Great for live demos.</div>
          <div class="btn-row">
            <button class="btn primary" id="simDay">${ICO.trend} Simulate 1 day</button>
            <button class="btn" id="sim3">Simulate 3 days</button>
          </div>
          <div id="simOut"></div>
        </div>
        <div class="card" style="margin-top:14px">
          <h3>Quarantine issues</h3>
          <div class="sub">Rows kept aside instead of polluting stock/forecasts</div>
          <div class="issue-tags">${Object.entries(dq.issues || {}).map(([k, v]) =>
            `<span>${esc(k)}: <b>${fmtN(v)}</b></span>`).join("") || `<span>none</span>`}</div>
          <div class="divider"></div>
          <div class="table-wrap" style="max-height:300px"><table>
            <thead><tr><th>Row</th><th>Issues</th></tr></thead>
            <tbody>${(dq.rows || []).slice(0, 30).map((r) => `<tr>
              <td class="mono" style="max-width:330px;overflow:hidden;text-overflow:ellipsis">${esc(JSON.stringify(r.record)).slice(0, 90)}…</td>
              <td>${(r.issues || []).map((i) => badge(issueLabel(i), "bad")).join(" ")}</td></tr>`).join("") ||
              `<tr><td colspan="2" class="empty">Nothing quarantined</td></tr>`}</tbody>
          </table></div>
        </div>
      </div>
    </div>`;

  const doUpload = async (file) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("kind", $("#upKind").value);
    $("#upOut").innerHTML = `<div class="notice" style="margin-top:12px">Processing <b>${esc(file.name)}</b>…</div>`;
    try {
      const key = apiKey();
      const res = await fetch("/api/upload", { method: "POST", body: fd,
        headers: { ...sessionHeaders(), ...(key ? { "X-API-Key": key } : {}) } });
      const r = await res.json();
      if (!res.ok) throw new Error(r.detail || "upload failed");
      const issues = Object.entries(r.issues || {}).map(([k, v]) => `<span>${esc(issueLabel(k))}: <b>${v}</b></span>`).join("");
      $("#upOut").innerHTML = `<div class="result">
        <b>${esc(r.filename)}</b> → ${badge(r.kind, "info")}
        <div class="grid g4" style="margin-top:10px">
          ${kpi("Rows", fmtN(r.rows), "", "", false)}
          ${kpi("Accepted", fmtN(r.accepted), `${r.inserted} new`, "good", false)}
          ${kpi("Quarantined", fmtN(r.rejected), "kept for review", r.rejected ? "warn" : "", false)}
          ${kpi("Stock guard", fmtN(r.no_stock), "rows without stock", "", false)}
        </div>          <div class="issue-tags">${issues || "<span>clean file — no issues</span>"}</div></div>`;
      toast("Upload ingested");
    } catch (e) {
      $("#upOut").innerHTML = `<div class="notice bad" style="margin-top:12px">${esc(e.message)}</div>`;
    }
  };
  const drop = $("#drop");
  drop.onclick = () => $("#file").click();
  $("#file").onchange = (e) => e.target.files[0] && doUpload(e.target.files[0]);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over");
    if (e.dataTransfer.files[0]) doUpload(e.dataTransfer.files[0]); };

  const sim = async (days) => {
    $("#simOut").innerHTML = `<div class="notice" style="margin-top:12px">Simulating ${days} day(s)…</div>`;
    try {
      const r = await post(`/api/simulate/day?days=${days}`);
      $("#simOut").innerHTML = `<div class="result">Simulated POS feed → <b>${r.inserted}</b> transactions ingested
        (${r.accepted} accepted, ${r.rejected} quarantined). Stock, alerts and forecasts updated.</div>`;
    } catch (e) { $("#simOut").innerHTML = `<div class="notice bad" style="margin-top:12px">${esc(e.message)}</div>`; }
  };
  $("#simDay").onclick = () => sim(1);
  $("#sim3").onclick = () => sim(3);
}

/* ============================== CHAT ============================== */
const QUICK = ["Stock of Dolo 650", "What expires in the next 30 days?", "What should I reorder?",
  "Sales last month", "Forecast for Telma 40", "Show waste report",
  "Which suppliers should I return expired stock to?", "Create reorder for Pan 40"];

async function viewChat(view) {
  const hist = await api("/api/chat/history");
  view.innerHTML = `
    <div class="chat-wrap"><div class="chat-card">
      <div class="chat-head">
        <div><div class="t"><span class="ai-ava" aria-hidden="true">${ICO.spark}</span>PharmaAI Assistant</div>
        <div class="s">Grounded in live stock, expiry, forecast &amp; waste data · can execute actions</div></div>
        <button class="btn sm ghost" id="chClear" style="color:#fff;border-color:rgba(255,255,255,.3)">Clear</button>
      </div>
      <div class="chat-log" id="chLog"></div>
      <div class="chips">${QUICK.map((q) => `<button data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div>
      <div class="composer">
        <input id="chInput" placeholder="Ask about stock, expiry, reorders, waste… e.g. “how many Dolo 650 are left?”" autocomplete="off">
        <button class="btn primary" id="chSend">Send</button>
      </div>
    </div></div>`;

  const log = $("#chLog");
  const addMsg = (role, text) => {
    const el = document.createElement("div");
    el.className = `msg ${role}`;
    el.innerHTML = renderMd(text);
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  };
  const addCards = (cards) => {
    if (!cards || !cards.length) return;
    const wrap = document.createElement("div");
    wrap.className = "chat-cards";
    wrap.innerHTML = cards.map(renderCard).join("");
    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;
  };

  if (!hist.length) addMsg("bot", "Hello! I'm PharmaAI, your pharmacy assistant. Ask me anything about stock, expiry, reorders, waste or suppliers — or use the shortcuts below.");
  else hist.forEach((h) => addMsg(h.role === "assistant" ? "bot" : "user", h.message));

  const send = async (text) => {
    if (!text.trim()) return;
    addMsg("user", text);
    const typing = document.createElement("div");
    typing.className = "msg bot";
    typing.innerHTML = `<span class="typing"></span><span class="typing"></span><span class="typing"></span>`;
    log.appendChild(typing); log.scrollTop = log.scrollHeight;
    try {
      const r = await post("/api/chat", { message: text });
      typing.remove();
      addMsg("bot", r.text);
      addCards(r.cards);
    } catch (e) {
      typing.remove();
      addMsg("bot", `⚠ ${e.message}`);
    }
  };
  $("#chSend").onclick = () => { send($("#chInput").value); $("#chInput").value = ""; };
  $("#chInput").onkeydown = (e) => { if (e.key === "Enter") { send(e.target.value); e.target.value = ""; } };
  $$(".chips button").forEach((b) => b.onclick = () => send(b.dataset.q));
  $("#chClear").onclick = async () => {
    await api("/api/chat/history?limit=1").catch(() => {});
    viewChat(view);
  };
  $("#chInput").focus();
}

function renderMd(t) {
  return esc(t)
    .replace(/\*\*(.+?)\*\*/g, "<span class='strong'>$1</span>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>");
}

function renderCard(c) {
  if (c.type === "kpis") {
    return `<div class="mini">${c.title ? `<div class="mt">${esc(c.title)}</div>` : ""}
      <div class="kpis">${(c.items || []).map((i) => `<div><div class="l">${esc(i.label)}</div>
        <div class="v ${esc(i.tone || "")}">${esc(i.value)}</div></div>`).join("")}</div></div>`;
  }
  if (c.type === "table") {
    return `<div class="mini"><div class="mt">${esc(c.title)}</div>
      <div style="overflow:auto"><table><thead><tr>${(c.columns || []).map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead>
      <tbody>${(c.rows || []).map((r) => `<tr>${r.map((cell) => `<td>${esc(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>
      ${c.note ? `<div class="note">${esc(c.note)}</div>` : ""}</div>`;
  }
  if (c.type === "list") {
    return `<div class="mini"><div class="mt">${esc(c.title)}</div>
      <ul>${(c.items || []).map((i) => `<li>${esc(i)}</li>`).join("")}</ul>
      ${c.note ? `<div class="note">${esc(c.note)}</div>` : ""}</div>`;
  }
  if (c.type === "actions") {
    return `<div class="chips" style="border:0;padding:0;background:transparent">
      ${(c.items || []).map((a) => a.href
        ? `<a class="btn" href="${esc(a.href)}" download>${esc(a.label)}</a>`
        : `<button onclick="location.hash='chat';window.__chatAsk && window.__chatAsk(${JSON.stringify(a.intent)})">${esc(a.label)}</button>`).join("")}</div>`;
  }
  return "";
}

/* ============================== SETTINGS ============================== */
async function viewSettings(view) {
  const meta = await api("/api/meta");
  const s = meta.settings;
  const fields = [
    ["pharmacy_name", "Pharmacy name", "text"],
    ["service_level", "Service level (0.90–0.99)", "number", "step=0.01 min=0.9 max=0.99"],
    ["review_period_days", "Review period (days)", "number", "min=1 max=60"],
    ["pack_size", "Order pack size (units)", "number", "min=1"],
    ["expiry_critical_days", "Expiry critical window (days)", "number", "min=1"],
    ["expiry_warning_days", "Expiry warning window (days)", "number", "min=1"],
    ["overstock_days", "Overstock threshold (days of cover)", "number", "min=1"],
    ["forecast_horizon_days", "Forecast horizon (days)", "number", "min=7 max=120"],
    ["llm_model", "LLM model (optional, OpenAI-compatible)", "text", "placeholder=leave blank to use built-in engine"],
  ];
  view.innerHTML = `
    <div class="grid g2">
      <div class="card">
        <h3>Forecast &amp; alert policy</h3>
        <div class="sub">Applied immediately to reorder maths and alert thresholds</div>
        <div class="form-grid">${fields.map(([k, label, type, extra]) => `
          <div class="field"><label class="f">${label}</label>
            <input id="st-${k}" type="${type}" value="${esc(s[k] ?? "")}" ${extra || ""}></div>`).join("")}
        </div>
        <div class="btn-row"><button class="btn primary" id="stSave">Save settings</button></div>
      </div>
      <div>
        <div class="card">
          <h3>Data source</h3>
          <div class="sub">Platform seeded from the Kaggle challenge dataset</div>
          <div class="grid g2" style="gap:8px">
            <div><div class="muted">Dataset</div><b>${esc(meta.dataset.name)}</b></div>
            <div><div class="muted">Sales rows</div><b>${fmtN(meta.source_counts.sales)}</b></div>
            <div><div class="muted">Purchases</div><b>${fmtN(meta.source_counts.purchases)}</b></div>
            <div><div class="muted">Batches</div><b>${fmtN(meta.source_counts.batches)}</b></div>
          </div>
          <div class="divider"></div>
          <div class="btn-row">
            <button class="btn" id="stInv">${ICO.down} Inventory CSV</button>
            <button class="btn" id="stFc">${ICO.down} Forecast CSV</button>
            <button class="btn" id="stWaste">${ICO.down} Waste CSV</button>
          </div>
        </div>
        <div class="card" style="margin-top:14px">
          <h3>How the AI works</h3>
          <ul style="margin:8px 0 0;padding-left:18px;font-size:13px;line-height:1.7">
            <li><b>Categorisation</b> — brand/molecule rules classify every SKU on ingest.</li>
            <li><b>Forecasting</b> — damped Holt-Winters (weekly seasonality) competes with SES,
              seasonal-naive and moving average; a rolling back-test picks the winner, and long-range
              projections are blended to the weekday baseline.</li>
            <li><b>Reorders</b> — lead-time demand + safety stock (z × σ × √LT) − usable FEFO stock,
              with a stockout-date projection.</li>
            <li><b>Alerts</b> — expiry, low stock, stockout risk, overstock, waste and data-quality triggers.</li>
          </ul>
        </div>
      </div>
    </div>`;
  $("#stSave").onclick = async () => {
    const values = {};
    fields.forEach(([k]) => { values[k] = $(`#st-${k}`).value; });
    try { await post("/api/settings", { values }); toast("Settings saved"); viewSettings(view); }
    catch (e) { toast(e.message, true); }
  };
  $("#stInv").onclick = () => download("/api/reports/inventory.csv");
  $("#stFc").onclick = () => download("/api/reports/forecast.csv");
  $("#stWaste").onclick = () => download("/api/reports/waste.csv");
}

/* ============================== modals ============================== */
let lastFocused = null;
function openModal(title, html) {
  lastFocused = document.activeElement;
  closeModal();
  const wrap = document.createElement("div");
  wrap.id = "modal";
  wrap.style.cssText = "position:fixed;inset:0;background:rgba(8,20,24,.55);z-index:80;display:grid;place-items:center;padding:20px;overflow:auto";
  wrap.innerHTML = `<div class="modal-panel" role="dialog" aria-modal="true" aria-label="${title}"
    style="background:#fff;border-radius:var(--radius-lg);max-width:860px;width:100%;
    max-height:90vh;overflow:auto;box-shadow:var(--shadow-3)">
    <div style="display:flex;justify-content:space-between;align-items:center;padding:15px 20px;border-bottom:1px solid var(--line);position:sticky;top:0;background:#fff;z-index:2">
      <h3 style="margin:0;font-size:16px">${title}</h3>
      <button class="btn sm ghost" id="modalX" aria-label="Close dialog">✕</button></div>
    <div style="padding:18px 20px">${html}</div></div>`;
  wrap.addEventListener("mousedown", (e) => { if (e.target === wrap) closeModal(); });
  const onKey = (e) => {
    if (e.key === "Escape") { e.preventDefault(); closeModal(); return; }
    if (e.key !== "Tab") return; // trap focus inside the dialog
    const focusables = wrap.querySelectorAll('button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])');
    if (!focusables.length) return;
    const first = focusables[0], last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  wrap.addEventListener("keydown", onKey);
  // body-focused Escape must also close — listen at document level while open
  document.addEventListener("keydown", onKey);
  modalCleanup = onKey;
  document.body.appendChild(wrap);
  $("#modalX").onclick = closeModal;
  const af = wrap.querySelector("input, select, textarea, button.primary, button") ;
  if (af) af.focus();
}
function closeModal() {
  document.removeEventListener("keydown", modalCleanup);
  $("#modal")?.remove();
  if (lastFocused?.focus) lastFocused.focus();
}
let modalCleanup = null; // handler ref of the currently open modal, for removal

/* ------------------------------ boot ------------------------------ */
const VIEWS = { dashboard: viewDashboard, inventory: viewInventory, shelf: viewShelf,
  counter: viewCounter, shelftasks: viewShelfTasks,
  forecast: viewForecast, alerts: viewAlerts, waste: viewWaste, suppliers: viewSuppliers,
  upload: viewUpload, chat: viewChat, settings: viewSettings };

window.__chatAsk = null;
const origViewChat = viewChat;
viewChat = async function (view) { await origViewChat(view); window.__chatAsk = (t) => $("#chSend")?.click(); };$( "#refreshBtn").onclick = refreshAll;
$( "#rxAddBtn").onclick = () => prescriptionModal();
$( "#menuBtn").onclick = () => $("#sidebar").classList.contains("open") ? closeSidebar() : openSidebar();
$( "#scrim").onclick = closeSidebar;
$("#logoutBtn").onclick = () => { setSession(null); location.href = "/"; };

/* drawer: the left rail slides in over the content (see styles.css media queries) */
function openSidebar() {
  $("#sidebar")?.classList.add("open");
  $("#scrim").classList.add("show");
  $("#menuBtn").setAttribute("aria-expanded", "true");
}
function closeSidebar() {
  $("#sidebar")?.classList.remove("open");
  $("#scrim").classList.remove("show");
  $("#menuBtn").setAttribute("aria-expanded", "false");
}

function applyRoleNav() {
  const admin = isAdmin();
  $$("#nav a[data-roles='admin']").forEach((a) => a.style.display = admin ? "" : "none");
  // hide section labels (Supply chain / Data & AI) whose every link is hidden
  let label = null, visible = 0;
  const flush = () => { if (label) label.style.display = visible ? "" : "none"; };
  for (const el of document.querySelectorAll("#nav > *")) {
    if (el.classList.contains("nav-label")) { flush(); label = el; visible = 0; }
    else if (el.tagName === "A" && el.style.display !== "none") visible++;
  }
  flush();
  const refreshBtn = $("#refreshBtn");
  if (refreshBtn) refreshBtn.style.display = admin ? "" : "none"; // recompute is admin-only
  const pill = $("#alertPill");
  if (pill) pill.style.display = admin ? "" : "none";
  const rx = $("#rxAddBtn");
  if (rx) rx.style.display = admin ? "" : "none";
}

async function updateTaskBadge() {
  const pill = $("#taskPill");
  if (!pill || !session()) return;
  try {
    const c = await api("/api/shelf/tasks?status=pending&limit=1");
    pill.textContent = c.counts?.pending ?? 0;
  } catch { /* not signed in yet */ }
}

function showLogin() {
  if ($("#loginOverlay")) return;
  const el = document.createElement("div");
  el.id = "loginOverlay";
  el.style.cssText = "position:fixed;inset:0;background:linear-gradient(160deg,#071c4d,#0d47a1 55%,#1565c0);z-index:200;display:grid;place-items:center;padding:20px;overflow:auto";
  el.innerHTML = `
    <div class="auth-split" role="dialog" aria-modal="true" aria-label="Sign in">
      <div class="auth-brand">
        <div class="ab-logo">
          <span class="brand-mark" aria-hidden="true"><svg viewBox="0 0 24 24" width="19" height="19"><path d="M10 3h4v7h7v4h-7v7h-4v-7H3v-4h7z" fill="currentColor"/></svg></span>
          <span><div class="ab-name">HealthFirst Pharmacy</div><div class="ab-sub">Smart Inventory Platform</div></span>
        </div>
        <h2>Welcome Back!</h2>
        <p class="ab-sub2">Sign in to manage stock, expiry risk, reorders and shelf operations —
          all grounded in live pharmacy data.</p>
        <div class="auth-illus">${AUTH_ART}</div>
        <div class="ab-foot">New here? <a href="#" onclick="return false" title="Ask an administrator to provision your account">Contact your administrator</a></div>
      </div>
      <div class="auth-form">
        <h2>Login</h2>
        <div class="af-sub">Sign in as admin or pharmacist</div>
        <form id="liForm">
          <div class="field">
            <label class="f" for="liUser">Email / mobile / username</label>
            <input id="liUser" autocomplete="username" required placeholder="e.g. admin">
          </div>
          <div class="field">
            <label class="f" for="liPass">Password</label>
            <input id="liPass" type="password" autocomplete="current-password" required placeholder="••••••••">
          </div>
          <div class="af-row">
            <label style="display:flex;gap:6px;align-items:center;color:var(--ink-3)">
              <input type="checkbox" id="liRemember" style="width:auto"> Remember me</label>
            <a href="#" onclick="return false" title="Passwords are managed by your administrator">Forgot password?</a>
          </div>
          <button class="btn primary" id="liGo" type="submit">Login</button>
          <div id="liErr" role="alert" style="margin-top:8px"></div>
        </form>
        <div class="af-demo">
          <span class="muted">Demo accounts (click to sign in):</span>
          <button class="btn sm demo-btn" id="liAdmin" type="button">Admin · admin / admin123</button>
          <button class="btn sm demo-btn" id="liPharm" type="button">${ICO.pill} Pharmacist · pharmacist / pharm123</button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(el);
  const go = async (u, p) => {
    $("#liErr").innerHTML = "";
    try {
      const res = await fetch("/api/login", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: u, password: p }) });
      const r = await res.json();
      if (!res.ok) throw new Error(r.detail || "login failed");
      setSession(r);
      location.reload();
    } catch (e) { $("#liErr").innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; }
  };
  $("#liForm").onsubmit = (e) => { e.preventDefault(); $("#liGo").click(); };
  $("#liGo").onclick = () => go($("#liUser").value.trim(), $("#liPass").value);
  $("#liPass").onkeydown = (e) => { if (e.key === "Enter") $("#liGo").click(); };
  $("#liAdmin").onclick = () => go("admin", "admin123");
  $("#liPharm").onclick = () => go("pharmacist", "pharm123");
  $("#liUser").focus();
}

/* header search: filter the live catalogue into a dropdown panel */
(function wireHeaderSearch() {
  const input = document.getElementById("hdrSearch");
  const panel = document.getElementById("hdrResults");
  if (!input || !panel) return;
  let drugsCache = null, t = null;
  const close = () => { panel.hidden = true; };
  const render = (items, q) => {
    panel.innerHTML = items.length
      ? items.map((d) => `<button type="button" class="hs-item" data-hash="#inventory">
          <span><span class="hs-name">${esc(d.name)}</span>
          <span class="hs-sub" style="display:block">${esc(d.category || "")} · ${esc(d.generic || "")}</span></span>
          <span class="hs-right">${fmtN(d.usable)} in stock</span>
        </button>`).join("")
      : `<div class="hs-empty">No medicines match “${esc(q)}”</div>`;
    panel.hidden = false;
    panel.querySelectorAll(".hs-item").forEach((b) => b.onclick = () => { close(); location.hash = b.dataset.hash; });
  };
  const search = async (q) => {
    if (!drugsCache) { try { drugsCache = await api("/api/drugs"); } catch { return; } }
    const s = q.trim().toLowerCase();
    if (!s) { close(); return; }
    render(drugsCache.filter((d) =>
      String(d.name || "").toLowerCase().includes(s) ||
      String(d.generic || "").toLowerCase().includes(s) ||
      String(d.category || "").toLowerCase().includes(s)).slice(0, 8), q);
  };
  input.addEventListener("input", () => { clearTimeout(t); const q = input.value; t = setTimeout(() => search(q), 220); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); close(); location.hash = "#inventory"; }
    if (e.key === "Escape") { close(); input.blur(); }
  });
  document.addEventListener("click", (e) => { if (!panel.contains(e.target) && e.target !== input) close(); });
})();

/* avatar + account menu in the topbar */
(function wireChatFab() {
  const fab = document.getElementById("chatFab");
  if (fab) fab.onclick = () => { location.hash = "#chat"; };
})();
function renderMe(me) {
  const name = me.name || (me.role === "admin" ? "Admin" : "Pharmacist");
  const initial = (name[0] || "U").toUpperCase();
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  set("#meName", name); set("#mmName", name);
  set("#meRole", me.role === "admin" ? "Administrator" : "Pharmacist");
  set("#mmUser", me.username ? `@${me.username}` : "");
  set("#mmRole", me.role || "user");
  const av = $("#meAv");
  if (av) { av.textContent = initial; av.style.background = "#fff"; av.style.color = "var(--blue-800)"; }
  const btn = $("#meBtn"), menu = $("#meMenu"), wrap = $("#meWrap");
  if (!btn || !menu) return;
  const close = () => { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); };
  btn.onclick = () => {
    const open = menu.hidden;
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
  };
  document.addEventListener("click", (e) => { if (wrap && !wrap.contains(e.target)) close(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !menu.hidden) { close(); btn.focus(); } });
}

(async function boot() {
  let me = null;
  try { me = await api("/api/me"); } catch { me = null; }
  if (!me) { showLogin(); return; }
  USER = me;
  applyRoleNav();
  renderMe(me);
  try {
    const meta = await api("/api/meta");
    $("#brandName").textContent = meta.pharmacy || "Pharmacy";
    $("#asOfChip").textContent = `As of ${fmtDate(meta.as_of)}`;
    OVERVIEW = meta;
  } catch { /* ignore */ }
  if (isAdmin()) {
    try { renderShell(await api("/api/overview")); } catch { /* server warming up */ }
    setInterval(async () => { try { renderShell(await api("/api/overview")); } catch { /* ignore */ } }, 30000);
  }
  updateTaskBadge();
  navigate();
  setInterval(updateTaskBadge, 60000);
})();
