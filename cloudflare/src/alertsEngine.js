/**
 * Smart alert engine — Worker port of pharmacy/alerts.py.
 * Recomputes every rule from D1, upserts alerts by dedup_key, and resolves
 * alerts whose triggering condition no longer holds. Rules: expired stock,
 * expiry-critical / expiry-warning, low stock, stockout risk, overstock,
 * blocked (expired) units, demand spike, price rising, vendor returns due,
 * reorder approvals pending, data-quality.
 */
import { all, first, write, saveBookmark } from "./db.js";
import { todayStr, addDays, computePlan } from "./plan.js";

export async function refreshAlerts(session) {
  const today = todayStr();
  const now = new Date().toISOString();
  const setting = async (k, dflt) =>
    Number((await first(session, "SELECT value FROM settings WHERE key = ?", [k]))?.value) || dflt;
  const critDays = await setting("expiry_critical_days", 30);
  const warnDays = await setting("expiry_warning_days", 90);
  const overstockDays = await setting("overstock_days", 180);
  const found = new Map();
  const add = (key, atype, severity, title, message, drug_id = null, batch_id = null, details = {}) =>
    found.set(key, { key, atype, severity, title, message, drug_id, batch_id, details: JSON.stringify(details ?? {}) });

  // ---- expiry -------------------------------------------------------------
  const expRows = await all(session, `
    SELECT b.id, b.drug_id, b.batch_no, b.expiry_date, b.qty_remaining, b.unit_cost,
      d.name AS drug,
      CAST(julianday(b.expiry_date) - julianday(?) AS INTEGER) AS days_left
    FROM batches b JOIN drugs d ON d.id = b.drug_id
    WHERE b.qty_remaining > 0 AND b.expiry_date IS NOT NULL AND b.expiry_date <= ?
    ORDER BY b.expiry_date`, [today, addDays(today, warnDays)]);
  const byDrug = new Map();
  for (const r of expRows) {
    if (!byDrug.has(r.drug_id)) byDrug.set(r.drug_id, []);
    byDrug.get(r.drug_id).push(r);
  }
  for (const [drugId, rows] of byDrug) {
    const name = rows[0].drug;
    const expired = rows.filter((r) => r.days_left < 0);
    const critical = rows.filter((r) => r.days_left >= 0 && r.days_left <= critDays);
    const soon = rows.filter((r) => r.days_left > critDays && r.days_left <= warnDays);
    if (expired.length) {
      const qty = expired.reduce((a, r) => a + r.qty_remaining, 0);
      const val = expired.reduce((a, r) => a + r.qty_remaining * r.unit_cost, 0);
      add(`expired:${drugId}`, "expired_stock", "critical",
        `${expired.length} expired batch${expired.length > 1 ? "es" : ""} of ${name}`,
        `${qty} units (₹${Math.round(val).toLocaleString("en-IN")}) already past expiry — block dispensing and raise a return-to-vendor request.`,
        drugId, expired[0].id, { qty, value: Math.round(val * 100) / 100, batches: expired.slice(0, 20).map((r) => r.batch_no) });
    }
    if (critical.length) {
      const qty = critical.reduce((a, r) => a + r.qty_remaining, 0);
      const lastDay = critical.map((r) => r.expiry_date).sort().pop();
      add(`expiry_crit:${drugId}`, "expiry_soon", "high",
        `${name}: ${critical.length} batch${critical.length > 1 ? "es" : ""} expire within ${critDays} days`,
        `${qty} units expire by ${lastDay} — push FEFO dispensing and consider a vendor return.`,
        drugId, critical[0].id, { qty, batches: critical.slice(0, 20).map((r) => [r.batch_no, r.expiry_date]) });
    }
    if (soon.length) {
      const qty = soon.reduce((a, r) => a + r.qty_remaining, 0);
      add(`expiry_warn:${drugId}`, "expiry_soon", "medium",
        `${name}: ${soon.length} batch${soon.length > 1 ? "es" : ""} expiring within ${warnDays} days`,
        `${qty} units expire before ${addDays(today, warnDays)} — schedule run-down or return.`,
        drugId, soon[0].id, { qty, batches: soon.slice(0, 20).map((r) => [r.batch_no, r.expiry_date]) });
    }
  }

  // ---- stock levels / demand ----------------------------------------------
  const drugs = await all(session, `
    SELECT d.*,
      COALESCE(SUM(CASE WHEN b.expiry_date IS NULL OR b.expiry_date >= ?
                        THEN b.qty_remaining ELSE 0 END), 0) AS usable,
      COALESCE(SUM(CASE WHEN b.expiry_date < ? THEN b.qty_remaining ELSE 0 END), 0) AS expired,
      ROUND(COALESCE(SUM(CASE WHEN b.expiry_date IS NULL OR b.expiry_date >= ?
                             THEN b.qty_remaining * b.unit_cost ELSE 0 END), 0), 2) AS usable_value
    FROM drugs d LEFT JOIN batches b ON b.drug_id = d.id
    GROUP BY d.id`, [today, today, today]);
  const avgRows = await all(session, `
    SELECT drug_id, ROUND(AVG(q * 1.0), 2) AS avg_daily FROM (
      SELECT drug_id, date, SUM(qty) AS q FROM sales_daily
      WHERE date > date(?, '-90 day') GROUP BY drug_id, date
    ) GROUP BY drug_id`, [today]);
  const avgBy = new Map(avgRows.map((r) => [r.drug_id, r.avg_daily]));
  for (const d of drugs) {
    const plan = computePlan(d, avgBy.get(d.id) || 0, today);
    if (d.usable < plan.reorder_point) {
      add(`low_stock:${d.id}`, "low_stock", "high", `Low stock: ${d.name}`,
        `Only ${d.usable} units on hand vs a reorder point of ${Math.round(plan.reorder_point)} (${plan.lead_time_days}d lead time + ${Math.round(plan.safety_stock)} safety stock). Suggested order: ${plan.order_qty} units.`,
        d.id, null, plan);
    }
    if (plan.avg_daily > 0 && plan.cover_days <= plan.lead_time_days) {
      const stockout = addDays(today, Math.floor(plan.cover_days));
      add(`stockout:${d.id}`, "stockout_risk", "critical", `Stockout risk: ${d.name}`,
        `Projected to run out on ${stockout} at current demand (${plan.avg_daily} units/day) — reorder today.`,
        d.id, null, { ...plan, stockout_date: stockout });
    }
    if (plan.avg_daily > 0 && plan.cover_days > overstockDays) {
      add(`overstock:${d.id}`, "overstock", "medium", `Overstock: ${d.name}`,
        `${d.usable} units = ${Math.round(plan.cover_days)} days of cover (threshold ${overstockDays}d). Excess stock is the main driver of expiry waste — pause replenishment.`,
        d.id, null, plan);
    }
    if (d.expired > 0) {
      add(`expired_units:${d.id}`, "expired_stock", "medium", `Blocked stock: ${d.name}`,
        `${d.expired} units sit in expired batches and are excluded from available stock.`, d.id, null, plan);
    }
    const anchor = (await first(session,
      "SELECT MAX(date) AS a FROM sales WHERE drug_id = ? AND COALESCE(source, '') != 'pos_sim'", [d.id]))?.a;
    if (anchor) {
      const recent = (await first(session,
        "SELECT COALESCE(SUM(qty), 0) AS q FROM sales_daily WHERE drug_id = ? AND date > date(?, '-7 day') AND date <= ?",
        [d.id, anchor, anchor]))?.q || 0;
      const prior28 = (await first(session,
        "SELECT COALESCE(SUM(qty), 0) AS q FROM sales_daily WHERE drug_id = ? AND date > date(?, '-35 day') AND date <= date(?, '-7 day')",
        [d.id, anchor, anchor]))?.q || 0;
      const base = prior28 / 4;
      if (base >= 15 && recent >= 50 && recent >= base * 1.5) {
        const pct = Math.round((recent / base - 1) * 100);
        add(`spike:${d.id}`, "demand_spike", "medium", `Demand spike: ${d.name}`,
          `${recent} units sold in the 7 days to ${anchor} — ${pct}% above the 28-day baseline (${Math.round(base * 10) / 10}/week). Adjust stock level; ${plan.order_qty > 0 ? `suggested order ${plan.order_qty} units` : "no reorder needed — stock is sufficient, keep levels flat"}.`,
          d.id, null, { recent, baseline_weekly: Math.round(base * 10) / 10, anchor, change_pct: pct, order_qty: plan.order_qty });
      }
      const pr = await first(session,
        "SELECT AVG(unit_price) AS p, COUNT(*) AS n FROM sales WHERE drug_id = ? AND date > date(?, '-90 day') AND date <= ? AND unit_price > 0",
        [d.id, anchor, anchor]);
      const pp = await first(session,
        "SELECT AVG(unit_price) AS p, COUNT(*) AS n FROM sales WHERE drug_id = ? AND date <= date(?, '-90 day') AND date > date(?, '-180 day') AND unit_price > 0",
        [d.id, anchor, anchor]);
      if (pr && pp && pr.n >= 15 && pp.n >= 15 && pp.p && pr.p / pp.p - 1 >= 0.08) {
        const change = pr.p / pp.p - 1;
        add(`price:${d.id}`, "price_rising", "medium", `Price rising: ${d.name}`,
          `Avg sale price ₹${pr.p.toFixed(2)} vs ₹${pp.p.toFixed(2)} in the prior 90 days (+${Math.round(change * 100)}%) — stock up before further hikes; ${plan.order_qty > 0 ? `suggested order ${plan.order_qty} units` : "current stock covers demand — hold quantities flat"}.`,
          d.id, null, { recent_price: Math.round(pr.p * 100) / 100, prior_price: Math.round(pp.p * 100) / 100, change_pct: Math.round(change * 1000) / 10, order_qty: plan.order_qty });
      }
    }
  }

  // ---- pending vendor returns ----------------------------------------------
  for (const sup of await all(session, `
    SELECT s.id, s.name, COUNT(w.id) AS n, SUM(w.value) AS v FROM suppliers s
    JOIN waste w ON w.supplier_id = s.id WHERE w.status = 'pending' GROUP BY s.id`)) {
    add(`return_due:${sup.id}`, "vendor_return", "medium", `Return pending: ${sup.name}`,
      `${sup.n} waste lot${sup.n > 1 ? "s" : ""} worth ₹${Math.round(sup.v || 0).toLocaleString("en-IN")} awaiting a return-to-vendor request.`,
      null, null, { supplier_id: sup.id, qty: sup.n, value: Math.round((sup.v || 0) * 100) / 100 });
  }

  // ---- reorder requests awaiting approval ----------------------------------
  for (const r of await all(session, `
    SELECT r.id, r.qty, r.due_date, d.name AS drug, s.name AS supplier
    FROM reorders r JOIN drugs d ON d.id = r.drug_id
    LEFT JOIN suppliers s ON s.id = r.supplier_id
    WHERE r.status IN ('suggested', 'ordered')`)) {
    add(`po_pending:${r.id}`, "approval_request", "medium",
      `Low-supply request awaiting approval: ${r.drug}`,
      `Purchase request for ${r.qty} units of ${r.drug}${r.supplier ? ` from ${r.supplier}` : ""}${r.due_date ? ` (due ${r.due_date})` : ""} — manager to review; a supplier draft email is in the outbox.`,
      null, null, { reorder_id: r.id, qty: r.qty });
  }

  // ---- data quality ----------------------------------------------------------
  for (const up of await all(session, `
    SELECT id, filename, rows_total, rows_rejected, issues FROM uploads
    WHERE rows_rejected > 0 ORDER BY id DESC LIMIT 10`)) {
    let counts = {};
    try { counts = JSON.parse(up.issues || "{}").counts || {}; } catch { counts = {}; }
    const top = Object.entries(counts).slice(0, 4).map(([k, v]) => `${k}: ${v}`).join(", ") || "see report";
    add(`dq:${up.id}`, "data_quality", "medium", `Data quality: ${up.filename}`,
      `${up.rows_rejected} of ${up.rows_total} rows quarantined (${top}). They are excluded from stock, forecasts and alerts.`,
      null, null, { upload_id: up.id, counts });
  }

  // ---- upsert: update seen keys, insert new, resolve the rest ----------------
  const existing = new Set(
    (await all(session, "SELECT dedup_key FROM alerts WHERE status != 'resolved'")).map((r) => r.dedup_key));
  for (const a of found.values()) {
    if (existing.has(a.key)) {
      await write(session,
        "UPDATE alerts SET severity = ?, title = ?, message = ?, details = ?, drug_id = ?, batch_id = ?, updated_at = ? WHERE dedup_key = ?",
        [a.severity, a.title, a.message, a.details, a.drug_id, a.batch_id, now, a.key]);
    } else {
      await write(session,
        "INSERT INTO alerts(atype, severity, drug_id, batch_id, title, message, details, dedup_key, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,'active',?,?)",
        [a.atype, a.severity, a.drug_id, a.batch_id, a.title, a.message, a.details, a.key, now, now]);
    }
  }
  for (const key of existing) {
    if (!found.has(key)) {
      await write(session, "UPDATE alerts SET status = 'resolved', updated_at = ? WHERE dedup_key = ?", [now, key]);
    }
  }
  await saveBookmark(session);
  return { created: [...found.keys()].filter((k) => !existing.has(k)).length, total: found.size };
}
