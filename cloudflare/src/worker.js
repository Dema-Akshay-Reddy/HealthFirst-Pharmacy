/**
 * Smart Pharmacy Inventory — Cloudflare Worker.
 *
 * Architecture (2 real-time-synced databases + load balancer + monthly archive):
 *
 *   Internet ──▶ Cloudflare anycast edge (network LB) ──▶ this Worker
 *                 │
 *                 ├─▶ D1 PRIMARY  (DB_A)  ← every WRITE lands here
 *                 │      │  read replication = continuous, real-time sync
 *                 │      ▼
 *                 ├─▶ D1 REPLICA  (DB_B)  ← nearest-colo READS via Sessions API
 *                 │
 *                 ├─▶ R2 ARCHIVE          ← monthly snapshot (cron: 5 0 1 * *)
 *                 │
 *                 └─▶ ASSETS              ← the SPA (static/)
 *
 * Consistency: the Sessions API bookmark travels in `X-D1-Bookmark` on every
 * response and is accepted back on the next request, so a client is never
 * served a replica read older than its own last write — the two databases
 * are effectively in real time.
 */
import {
  makeSession, jsonResponse, write, all, first, scalar, saveBookmark,
} from "./db.js";
import { handleArchive } from "./archive.js";
import {
  overview, charts, activity, suppliersList, expiry, wasteAll, listReturns,
} from "./analytics.js";
import { ensureUsers, login, currentUser, roleAllows, makeHash } from "./auth.js";
import { todayStr, addDays, computePlan } from "./plan.js";
import { refreshAlerts } from "./alertsEngine.js";

const PUBLIC_ROUTES = new Set(["POST /api/login", "GET /api/me", "POST /api/logout", "GET /api/health"]);

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (path === "/healthz" || path === "/api/health") {
      return Response.json({ status: "ok", databases: 2, archive: "r2", cron: "monthly" });
    }

    if (path.startsWith("/api/")) {
      const session = makeSession(env, request);
      try {
        await ensureUsers(session, env); // idempotent — seeds configured accounts once
        const user = await currentUser(session, request);
        const key = `${request.method} ${path}`;
        if (!PUBLIC_ROUTES.has(key)) {
          if (!user) return jsonResponse(session, { detail: "login required" }, { status: 401 });
          if (!roleAllows(user, request.method, path)) {
            return jsonResponse(session, { detail: "admin access required for this area" }, { status: 403 });
          }
        }
        const response = await routeApi(request, env, session, user, path, url);
        // CSV reports return raw text, not JSON-wrapped
        if (typeof response.body === "string" && response.init?.headers?.["Content-Type"]?.startsWith("text/csv")) {
          const fname = path.split("/").pop() || "report.csv";
          return new Response(response.body, {
            headers: { ...response.init.headers, "Content-Disposition": `attachment; filename=${fname.endsWith(".csv") ? fname : fname + ".csv"}`,
              ...(session.getBookmark ? { "X-D1-Bookmark": session.getBookmark() || "" } : {}) },
          });
        }
        return jsonResponse(session, response.body, response.init);
      } catch (err) {
        return jsonResponse(session, { detail: err.message || "internal error" }, { status: 500 });
      }
    }

    // the SPA references assets with the /static/ prefix (same as the local
    // Python server); the Workers asset binding serves them at the root, so
    // rewrite /static/<file> → /<file> before handing off to ASSETS
    if (path.startsWith("/static/")) {
      const inner = new URL(path.slice("/static".length) + url.search, url.origin);
      return env.ASSETS.fetch(new Request(inner, request));
    }
    return env.ASSETS.fetch(request);
  },

  /** Monthly archive: 00:05 UTC on the 1st (wrangler.jsonc → triggers.crons). */
  async scheduled(controller, env, ctx) {
    try {
      const result = await handleArchive(env);
      console.log("[archive:scheduled]", JSON.stringify(result));
    } catch (err) {
      console.error("[archive:scheduled] FAILED", err && (err.stack || err.message || String(err)));
      throw err;
    }
  },
};

