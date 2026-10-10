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
import { planCapture, capturedWrite, ensureSchemaPrimary } from "./replication.js";

/** request-scoped replication metadata, keyed by session object */
const META = new WeakMap();

/** A request-scoped D1 session bound to the caller's bookmark. */
export function makeSession(env, request) {
  const bookmark = request.headers.get("X-D1-Bookmark")
    || new URL(request.url).searchParams.get("bookmark")
    || "first-unconstrained"; // first request: pick the freshest instance
  const session = env.DB_A.withSession(bookmark);
  META.set(session, { txn: crypto.randomUUID(), wrote: false });
  return session;
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
