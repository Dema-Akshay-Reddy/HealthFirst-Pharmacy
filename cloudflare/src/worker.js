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
  makeSession, jsonResponse, write, writeMany, all, first, scalar, saveBookmark, sessionWrote,
} from "./db.js";
import {
  syncStatus as replicationStatus, drainReplication, reconcile as reconcileReplication,
} from "./replication.js";
import { handleArchive } from "./archive.js";
import {
  overview, charts, activity, suppliersList, expiry, wasteAll, listReturns,
} from "./analytics.js";
import { ensureUsers, login, currentUser, roleAllows, hashPassword, resolveUserId } from "./auth.js";
import { normNameMayExist, catalogBloomStats } from "./bloom.js";
import {
  ChatError, listConversations, createConversation, listMessages,
  renameConversation, archiveConversation, deleteConversation, clearHistory,
  recentHistory, postMessage,
} from "./chat.js";
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
        const response = withErrorStatus(await routeApi(request, env, session, user, path, url));
        // low-latency sync: this request's writes were captured in the outbox
        // transactionally — drain them to the secondary without blocking the
        // response (the minute cron is the safety net if this isolate dies)
        if (sessionWrote(session)) {
          ctx?.waitUntil?.(drainReplication(env).catch((err) =>
            console.error("[replication] post-write drain failed:", err?.message || err)));
        }
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
        const rid = crypto.randomUUID().slice(0, 8);
        // full error server-side only; clients get a correlation id, never internals
        console.error(`[api ${rid}] ${request.method} ${path}:`, err?.stack || err);
        const DEBUG_API = (env?.ENVIRONMENT || "") !== "production" &&
          new URL(request.url).searchParams.has("debug");
        return jsonResponse(session,
          { detail: DEBUG_API ? `internal error: ${err?.message || err}` : "internal error", request_id: rid },
          { status: 500 });
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

  /**
   * Cron (wrangler.jsonc → triggers.crons):
   *   `* * * * *`   → replication safety net (drains the outbox to DB_B even
   *                   if every post-write waitUntil drain was lost)
   *   `5 0 1 * *`   → monthly archive at 00:05 UTC on the 1st
   */
  async scheduled(controller, env, ctx) {
    const cron = controller?.cron || "";
    if (cron.includes("0 1 * *")) {
      try {
        const result = await handleArchive(env);
        console.log("[archive:scheduled]", JSON.stringify(result));
      } catch (err) {
        console.error("[archive:scheduled] FAILED", err && (err.stack || err.message || String(err)));
        throw err;
      }
      return;
    }
    try {
      const result = await drainReplication(env);
      if (result.applied || result.failed || result.pending) {
        console.log("[replication:cron]", JSON.stringify(result));
      }
    } catch (err) {
      // never throw: a replication hiccup must not look like a cron failure
      console.error("[replication:cron] FAILED", err && (err.stack || err.message || String(err)));
    }
  },
};

/* ------------------------------------------------------------------ routes */

/** Handlers signal HTTP errors with fail(status, detail). */
const fail = (status, detail) => ({ __error_status: status, detail });
function withErrorStatus(response) {
  // Errors arrive in two shapes and BOTH must normalise to
  // { body: { detail }, init: { status } }:
  //   routeApi `case` → return fail(status, detail)          (bare object)
  //   helper route    → return { body: fail(status, detail) } (wrapped)
  // Previously only the wrapped shape was recognised, so a direct
  // `return fail(...)` from a case reached Response.json(undefined) and
  // answered 200 "undefined" instead of e.g. 400 { detail }.
  const bare = response && typeof response === "object"
    && Number.isInteger(response.__error_status);
  const wrapped = !bare && response && typeof response === "object"
    && response.body && typeof response.body === "object"
    && Number.isInteger(response.body.__error_status);
  if (!bare && !wrapped) return response;
  const { __error_status, ...rest } = bare ? response : response.body;
  return { body: rest, init: { ...(response.init || {}), status: __error_status } };
}