/* ------------------------------------------------------------------ routes */
async function routeApi(request, env, session, user, path, url) {
  const method = request.method;

  switch (`${method} ${path}`) {
    case "POST /api/login": {
      const body = await request.json().catch(() => ({}));
      const result = await login(session, body.username, body.password);
      return result
        ? { body: result }
        : { body: { detail: "invalid username or password" }, init: { status: 401 } };
    }
    case "GET /api/me":
      return user
        ? { body: { username: user.u, role: user.r } }
        : { body: { detail: "not logged in" }, init: { status: 401 } };
    case "POST /api/logout":
      return { body: { ok: true } };
    case "GET /api/meta":
      return { body: await meta(session) };
    case "GET /api/drugs":
      return { body: await listDrugs(session) };
    case "GET /api/batches":
      return { body: await listBatches(session, url) };
    case "GET /api/shelf/tasks":
      return { body: await shelfTasks(session, url) };
    case "POST /api/shelf/tasks/refresh":
      return { body: await refreshShelfTasks(session) };
    case "POST /api/shelf/tasks": {
      const body = await request.json().catch(() => ({}));
      return { body: await completeShelfTask(session, body), init: { status: 200 } };
    }
    case "GET /api/counter/lookup":
      return { body: await counterLookup(session, url) };
    case "POST /api/prescriptions": {
      const body = await request.json().catch(() => ({}));
      return { body: await issuePrescription(session, body) };
    }
    case "POST /api/dispense": {
      const body = await request.json().catch(() => ({}));
      return { body: await issuePrescription(session, body) };
    }
    case "GET /api/alerts":
      return { body: await listAlerts(session) };
    case "GET /api/sync/status":
      return { body: await syncStatus(session) };
    case "POST /api/archive/run":
      return { body: await runArchiveNow(env, session) };
    case "GET /api/overview":
      return { body: await overview(session) };
    case "GET /api/charts":
      return { body: await charts(session) };
    case "GET /api/activity":
      return { body: await activity(session, url) };
    case "GET /api/suppliers":
      return { body: await suppliersList(session) };
    case "GET /api/expiry":
      return { body: await expiry(session) };
    case "GET /api/waste":
      return { body: await wasteAll(session) };
    case "GET /api/returns":
      return { body: await listReturns(session) };
    case "GET /api/forecast":
      return { body: [] }; // per-SKU forecast model runs in the Python service
    case "GET /api/reorders":
      return { body: await reorderSuggestions(session) };
    case "GET /api/uploads":
      return { body: await uploadsList(session) };
    case "GET /api/data-quality":
      return { body: await dataQuality(session) };
    case "GET /api/chat/history":
      return { body: [] };
    case "POST /api/chat": {
      const body = await request.json().catch(() => ({}));
      return { body: await chatRespond(session, body.message) };
    }
    case "POST /api/alerts/ack-all":
      return { body: await ackAllAlerts(session) };
    case "POST /api/alerts/refresh":
      return { body: await refreshAlerts(session) };
    case "GET /api/users":
      return { body: await listUsers(session) };
    case "POST /api/users": {
      const body = await request.json().catch(() => ({}));
      return { body: await createUser(session, body) };
    }
    case "POST /api/reorders": {
      const body = await request.json().catch(() => ({}));
      return { body: await createReorder(session, body) };
    }
    case "POST /api/waste": {
      const body = await request.json().catch(() => ({}));
      return { body: await addWaste(session, body) };
    }
    case "POST /api/returns": {
      const body = await request.json().catch(() => ({}));
      return { body: await createReturn(session, body) };
    }
    case "POST /api/settings": {
      const body = await request.json().catch(() => ({}));
      return { body: await saveSettings(session, body) };
    }
    case "POST /api/notifications": {
      const body = await request.json().catch(() => ({}));
      return { body: await createNotification(session, body) };
    }
    case "POST /api/notifications/build-reorder-drafts":
      return { body: await buildReorderDrafts(session) };
    case "GET /api/sales": {
      const drugId = Number(url.searchParams.get("drug_id") || 0);
      const days = Math.min(365, Math.max(1, Number(url.searchParams.get("days") || 90)));
      const rows = await all(session, `
        SELECT date, SUM(qty) AS qty, ROUND(SUM(revenue), 2) AS revenue
        FROM sales_daily WHERE date > date('now', ?)${drugId ? " AND drug_id = ?" : ""}
        GROUP BY date ORDER BY date`, drugId ? [`-${days} day`, drugId] : [`-${days} day`]);
      return { body: rows };
    }
    case "GET /api/forecast-vs-actual":
      return { body: { points: [], model: null,
        note: "The damped Holt-Winters back-test runs in the full Python service; live reorder maths on this Worker cover policy planning." } };
    case "GET /api/upload/template":
      return { body: uploadTemplate(), init: { headers: { "Content-Type": "text/csv; charset=utf-8" } } };
  }

  // task completion by id: POST /api/shelf/tasks/:id/done
  if (method === "POST" && /^\/api\/shelf\/tasks\/\d+\/done$/.test(path)) {
    const id = Number(path.split("/")[4]);
    return { body: await completeShelfTask(session, { id }) };
  }
  // drug policy update: PATCH /api/drugs/:id
  if (method === "PATCH" && /^\/api\/drugs\/\d+$/.test(path)) {
    const id = Number(path.split("/")[3]);
    const body = await request.json().catch(() => ({}));
    return { body: await updateDrugPolicy(session, id, body) };
  }
  // alert acknowledge: POST /api/alerts/:id/ack
  if (method === "POST" && /^\/api\/alerts\/\d+\/ack$/.test(path)) {
    const id = Number(path.split("/")[3]);
    await write(session, "UPDATE alerts SET status='acknowledged', updated_at=? WHERE id=?",
      [new Date().toISOString(), id]);
    await saveBookmark(session);
    return { body: { ok: true, id } };
  }
  // notification status: PATCH /api/notifications/:id
  if (method === "PATCH" && /^\/api\/notifications\/\d+$/.test(path)) {
    const id = Number(path.split("/")[3]);
    const body = await request.json().catch(() => ({}));
    await write(session, "UPDATE notifications SET status=?, sent_at=? WHERE id=?",
      [String(body.status || "draft"), body.status === "sent" ? new Date().toISOString() : null, id]);
    await saveBookmark(session);
    return { body: { ok: true, id, status: body.status } };
  }
  // reorder status: PATCH /api/reorders/:id
  if (method === "PATCH" && /^\/api\/reorders\/\d+$/.test(path)) {
    const id = Number(path.split("/")[3]);
    const body = await request.json().catch(() => ({}));
    await write(session, "UPDATE reorders SET status=?, updated_at=? WHERE id=?",
      [String(body.status || "suggested"), new Date().toISOString(), id]);
    await saveBookmark(session);
    return { body: { ok: true, id, status: body.status } };
  }
  // return status: PATCH /api/returns/:id
  if (method === "PATCH" && /^\/api\/returns\/\d+$/.test(path)) {
    const id = Number(path.split("/")[3]);
    const body = await request.json().catch(() => ({}));
    const status = String(body.status || "requested");
    await write(session, "UPDATE returns SET status=?, updated_at=? WHERE id=?",
      [status, new Date().toISOString(), id]);
    const row = await first(session, "SELECT waste_ids FROM returns WHERE id=?", [id]);
    let ids = []; try { ids = JSON.parse(row?.waste_ids || "[]"); } catch { ids = []; }
    if (ids.length) {
      const wstatus = status === "credited" ? "returned_to_vendor"
        : status === "rejected" ? "pending" : "return_requested";
      await write(session, `UPDATE waste SET status=? WHERE id IN (${ids.map(() => "?").join(",")})`,
        [wstatus, ...ids]);
    }
    await saveBookmark(session);
    return { body: { ok: true, id, status } };
  }
  // CSV reports: GET /api/reports/<name>.csv
  if (method === "GET" && /^\/api\/reports\/[a-z-]+\.csv$/.test(path)) {
    return { body: await csvReport(session, path), init: { headers: { "Content-Type": "text/csv; charset=utf-8" } } };
  }

  return { body: { detail: "not found" }, init: { status: 404 } };
}

