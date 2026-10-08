/**
 * Analytics + operations endpoints ported from the Python backend
 * (pharmacy/analytics.py, pharmacy/shelf.py, pharmacy/suppliers.py,
 * pharmacy/alerts.py helpers). All reads go through the D1 session so they
 * stay bookmark-ordered and replica-served.
 *
 * NOTE: the Worker schema has no `forecasts` table (that model runs in the
 * Python service), so reorder suggestions / accuracy fall back to
 * stock-level maths from drugs.reorder_point — enough to drive every view.
 */
import { all, first, scalar, write } from "./db.js";

const round2 = (n) => Math.round((Number(n) || 0) * 100) / 100;
const todayStr = () => new Date().toISOString().slice(0, 10);
const addDays = (iso, n) => new Date(new Date(iso + "T00:00:00Z").getTime() + n * 86400000).toISOString().slice(0, 10);
const daysBetween = (a, b) => Math.round((new Date(b + "T00:00:00Z") - new Date(a + "T00:00:00Z")) / 86400000);

/* ------------------------------ /api/overview ------------------------------ */
export async function overview(session) {
  const today = todayStr();
  const settings = Object.fromEntries(
    (await all(session, "SELECT key, value FROM settings")).map((r) => [r.key, r.value]));
  const warnDays = Number(settings.expiry_warning_days || 90);

  const stock = await first(session, `
    SELECT
      COALESCE(SUM(CASE WHEN expiry_date IS NULL OR expiry_date >= ? THEN qty_remaining ELSE 0 END),0) AS usable_qty,
      COALESCE(SUM(CASE WHEN expiry_date IS NULL OR expiry_date >= ? THEN qty_remaining*unit_cost ELSE 0 END),0) AS usable_value,
      COALESCE(SUM(CASE WHEN expiry_date < ? THEN qty_remaining ELSE 0 END),0) AS expired_qty,
      COALESCE(SUM(CASE WHEN expiry_date < ? THEN qty_remaining*unit_cost ELSE 0 END),0) AS expired_value,
      COUNT(*) AS batches FROM batches`, [today, today, today, today]);
  const retail = await scalar(session, `
    SELECT COALESCE(SUM(b.qty_remaining * d.mrp),0) FROM batches b JOIN drugs d ON d.id=b.drug_id
    WHERE b.expiry_date IS NULL OR b.expiry_date >= ?`, [today], 0);

  const expiring = await scalar(session, `
    SELECT COUNT(*) FROM batches WHERE qty_remaining > 0 AND expiry_date IS NOT NULL
    AND expiry_date >= ? AND expiry_date <= ?`, [today, addDays(today, warnDays)], 0);
  const expiringValue = await scalar(session, `
    SELECT COALESCE(SUM(qty_remaining*unit_cost),0) FROM batches WHERE qty_remaining > 0
    AND expiry_date IS NOT NULL AND expiry_date >= ? AND expiry_date <= ?`,
    [today, addDays(today, warnDays)], 0);

  // stock-level low-stock view (no forecasts table on the Worker)
  const lowRows = await all(session, `
    SELECT d.id, d.reorder_point,
      COALESCE(SUM(CASE WHEN b.expiry_date IS NULL OR b.expiry_date >= ? THEN b.qty_remaining ELSE 0 END),0) AS usable
    FROM drugs d LEFT JOIN batches b ON b.drug_id = d.id GROUP BY d.id`, [today]);
  let lowStock = 0, orderNow = 0;
  for (const r of lowRows) {
    const rop = Number(r.reorder_point || 0);
    if (rop > 0 && r.usable < rop) { lowStock++; orderNow++; }
  }

  const dataTo = (await scalar(session, "SELECT MAX(date) FROM sales", [], null)) || today;
  const anchor = dataTo;
  const revLast30 = await first(session, `
    SELECT COALESCE(SUM(revenue),0) AS rev, COALESCE(SUM(qty),0) AS qty FROM sales_daily
    WHERE date > ? AND date <= ?`, [addDays(anchor, -30), anchor]);
  const revPrev30 = await scalar(session, `
    SELECT COALESCE(SUM(revenue),0) FROM sales_daily WHERE date > ? AND date <= ?`,
    [addDays(anchor, -60), addDays(anchor, -30)], 0);
  const waste = await first(session,
    "SELECT COALESCE(SUM(value),0) AS v, COALESCE(SUM(qty),0) AS q, COUNT(*) AS n FROM waste");

  const series = await all(session, `
    SELECT date, SUM(qty) AS qty, SUM(revenue) AS revenue FROM sales_daily
    WHERE date > ? AND date <= ? GROUP BY date ORDER BY date`, [addDays(anchor, -90), anchor]);

  const alertSummary = await first(session, `
    SELECT COUNT(*) AS active,
      SUM(CASE WHEN severity='critical' THEN 1 ELSE 0 END) AS critical,
      SUM(CASE WHEN severity='high' THEN 1 ELSE 0 END) AS high,
      SUM(CASE WHEN status='acknowledged' THEN 1 ELSE 0 END) AS acknowledged
    FROM alerts WHERE status != 'resolved'`);

  return {
    pharmacy: settings.pharmacy_name || "HealthFirst Pharmacy",
    as_of: today,
    sku_count: await scalar(session, "SELECT COUNT(*) FROM drugs", [], 0),
    batch_count: stock.batches,
    supplier_count: await scalar(session, "SELECT COUNT(*) FROM suppliers", [], 0),
    usable_qty: stock.usable_qty, usable_value: round2(stock.usable_value),
    retail_value: round2(retail),
    expired_qty: stock.expired_qty, expired_value: round2(stock.expired_value),
    expiring_90d: expiring, expiring_90d_value: round2(expiringValue),
    low_stock: lowStock, order_now: orderNow,
    sales_30d_qty: revLast30?.qty || 0, sales_30d_rev: round2(revLast30?.rev || 0),
    sales_prev30_rev: round2(revPrev30),
    sales_growth_pct: revPrev30 ? round2(((revLast30?.rev || 0) - revPrev30) / revPrev30 * 100) : null,
    sales_window: `${addDays(anchor, -29)} to ${dataTo}`,
    sales_window_90: `${addDays(anchor, -89)} to ${dataTo}`,
    data_staleness_days: daysBetween(dataTo, today),
    waste_value: round2(waste?.v || 0), waste_qty: waste?.q || 0, waste_lots: waste?.n || 0,
    forecast_accuracy: null, forecast_accuracy_daily: null,
    alerts: { active: alertSummary?.active || 0, critical: alertSummary?.critical || 0,
      high: alertSummary?.high || 0, acknowledged: alertSummary?.acknowledged || 0 },
    sales_series: series.map((r) => ({ date: r.date, qty: r.qty, revenue: round2(r.revenue) })),
    total_sales: await scalar(session, "SELECT COUNT(*) FROM sales", [], 0),
    total_purchases: await scalar(session, "SELECT COUNT(*) FROM purchases", [], 0),
    quarantined: await scalar(session, "SELECT COUNT(*) FROM quarantined", [], 0),
    data_from: await scalar(session, "SELECT MIN(date) FROM sales", [], null),
    data_to: dataTo,
  };
}

