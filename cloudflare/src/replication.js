/**
 * Replication between the two INDEPENDENT D1 databases (primary → secondary).
 *
 * Why this exists: `pharmacy-primary` (DB_A) and `pharmacy-secondary` (DB_B)
 * are separate D1 databases. Cloudflare's read-replication feature only syncs
 * replicas OF ONE database — it cannot keep two independent databases in sync.
 * So we implement a transactional outbox with state-based reconciliation:
 *
 *   Capture (in db.js write()):
 *     every business statement is executed in a `session.batch()` TOGETHER
 *     with a matching outbox event (row key(s) + table + op). D1 batches are
 *     transactions, so a write can never commit without its event (and vice
 *     versa) — no lost changes, no phantom events.
 *
 *   Apply (drainReplication):
 *     events are grouped by request txn and applied to DB_B as ONE atomic
 *     batch per group: the current row STATE is re-read from the primary and
 *     upserted (or the row deleted) on the secondary, plus a watermark update.
 *     Because we copy final row state — never deltas — a retried or duplicated
 *     event can only rewrite the same value; it can never double-apply an
 *     inventory decrement. Multi-table workflows (dispense: batches + sales +
 *     bills + bill_items) therefore land on the secondary atomically.
 *
 *   Track:
 *     `replication_state` on BOTH databases — DB_B's copy is updated inside the
 *     same atomic batch as the data (proof of commit); DB_A's copy records the
 *     progress + the latest failure for /api/sync/status.
 *
 * Bookkeeping tables (sync_bookmarks, replication_outbox, replication_state)
 * never replicate — they are per-database.
 */

/* Primary-key columns per replicated table (nullish = not replicated). */
export const PKS = {
  suppliers: ["id"], drugs: ["id"], shelves: ["id"], users: ["id"],
  batches: ["id"], sales: ["id"], purchases: ["id"], bills: ["id"],
  bill_items: ["id"], sales_daily: ["drug_id", "date"], alerts: ["id"],
  reorders: ["id"], notifications: ["id"], waste: ["id"], returns: ["id"],
  uploads: ["id"], quarantined: ["id"], shelf_tasks: ["id"],
  settings: ["key"], archive_runs: ["id"],
};
const EXCLUDED = new Set(["sync_bookmarks", "replication_outbox", "replication_state"]);

/**
 * FK-safe apply order: parents before children (upserts) — a batch that
 * inserts sales + bills + bill_items must not violate references, even when
 * the secondary is behind or was re-seeded. Deletes run in reverse.
 */
const TABLE_ORDER = ["suppliers", "shelves", "drugs", "users", "settings", "batches",
  "sales", "purchases", "bills", "sales_daily", "alerts", "reorders",
  "notifications", "waste", "returns", "uploads", "quarantined", "shelf_tasks",
  "bill_items", "archive_runs"];
const rank = (t) => { const i = TABLE_ORDER.indexOf(t); return i < 0 ? 999 : i; };

const OUTBOX_DDL = `
CREATE TABLE IF NOT EXISTS replication_outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  txn TEXT, table_name TEXT NOT NULL, op TEXT NOT NULL,
  key_json TEXT, note TEXT, created_at TEXT DEFAULT (datetime('now')),
  applied_at TEXT
)`;
const OUTBOX_IDX = `
CREATE INDEX IF NOT EXISTS idx_replication_outbox_pending
ON replication_outbox(applied_at, id)`;
const STATE_DDL = `
CREATE TABLE IF NOT EXISTS replication_state(
  id INTEGER PRIMARY KEY CHECK(id = 1),
  last_applied_seq INTEGER DEFAULT 0, last_applied_at TEXT,
  last_success_at TEXT, last_error TEXT, last_error_at TEXT,
  consecutive_failures INTEGER DEFAULT 0,
  reconciled_at TEXT, updated_at TEXT
)`;

let ensuredPrimary = false, ensuredSecondary = false;

/**
 * Idempotent DDL for the primary (outbox + state + archive error column).
 * Called lazily on the first write/drain per isolate, so a deployment order
 * mistake (code before migration) can never break the application's writes.
 */