/* ------------------------------------------------------------------ handlers */
async function reorderSuggestions(session) {
  const today = todayStr();
  const history = await all(session, `
    SELECT r.*, d.name AS drug, s.name AS supplier FROM reorders r
    JOIN drugs d ON d.id = r.drug_id LEFT JOIN suppliers s ON s.id = r.supplier_id
    ORDER BY r.id DESC`);
  const drugs = await all(session, `
    SELECT d.id, d.name, d.lead_time_days, d.reorder_point, d.supplier_id,
      (SELECT name FROM suppliers WHERE id = d.supplier_id) AS supplier,
      COALESCE((SELECT SUM(CASE WHEN b.expiry_date IS NULL OR b.expiry_date >= ?
                                THEN b.qty_remaining ELSE 0 END) FROM batches b WHERE b.drug_id = d.id), 0) AS usable
    FROM drugs d ORDER BY d.name`, [today]);
  const avgRows = await all(session, `
    SELECT drug_id, ROUND(AVG(q * 1.0), 2) AS avg_daily FROM (
      SELECT drug_id, date, SUM(qty) AS q FROM sales_daily
      WHERE date > date(?, '-90 day') GROUP BY drug_id, date
    ) GROUP BY drug_id`, [today]);
  const avgBy = new Map(avgRows.map((r) => [r.drug_id, r.avg_daily]));
  const suggestions = drugs.map((d) => {
    const avgDaily = avgBy.get(d.id) || 0;
    const lt = Number(d.lead_time_days || 7);
    const rop = Number(d.reorder_point || 0);
    const demand = Math.ceil(avgDaily * lt);
    const safety = Math.ceil(avgDaily * 3);
    const point = rop || demand + safety;
    const orderQty = Math.max(0, Math.ceil(point - d.usable + safety));
    const coverDays = avgDaily > 0 ? Math.round(d.usable / avgDaily) : 9999;
    return {
      drug_id: d.id, drug: d.name, supplier: d.supplier, supplier_id: d.supplier_id,
      available: d.usable, demand, safety, reorder_point: point,
      order_qty: orderQty, status: d.usable < point ? "order_now" : "scheduled",
      due_date: addDays(today, lt), cover_days: coverDays,
      avg_daily: avgDaily, lead_time_days: lt,
    };
  }).sort((a, b) => (a.status === "order_now" ? 0 : 1) - (b.status === "order_now" ? 0 : 1)
    || String(a.due_date).localeCompare(String(b.due_date)));
  return { history, suggestions };
}

async function uploadsList(session) {
  const rows = await all(session, "SELECT * FROM uploads ORDER BY id DESC LIMIT 50");
  return rows.map((r) => { let issues = {}; try { issues = JSON.parse(r.issues || "{}"); } catch {} return { ...r, issues }; });
}

async function dataQuality(session, limit = 100) {
  const rows = (await all(session, "SELECT * FROM quarantined ORDER BY id DESC LIMIT ?", [limit]))
    .map((r) => { let record = {}, issues = []; try { record = JSON.parse(r.record || "{}"); } catch {}
      try { issues = JSON.parse(r.issues || "[]"); } catch {} return { ...r, record, issues }; });
  const stats = await all(session, "SELECT kind, COUNT(*) AS n FROM quarantined GROUP BY kind");
  const issueCounts = {};
  for (const r of await all(session, "SELECT issues FROM quarantined LIMIT 2000")) {
    let list = []; try { list = JSON.parse(r.issues || "[]"); } catch {}
    for (const i of list) issueCounts[i] = (issueCounts[i] || 0) + 1;
  }
  const uploads = await all(session, `
    SELECT id, filename, kind, rows_total, rows_accepted, rows_rejected, created_at
    FROM uploads ORDER BY id DESC LIMIT 20`);
  return { rows, stats, issues: issueCounts, uploads };
}

async function listUsers(session) {
  return all(session, "SELECT id, username, role, name, created_at FROM users ORDER BY id");
}

async function createUser(session, body) {
  const username = String(body?.username || "").trim().toLowerCase();
  const role = body?.role === "admin" ? "admin" : "pharmacist";
  if (!username || !body?.password) return { detail: "username and password required" };
  if (await first(session, "SELECT id FROM users WHERE username = ?", [username])) {
    return { detail: "username already exists" };
  }
  await write(session,
    "INSERT INTO users(username, password_hash, role, name, created_at) VALUES(?,?,?,?,datetime('now'))",
    [username, await makeHash(body.password), role, body.name || username]);
  await saveBookmark(session);
  return { ok: true, username, role };
}

async function createReorder(session, body) {
  const drugId = Number(body?.drug_id);
  const drug = await first(session, "SELECT * FROM drugs WHERE id = ?", [drugId]);
  if (!drug) return { detail: "drug not found" };
  const today = todayStr();
  const usable = await scalar(session, `
    SELECT COALESCE(SUM(CASE WHEN expiry_date IS NULL OR expiry_date >= ? THEN qty_remaining ELSE 0 END),0)
    FROM batches WHERE drug_id = ?`, [today, drugId], 0);
  const avgDaily = (await first(session, `
    SELECT ROUND(AVG(q*1.0),2) AS a FROM (
      SELECT date, SUM(qty) AS q FROM sales_daily WHERE drug_id=? AND date > date(?, '-90 day') GROUP BY date
    )`, [drugId, today]))?.a || 0;
  const point = Number(drug.reorder_point || 0) || Math.ceil(avgDaily * (drug.lead_time_days || 7)) + Math.ceil(avgDaily * 3);
  const qty = Number(body?.qty) || Math.max(0, Math.ceil(point - usable + avgDaily * 3));
  const r = await write(session,
    "INSERT INTO reorders(drug_id, supplier_id, qty, due_date, reason, status, source, notes, created_at, updated_at) " +
    "VALUES(?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
    [drugId, drug.supplier_id, qty, addDays(today, Number(drug.lead_time_days || 7)),
      `AI suggestion (${usable < point ? "order_now" : "scheduled"})`, "ordered", "manual", body?.notes || null]);
  let notification = null;
  const supplier = drug.supplier_id
    ? await first(session, "SELECT * FROM suppliers WHERE id = ?", [drug.supplier_id]) : null;
  if (supplier) {
    const subject = `Purchase request — ${drug.name} (${qty} units)`;
    const text = `Dear ${supplier.name},\n\nPlease share availability and pricing for ${qty} units of ${drug.name}.\n\nRegards,\nHealthFirst Pharmacy`;
    const n = await write(session,
      "INSERT INTO notifications(supplier_id, subject, body, status, related_type, related_id, created_at) VALUES(?,?,?,?,?,?,datetime('now'))",
      [supplier.id, subject, text, "draft", "reorder", r.result?.meta?.last_row_id]);
    notification = { id: n.result?.meta?.last_row_id, subject, status: "draft" };
  }
  await saveBookmark(session);
  return { id: r.result?.meta?.last_row_id, qty, notification };
}