async function routeApi(request, env, session, user, path, url) {
  const method = request.method;

  // dynamic chat routes: /api/chat/conversations/{id}[/messages]
  const convMatch = path.match(/^\/api\/chat\/conversations\/(\d+)(\/messages)?$/);
  if (convMatch) {
    if (!user) return { body: { detail: "not logged in" }, init: { status: 401 } };
    const uid = await resolveUserId(session, user);
    if (!uid) return { body: { detail: "account not provisioned" }, init: { status: 403 } };
    const id = Number(convMatch[1]);
    try {
      if (convMatch[2] && method === "GET") {
        return { body: await listMessages(session, uid, id, {
          limit: url.searchParams.get("limit"), before: url.searchParams.get("before"),
        }) };
      }
      if (!convMatch[2] && method === "PATCH") {
        const body = await request.json().catch(() => ({}));
        if (typeof body.title === "string") return { body: await renameConversation(session, uid, id, body.title) };
        if (body.archived) return { body: await archiveConversation(session, uid, id) };
        return fail(400, "title or archived required");
      }
      if (!convMatch[2] && method === "DELETE") {
        return { body: await deleteConversation(session, uid, id) };
      }
      return fail(405, "method not allowed");
    } catch (e) {
      if (e instanceof ChatError) return { body: { detail: e.detail }, init: { status: e.status } };
      throw e;
    }
  }

  switch (`${method} ${path}`) {
    case "POST /api/login": {
      const body = await request.json().catch(() => ({}));
      const result = await login(session, request, body.username, body.password);
      return { body: result.body, init: { status: result.status } };
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
      return { body: Array.isArray(body?.items) && body.items.length
        ? await issuePrescriptionMulti(session, body)
        : await issuePrescription(session, body) };
    }
    case "POST /api/dispense": {
      const body = await request.json().catch(() => ({}));
      return { body: await issuePrescription(session, body) };
    }
    case "GET /api/alerts":
      return { body: await listAlerts(session) };
    case "GET /api/sync/status":
      return { body: await replicationStatus(env) };
    case "POST /api/sync/run":
      return { body: await drainReplication(env) };
    case "POST /api/sync/reconcile": {
      const prune = url.searchParams.get("prune") === "1";
      const table = url.searchParams.get("table");
      return { body: await reconcileReplication(env, { prune, table }) };
    }
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
      return { body: await layaForecast(session) };
    case "GET /api/forecast/laya":
      return { body: await layaForecast(session) };
    case "GET /api/reorders":
      return { body: await reorderSuggestions(session) };
    case "GET /api/uploads":
      return { body: await uploadsList(session) };
    case "GET /api/data-quality":
      return { body: await dataQuality(session) };
    case "GET /api/chat/history": {
      // backward-compatible flat history — scoped to the session user only
      if (!user) return { body: { detail: "not logged in" }, init: { status: 401 } };
      const uid = await resolveUserId(session, user);
      if (!uid) return { body: { detail: "account not provisioned" }, init: { status: 403 } };
      return { body: await recentHistory(session, uid, url.searchParams.get("limit")) };
    }
    case "DELETE /api/chat/history": {
      if (!user) return { body: { detail: "not logged in" }, init: { status: 401 } };
      const uid = await resolveUserId(session, user);
      if (!uid) return { body: { detail: "account not provisioned" }, init: { status: 403 } };
      return { body: await clearHistory(session, uid) };
    }
    case "GET /api/chat/conversations": {
      if (!user) return { body: { detail: "not logged in" }, init: { status: 401 } };
      const uid = await resolveUserId(session, user);
      if (!uid) return { body: { detail: "account not provisioned" }, init: { status: 403 } };
      return { body: await listConversations(session, uid, {
        limit: url.searchParams.get("limit"), offset: url.searchParams.get("offset"),
      }) };
    }
    case "POST /api/chat/conversations": {
      if (!user) return { body: { detail: "not logged in" }, init: { status: 401 } };
      const uid = await resolveUserId(session, user);
      if (!uid) return { body: { detail: "account not provisioned" }, init: { status: 403 } };
      const body = await request.json().catch(() => ({}));
      const conv = await createConversation(session, uid, body.title);
      return conv
        ? { body: conv }
        : fail(500, "could not create conversation");
    }
    case "POST /api/chat": {
      if (!user) return { body: { detail: "not logged in" }, init: { status: 401 } };
      const uid = await resolveUserId(session, user);
      if (!uid) return { body: { detail: "account not provisioned" }, init: { status: 403 } };
      const body = await request.json().catch(() => ({}));
      try {
        return { body: await postMessage(session, uid, body, (msg) => chatRespond(session, msg)) };
      } catch (e) {
        if (e instanceof ChatError) return { body: { detail: e.detail }, init: { status: e.status } };
        throw e;
      }
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
    case "GET /api/invoices":
      return { body: await listBills(session, url) };
  }

  // season-based forecast: GET /api/forecast/seasonal/:drugId?days=90
  if (method === "GET" && /^\/api\/forecast\/seasonal\/\d+$/.test(path)) {
    const drugId = Number(path.split("/")[4]);
    const days = Math.min(365, Math.max(7, Number(url.searchParams.get("days") || 90)));
    return { body: await seasonalForecast(session, drugId, days) };
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
    // mirror the Python service: order_now when below ROP, scheduled only when a
    // reorder is actually projected inside the 90-day horizon, else healthy (no due date)
    const status = d.usable < point ? "order_now"
      : (avgDaily > 0 && coverDays <= 90 ? "scheduled" : "healthy");
    const due = status === "order_now" ? today
      : status === "scheduled" ? addDays(today, Math.max(0, Math.floor((d.usable - point) / avgDaily)))
      : null;
    return {
      drug_id: d.id, drug: d.name, supplier: d.supplier, supplier_id: d.supplier_id,
      available: d.usable, demand, safety, reorder_point: point,
      order_qty: orderQty, status, due_date: due, cover_days: coverDays,
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
  if (!username || !body?.password) return fail(400, "username and password required");
  if (String(body.password).length < 6) return fail(400, "password must be at least 6 characters");
  if (await first(session, "SELECT id FROM users WHERE username = ?", [username])) {
    return fail(409, "username already exists");
  }
  await write(session,
    "INSERT INTO users(username, password_hash, role, name, created_at) VALUES(?,?,?,?,datetime('now'))",
    [username, await hashPassword(body.password), role, body.name || username]);
  await saveBookmark(session);
  return { ok: true, username, role };
}

async function createReorder(session, body) {
  const drugId = Number(body?.drug_id);
  const drug = await first(session, "SELECT * FROM drugs WHERE id = ?", [drugId]);
  if (!drug) return fail(404, "drug not found");
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
  if (!drugId || !qty || qty <= 0) return fail(400, "drug_id and positive qty required");
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
  if (!batch || batch.qty_remaining < qty) return fail(409, "not enough stock in the batch");
  const value = Math.round(qty * (batch.unit_cost || 0) * 100) / 100;
  // guarded decrement first (closes the read-then-write race), then the record;
  // a failure after the decrement compensates it so stock is never lost
  const dec = await writeMany(session, [
    { sql: "UPDATE batches SET qty_remaining = qty_remaining - ? WHERE id = ? AND qty_remaining >= ?", params: [qty, batch.id] },
  ]);
  if (!(dec[0]?.meta?.changes > 0)) return fail(409, "not enough stock in the batch");
  let w;
  try {
    w = await write(session,
      "INSERT INTO waste(drug_id, batch_id, supplier_id, qty, reason, unit_cost, value, status, note, created_at) " +
      "VALUES(?,?,?,?,?,?,?,?,?,datetime('now'))",
      [drugId, batch.id, batch.supplier_id, qty, reason, batch.unit_cost, value, "pending", body?.note || null]);
  } catch (e) {
    await writeMany(session, [
      { sql: "UPDATE batches SET qty_remaining = qty_remaining + ? WHERE id = ?", params: [qty, batch.id] },
    ]);
    return fail(500, "could not record the waste entry — stock restored, please retry");
  }
  await saveBookmark(session);
  await refreshAlerts(session);
  return { ok: true, qty, batch: batch.batch_no, value, id: w.result?.meta?.last_row_id };
}

async function createReturn(session, body) {
  const supplierId = Number(body?.supplier_id);
  if (!supplierId) return fail(400, "supplier_id required");
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
  if (!lots.length) return fail(404, "no pending waste lots for this supplier");
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
  if (!body?.supplier_id || !body?.subject) return fail(400, "supplier_id and subject required");
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
  if (!text) return fail(400, "message required");
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
    const expiredAsked = /(already|lapsed|past|expired)/.test(low) && !/next|within|soon|coming|upcoming/.test(low);
    if (expiredAsked) {
      const rows = await all(session, `
        SELECT d.name AS drug, COUNT(*) AS lots, SUM(b.qty_remaining) AS units,
               ROUND(SUM(b.qty_remaining * b.unit_cost), 0) AS value
        FROM batches b JOIN drugs d ON d.id = b.drug_id
        WHERE b.qty_remaining > 0 AND b.expiry_date IS NOT NULL AND b.expiry_date < ?
        GROUP BY b.drug_id ORDER BY lots DESC LIMIT 8`, [today]);
      const lots = rows.reduce((a, r) => a + r.lots, 0);
      const value = rows.reduce((a, r) => a + (r.value || 0), 0);
      return {
        text: rows.length
          ? `**${lots} batches** are already expired and blocked from dispensing — worth ₹${value.toLocaleString("en-IN")} (top medicines below).`
          : "No expired stock — nothing is past its expiry date.",
        cards: rows.length ? [{ type: "table", title: "Already expired",
          columns: ["Medicine", "Lots", "Units", "Value"],
          rows: rows.map((r) => [r.drug, r.lots, r.units, r.value]) }] : [],
      };
    }
    // future window: honour an explicit horizon (default 90 days)
    const m = low.match(/(\d+)\s*(day|days|week|weeks|month|months)/);
    const horizon = m ? (m[2].startsWith("d") ? +m[1]
      : m[2].startsWith("w") ? +m[1] * 7 : +m[1] * 30) : 90;
    const end = new Date(Date.now() + horizon * 86400000).toISOString().slice(0, 10);
    const soon = await all(session, `
      SELECT d.name AS drug, COUNT(*) AS lots, SUM(b.qty_remaining) AS units, MIN(b.expiry_date) AS next
      FROM batches b JOIN drugs d ON d.id = b.drug_id
      WHERE b.qty_remaining > 0 AND b.expiry_date IS NOT NULL
        AND b.expiry_date >= ? AND b.expiry_date <= ?
      GROUP BY b.drug_id ORDER BY next LIMIT 8`, [today, end]);
    const expiredLots = await scalar(session,
      "SELECT COUNT(*) FROM batches WHERE qty_remaining > 0 AND expiry_date IS NOT NULL AND expiry_date < ?",
      [today], 0);
    const note = expiredLots
      ? ` ${expiredLots} other batches are already expired (separate from this window).` : "";
    return {
      text: soon.length
        ? `**${soon.reduce((a, r) => a + r.lots, 0)} batches** across ${soon.length} medicines expire within the next ${horizon} days:` + note
        : `Nothing expires within the next ${horizon} days.` + note,
      cards: soon.length ? [{ type: "table", title: `Expiring within ${horizon} days`,
        columns: ["Medicine", "Lots", "Units", "Next expiry"],
        rows: soon.map((r) => [r.drug, r.lots, r.units, r.next]) }] : [],
    };
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
    catalog_bloom: catalogBloomStats(),
    databases: { primary: "pharmacy-primary", secondary: "pharmacy-secondary",
      sync: "transactional outbox (primary → secondary)" },
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
  const nearest = new Map();
  for (const r of rows) {
    const cur = nearest.get(r.drug_id);
    if (!cur || (r.expiry_date || "9999") < (cur.expiry_date || "9999")) nearest.set(r.drug_id, r);
  }
  let created = 0;
  for (const r of nearest.values()) {
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
  if (!id) return fail(400, "task id required");
  const task = await first(session, "SELECT * FROM shelf_tasks WHERE id = ?", [id]);
  if (!task) return fail(404, "task not found");
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
  const qty = Math.min(1000, Math.max(1, Number(url.searchParams.get("qty") || 1)));
  const norm = name.toLowerCase().replace(/[-_/.,]/g, " ").replace(/\s+/g, " ").trim();
  // Bloom pre-check on the exact-match probe only: an explicit `false` means
  // definitely absent (skip the SQL). `true`/`null` (no filter, build failed,
  // stale) must BOTH fall through to the SQL — a positive is never proof and
  // a failed build must never deny a real record.
  if ((await normNameMayExist(session, norm)) === false) {
    return fail(404, `medicine not found in catalogue: ${name}`);
  }
  const drug = (await first(session, "SELECT * FROM drugs WHERE norm_name = ?", [norm]))
    || (await first(session,
      "SELECT * FROM drugs WHERE LOWER(COALESCE(generic,'')) = ? ORDER BY id LIMIT 1", [norm]));
  if (!drug) return fail(404, `medicine not found in catalogue: ${name}`);

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
  if (!qty || qty <= 0) return fail(400, "qty must be positive");

  let drug = null;
  if (body.drug_id) {
    drug = await first(session, "SELECT * FROM drugs WHERE id = ?", [Number(body.drug_id)]);
  } else if (body.name) {
    const norm = String(body.name).toLowerCase().replace(/[-_/.,]/g, " ").replace(/\s+/g, " ").trim();
    drug = (await first(session, "SELECT * FROM drugs WHERE norm_name = ?", [norm]))
        || (await first(session,
          "SELECT * FROM drugs WHERE LOWER(COALESCE(generic,'')) = ? ORDER BY id LIMIT 1", [norm]));
  }
  if (!drug) return fail(404, "medicine not found in catalogue");

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
    // guarded decrement: fails if another request drained the batch first
    const dec = await writeMany(session, [
      { sql: "UPDATE batches SET qty_remaining = qty_remaining - ? WHERE id = ? AND qty_remaining >= ?",
        params: [take, b.id, take] },
    ]);
    if (!(dec[0]?.meta?.changes > 0)) continue; // batch was consumed concurrently — FEFO moves on
    taken.push({ batch_no: b.batch_no, expiry: b.expiry_date, qty: take,
      shelf: await scalar(session, "SELECT code FROM shelves WHERE id = ?", [b.shelf_id], null) });
    remaining -= take;
  }
  const dispensed = qty - remaining;

  // patient bill: one receipt with one line per (medicine, batch) issued
  let bill = null;
  if (taken.length) {
    const day = new Date().toISOString().slice(0, 10).replace(/-/g, "");
    const nRow = await first(session, "SELECT COUNT(*) AS n FROM bills WHERE bill_no LIKE ?", [`BILL-${day}-%`]);
    const billNo = `BILL-${day}-${String((nRow?.n || 0) + 1).padStart(3, "0")}`;
    const amount = Math.round(taken.reduce((a, t) => a + (drug.mrp || 0) * t.qty, 0) * 100) / 100;
    // bill + all its line items commit in ONE transaction (bill_items writeMany
    // resolves the parent id in SQL, so the batch needs no intermediate reads)
    await writeMany(session, [
      { sql: "INSERT INTO bills(bill_no, date, patient, doctor, note, items_count, units, amount, source, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        params: [
          billNo, new Date().toISOString().slice(0, 10), body.patient || "", body.doctor || "",
          [body.doctor ? `prescribed by ${body.doctor}` : "", body.patient ? `patient ${body.patient}` : body.patient, body.note]
            .filter(Boolean).join("; "),
          taken.length, taken.reduce((a, t) => a + t.qty, 0), amount, "counter", new Date().toISOString()] },
      ...taken.map((t) => ({
        sql: "INSERT INTO bill_items(bill_id, drug_id, drug_name, batch_no, qty, unit_price, line_total, created_at) " +
          "SELECT id, ?, ?, ?, ?, ?, ?, ? FROM bills WHERE bill_no = ?",
        params: [drug.id, drug.name, t.batch_no, t.qty, drug.mrp || 0,
          Math.round((drug.mrp || 0) * t.qty * 100) / 100, new Date().toISOString(), billNo] })),
    ]);
    bill = { bill_no: billNo, amount, units: taken.reduce((a, t) => a + t.qty, 0), items: taken.map((t) => ({
      drug_name: drug.name, batch_no: t.batch_no, qty: t.qty,
      unit_price: drug.mrp || 0, line_total: Math.round((drug.mrp || 0) * t.qty * 100) / 100 })) };
  }

  const after = await first(session, `
    SELECT COALESCE(SUM(CASE WHEN expiry_date IS NULL OR expiry_date >= date('now')
                             THEN qty_remaining ELSE 0 END), 0) AS usable
    FROM batches WHERE drug_id = ?`, [drug.id]);
  await saveBookmark(session);
  await refreshAlerts(session);

  return {
    drug_id: drug.id, drug: drug.name, requested: qty, dispensed,
    batches: taken, shortfall: remaining, blocked_expired: blockedRow?.n || 0,
    bill,
    usable_before: before.usable, usable_after: after.usable,
    at: new Date().toISOString(),
  };
}

/* Patient bills ledger (admin AND pharmacist): one receipt per counter issue. */
async function listBills(session, url) {
  const q = (url.searchParams.get("q") || url.searchParams.get("drug") || "").trim().toLowerCase();
  const kind = url.searchParams.get("kind") || "bills";
  if (kind === "purchases") return invoicesList(session, url);
  const limit = Math.min(2000, Math.max(1, Number(url.searchParams.get("limit") || 200)));
  let where = ""; const params = [];
  if (q) {
    where = "WHERE (LOWER(b.bill_no) LIKE ? OR LOWER(b.patient) LIKE ? OR b.id IN " +
      "(SELECT bi.bill_id FROM bill_items bi WHERE LOWER(bi.drug_name) LIKE ?))";
    params.push(`%${q}%`, `%${q}%`, `%${q}%`);
  }
  const rows = await all(session,
    `SELECT b.* FROM bills b ${where} ORDER BY b.id DESC LIMIT ?`, [...params, limit]);
  const items = await all(session,
    `SELECT * FROM bill_items ${rows.length ? `WHERE bill_id IN (${rows.map((r) => r.id).join(",")})` : "WHERE 0"} ORDER BY id`);
  for (const b of rows) b.items = items.filter((i) => i.bill_id === b.id);
  const sRow = await first(session,
    `SELECT COUNT(*) AS bills, COALESCE(SUM(units),0) AS units, COALESCE(SUM(amount),0) AS amount FROM bills b ${where}`, params);
  return { items: rows,
    summary: { bills: sRow?.bills || 0, units: sRow?.units || 0, amount: Math.round((sRow?.amount || 0) * 100) / 100 } };
}

/* Multi-line prescription: several medicines, each with its own qty, one request. */
async function issuePrescriptionMulti(session, body) {
  const lines = [];
  for (let i = 0; i < body.items.length; i++) {
    const it = body.items[i] || {};
    const qty = Math.floor(Number(it.qty));
    if (!qty || qty <= 0) return fail(400, `item ${i + 1}: qty must be positive`);
    let drug = null;
    if (it.drug_id) {
      drug = await first(session, "SELECT * FROM drugs WHERE id = ?", [Number(it.drug_id)]);
    } else if (it.name) {
      const norm = String(it.name).toLowerCase().replace(/[-_/.,]/g, " ").replace(/\s+/g, " ").trim();
      drug = (await first(session, "SELECT * FROM drugs WHERE norm_name = ?", [norm]))
        || (await first(session, "SELECT * FROM drugs WHERE LOWER(COALESCE(generic,'')) = ? ORDER BY id LIMIT 1", [norm]));
    }
    if (!drug) return fail(404, `item ${i + 1}: medicine not found in catalogue`);
    lines.push({ drug, qty });
  }
  const results = [];
  for (const { drug, qty } of lines) {
    results.push(await issuePrescription(session, {
      drug_id: drug.id, qty,
      doctor: body.doctor || "", patient: body.patient || "", note: body.note || "",
    }));
    if (results[results.length - 1]?.detail) {
      const inner = results[results.length - 1];
      return fail(Number.isInteger(inner.__error_status) ? inner.__error_status : 409,
        `line for ${drug.name}: ${inner.detail}`);
    }
  }
  return {
    mode: "multi", items: results, drugs: results.map((r) => r.drug),
    requested: results.reduce((a, r) => a + (r.requested || 0), 0),
    dispensed: results.reduce((a, r) => a + (r.dispensed || 0), 0),
    shortfall: results.reduce((a, r) => a + (r.shortfall || 0), 0),
    bill: results.length === 1 ? results[0].bill
      : { bill_no: results.map((r) => r.bill?.bill_no).filter(Boolean).join(" + "),
          amount: Math.round(results.reduce((a, r) => a + (r.bill?.amount || 0), 0) * 100) / 100,
          items: results.flatMap((r) => r.bill?.items || []) },
    at: new Date().toISOString(),
  };
}

/* Purchase invoice ledger (admin AND pharmacist): one row per received batch. */
async function invoicesList(session, url) {
  const drugQ = (url.searchParams.get("drug") || "").trim().toLowerCase();
  const supplierId = Number(url.searchParams.get("supplier") || 0);
  const limit = Math.min(2000, Math.max(1, Number(url.searchParams.get("limit") || 200)));
  const cond = [];
  const params = [];
  if (drugQ) { cond.push("(LOWER(d.name) LIKE ? OR LOWER(COALESCE(d.generic,'')) LIKE ?)"); params.push(`%${drugQ}%`, `%${drugQ}%`); }
  if (supplierId) { cond.push("p.supplier_id = ?"); params.push(supplierId); }
  const where = cond.length ? `WHERE ${cond.join(" AND ")}` : "";
  const items = await all(session, `
    SELECT p.id, p.purchase_id AS invoice_no, p.date_received, d.name AS drug, d.category,
           b.batch_no, p.qty, p.unit_cost, p.total, s.name AS supplier
    FROM purchases p
    JOIN drugs d ON d.id = p.drug_id
    LEFT JOIN batches b ON b.id = p.batch_id
    LEFT JOIN suppliers s ON s.id = p.supplier_id
    ${where} ORDER BY p.date_received DESC, p.id DESC LIMIT ?`, [...params, limit]);
  const invoices = await all(session, `
    SELECT p.purchase_id AS invoice_no, p.date_received, s.name AS supplier,
           COUNT(*) AS lines, SUM(p.qty) AS units, ROUND(SUM(p.total), 2) AS amount
    FROM purchases p JOIN drugs d ON d.id = p.drug_id
    LEFT JOIN suppliers s ON s.id = p.supplier_id
    ${where} GROUP BY p.purchase_id, p.date_received, s.name
    ORDER BY p.date_received DESC LIMIT ?`, [...params, limit]);
  return {
    items, invoices,
    summary: { lines: items.length, units: items.reduce((a, r) => a + (r.qty || 0), 0),
      total_cost: Math.round(items.reduce((a, r) => a + (r.total || 0), 0) * 100) / 100 },
  };
}

/* Season-of-year prediction: month-of-year factors learned from sales_daily,
   blended over the run-rate baseline (the full model runs in Python). */
const SEASON_NAMES = ["Winter", "Winter", "Summer", "Summer", "Summer", "Monsoon",
  "Monsoon", "Monsoon", "Post-monsoon", "Post-monsoon", "Winter", "Winter"];

async function seasonalForecast(session, drugId, days) {
  const drug = await first(session, "SELECT * FROM drugs WHERE id = ?", [drugId]);
  if (!drug) return { detail: "unknown drug" };
  const months = await all(session, `
    SELECT substr(date,1,7) AS ym, SUM(qty) AS qty FROM sales_daily
    WHERE drug_id = ? GROUP BY ym ORDER BY ym`, [drugId]);
  if (months.length < 14) {
    return { drug: drug.name, available: false,
      reason: "Need at least 14 months of sales history to learn a season pattern; this medicine has less." };
  }
  const totals = months.map((m) => ({ ym: m.ym, t: Number(m.qty || 0) }));
  const med = totals.map((x) => x.t).sort((a, b) => a - b)[Math.floor(totals.length / 2)];
  if (!med) return { drug: drug.name, available: false, reason: "no usable sales volume" };
  const buckets = {};
  for (const { ym, t } of totals) {
    const m = Number(ym.slice(5, 7));
    (buckets[m] = buckets[m] || []).push(t / med);
  }
  const factors = Array.from({ length: 12 }, (_, i) => {
    const arr = buckets[i + 1] || [1.0];
    return Math.round(arr.reduce((a, x) => a + x, 0) / arr.length * 100) / 100;
  });
  // baseline: trailing 56-day run-rate (no model on the Worker)
  const base = await scalar(session, `
    SELECT COALESCE(SUM(qty),0) FROM sales_daily WHERE drug_id = ? AND date > date('now', '-56 day')`, [drugId], 0);
  const avgDaily = base / 56;
  const today = new Date();
  const daily = [];
  for (let i = 0; i < days; i++) {
    const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() + i);
    const m = d.getMonth();
    const f = 0.5 + 0.5 * factors[m];
    const q = Math.round(avgDaily * f * 10) / 10;
    daily.push({ date: d.toISOString().slice(0, 10), qty: q, month_factor: factors[m], season: SEASON_NAMES[m] });
  }
  const thisM = today.getMonth();
  const nxtM = (thisM + 1) % 12;
  const peakMonth = factors.indexOf(Math.max(...factors)) + 1;
  return {
    drug: drug.name, drug_id: drugId, available: true,
    season: SEASON_NAMES[thisM], this_month_factor: factors[thisM],
    next_month_factor: factors[nxtM], next_month_season: SEASON_NAMES[nxtM],
    peak_month: peakMonth, peak_season: SEASON_NAMES[peakMonth - 1],
    peak_month_name: ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][peakMonth - 1],
    months: ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"],
    factors, history_months: months.length,
    window_days: days, total_units: Math.round(daily.reduce((a, d) => a + d.qty, 0) * 10) / 10,
    avg_daily: Math.round(daily.reduce((a, d) => a + d.qty, 0) / days * 100) / 100,
    daily,
    note: `Currently ${SEASON_NAMES[thisM]} — demand index ${factors[thisM].toFixed(2)}× the yearly average. Peak: ${SEASON_NAMES[peakMonth - 1]} at ${factors[peakMonth - 1].toFixed(2)}×.`,
  };
}