export async function ensureSchemaPrimary(session) {
  if (ensuredPrimary) return;
  await session.prepare(OUTBOX_DDL).run();
  await session.prepare(OUTBOX_IDX).run();
  await session.prepare(STATE_DDL).run();
  try { await session.prepare("ALTER TABLE archive_runs ADD COLUMN error TEXT").run(); }
  catch { /* column already exists */ }
  ensuredPrimary = true;
}

/** Idempotent DDL for the secondary (state table only). */
export async function ensureSchemaSecondary(db) {
  if (ensuredSecondary) return;
  await db.prepare(STATE_DDL).run();
  ensuredSecondary = true;
}

/**
 * Run the outbox DDL once per isolate, on the session's own database.
 * Exported so db.js can guarantee the outbox exists BEFORE the first
 * captured write (protects against deploy-before-migrate ordering).
 */
export async function ensureSchemaFor(session) {
  await ensureSchemaPrimary(session);
}

/* ------------------------------------------------------------------ capture */

const IDENT = "[A-Za-z_][A-Za-z0-9_]*";

/** Strip single/double-quoted string literals so `?`-counting is safe. */
function stripLiterals(s) {
  return s.replace(/'(?:[^']|'')*'/g, "''").replace(/"[^"]*"/g, '""');
}

/** Split `a, ?, f('x', ?)` on top-level commas (paren-depth aware). */
function splitTopLevel(s) {
  const parts = []; let depth = 0, cur = "";
  for (const ch of s) {
    if (ch === "(") depth++;
    if (ch === ")") depth--;
    if (ch === "," && depth === 0) { parts.push(cur.trim()); cur = ""; }
    else cur += ch;
  }
  if (cur.trim()) parts.push(cur.trim());
  return parts;
}

const countPlaceholders = (s) => (stripLiterals(s).match(/\?/g) || []).length;

/**
 * Classify a business statement into a capture plan.
 *  → {kind:'insert', table, keyFromParams:[{paramIndex}|{literal}]}  (pk in column list)
 *  → {kind:'insert', table}                                           (rowid post-capture)
 *  → {kind:'update'|'delete', table, where, whereParamOffset}
 *  → {kind:'none'}     (bookkeeping table — never replicated)
 *  → {kind:'unknown'}  (unrecognised shape — flagged, never silently skipped)
 */