/* ------------------------------ /api/charts ------------------------------ */
export async function charts(session) {
  const monthly = (await all(session, `
    SELECT substr(date,1,7) AS month, SUM(qty) AS qty, SUM(revenue) AS revenue
    FROM sales_daily GROUP BY month ORDER BY month DESC LIMIT 18`)).reverse();
  const categories = await all(session, `
    SELECT d.category, COUNT(DISTINCT d.id) AS skus,
      COALESCE(SUM(b.qty_remaining * b.unit_cost),0) AS value
    FROM drugs d LEFT JOIN batches b ON b.drug_id = d.id
    GROUP BY d.category ORDER BY value DESC`);
  const dataTo = (await scalar(session, "SELECT MAX(date) FROM sales", [], null)) || todayStr();
  const cutoff = addDays(dataTo, -90);
  let topDrugs = await all(session, `
    SELECT d.name, d.category, SUM(sd.qty) AS qty, SUM(sd.revenue) AS revenue
    FROM sales_daily sd JOIN drugs d ON d.id = sd.drug_id WHERE sd.date >= ?
    GROUP BY d.id ORDER BY revenue DESC LIMIT 8`, [cutoff]);
  if (!topDrugs.length) {
    topDrugs = await all(session, `
      SELECT d.name, d.category, SUM(sd.qty) AS qty, SUM(sd.revenue) AS revenue
      FROM sales_daily sd JOIN drugs d ON d.id = sd.drug_id
      GROUP BY d.id ORDER BY revenue DESC LIMIT 8`);
  }
  // expiry timeline by calendar quarter
  const rows = await all(session, `
    SELECT expiry_date, qty_remaining, unit_cost FROM batches
    WHERE qty_remaining > 0 AND expiry_date IS NOT NULL ORDER BY expiry_date`);
  const buckets = new Map();
  for (const r of rows) {
    const d = new Date(r.expiry_date + "T00:00:00Z");
    const key = `${d.getUTCFullYear()}-${String(Math.floor(d.getUTCMonth() / 3) * 3 + 1).padStart(2, "0")}`;
    const b = buckets.get(key) || { bucket: key, qty: 0, value: 0 };
    b.qty += r.qty_remaining; b.value += r.qty_remaining * r.unit_cost;
    buckets.set(key, b);
  }
  const supplierSpend = await all(session, `
    SELECT s.name AS supplier, COALESCE(SUM(p.total),0) AS spend,
      (SELECT COALESCE(SUM(w.value),0) FROM waste w WHERE w.supplier_id = s.id) AS waste
    FROM suppliers s LEFT JOIN purchases p ON p.supplier_id = s.id
    GROUP BY s.id ORDER BY spend DESC`);
  const categorySales = await all(session, `
    SELECT d.category, SUM(sd.revenue) AS revenue, SUM(sd.qty) AS qty
    FROM sales_daily sd JOIN drugs d ON d.id = sd.drug_id
    GROUP BY d.category ORDER BY revenue DESC`);
  return {
    monthly, categories, category_sales: categorySales, top_drugs: topDrugs,
    expiry_timeline: { buckets: [...buckets.values()].sort((a, b) => a.bucket.localeCompare(b.bucket))
      .map((b) => ({ ...b, value: round2(b.value) })) },
    supplier_spend: supplierSpend.map((s) => ({ ...s, spend: round2(s.spend), waste: round2(s.waste) })),
    sales_window_90: dataTo,
    forecast_accuracy: [],
  };
}