async function addWaste(session, body) {
  const drugId = Number(body?.drug_id);
  const qty = Number(body?.qty);
  const reason = ["damaged", "recalled", "theft", "other"].includes(body?.reason) ? body.reason : "damaged";
  if (!drugId || !qty || qty <= 0) return { detail: "drug_id and positive qty required" };
  const today = todayStr();
  let batch = null;
  if (body?.batch_no) {
    batch = await first(session, "SELECT * FROM batches WHERE drug_id = ? AND batch_no = ?", [drugId, body.batch_no]);
  } else {
    batch = await first(session, `
      SELECT * FROM batches WHERE drug_id = ? AND qty_remaining >= ?
      AND (expiry_date IS NULL OR expiry_date >= ?) ORDER BY expiry_date IS NULL, expiry_date`,
      [drugId, qty, today]);
    if (!batch) batch = await first(session,
      "SELECT * FROM batches WHERE drug_id = ? AND qty_remaining >= ? ORDER BY expiry_date IS NULL, expiry_date",
      [drugId, qty]);
  }
  if (!batch || batch.qty_remaining < qty) return { detail: "not enough stock in the batch" };
  const value = Math.round(qty * (batch.unit_cost || 0) * 100) / 100;
  await write(session, "UPDATE batches SET qty_remaining = qty_remaining - ? WHERE id = ?", [qty, batch.id]);
  const w = await write(session,
    "INSERT INTO waste(drug_id, batch_id, supplier_id, qty, reason, unit_cost, value, status, note, created_at) " +
    "VALUES(?,?,?,?,?,?,?,?,?,datetime('now'))",
    [drugId, batch.id, batch.supplier_id, qty, reason, batch.unit_cost, value, "pending", body?.note || null]);
  await saveBookmark(session);
  await refreshAlerts(session);
  return { ok: true, qty, batch: batch.batch_no, value, id: w.result?.meta?.last_row_id };
}

async function createReturn(session, body) {
  const supplierId = Number(body?.supplier_id);
  if (!supplierId) return { detail: "supplier_id required" };
  let ids = Array.isArray(body?.waste_ids) ? body.waste_ids.map(Number).filter(Boolean) : [];
  let lots = [];
  if (ids.length) {
    const marks = ids.map(() => "?").join(",");
    lots = await all(session, `
      SELECT w.*, b.batch_no FROM waste w LEFT JOIN batches b ON b.id = w.batch_id
      WHERE w.supplier_id = ? AND w.id IN (${marks}) AND w.status = 'pending'`, [supplierId, ...ids]);
  } else {
    lots = await all(session, `
      SELECT w.*, b.batch_no FROM waste w LEFT JOIN batches b ON b.id = w.batch_id
      WHERE w.supplier_id = ? AND w.status = 'pending'`, [supplierId]);
  }
  if (!lots.length) return { detail: "no pending waste lots for this supplier" };
  const qty = lots.reduce((a, r) => a + r.qty, 0);
  const value = Math.round(lots.reduce((a, r) => a + r.value, 0) * 100) / 100;
  ids = lots.map((r) => r.id);
  const ref = `RTV-${today.replace(/-/g, "")}-${String(supplierId).padStart(2, "0")}`;
  const r = await write(session,
    "INSERT INTO returns(supplier_id, reference, qty, value, waste_ids, status, note, created_at) VALUES(?,?,?,?,?,?,?,datetime('now'))",
    [supplierId, ref, qty, value, JSON.stringify(ids), "requested", body?.note || null]);
  await write(session, `UPDATE waste SET status='return_requested' WHERE id IN (${ids.map(() => "?").join(",")})`, ids);
  const supplier = await first(session, "SELECT * FROM suppliers WHERE id = ?", [supplierId]);
  if (supplier) {
    const subject = `Return request ${ref} – ${qty} units`;
    const text = `Dear ${supplier.name},\n\nPlease accept our return request ${ref} covering ${lots.length} lot(s), ${qty} units (value ${value}).\n\nKindly confirm pick-up and credit note.\nHealthFirst Pharmacy`;
    await write(session,
      "INSERT INTO notifications(supplier_id, subject, body, status, related_type, related_id, created_at) VALUES(?,?,?,?,?,?,datetime('now'))",
      [supplierId, subject, text, "draft", "return", r.result?.meta?.last_row_id]);
  }
  await saveBookmark(session);
  return { id: r.result?.meta?.last_row_id, reference: ref, qty, value, lots: lots.length };
}

async function saveSettings(session, body) {
  const values = body?.values || {};
  for (const [k, v] of Object.entries(values)) {
    await write(session,
      "INSERT INTO settings(key, value, updated_at) VALUES(?,?,datetime('now')) " +
      "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
      [String(k), String(v ?? "")]);
  }
  await saveBookmark(session);
  return { ok: true, saved: Object.keys(values).length };
}

async function createNotification(session, body) {
  if (!body?.supplier_id || !body?.subject) return { detail: "supplier_id and subject required" };
  const id = await write(session,
    "INSERT INTO notifications(supplier_id, subject, body, status, created_at) VALUES(?,?,?,?,datetime('now'))",
    [Number(body.supplier_id), String(body.subject), String(body.body || ""), body.send ? "sent" : "draft"]);
  await saveBookmark(session);
  return { ok: true, id: id.result?.meta?.last_row_id, status: body.send ? "sent" : "draft" };
}

async function ackAllAlerts(session) {
  await write(session, "UPDATE alerts SET status='acknowledged', updated_at=? WHERE status='active'",
    [new Date().toISOString()]);
  await saveBookmark(session);
  const s = await first(session, "SELECT COUNT(*) AS active FROM alerts WHERE status='active'");
  return { ok: true, summary: { active: s?.active || 0 } };
}

/* Minimal grounded chat: stock / expiry / reorder questions answered from D1. */
const CHAT_FILLER = new Set(["stock", "of", "how", "many", "left", "do", "we", "i", "is", "are", "in", "the", "a", "an",
  "available", "units", "unit", "qty", "much", "what", "whats", "show", "me", "check", "get", "about", "tell", "for",
  "please", "current", "currently", "now", "there", "any", "on", "hand", "have", "has", "should", "to", "next", "days",
  "day", "forecast", "reorder", "reorders", "sales", "waste", "report", "create", "draft"]);

