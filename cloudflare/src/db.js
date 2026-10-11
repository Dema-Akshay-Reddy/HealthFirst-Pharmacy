/**
 * D1 session + write layer, with transactional-outbox replication capture.
 *
 * Architecture roles:
 *   PRIMARY (DB_A)  = the writable database — every write lands here.
 *   SECONDARY (DB_B)= an independent D1 database kept in sync by the
 *                     transactional outbox in replication.js (DB_A and DB_B
 *                     are separate databases — Cloudflare read replication
 *                     cannot sync two independent databases, so we replicate
 *                     explicitly: capture events inside the same D1 batch as
 *                     the business write, then drain them to DB_B).
 *
 * Network load balancing: Cloudflare's anycast edge routes each user to the
 * nearest colo automatically. The D1 Sessions API bookmark (X-D1-Bookmark)
 * is still threaded through responses so clients keep sequential consistency
 * on the primary.
 */
import { planCapture, capturedWrite, ensureSchemaPrimary, PKS } from "./replication.js";

/** request-scoped replication metadata, keyed by session object */
const META = new WeakMap();

/**
 * A request-scoped D1 session bound to the caller's bookmark.
 *
 * Read routing across the two databases: DB_A (primary) and DB_B (secondary,
 * kept in sync by the transactional outbox) each carry their own daily D1
 * row-read budget. `makeSession(env, request, readReplica = true)` binds the
 * session to DB_B whenever the caller's bookmark allows — a client's own
 * latest write is honored first via the bookmark (read-your-writes), else the
 * request falls back to DB_A. Writes ALWAYS land on DB_A (makeSession is used
 * both for read sessions and write sessions in worker.js — see makeSession). */
export function makeSession(env, request, { readReplica = false } = {}) {
  const bookmark = request.headers.get("X-D1-Bookmark")
    || new URL(request.url).searchParams.get("bookmark")
    || "first-unconstrained"; // first request: pick the freshest instance
  // Database choice: with a concrete client bookmark (the client wrote before)
  // we honor read-your-writes on the PRIMARY. Unconstrained reads may go to
  // the replica when one is configured and replication is not erroring.
  let db = env.DB_A;
  if (readReplica && env.DB_B && bookmark === "first-unconstrained") {
    db = env.DB_B; // read path only; writes are captured on DB_A by write()
  }
  const session = db.withSession(bookmark);
  META.set(session, { txn: crypto.randomUUID(), wrote: false, replica: db === env.DB_B && env.DB_B !== undefined, bookmark });
  return session;
}

/** True when the session is bound to the secondary (read-replica) database. */
export function sessionOnReplica(session) {
  const m = META.get(session);
  return Boolean(m && m.replica);
}

/** Wrap the session so every response carries the latest sync bookmark. */
export function attachBookmark(response, session) {
  try {
    const b = session.getBookmark();
    if (b) response.headers.set("X-D1-Bookmark", b);
  } catch { /* bookmark unavailable — next request starts unconstrained */ }
  return response;
}

/**
 * Read-your-writes helper: run `fn` with the session, return its JSON result
 * plus the bookmark header already applied.
 */
export function jsonResponse(session, data, init = {}) {
  const response = Response.json(data, init);
  return attachBookmark(response, session);
}

/**
 * Write path: executes the statement on the PRIMARY together with its
 * replication outbox event in ONE D1 batch (= one transaction). Either both
 * the write and its event commit, or neither does — the secondary can never
 * silently miss a change. Every call site goes through here.
 */
export async function write(session, sql, params = []) {
  // plan.kind: insert | update | delete → captured atomically with the write;
  // "none" → bookkeeping table (plain write); "unknown" → write still happens
  // but the shape is flagged in the outbox and reported by /api/sync/status.
  const plan = planCapture(sql, params);
  if (plan.kind === "insert" || plan.kind === "update" || plan.kind === "delete") {
    // idempotent no-op after the first call per isolate; guarantees the outbox
    // exists even if the Worker was deployed before the migration ran
    await ensureSchemaPrimary(session);
  }
  const { result, bookmark } = await capturedWrite(session, META.get(session), sql, params, plan);
  return { result, bookmark };
}

/**
 * Atomic multi-statement write: runs ALL statements plus their replication
 * captures in ONE D1 batch (= one transaction). Either everything commits or
 * nothing does — this is how multi-table business workflows (e.g. dispense:
 * batches + sales + bills + bill_items) stay consistent on failure.
 *
 * `ops` is [{ sql, params }]. Returns the D1 result for each business
 * statement (in order), so callers can assert guard changes, e.g. an UPDATE
 * ... WHERE qty_remaining >= ? must report meta.changes > 0.
 */