/* ------------------------------ /api/activity ------------------------------ */
export async function activity(session, url) {
  const limit = Math.min(100, Math.max(1, Number(url.searchParams.get("limit") || 25)));
  const uploads = await all(session, `
    SELECT id, filename, kind, rows_total, rows_accepted, rows_rejected, created_at
    FROM uploads ORDER BY id DESC LIMIT ?`, [limit]);
  const reorders = await all(session, `
    SELECT r.id, r.qty, r.status, r.created_at, d.name AS drug
    FROM reorders r JOIN drugs d ON d.id = r.drug_id ORDER BY r.id DESC LIMIT ?`, [limit]);
  const waste = await all(session, `
    SELECT w.id, w.qty, w.value, w.reason, w.status, w.created_at, d.name AS drug
    FROM waste w JOIN drugs d ON d.id = w.drug_id ORDER BY w.id DESC LIMIT ?`, [limit]);
  const notifications = await all(session, `
    SELECT n.id, n.subject, n.status, n.created_at, s.name AS supplier
    FROM notifications n LEFT JOIN suppliers s ON s.id = n.supplier_id
    ORDER BY n.id DESC LIMIT ?`, [limit]);
  return { uploads, reorders, waste, notifications };
}

/* ------------------------------ /api/suppliers ------------------------------ */
export async function suppliersList(session) {
  const today = todayStr();
  const rows = await all(session, `
    SELECT s.*, COUNT(DISTINCT b.id) AS batches, COALESCE(SUM(b.qty_received),0) AS units
    FROM suppliers s LEFT JOIN batches b ON b.supplier_id = s.id
    GROUP BY s.id ORDER BY s.name`);
  const items = [];
  for (const r of rows) {
    const spend = await scalar(session, "SELECT COALESCE(SUM(total),0) FROM purchases WHERE supplier_id = ?", [r.id], 0);
    const w = await first(session, `
      SELECT COUNT(*) AS n, COALESCE(SUM(value),0) AS v, COALESCE(SUM(qty),0) AS q
      FROM waste WHERE supplier_id = ?`, [r.id]);
    const pos = await scalar(session, "SELECT COUNT(DISTINCT purchase_id) FROM purchases WHERE supplier_id = ?", [r.id], 0);
    const shelfLife = await scalar(session, `
      SELECT AVG(julianday(b.expiry_date) - julianday(b.received_date)) FROM batches b
      WHERE b.supplier_id = ? AND b.expiry_date IS NOT NULL AND b.received_date IS NOT NULL`, [r.id], 0) || 0;
    const expiring = await scalar(session, `
      SELECT COUNT(*) FROM batches WHERE supplier_id = ? AND qty_remaining > 0
      AND expiry_date IS NOT NULL AND expiry_date < ?`, [r.id, today], 0);
    const wastePct = spend ? (w.v / spend) * 100 : 0;
    const score = round2(Math.max(0, Math.min(100, 100 - wastePct * 1.6 - expiring * 1.5 + Math.min(shelfLife, 730) / 60)));
    items.push({
      id: r.id, name: r.name, email: r.email, lead_time_days: r.lead_time_days,
      orders: pos, batches: r.batches, units: r.units,
      spend: round2(spend), waste_qty: w.q, waste_value: round2(w.v),
      waste_pct: round2(wastePct), expired_batches: expiring,
      avg_shelf_life_days: round2(shelfLife), score,
    });
  }
  const notifications = await all(session, `
    SELECT n.*, s.name AS supplier, s.email FROM notifications n
    LEFT JOIN suppliers s ON s.id = n.supplier_id ORDER BY n.id DESC LIMIT 100`);
  return { items, notifications };
}