async function chatRespond(session, message) {
  const text = String(message || "").trim();
  if (!text) return { detail: "message required" };
  const low = text.toLowerCase();
  const today = todayStr();
  const q = low.replace(/[^a-z0-9\s]/g, " ").split(/\s+/).filter((w) => w && !CHAT_FILLER.has(w)).join(" ");
  const drug = (q && await first(session, "SELECT * FROM drugs WHERE norm_name = ?", [q]))
    || (q && await first(session,
      "SELECT * FROM drugs WHERE LOWER(name) LIKE ? OR LOWER(COALESCE(generic,'')) LIKE ? ORDER BY id LIMIT 1",
      [`%${q}%`, `%${q}%`])) || null;
  if (drug && /(stock|left|have|how many|available)/.test(low)) {
    const usable = await scalar(session, `
      SELECT COALESCE(SUM(CASE WHEN expiry_date IS NULL OR expiry_date >= ? THEN qty_remaining ELSE 0 END),0)
      FROM batches WHERE drug_id = ?`, [today, drug.id], 0);
    return { text: `**${drug.name}**: ${usable} usable units in stock (expired batches excluded).`, cards: [] };
  }
  if (/return/.test(low)) {
    const rows = await listReturns(session);
    const pending = rows.filter((r) => r.status === "requested").length;
    const value = rows.reduce((a, r) => a + (r.value || 0), 0);
    return { text: rows.length
      ? `${rows.length} vendor return(s) filed, ${pending} awaiting pick-up — total value ₹${value.toLocaleString("en-IN")}.`
      : "No vendor returns filed yet — expired lots can be returned from the Waste & Returns view.", cards: [] };
  }
  if (/waste|write-?off/.test(low)) {
    const w = await wasteAll(session);
    const s = w.summary || {};
    return { text: `Waste to date: ₹${(s.total_value || 0).toLocaleString("en-IN")} across ${s.total_qty || 0} units in ${s.total_lots || 0} lots.` +
      (w.items?.length ? " Biggest lots below." : ""),
      cards: w.items?.length ? [{ type: "table", title: "Waste lots", columns: ["Medicine", "Qty", "Reason", "Value"],
        rows: w.items.slice(0, 8).map((r) => [r.drug, r.qty, r.reason, r.value]) }] : [] };
  }
  if (/expir/.test(low)) {
    const soon = await all(session, `
      SELECT d.name AS drug, COUNT(*) AS lots, SUM(b.qty_remaining) AS qty, MIN(b.expiry_date) AS next
      FROM batches b JOIN drugs d ON d.id = b.drug_id
      WHERE b.qty_remaining > 0 AND b.expiry_date IS NOT NULL AND b.expiry_date <= date(?, '+90 day')
      GROUP BY b.drug_id ORDER BY next LIMIT 8`, [today]);
    return { text: soon.length ? "Expiring within 90 days:" : "Nothing expires in the next 90 days.",
      cards: soon.length ? [{ type: "table", title: "Expiring ≤ 90 days",
        columns: ["Medicine", "Lots", "Units", "Next expiry"],
        rows: soon.map((r) => [r.drug, r.lots, r.qty, r.next]) }] : [] };
  }
  if (/reorder|order now|purchase/.test(low)) {
    const { suggestions } = await reorderSuggestions(session);
    const need = suggestions.filter((s) => s.status === "order_now").slice(0, 8);
    return { text: need.length ? "These are below their reorder point:" : "Nothing needs reordering right now.",
      cards: need.length ? [{ type: "table", title: "Order now",
        columns: ["Medicine", "On hand", "ROP", "Order qty"],
        rows: need.map((s) => [s.drug, s.available, s.reorder_point, s.order_qty]) }] : [] };
  }
  if (drug && /forecast|runway|cover/.test(low)) {
    const usable = await scalar(session, `
      SELECT COALESCE(SUM(CASE WHEN expiry_date IS NULL OR expiry_date >= ? THEN qty_remaining ELSE 0 END),0)
      FROM batches WHERE drug_id = ?`, [today, drug.id], 0);
    const avgDaily = (await first(session, `
      SELECT ROUND(AVG(q*1.0),2) AS a FROM (
        SELECT date, SUM(qty) AS q FROM sales_daily WHERE drug_id=? AND date > date(?, '-90 day') GROUP BY date
      )`, [drug.id, today]))?.a || 0;
    return { text: `**${drug.name}**: ${usable} units on hand` +
      (avgDaily ? ` at ~${avgDaily}/day → about ${Math.round(usable / avgDaily)} days of cover.` : " (no recent sales to forecast from)."), cards: [] };
  }
  if (/sales|sold|revenue/.test(low)) {
    const rows = await all(session, `
      SELECT d.name AS drug, SUM(s.qty) AS units, ROUND(SUM(s.revenue),0) AS revenue
      FROM sales_daily s JOIN drugs d ON d.id = s.drug_id
      WHERE s.date > date(?, '-30 day') GROUP BY s.drug_id ORDER BY revenue DESC LIMIT 8`, [today]);
    const tot = rows.reduce((a, r) => a + (r.revenue || 0), 0);
    return { text: rows.length ? `Last 30 days: ₹${tot.toLocaleString("en-IN")} revenue.` : "No sales recorded in the last 30 days.",
      cards: rows.length ? [{ type: "table", title: "Top sellers (30d)", columns: ["Medicine", "Units", "Revenue"],
        rows: rows.map((r) => [r.drug, r.units, r.revenue]) }] : [] };
  }
  const o = await overview(session);
  return { text: `**HealthFirst Pharmacy** — ${o.usable_qty} usable units across ${o.sku_count} SKUs; ` +
    `${o.low_stock} SKU(s) low, ${o.expired_qty} expired units. Ask me about a medicine's stock, ` +
    `expiry queues or reorders.`, cards: [] };
}