async function listAlerts(session) {
  const items = await all(session, `
    SELECT a.*, d.name AS drug FROM alerts a LEFT JOIN drugs d ON d.id = a.drug_id
    WHERE a.status != 'resolved'
    ORDER BY a.created_at DESC, CASE a.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
      a.id DESC LIMIT 200`);
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

// /api/sync/status is served by replication.js syncStatus(env) — it reports
// real outbox progress, pending work, secondary watermark and the latest
// failure, and never infers "in sync" from bindings merely being configured.

async function runArchiveNow(env, session) {
  const result = await handleArchive(env);
  await saveBookmark(session);
  return result;
}

/* ------------------------------------------------------------------ *
 * Laya AI outlook — the real Python engine, reachable.
 *
 * The sklearn model (pharmacy/laya.py) cannot run inside a Workers isolate,
 * so it runs on a small hosted Python service (Render free tier). The Worker
 * builds each SKU's state snapshot from ITS OWN D1 tables (same SQL
 * semantics as pharmacy/laya.py state_from_db), POSTs the states to the
 * service with a shared secret, and returns the service's payload verbatim —
 * the same card payloads the local app and chatbot produce.
 *
 * Configuration: LAYA_SERVICE_URL (vars) + LAYA_SHARED_KEY (secret).
 * Unset/failed service → the SAME graceful fallback error rows the local
 * app uses (engine_unavailable) — never invented predictions.
 * ------------------------------------------------------------------ */

const LAYA_STATES_SQL = {
  // latest data timestamp across all sales (the engine anchors predictions here)
  latestSalesDate: `SELECT MAX(date) AS d FROM sales WHERE date IS NOT NULL`,
  // per-SKU sales totals inside a date window (parameterised start/end)
  salesTotal: (start, end) => `SELECT COALESCE(SUM(qty),0) AS n FROM sales
    WHERE drug_id = ? AND date IS NOT NULL AND date >= '${start}' AND date <= '${end}'`,
  // per-SKU distinct transactions in the 28-day window
  txns: (start) => `SELECT COUNT(DISTINCT txn_id) AS n FROM sales
    WHERE drug_id = ? AND date IS NOT NULL AND date >= '${start}'`,
  // last 60 receipts with dates (for interval gaps + last receipt qty)
  receipts: `SELECT date_received, qty FROM purchases
    WHERE drug_id = ? AND date_received IS NOT NULL
    ORDER BY date_received DESC LIMIT 60`,
  // FEFO current batch + usable stock mirror pharmacy/forecasting.available_stock
  currentBatch: `SELECT batch_no, qty_remaining FROM batches
    WHERE drug_id = ? AND qty_remaining > 0
    ORDER BY expiry_date IS NULL, expiry_date LIMIT 1`,
  usableStock: `SELECT COALESCE(SUM(CASE WHEN expiry_date IS NULL OR expiry_date >= ?
    THEN qty_remaining ELSE 0 END),0) AS usable FROM batches WHERE drug_id = ?`,
  supplier: `SELECT name FROM suppliers WHERE id = ?`,
};

function _fmt(n) { return Math.round(n * 100) / 100; }
function _daysAgo(iso, n) {
  return new Date(new Date(iso + "T00:00:00Z").getTime() - n * 86400000).toISOString().slice(0, 10);
}

async function layaStateFor(session, drug, asOf, salesCache) {
  const id = drug.id;
  const sales = (start, end) => async () => {
    const r = await scalar(session, LAYA_STATES_SQL.salesTotal(start, end), [id], 0);
    return Number(r) || 0;
  };
  const s28 = await sales(_daysAgo(asOf, 27), asOf)();
  const s365 = await sales(_daysAgo(asOf, 365), asOf)();
  if (s28 === 0 && s365 === 0) return null; // no demand signal: same rule as state_from_db

  const weekly = [];
  for (let w = 0; w < 8; w++) {
    const e = _daysAgo(asOf, 7 * w), s = _daysAgo(asOf, 7 * w + 6);
    weekly.push(await sales(s, e)());
  }
  weekly.reverse(); // oldest first, matching the training data
  const s7 = await sales(_daysAgo(asOf, 6), asOf)();
  const s14 = await sales(_daysAgo(asOf, 13), asOf)();
  const s56 = await sales(_daysAgo(asOf, 55), asOf)();
  const recentDaily = s14 / 14.0, baselineDaily = s56 / 56.0;
  if (baselineDaily <= 0) return null;

  const receipts = await all(session, LAYA_STATES_SQL.receipts, [id]);
  let daysSince = 999;
  if (receipts.length && receipts[0].date_received) {
    const d = Math.floor((new Date(asOf) - new Date(String(receipts[0].date_received).slice(0, 10))) / 86400000);
    if (Number.isFinite(d)) daysSince = Math.max(0, d);
  }
  const dates = [...new Set(receipts.map((r) => String(r.date_received).slice(0, 10)))].sort();
  const gaps = [];
  for (let i = 1; i < dates.length; i++) {
    const g = Math.round((new Date(dates[i]) - new Date(dates[i - 1])) / 86400000);
    if (Number.isFinite(g) && g >= 0) gaps.push(g);
  }
  const medianInterval = gaps.length
    ? (gaps.length % 2 ? gaps[(gaps.length - 1) / 2]
      : (gaps[gaps.length / 2 - 1] + gaps[gaps.length / 2]) / 2) : 30;
  const txns = Number(await scalar(session, LAYA_STATES_SQL.txns(_daysAgo(asOf, 27)), [id], 0)) || 0;

  // Last year's same window: previous year equivalent date (Feb 29 auto-handles
  // via JS Date arithmetic - it crops to Mar 1, which is close enough for a
  // 14-day demand total, matching the engine's intent).
  const lyEnd = new Date(asOf + "T00:00:00Z"); lyEnd.setUTCFullYear(lyEnd.getUTCFullYear() - 1);
  const lyEndIso = lyEnd.toISOString().slice(0, 10);
  const lyStart = _daysAgo(lyEndIso, 13);
  const lyDaily = (await sales(lyStart, lyEndIso)()) / 14.0;
  const seasonality = lyDaily > 0 ? Math.round(Math.min(baselineDaily / Math.max(lyDaily, 0.01), 5.0) * 1000) / 1000 : 1.0;

  const batch = (await first(session, LAYA_STATES_SQL.currentBatch, [id])) || null;
  const usable = Number(await scalar(session, LAYA_STATES_SQL.usableStock, [asOf, id], 0)) || 0;
  const supplier = (await first(session, LAYA_STATES_SQL.supplier, [drug.supplier_id])) || null;

  return {
    as_of: asOf,
    drug: drug.name,
    current_batch: (batch && batch.batch_no) || "",
    supplier: (supplier && supplier.name) || "",
    days_since_last_receipt: daysSince,
    last_receipt_qty: Number((receipts[0] || {}).qty) || 0,
    estimated_current_batch_remaining: (batch && Number(batch.qty_remaining)) || 0,
    estimated_days_of_cover: Math.round((usable / Math.max(recentDaily, 0.01)) * 10) / 10,
    historical_median_reorder_interval_days: medianInterval,
    sales_units_7d: s7, sales_units_14d: s14, sales_units_28d: s28, sales_units_56d: s56,
    transactions_28d: txns,
    avg_units_per_transaction_28d: Math.round((s28 / Math.max(txns, 1)) * 10) / 10,
    recent_daily_demand: Math.round(recentDaily * 100) / 100,
    baseline_daily_demand: Math.round(baselineDaily * 100) / 100,
    recent_vs_baseline_ratio: Math.round((recentDaily / baselineDaily) * 1000) / 1000,
    same_period_last_year_daily_demand: Math.round(lyDaily * 100) / 100,
    seasonality_index: seasonality,
    weekly_sales_units_last_8_weeks: weekly,
    prescription_trend_note: "Prescription records are absent; observed sales volume and transaction frequency are the demand signal used here.",
  };
}

async function layaForecast(session) {
  const url = (env.LAYA_SERVICE_URL || "").replace(/\/+$/, "");
  if (!env.LAYA_SHARED_KEY) {
    return _layaFallback("engine_unavailable", "Laya service key is not configured (LAYA_SHARED_KEY unset).");
  }
  if (!url) {
    return _layaFallback("engine_unavailable", "Laya service is not configured (LAYA_SERVICE_URL unset).");
  }
  // Anchor on the latest sales date across all SKUs, mirroring state_from_db
  const latestRow = await first(session, LAYA_STATES_SQL.latestSalesDate);
  const asOf = latestRow && latestRow.d ? String(latestRow.d).slice(0, 10) : todayStr();
  const drugs = await all(session, `SELECT id, name, supplier_id FROM drugs ORDER BY category, name`);
  const payloadRows = [];
  for (const d of drugs) {
    const state = await layaStateFor(session, d, asOf);
    if (!state) continue; // no demand signal: matches the engine's None case
    payloadRows.push({ drug_id: d.id, drug: d.name, state });
  }
  if (!payloadRows.length) {
    return _layaFallback("no_data", "No catalogue SKU has enough demand history for a prediction.");
  }
  try {
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), 25000); // keep below the 30s Worker cap
    const resp = await fetch(url + "/v1/laya/latest", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Laya-Key": env.LAYA_SHARED_KEY },
      body: JSON.stringify({ rows: payloadRows }),
      signal: ac.signal,
    });
    clearTimeout(timer);
    if (!resp.ok) {
      const txt = await resp.text().catch(() => "");
      return _layaFallback("engine_unavailable", `Laya service returned ${resp.status}` + (txt ? `: ${txt.slice(0, 180)}` : ""));
    }
    const out = await resp.json();
    if (!out || !Array.isArray(out.rows)) {
      return _layaFallback("malformed", "Laya service returned an unexpected payload shape.");
    }
    return { engine: out.engine || { status: "ready", detail: null }, generated_at: out.generated_at, state: "latest", rows: out.rows };
  } catch (err) {
    const isTimeout = err && (err.name === "AbortError" || String(err).includes("TimeoutError"));
    return _layaFallback(isTimeout ? "timeout" : "engine_unavailable",
      isTimeout ? "Laya service call timed out." : `Laya service unreachable: ${String(err && err.message || err).slice(0, 160)}`);
  }
}

function _layaFallback(error, detail) {
  return { engine: { status: "unavailable", detail }, generated_at: todayStr(), state: "latest",
           rows: [{ drug: "—", as_of: null, card: null, trajectory: null, reorder_by: null,
                    order_qty: null, order_qty_open: false, error, error_detail: detail }] };
}