/* ------------------------------ /api/expiry ------------------------------ */
function statusFor(days, crit, warn) {
  if (days === null) return "ok";
  if (days < 0) return "expired";
  if (days <= crit) return "critical";
  if (days <= warn) return "warning";
  return "ok";
}

export async function expiry(session) {
  const today = todayStr();
  const settings = Object.fromEntries(
    (await all(session, "SELECT key, value FROM settings")).map((r) => [r.key, r.value]));
  const crit = Number(settings.expiry_critical_days || 30);
  const warn = Number(settings.expiry_warning_days || 90);
  const rows = await all(session, `
    SELECT b.drug_id, d.name AS drug, b.expiry_date, b.qty_remaining, b.unit_cost
    FROM batches b JOIN drugs d ON d.id = b.drug_id
    WHERE b.qty_remaining > 0 AND b.expiry_date IS NOT NULL`);
  const buckets = {
    expired: { label: "Expired", qty: 0, value: 0, batches: 0 },
    [`0-${crit}`]: { label: `Next ${crit} days`, qty: 0, value: 0, batches: 0 },
    [`${crit + 1}-${warn}`]: { label: `${crit + 1}–${warn} days`, qty: 0, value: 0, batches: 0 },
    [`${warn + 1}-180`]: { label: `${warn + 1}–180 days`, qty: 0, value: 0, batches: 0 },
    "180+": { label: "Beyond 180 days", qty: 0, value: 0, batches: 0 },
  };
  const perDrug = {};
  for (const r of rows) {
    const days = daysBetween(today, r.expiry_date);
    const key = days < 0 ? "expired" : days <= crit ? `0-${crit}`
      : days <= warn ? `${crit + 1}-${warn}` : days <= 180 ? `${warn + 1}-180` : "180+";
    const val = r.qty_remaining * r.unit_cost;
    buckets[key].qty += r.qty_remaining; buckets[key].value += val; buckets[key].batches += 1;
    const d = perDrug[r.drug] || (perDrug[r.drug] = { expired: 0, critical: 0, warning: 0, safe: 0 });
    if (key === "expired") d.expired += r.qty_remaining;
    else if (key === `0-${crit}`) d.critical += r.qty_remaining;
    else if (key === `${crit + 1}-${warn}`) d.warning += r.qty_remaining;
    else d.safe += r.qty_remaining;
  }
  for (const v of Object.values(buckets)) v.value = round2(v.value);
  // FEFO shelf list with per-drug rank
  const shelfRows = await all(session, `
    SELECT b.*, d.name AS drug, s.name AS supplier, sh.code AS shelf_code, sh.zone AS shelf_zone
    FROM batches b JOIN drugs d ON d.id = b.drug_id
    LEFT JOIN suppliers s ON s.id = b.supplier_id
    LEFT JOIN shelves sh ON sh.id = b.shelf_id
    WHERE b.qty_remaining > 0 ORDER BY d.name, b.expiry_date IS NULL, b.expiry_date LIMIT 400`);
  const rank = new Map();
  const shelf = shelfRows.map((r) => {
    const days = r.expiry_date ? daysBetween(today, r.expiry_date) : null;
    rank.set(r.drug_id, (rank.get(r.drug_id) || 0) + 1);
    return { ...r, fefo_rank: rank.get(r.drug_id), status: statusFor(days, crit, warn),
      days_to_expiry: days, value: round2(r.qty_remaining * r.unit_cost) };
  });
  return { buckets, per_drug: perDrug, shelf };
}