export function planCapture(sql, params) {
  const s = sql.trim();

  const ins = s.match(new RegExp(`^INSERT\\s+(?:OR\\s+\\w+\\s+)?INTO\\s+(${IDENT})\\s*\\(([^)]*)\\)`, "i"));
  if (ins) {
    const table = ins[1];
    if (EXCLUDED.has(table)) return { kind: "none", table };
    const pks = PKS[table];
    if (!pks) return { kind: "unknown", table, reason: "table not in PKS map" };
    const columns = splitTopLevel(ins[2]).map((c) => c.replace(/["`\s]/g, "").toLowerCase());
    const pkIdx = pks.map((p) => columns.indexOf(p.toLowerCase()));
    const tail = s.slice(ins.index + ins[0].length).trimStart();
    if (pkIdx.every((x) => x >= 0) && /^VALUES\s*\(/i.test(tail)) {
      // pk value is in the column list — locate the VALUES clause (balanced parens)
      const vStart = tail.search(/\(/);
      let depth = 0, i = vStart;
      for (; i < tail.length; i++) {
        if (tail[i] === "(") depth++;
        else if (tail[i] === ")") { depth--; if (depth === 0) break; }
      }
      const values = splitTopLevel(tail.slice(vStart + 1, i));
      let pIdx = 0;
      const keySources = values.map((v) => {
        const n = countPlaceholders(v);
        let src;
        if (n === 1 && v.trim() === "?") src = { paramIndex: pIdx };
        else if (n === 0) {
          const lit = v.trim();
          // unquote SQL string literals so the key matches the stored value
          src = { literal: lit.startsWith("'") && lit.endsWith("'")
            ? lit.slice(1, -1).replace(/''/g, "'") : lit };
        } else src = { expression: v };
        pIdx += n;
        return src;
      });
      if (keySources.every((k) => k.expression === undefined)) {
        return { kind: "insert", table, keySources: pkIdx.map((x) => keySources[x]) };
      }
    }
    // pk not listed, INSERT…SELECT, or computed key → post-capture via rowid.
    // (A guarded INSERT…SELECT that inserts 0 rows captures a stale rowid,
    //  which is harmless: the apply step re-reads current row state.)
    return { kind: "insert", table };
  }
  if (/^INSERT\b/i.test(s)) {
    const t = s.match(new RegExp(`^INSERT\\s+(?:OR\\s+\\w+\\s+)?INTO\\s+(${IDENT})`, "i"));
    if (t && EXCLUDED.has(t[1])) return { kind: "none", table: t[1] };
    return { kind: "unknown", table: t?.[1] || null, reason: "INSERT without parseable column list" };
  }

  const upd = s.match(new RegExp(`^UPDATE\\s+(${IDENT})\\s+SET\\s+([\\s\\S]*?)\\s+WHERE\\s+([\\s\\S]+)$`, "i"));
  if (upd) {
    const table = upd[1];
    if (EXCLUDED.has(table)) return { kind: "none", table };
    if (!PKS[table]) return { kind: "unknown", table, reason: "table not in PKS map" };
    return {
      kind: "update", table, where: upd[3].trim(),
      whereParamOffset: countPlaceholders(upd[2]),
    };
  }

  const del = s.match(new RegExp(`^DELETE\\s+FROM\\s+(${IDENT})(?:\\s+WHERE\\s+([\\s\\S]+))?$`, "i"));
  if (del) {
    const table = del[1];
    if (EXCLUDED.has(table)) return { kind: "none", table };
    if (!del[2]) return { kind: "unknown", table, reason: "unbounded DELETE" };
    if (!PKS[table]) return { kind: "unknown", table, reason: "table not in PKS map" };
    return { kind: "delete", table, where: del[2].trim(), whereParamOffset: 0 };
  }

  if (/^(SELECT|PRAGMA|WITH)\b/i.test(s)) return { kind: "none" };
  return { kind: "unknown", table: null, reason: "unrecognised statement" };
}

/** The outbox statement that captures keys BEFORE an update/delete runs. */
function preCaptureSql(table, op, where, txn) {
  const pks = PKS[table];
  return {
    sql: `INSERT INTO replication_outbox(txn, table_name, op, key_json)
          SELECT ?, ?, ?, json_array(${pks.join(",")}) FROM ${table} WHERE ${where}`,
    params: [txn, table, op],
  };
}

/** The outbox statement that captures the key of a just-inserted row. */
function postCaptureSql(table, txn) {
  return {
    sql: `INSERT INTO replication_outbox(txn, table_name, op, key_json)
          SELECT ?, ?, 'upsert', json_array(rowid) FROM ${table} WHERE rowid = last_insert_rowid()`,
    params: [txn, table],
  };
}

/**
 * Execute one business statement on the session WITH its capture event
 * atomically (D1 batch = transaction). Falls back to a flagged 'unknown'
 * event when the statement shape is not recognised — reported by
 * /api/sync/status as `unreplicated`, never silently dropped.
 */
export async function capturedWrite(session, meta, sql, params, plan) {
  const txn = meta?.txn || null;
  const stamp = () => { if (meta) meta.wrote = true; };

  if (plan.kind === "insert" && plan.keySources) {
    // pk values are known from the column list → pre-capture, then insert
    const keyParams = plan.keySources.map((src) =>
      src.paramIndex !== undefined ? params[src.paramIndex] : src.literal);
    const pks = PKS[plan.table];
    const capture = {
      sql: `INSERT INTO replication_outbox(txn, table_name, op, key_json)
            VALUES(?, ?, 'upsert', json_array(${pks.map(() => "?").join(",")}))`,
      params: [txn, plan.table, ...keyParams],
    };
    const results = await session.batch([
      session.prepare(capture.sql).bind(...capture.params),
      session.prepare(sql).bind(...params),
    ]);
    stamp();
    return { result: results[1], bookmark: session.getBookmark() };
  }

  if (plan.kind === "insert") {
    // post-capture via last_insert_rowid() in the same transaction
    const post = postCaptureSql(plan.table, txn);
    const results = await session.batch([
      session.prepare(sql).bind(...params),
      session.prepare(post.sql).bind(...post.params),
    ]);
    stamp();
    return { result: results[0], bookmark: session.getBookmark() };
  }

  if (plan.kind === "update" || plan.kind === "delete") {
    const pre = preCaptureSql(plan.table, plan.kind, plan.where, txn);
    const whereParams = params.slice(plan.whereParamOffset);
    const results = await session.batch([
      session.prepare(pre.sql).bind(...pre.params, ...whereParams),
      session.prepare(sql).bind(...params),
    ]);
    stamp();
    return { result: results[1], bookmark: session.getBookmark() };
  }

  // kind 'none' | 'unknown': still perform the write, then flag unknowns.
  const result = await session.prepare(sql).bind(...params).run();
  stamp();
  if (plan.kind === "unknown") {
    try {
      await session.prepare(
        "INSERT INTO replication_outbox(txn, table_name, op, key_json, note) VALUES(?,?, 'unknown', NULL, ?)"
      ).bind(txn, plan.table || "?", `${plan.reason}: ${sql.slice(0, 300)}`).run();
    } catch (err) {
      console.warn("[replication] could not flag unreplicated write:", err?.message || err);
    }
    console.warn("[replication] unreplicated write shape:", plan.reason, sql.slice(0, 200));
  }
  return { result, bookmark: session.getBookmark() };
}

/* -------------------------------------------------------------------- drain */

const chunk = (arr, n) => { const out = []; for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n)); return out; };

async function allS(db, sql, params = []) {
  const { results } = await (params.length ? db.prepare(sql).bind(...params) : db.prepare(sql)).all();
  return results || [];
}
async function firstS(db, sql, params = []) {
  return (await allS(db, sql, params))[0] || null;
}

/** Apply pending outbox events to the secondary. Idempotent + retryable. */
export async function drainReplication(env, opts = {}) {
  if (!env.DB_A || !env.DB_B) {
    return { skipped: true, reason: "both DB_A and DB_B bindings are required" };
  }
  const maxEvents = opts.maxEvents ?? 500;
  const primary = env.DB_A.withSession("first-unconstrained");
  await ensureSchemaPrimary(primary);

  const pending = await allS(primary,
    "SELECT * FROM replication_outbox WHERE applied_at IS NULL AND op != 'unknown' ORDER BY id LIMIT ?",
    [maxEvents]);
  const unreplicated = await firstS(primary,
    "SELECT COUNT(*) AS n FROM replication_outbox WHERE applied_at IS NULL AND op = 'unknown'");
  if (!pending.length) {
    return { applied: 0, pending: 0, unreplicated: unreplicated?.n || 0 };
  }
  await ensureSchemaSecondary(env.DB_B);

  // group events by request txn so multi-table workflows apply atomically
  const groups = new Map();
  for (const ev of pending) {
    const k = ev.txn || `#${ev.id}`;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(ev);
  }

  let applied = 0, failed = 0, lastError = null, lastSeq = 0;
  const deadline = Date.now() + (opts.budgetMs ?? 20000);

  for (const events of groups.values()) {
    if (Date.now() > deadline) break;
    try {
      const upserts = new Map(), deletes = new Map();
      for (const ev of events) {
        if (!ev.key_json) continue;
        const bag = ev.op === "delete" ? deletes : upserts;
        if (!bag.has(ev.table_name)) bag.set(ev.table_name, new Set());
        bag.get(ev.table_name).add(ev.key_json);
      }

      // parents first for upserts…
      const stmts = [];
      for (const [table, keySet] of [...upserts].sort((a, b) => rank(a[0]) - rank(b[0]))) {
        const pks = PKS[table];
        if (!pks) throw new Error(`unknown table in outbox: ${table}`);
        const keys = [...keySet];
        const col = pks.length === 1 ? pks[0] : `(${pks.join(",")})`;
        const found = new Set();
        for (const part of chunk(keys, 40)) {
          const tuples = part.map(() => `(${pks.map(() => "?").join(",")})`).join(",");
          const params = part.flatMap((k) => JSON.parse(k));
          for (const row of await allS(primary,
            `SELECT * FROM ${table} WHERE ${col} IN (${tuples})`, params)) {
            const key = JSON.stringify(pks.map((p) => row[p]));
            found.add(key);
            const cols = Object.keys(row);
            const updCols = cols.filter((c) => !pks.includes(c));
            stmts.push(env.DB_B.prepare(
              `INSERT INTO ${table} (${cols.join(",")}) VALUES (${cols.map(() => "?").join(",")})
               ON CONFLICT(${pks.join(",")}) DO UPDATE SET ${updCols.map((c) => `${c}=excluded.${c}`).join(",")}`
            ).bind(...cols.map((c) => row[c])));
          }
        }
        // keys gone from the primary (deleted since capture) → delete on secondary
        for (const k of keys) {
          if (!found.has(k)) {
            const vals = JSON.parse(k);
            stmts.push(env.DB_B.prepare(
              `DELETE FROM ${table} WHERE ${pks.map((p) => `${p} = ?`).join(" AND ")}`).bind(...vals));
          }
        }
      }
      // …children first for deletes
      for (const [table, keySet] of [...deletes].sort((a, b) => rank(b[0]) - rank(a[0]))) {
        const pks = PKS[table];
        for (const k of keySet) {
          stmts.push(env.DB_B.prepare(
            `DELETE FROM ${table} WHERE ${pks.map((p) => `${p} = ?`).join(" AND ")}`).bind(...JSON.parse(k)));
        }
      }
      if (!stmts.length) { applied += events.length; lastSeq = Math.max(lastSeq, ...events.map((e) => e.id)); continue; }

      // atomic on the secondary: data + the secondary's own watermark
      const seq = Math.max(...events.map((e) => e.id));
      stmts.push(env.DB_B.prepare(
        `INSERT INTO replication_state(id, last_applied_seq, last_applied_at, updated_at)
         VALUES(1, ?, ?, datetime('now'))
         ON CONFLICT(id) DO UPDATE SET last_applied_seq=excluded.last_applied_seq,
           last_applied_at=excluded.last_applied_at, updated_at=excluded.updated_at`
      ).bind(seq, new Date().toISOString()));
      await env.DB_B.batch(stmts);

      // mark applied on the primary (direct statement — bookkeeping, not captured)
      for (const ids of chunk(events.map((e) => e.id), 40)) {
        await primary.prepare(
          `UPDATE replication_outbox SET applied_at = datetime('now') WHERE id IN (${ids.map(() => "?").join(",")})`
        ).bind(...ids).run();
      }
      await primary.prepare(
        `INSERT INTO replication_state(id, last_applied_seq, last_success_at, consecutive_failures, updated_at)
         VALUES(1, ?, datetime('now'), 0, datetime('now'))
         ON CONFLICT(id) DO UPDATE SET last_applied_seq=MAX(replication_state.last_applied_seq, excluded.last_applied_seq),
           last_success_at=excluded.last_success_at, last_error=NULL, consecutive_failures=0,
           updated_at=excluded.updated_at`
      ).bind(seq).run();

      applied += events.length;
      lastSeq = Math.max(lastSeq, seq);
    } catch (err) {
      failed += events.length;
      lastError = err?.message || String(err);
      try {
        await primary.prepare(
          `INSERT INTO replication_state(id, last_error, last_error_at, consecutive_failures, updated_at)
           VALUES(1, ?, datetime('now'), 1, datetime('now'))
           ON CONFLICT(id) DO UPDATE SET last_error=excluded.last_error,
             last_error_at=excluded.last_error_at,
             consecutive_failures=replication_state.consecutive_failures + 1,
             updated_at=excluded.updated_at`
        ).bind(lastError).run();
      } catch { /* status will still show the failure via the return value */ }
      break; // keep ordering: stop this drain, retry on the next trigger
    }
  }

  // prune applied events older than 7 days (keeps the outbox small; the
  // watermark + audit live in replication_state, not in the event rows)
  try {
    await primary.prepare(
      "DELETE FROM replication_outbox WHERE applied_at IS NOT NULL AND applied_at < datetime('now','-7 day')").run();
  } catch { /* non-fatal */ }

  const stillPending = await firstS(primary,
    "SELECT COUNT(*) AS n FROM replication_outbox WHERE applied_at IS NULL AND op != 'unknown'");
  return {
    applied, failed, pending: stillPending?.n || 0, last_seq: lastSeq,
    last_error: lastError, stopped_early: failed > 0,
  };
}

/* ------------------------------------------------------------------- status */

/** Accurate replication status — never infers success from bindings alone. */
export async function syncStatus(env) {
  const out = {
    mode: "transactional outbox (primary → secondary, state-based apply)",
    configured: { primary: !!env.DB_A, secondary: !!env.DB_B, archive: !!env.ARCHIVE },
    reachable: { primary: false, secondary: false },
  };
  if (!env.DB_A) return out;
  const primary = env.DB_A.withSession("first-unconstrained");
  try {
    await ensureSchemaPrimary(primary);
    out.reachable.primary = true;
  } catch (err) {
    out.error = `primary unreachable: ${err?.message || err}`;
    return out;
  }

  const counts = await firstS(primary,
    `SELECT COUNT(*) AS total,
            SUM(CASE WHEN applied_at IS NULL AND op != 'unknown' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN applied_at IS NULL AND op = 'unknown' THEN 1 ELSE 0 END) AS unreplicated
     FROM replication_outbox`);
  const oldest = await firstS(primary,
    "SELECT created_at FROM replication_outbox WHERE applied_at IS NULL AND op != 'unknown' ORDER BY id LIMIT 1");
  const state = await firstS(primary, "SELECT * FROM replication_state WHERE id = 1");
  const recentUnknown = await allS(primary,
    "SELECT id, table_name, note, created_at FROM replication_outbox WHERE op = 'unknown' AND applied_at IS NULL ORDER BY id DESC LIMIT 5");

  out.pending = counts?.pending || 0;
  out.unreplicated = { count: counts?.unreplicated || 0, recent: recentUnknown };
  out.oldest_pending_at = oldest?.created_at || null;
  out.lag_seconds = oldest?.created_at
    ? Math.max(0, Math.round((Date.now() - Date.parse(oldest.created_at + "Z")) / 1000)) : 0;
  out.last_applied_seq = state?.last_applied_seq || 0;
  out.last_success_at = state?.last_success_at || null;
  out.needs_reconcile = !state?.reconciled_at;
  out.last_failure = state?.last_error
    ? { error: state.last_error, at: state.last_error_at, consecutive: state.consecutive_failures }
    : null;

  if (env.DB_B) {
    try {
      await env.DB_B.prepare("SELECT 1").run();
      out.reachable.secondary = true;
      const sec = await firstS(env.DB_B, "SELECT * FROM replication_state WHERE id = 1");
      out.secondary_watermark = sec
        ? { last_applied_seq: sec.last_applied_seq || 0, at: sec.last_applied_at || null } : null;
      if (sec && (sec.last_applied_seq || 0) < (state?.last_applied_seq || 0)) {
        out.secondary_watermark.behind_by = (state.last_applied_seq || 0) - (sec.last_applied_seq || 0);
      }
    } catch (err) {
      out.reachable.secondary = false;
      out.secondary_error = err?.message || String(err);
    }
  }

  out.caught_up = out.reachable.secondary
    && out.pending === 0 && out.unreplicated.count === 0 && !out.last_failure;
  return out;
}

/* ---------------------------------------------------------------- reconcile */

/**
 * One-time (or repair) full copy: upsert every primary row into the
 * secondary, chunked. With prune=true also delete secondary rows that no
 * longer exist on the primary. Used to seed the secondary after the initial
 * import and to recover from sustained divergence.
 */
export async function reconcile(env, { prune = false, table: only = null, budgetMs = 25000 } = {}) {
  if (!env.DB_A || !env.DB_B) return { skipped: true, reason: "both bindings required" };
  const primary = env.DB_A.withSession("first-unconstrained");
  await ensureSchemaPrimary(primary);
  await ensureSchemaSecondary(env.DB_B);
  const deadline = Date.now() + budgetMs;
  const report = { tables: {}, copied: 0, deleted: 0, complete: true };

  for (const [table, pks] of Object.entries(PKS)) {
    if (only && table !== only) continue;
    if (Date.now() > deadline) { report.complete = false; break; }
    const rep = { copied: 0, deleted: 0 };
    const keyOf = (row) => JSON.stringify(pks.map((p) => row[p]));
    const primaryKeys = new Set();

    if (pks.length === 1 && pks[0] === "id") {
      let lastId = 0;
      for (;;) {
        const rows = await allS(primary, `SELECT * FROM ${table} WHERE id > ? ORDER BY id LIMIT 500`, [lastId]);
        if (!rows.length) break;
        for (const part of chunk(rows, 50)) {
          const stmts = part.map((row) => {
            primaryKeys.add(keyOf(row));
            const cols = Object.keys(row);
            const upd = cols.filter((c) => !pks.includes(c));
            return env.DB_B.prepare(
              `INSERT INTO ${table} (${cols.join(",")}) VALUES (${cols.map(() => "?").join(",")})
               ON CONFLICT(${pks.join(",")}) DO UPDATE SET ${upd.map((c) => `${c}=excluded.${c}`).join(",")}`
            ).bind(...cols.map((c) => row[c]));
          });
          await env.DB_B.batch(stmts);
          rep.copied += part.length;
        }
        lastId = rows[rows.length - 1].id;
        if (rows.length < 500) break;
      }
    } else {
      const rows = await allS(primary, `SELECT * FROM ${table}`);
      for (const part of chunk(rows, 50)) {
        const stmts = part.map((row) => {
          primaryKeys.add(keyOf(row));
          const cols = Object.keys(row);
          const upd = cols.filter((c) => !pks.includes(c));
          return env.DB_B.prepare(
            `INSERT INTO ${table} (${cols.join(",")}) VALUES (${cols.map(() => "?").join(",")})
             ON CONFLICT(${pks.join(",")}) DO UPDATE SET ${upd.map((c) => `${c}=excluded.${c}`).join(",")}`
          ).bind(...cols.map((c) => row[c]));
        });
        await env.DB_B.batch(stmts);
        rep.copied += part.length;
      }
    }

    if (prune) {
      const secRows = await allS(env.DB_B,
        `SELECT ${pks.join(",")} AS k0${pks.length > 1 ? "," + pks.slice(1).map((_, i) => `k${i + 1}`).join(",") : ""} FROM ${table}`);
      const extras = secRows.filter((r) => !primaryKeys.has(JSON.stringify(pks.map((_, i) => r[`k${i}`]))));
      for (const part of chunk(extras, 50)) {
        await env.DB_B.batch(part.map((r) => env.DB_B.prepare(
          `DELETE FROM ${table} WHERE ${pks.map((p) => `${p} = ?`).join(" AND ")}`
        ).bind(...pks.map((_, i) => r[`k${i}`]))));
        rep.deleted += part.length;
      }
    }
    report.tables[table] = rep;
    report.copied += rep.copied;
    report.deleted += rep.deleted;
  }

  if (report.complete) {
    await primary.prepare(
      `INSERT INTO replication_state(id, reconciled_at, updated_at) VALUES(1, datetime('now'), datetime('now'))
       ON CONFLICT(id) DO UPDATE SET reconciled_at=excluded.reconciled_at, updated_at=excluded.updated_at`
    ).run();
  }
  return report;
}