export async function writeMany(session, ops) {
  if (!ops?.length) return [];
  const meta = META.get(session);
  const stmts = [];
  const businessIdx = []; // position in stmts[] for each business statement
  let hasCaptured = false;
  for (const op of ops) {
    const sql = op.sql, params = op.params || [];
    const plan = planCapture(sql, params);
    if (plan.kind === "insert" && plan.keySources) {
      // pre-capture (pk known from the column list), then the insert
      const keyParams = plan.keySources.map((src) =>
        src.paramIndex !== undefined ? params[src.paramIndex] : src.literal);
      stmts.push(stmt(session,
        `INSERT INTO replication_outbox(txn, table_name, op, key_json)
         VALUES(?, ?, 'upsert', json_array(${PKS[plan.table].map(() => "?").join(",")}))`,
        [meta?.txn ?? null, plan.table, ...keyParams]));
      businessIdx.push(stmts.length);
      stmts.push(stmt(session, sql, params));
      hasCaptured = true;
    } else if (plan.kind === "insert") {
      // post-capture the just-inserted rowid in the same transaction
      businessIdx.push(stmts.length);
      stmts.push(stmt(session, sql, params));
      stmts.push(stmt(session,
        `INSERT INTO replication_outbox(txn, table_name, op, key_json)
         SELECT ?, ?, 'upsert', json_array(rowid) FROM ${plan.table}
         WHERE rowid = last_insert_rowid()`,
        [meta?.txn ?? null, plan.table]));
      hasCaptured = true;
    } else if (plan.kind === "update" || plan.kind === "delete") {
      // pre-capture the keys that the WHERE clause selects, then run it.
      // The WHERE may contain ? placeholders — they are the TAIL of the
      // business statement's params (after the SET-clause placeholders).
      const pks = PKS[plan.table];
      const whereParams = params.slice(plan.whereParamOffset);
      stmts.push(stmt(session,
        `INSERT INTO replication_outbox(txn, table_name, op, key_json)
         SELECT ?, ?, ?, json_array(${pks.join(",")}) FROM ${plan.table} WHERE ${plan.where}`,
        [meta?.txn ?? null, plan.table, plan.kind, ...whereParams]));
      businessIdx.push(stmts.length);
      stmts.push(stmt(session, sql, params));
      hasCaptured = true;
    } else {
      // 'none' | 'unknown': plain write; unknowns are flagged post-batch
      businessIdx.push(stmts.length);
      stmts.push(stmt(session, sql, params));
    }
  }
  if (hasCaptured) await ensureSchemaPrimary(session); // once per isolate
  let results = await session.batch(stmts);
  if (meta) meta.wrote = true; // triggers the post-request outbox drain
  // flag unrecognised shapes so /api/sync/status reports them (never silent)
  for (const op of ops) {
    const plan = planCapture(op.sql, op.params || []);
    if (plan.kind === "unknown") await capturedWrite(session, meta, op.sql, op.params || [], plan);
  }
  return businessIdx.map((i) => results[i]);
}

function stmt(session, sql, params) {
  return params?.length ? session.prepare(sql).bind(...params) : session.prepare(sql);
}

/** True when this request performed at least one captured write. */
export function sessionWrote(session) {
  return !!META.get(session)?.wrote;
}

/** Simple read helper (primary-served, bookmark-ordered). */
export async function all(session, sql, params = []) {
  const stmt = params.length ? session.prepare(sql).bind(...params) : session.prepare(sql);
  const { results } = await stmt.all();
  return results || [];
}

export async function first(session, sql, params = []) {
  const rows = await all(session, sql, params);
  return rows[0] ?? null;
}

export async function scalar(session, sql, params = [], fallback = 0) {
  const row = await first(session, sql, params);
  if (!row) return fallback;
  const v = Object.values(row)[0];
  return (v === null || v === undefined) ? fallback : v;
}

/** Persist the newest bookmark so cross-request consumers can pin to it. */
export async function saveBookmark(session, name = "latest") {
  const b = session.getBookmark();
  if (!b) return;
  await session.prepare(
    "INSERT INTO sync_bookmarks(name, bookmark, updated_at) VALUES(?,?,datetime('now')) " +
    "ON CONFLICT(name) DO UPDATE SET bookmark=excluded.bookmark, updated_at=excluded.updated_at"
  ).bind(name, b).run();
}