/* ------------------------------ /api/waste ------------------------------ */
export async function syncExpiredWaste(session) {
  const rows = await all(session, `
    SELECT b.*, d.name AS drug FROM batches b JOIN drugs d ON d.id = b.drug_id
    WHERE b.qty_remaining > 0 AND b.expiry_date IS NOT NULL AND b.expiry_date < ?
    AND NOT EXISTS (SELECT 1 FROM waste w WHERE w.batch_id = b.id AND w.reason = 'expired')`,
    [todayStr()]);
  const now = new Date().toISOString();
  for (const r of rows) {
    await write(session,
      "INSERT INTO waste(drug_id, batch_id, supplier_id, qty, reason, unit_cost, value, status, note, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
      [r.drug_id, r.id, r.supplier_id, r.qty_remaining, "expired", r.unit_cost,
        round2(r.qty_remaining * r.unit_cost), "pending",
        `Batch ${r.batch_no} expired on ${r.expiry_date}`, now]);
  }
  return rows.length;
}

export async function wasteSummary(session) {
  const total = await first(session,
    "SELECT COUNT(*) AS n, COALESCE(SUM(value),0) AS v, COALESCE(SUM(qty),0) AS q FROM waste");
  const byReason = await all(session,
    "SELECT reason, COUNT(*) AS n, SUM(value) AS v, SUM(qty) AS q FROM waste GROUP BY reason");
  const byDrug = await all(session, `
    SELECT d.name AS drug, SUM(w.value) AS v, SUM(w.qty) AS q
    FROM waste w JOIN drugs d ON d.id = w.drug_id GROUP BY d.name ORDER BY v DESC`);
  const bySupplier = await all(session, `
    SELECT s.name AS supplier, SUM(w.value) AS v, SUM(w.qty) AS q
    FROM waste w LEFT JOIN suppliers s ON s.id = w.supplier_id GROUP BY s.name ORDER BY v DESC`);
  const byMonth = await all(session, `
    SELECT substr(created_at,1,7) AS month, SUM(value) AS v, SUM(qty) AS q
    FROM waste GROUP BY month ORDER BY month`);
  const byStatus = await all(session,
    "SELECT status, COUNT(*) AS n, SUM(value) AS v FROM waste GROUP BY status");
  const purchasesTotal = await scalar(session, "SELECT COALESCE(SUM(total),0) FROM purchases", [], 0);
  const salesTotal = await scalar(session, "SELECT COALESCE(SUM(total),0) FROM sales", [], 0);
  const valueAtRisk = await scalar(session, `
    SELECT COALESCE(SUM(qty_remaining*unit_cost),0) FROM batches
    WHERE expiry_date IS NOT NULL AND expiry_date < ?
    AND NOT EXISTS (SELECT 1 FROM waste w WHERE w.batch_id = batches.id)`, [todayStr()], 0);
  return {
    total_qty: total.q, total_value: round2(total.v), total_lots: total.n,
    by_reason: byReason, by_drug: byDrug, by_supplier: bySupplier,
    by_month: byMonth, by_status: byStatus,
    purchase_value: round2(purchasesTotal), sales_value: round2(salesTotal),
    waste_pct_of_purchases: purchasesTotal ? round2(total.v / purchasesTotal * 100) : 0,
    value_at_risk: round2(valueAtRisk),
  };
}

export async function wasteAll(session) {
  await syncExpiredWaste(session);
  const items = await all(session, `
    SELECT w.*, d.name AS drug, b.batch_no, b.expiry_date, s.name AS supplier
    FROM waste w JOIN drugs d ON d.id = w.drug_id
    LEFT JOIN batches b ON b.id = w.batch_id
    LEFT JOIN suppliers s ON s.id = w.supplier_id
    ORDER BY w.id DESC LIMIT 200`);
  return { summary: await wasteSummary(session), items };
}

/* ------------------------------ /api/returns ------------------------------ */
export async function listReturns(session) {
  const rows = await all(session, `
    SELECT r.*, s.name AS supplier FROM returns r
    LEFT JOIN suppliers s ON s.id = r.supplier_id ORDER BY r.id DESC`);
  for (const r of rows) {
    let ids = [];
    try { ids = JSON.parse(r.waste_ids || "[]"); } catch { ids = []; }
    r.lots = ids.length ? await all(session, `
      SELECT w.qty, w.value, w.reason, d.name AS drug, b.batch_no, b.expiry_date
      FROM waste w JOIN drugs d ON d.id = w.drug_id
      LEFT JOIN batches b ON b.id = w.batch_id
      WHERE w.id IN (${ids.map(() => "?").join(",")})`, ids) : [];
  }
  return rows;
}