async function csvReport(session, path) {
  const name = path.split("/")[3] || "";
  const today = todayStr();
  let rows = [];
  if (name.startsWith("inventory")) {
    rows = await all(session, `
      SELECT d.name AS medicine, d.category, d.generic,
        COALESCE(SUM(CASE WHEN b.expiry_date IS NULL OR b.expiry_date >= ? THEN b.qty_remaining ELSE 0 END),0) AS usable,
        COALESCE(SUM(CASE WHEN b.expiry_date < ? THEN b.qty_remaining ELSE 0 END),0) AS expired,
        ROUND(COALESCE(SUM(CASE WHEN b.expiry_date IS NULL OR b.expiry_date >= ? THEN b.qty_remaining*b.unit_cost ELSE 0 END),0),2) AS usable_value
      FROM drugs d LEFT JOIN batches b ON b.drug_id = d.id
      GROUP BY d.id ORDER BY d.name`, [today, today, today]);
  } else if (name.startsWith("waste")) {
    rows = await all(session, `
      SELECT w.id, d.name AS drug, b.batch_no, w.qty, w.reason, w.value, w.status, w.created_at
      FROM waste w JOIN drugs d ON d.id = w.drug_id LEFT JOIN batches b ON b.id = w.batch_id
      ORDER BY w.id DESC`);
  } else if (name.startsWith("expiry")) {
    rows = await all(session, `
      SELECT d.name AS drug, b.batch_no, b.expiry_date, b.qty_remaining, b.unit_cost,
        CAST(julianday(b.expiry_date) - julianday(?) AS INTEGER) AS days_left
      FROM batches b JOIN drugs d ON d.id = b.drug_id
      WHERE b.qty_remaining > 0 AND b.expiry_date IS NOT NULL
      ORDER BY b.expiry_date`, [today]);
  } else {
    rows = [{ info: "no data" }];
  }
  if (!rows.length) rows = [{ info: "no data" }];
  const cols = Object.keys(rows[0]);
  const esc = (v) => { const s = v == null ? "" : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  return [cols.join(","), ...rows.map((r) => cols.map((c) => esc(r[c])).join(","))].join("\n");
}

async function meta(session) {
  const settings = Object.fromEntries(
    (await all(session, "SELECT key, value FROM settings")).map((r) => [r.key, r.value])
  );
  return {
    pharmacy: settings.pharmacy_name || "HealthFirst Pharmacy",
    as_of: new Date().toISOString().slice(0, 10),
    settings,
    dataset: { name: "Pharmacy Inventory — current dataset", provider: "bundled" },
    databases: { primary: "pharmacy-primary", replica: "pharmacy-replica (read)", sync: "D1 read replication" },
    archive: { bucket: "pharmacy-archive", schedule: "monthly (5 0 1 * *)" },
    source_counts: {
      sales: await scalar(session, "SELECT COUNT(*) FROM sales"),
      purchases: await scalar(session, "SELECT COUNT(*) FROM purchases"),
      batches: await scalar(session, "SELECT COUNT(*) FROM batches"),
    },
  };
}

async function listDrugs(session) {
  const today = todayStr();
  const drugs = await all(session, `
    SELECT d.*, sh.code AS shelf_code,
      COALESCE(SUM(CASE WHEN b.expiry_date IS NULL OR b.expiry_date >= ?
                        THEN b.qty_remaining ELSE 0 END), 0) AS usable,
      COALESCE(SUM(CASE WHEN b.expiry_date < ?
                        THEN b.qty_remaining ELSE 0 END), 0) AS expired,
      ROUND(COALESCE(SUM(CASE WHEN b.expiry_date IS NULL OR b.expiry_date >= ?
                             THEN b.qty_remaining * b.unit_cost ELSE 0 END), 0), 2) AS usable_value,
      ROUND(COALESCE(SUM(CASE WHEN b.expiry_date < ?
                             THEN b.qty_remaining * b.unit_cost ELSE 0 END), 0), 2) AS expired_value,
      COUNT(b.id) AS batches
    FROM drugs d
    LEFT JOIN batches b ON b.drug_id = d.id
    LEFT JOIN shelves sh ON sh.id = b.shelf_id
    GROUP BY d.id ORDER BY d.category, d.name`, [today, today, today, today]);
  const avgRows = await all(session, `
    SELECT drug_id, ROUND(AVG(q * 1.0), 2) AS avg_daily FROM (
      SELECT drug_id, date, SUM(qty) AS q FROM sales_daily
      WHERE date > date(?, '-90 day') GROUP BY drug_id, date
    ) GROUP BY drug_id`, [today]);
  const avgBy = new Map(avgRows.map((r) => [r.drug_id, r.avg_daily]));
  return drugs.map((d) => ({ ...d, plan: computePlan(d, avgBy.get(d.id) || 0, today) }));
}

async function updateDrugPolicy(session, id, body) {
  const drug = await first(session, "SELECT * FROM drugs WHERE id = ?", [id]);
  if (!drug) return { detail: "drug not found" };
  if (body?.lead_time_days != null) {
    await write(session, "UPDATE drugs SET lead_time_days = ? WHERE id = ?",
      [Math.max(1, Number(body.lead_time_days)), id]);
  }
  if (body?.reorder_point !== undefined) {
    await write(session, "UPDATE drugs SET reorder_point = ? WHERE id = ?",
      [body.reorder_point === null || body.reorder_point === "" ? null : Number(body.reorder_point), id]);
  }
  if (body?.supplier_id != null) {
    await write(session, "UPDATE drugs SET supplier_id = ? WHERE id = ?", [Number(body.supplier_id), id]);
  }
  await saveBookmark(session);
  await refreshAlerts(session);
  const rows = await listDrugs(session);
  return { ok: true, plan: rows.find((r) => r.id === id)?.plan || null };
}

function uploadTemplate() {
  const today = todayStr();
  const exp = addDays(today, 540);
  return [
    "Transaction_ID,Date,Drug_Name,Batch_Number,Qty_Sold,MRP_Unit_Price,Total_Amount",
    `TXN-90001,${today},Dolo 650,DOL-2601-45,4,30.0,120.0`,
    `TXN-90002,${today},Pan 40,PAN-2601-21,2,150.0,300.0`,
    "",
    "Purchase_ID,Date_Received,Drug_Name,Supplier_Name,Batch_Number,Qty_Received,Unit_Cost_Price,Total_Purchase_Cost,Expiry_Date",
    `PO-9001,${today},Dolo 650,Apollo Supply Chain,DOL-2601-11,500,23.0,11500.0,${exp}`,
  ].join("\n");
}

async function buildReorderDrafts(session) {
  const { suggestions } = await reorderSuggestions(session);
  const need = suggestions.filter((s) => s.status === "order_now" && s.supplier_id);
  let created = 0;
  for (const s of need) {
    const dup = await first(session,
      "SELECT id FROM notifications WHERE related_type = 'reorder_draft' AND related_id = ? AND supplier_id = ?",
      [s.drug_id, s.supplier_id]);
    if (dup) continue;
    await write(session,
      "INSERT INTO notifications(supplier_id, subject, body, status, related_type, related_id, created_at) VALUES(?,?,?,?,?,?,datetime('now'))",
      [s.supplier_id, `Purchase request — ${s.drug} (${s.order_qty} units)`,
        `Dear supplier,\n\nPlease share availability and pricing for ${s.order_qty} units of ${s.drug} (stock below reorder point).\n\nRegards,\nHealthFirst Pharmacy`,
        "draft", "reorder_draft", s.drug_id]);
    created++;
  }
  await saveBookmark(session);
  return { created };
}

async function listBatches(session, url) {
  const drugId = url.searchParams.get("drug_id");
  const status = url.searchParams.get("status");
  const includeEmpty = url.searchParams.get("include_empty") === "true";
  const params = [];
  let sql = `
    SELECT b.*, d.name AS drug, d.category, s.name AS supplier,
           sh.code AS shelf_code, sh.zone AS shelf_zone
    FROM batches b JOIN drugs d ON d.id = b.drug_id
    LEFT JOIN suppliers s ON s.id = b.supplier_id
    LEFT JOIN shelves sh ON sh.id = b.shelf_id
    WHERE 1=1`;
  if (drugId) { sql += " AND b.drug_id = ?"; params.push(Number(drugId)); }
  if (!includeEmpty) sql += " AND b.qty_remaining > 0";
  sql += " ORDER BY d.name, b.expiry_date IS NULL, b.expiry_date LIMIT 500";
  const rows = await all(session, sql, params);
  const crit = Number((await first(session, "SELECT value FROM settings WHERE key = 'expiry_critical_days'"))?.value) || 30;
  const warn = Number((await first(session, "SELECT value FROM settings WHERE key = 'expiry_warning_days'"))?.value) || 90;
  const today = todayStr();
  const rank = new Map();
  let out = rows.map((b) => {
    const days = b.expiry_date ? Math.floor((Date.parse(b.expiry_date + "T00:00:00Z") - Date.parse(today + "T00:00:00Z")) / 86400000) : null;
    const st = days === null ? "ok" : days < 0 ? "expired" : days <= crit ? "critical" : days <= warn ? "warning" : "ok";
    rank.set(b.drug_id, (rank.get(b.drug_id) || 0) + 1);
    return { ...b, fefo_rank: rank.get(b.drug_id), status: st, days_to_expiry: days,
      value: Math.round((b.qty_remaining || 0) * (b.unit_cost || 0) * 100) / 100 };
  });
  if (status) out = out.filter((b) => b.status === status);
  return out;
}

async function shelfTasks(session, url) {
  const status = url.searchParams.get("status") || "pending";
  const items = await all(session, `
    SELECT t.*, d.name AS drug, b.batch_no, fs.code AS from_code, ts.code AS to_code
    FROM shelf_tasks t
    LEFT JOIN drugs d ON d.id = t.drug_id
    LEFT JOIN batches b ON b.id = t.batch_id
    LEFT JOIN shelves fs ON fs.id = t.from_shelf_id
    LEFT JOIN shelves ts ON ts.id = t.to_shelf_id
    WHERE t.status = ? ORDER BY t.created_at DESC, t.id DESC LIMIT 200`, [status]);
  const countsRows = await all(session,
    "SELECT kind, COUNT(*) AS n FROM shelf_tasks WHERE status = 'pending' GROUP BY kind");
  const byKind = Object.fromEntries(countsRows.map((r) => [r.kind, r.n]));
  return { items, counts: { putaway: byKind.putaway || 0, shift: byKind.shift || 0,
    pending: (byKind.putaway || 0) + (byKind.shift || 0) } };
}

async function refreshShelfTasks(session) {
  const today = new Date().toISOString().slice(0, 10);

  // nearest-expiry usable batch of every medicine -> its pick shelf
  const rows = await all(session, `
    SELECT b.id AS batch_id, b.drug_id, b.batch_no, b.expiry_date, b.shelf_id, d.name AS drug
    FROM batches b JOIN drugs d ON d.id = b.drug_id
    WHERE b.qty_remaining > 0 AND (b.expiry_date IS NULL OR b.expiry_date >= ?)`, [today]);
  const first = new Map();
  for (const r of rows) {
    const cur = first.get(r.drug_id);
    if (!cur || (r.expiry_date || "9999") < (cur.expiry_date || "9999")) first.set(r.drug_id, r);
  }
  let created = 0;
  for (const r of first.values()) {
    const pick = await first(session,
      "SELECT id FROM shelves WHERE code = 'P' || ((? - 1) % 3 + 1)", [r.drug_id]);
    if (pick && r.shelf_id !== pick.id) {
      await write(session, `
        INSERT INTO shelf_tasks(kind, drug_id, batch_id, from_shelf_id, to_shelf_id, reason, status, created_at)
        SELECT 'shift', ?, ?, ?, ?, ?, 'pending', ?
        WHERE NOT EXISTS (SELECT 1 FROM shelf_tasks WHERE batch_id = ? AND to_shelf_id = ? AND status = 'pending')`,
        [r.drug_id, r.batch_id, r.shelf_id, pick.id,
          `${r.drug}: batch ${r.batch_no} is nearest to expiry — shift to pick shelf`, today,
          r.batch_id, pick.id]);
      created++;
    }
  }

  // expired stock -> quarantine
  const expired = await all(session, `
    SELECT b.drug_id, d.name AS drug, COUNT(*) AS lots
    FROM batches b JOIN drugs d ON d.id = b.drug_id
    WHERE b.qty_remaining > 0 AND b.expiry_date < ? GROUP BY b.drug_id`, [today]);
  const q = await first(session, "SELECT id FROM shelves WHERE code = 'Q1'");
  for (const e of expired) {
    await write(session, `
      INSERT INTO shelf_tasks(kind, drug_id, batch_id, from_shelf_id, to_shelf_id, reason, status, created_at)
      SELECT 'shift', ?, NULL, NULL, ?, ?, 'pending', ?
      WHERE NOT EXISTS (SELECT 1 FROM shelf_tasks WHERE drug_id = ? AND batch_id IS NULL AND to_shelf_id = ? AND status = 'pending')`,
      [e.drug_id, q.id, `${e.lots} expired batch lot(s) of ${e.drug} — shift to quarantine shelf Q1`, today,
        e.drug_id, q.id]);
    created++;
  }

  const counts = await first(session, `
    SELECT SUM(CASE WHEN kind='putaway' THEN 1 ELSE 0 END) AS putaway,
           SUM(CASE WHEN kind='shift' THEN 1 ELSE 0 END) AS shift
    FROM shelf_tasks WHERE status='pending'`);
  return { created, counts: { putaway: counts?.putaway || 0, shift: counts?.shift || 0,
    pending: (counts?.putaway || 0) + (counts?.shift || 0) } };
}

async function completeShelfTask(session, body) {
  const id = Number(body?.id);
  if (!id) return { detail: "task id required" };
  const task = await first(session, "SELECT * FROM shelf_tasks WHERE id = ?", [id]);
  if (!task) return { detail: "task not found" };
  await write(session, "UPDATE shelf_tasks SET status = 'done', done_at = datetime('now') WHERE id = ?", [id]);
  if (task.to_shelf_id) {
    if (task.batch_id) {
      await write(session, "UPDATE batches SET shelf_id = ? WHERE id = ?", [task.to_shelf_id, task.batch_id]);
    } else if (task.kind === "shift" && task.drug_id) {
      await write(session, `
        UPDATE batches SET shelf_id = ? WHERE drug_id = ? AND qty_remaining > 0
        AND expiry_date IS NOT NULL AND expiry_date < date('now')`,
        [task.to_shelf_id, task.drug_id]);
    }
  }
  await saveBookmark(session);
  return { ok: true, task: await first(session, "SELECT * FROM shelf_tasks WHERE id = ?", [id]) };
}

async function counterLookup(session, url) {
  const name = url.searchParams.get("name") || "";
  const qty = Math.max(1, Number(url.searchParams.get("qty") || 1));
  const norm = name.toLowerCase().replace(/[-_/.,]/g, " ").replace(/\s+/g, " ").trim();
  const drug = (await first(session, "SELECT * FROM drugs WHERE norm_name = ?", [norm]))
    || (await first(session,
      "SELECT * FROM drugs WHERE LOWER(COALESCE(generic,'')) = ? ORDER BY id LIMIT 1", [norm]));
  if (!drug) return { detail: `medicine not found in catalogue: ${name}` };

  const pick = await first(session, `
    SELECT b.id AS batch_id, b.batch_no, b.expiry_date,
           CAST(julianday(b.expiry_date) - julianday('now') AS INTEGER) AS days_to_expiry,
           b.qty_remaining, sh.code AS shelf_code, sh.zone AS shelf_zone
    FROM batches b LEFT JOIN shelves sh ON sh.id = b.shelf_id
    WHERE b.drug_id = ? AND b.qty_remaining > 0
      AND (b.expiry_date IS NULL OR b.expiry_date >= date('now'))
    ORDER BY b.expiry_date IS NULL, b.expiry_date LIMIT 1`, [drug.id]);
  const stock = await first(session, `
    SELECT COALESCE(SUM(CASE WHEN expiry_date IS NULL OR expiry_date >= date('now')
                             THEN qty_remaining ELSE 0 END), 0) AS usable,
           COALESCE(SUM(CASE WHEN expiry_date < date('now') THEN qty_remaining ELSE 0 END), 0) AS expired
    FROM batches WHERE drug_id = ?`, [drug.id]);

  return {
    drug_id: drug.id, drug: drug.name, generic: drug.generic, category: drug.category,
    usable: stock.usable, expired: stock.expired, requested_qty: qty,
    pick, enough: !!pick && pick.qty_remaining >= qty && stock.usable >= qty,
  };
}

async function issuePrescription(session, body) {
  const qty = Number(body?.qty);
  if (!qty || qty <= 0) return { detail: "qty must be positive" };

  let drug = null;
  if (body.drug_id) {
    drug = await first(session, "SELECT * FROM drugs WHERE id = ?", [Number(body.drug_id)]);
  } else if (body.name) {
    const norm = String(body.name).toLowerCase().replace(/[-_/.,]/g, " ").replace(/\s+/g, " ").trim();
    drug = (await first(session, "SELECT * FROM drugs WHERE norm_name = ?", [norm]))
        || (await first(session,
          "SELECT * FROM drugs WHERE LOWER(COALESCE(generic,'')) = ? ORDER BY id LIMIT 1", [norm]));
  }
  if (!drug) return { detail: "medicine not found in catalogue" };

  const before = await first(session, `
    SELECT COALESCE(SUM(CASE WHEN expiry_date IS NULL OR expiry_date >= date('now')
                             THEN qty_remaining ELSE 0 END), 0) AS usable
    FROM batches WHERE drug_id = ?`, [drug.id]);

  // FEFO on the primary (single-writer keeps the batch arithmetic race-free)
  const candidates = await all(session, `
    SELECT * FROM batches WHERE drug_id = ? AND qty_remaining > 0
    AND (expiry_date IS NULL OR expiry_date >= date('now'))
    ORDER BY expiry_date IS NULL, expiry_date`, [drug.id]);
  const blockedRow = await first(session, `
    SELECT COALESCE(SUM(qty_remaining), 0) AS n FROM batches
    WHERE drug_id = ? AND qty_remaining > 0 AND expiry_date < date('now')`, [drug.id]);

  let remaining = qty;
  const taken = [];
  for (const b of candidates) {
    if (remaining <= 0) break;
    const take = Math.min(remaining, b.qty_remaining);
    await write(session, "UPDATE batches SET qty_remaining = qty_remaining - ? WHERE id = ?",
      [take, b.id]);
    taken.push({ batch_no: b.batch_no, expiry: b.expiry_date, qty: take,
      shelf: await scalar(session, "SELECT code FROM shelves WHERE id = ?", [b.shelf_id], null) });
    remaining -= take;
  }
  await write(session,
    "INSERT INTO sales(txn_id, date, drug_id, qty, unit_price, total, source, created_at) VALUES(?,?,?,?,?,?,?,?)",
    [`rx-${crypto.randomUUID()}`, new Date().toISOString().slice(0, 10), drug.id, qty - remaining,
      drug.mrp || 0, (drug.mrp || 0) * (qty - remaining), "prescription", new Date().toISOString()]);

  const after = await first(session, `
    SELECT COALESCE(SUM(CASE WHEN expiry_date IS NULL OR expiry_date >= date('now')
                             THEN qty_remaining ELSE 0 END), 0) AS usable
    FROM batches WHERE drug_id = ?`, [drug.id]);
  await saveBookmark(session);
  await refreshAlerts(session);

  return {
    drug_id: drug.id, drug: drug.name, requested: qty, dispensed: qty - remaining,
    batches: taken, shortfall: remaining, blocked_expired: blockedRow?.n || 0,
    usable_before: before.usable, usable_after: after.usable,
    at: new Date().toISOString(),
  };
}

async function listAlerts(session) {
  const items = await all(session, `
    SELECT a.*, d.name AS drug FROM alerts a LEFT JOIN drugs d ON d.id = a.drug_id
    WHERE a.status != 'resolved'
    ORDER BY CASE a.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
      a.updated_at DESC LIMIT 200`);
  const s = await first(session, `
    SELECT COUNT(*) AS active,
           SUM(CASE WHEN severity='critical' THEN 1 ELSE 0 END) AS critical,
           SUM(CASE WHEN severity='high' THEN 1 ELSE 0 END) AS high,
           SUM(CASE WHEN severity='medium' THEN 1 ELSE 0 END) AS medium,
           SUM(CASE WHEN status='acknowledged' THEN 1 ELSE 0 END) AS acknowledged
    FROM alerts WHERE status != 'resolved'`);
  return {
    items: items.map((a) => { let details = {}; try { details = JSON.parse(a.details || "{}"); } catch { details = {}; } return { ...a, details }; }),
    summary: { active: s?.active || 0, critical: s?.critical || 0, high: s?.high || 0,
      medium: s?.medium || 0, acknowledged: s?.acknowledged || 0 },
  };
}

async function syncStatus(session) {
  const b = await first(session,
    "SELECT bookmark, updated_at FROM sync_bookmarks WHERE name = 'latest'");
  return {
    primary: "pharmacy-primary", replica: "read replica (auto)",
    replication: "D1 read replication — continuous, real time",
    last_bookmark: b?.bookmark || null, bookmark_saved_at: b?.updated_at || null,
    session_bookmark: session.getBookmark(),
  };
}

async function runArchiveNow(env, session) {
  const result = await handleArchive(env);
  await saveBookmark(session);
  return result;
}
