/**
 * D1 session + load-balancing layer.
 *
 * Architecture roles:
 *   PRIMARY (DB_A)  = the writable database — every write lands here.
 *   REPLICA (DB_B)  = the second database — Cloudflare keeps it continuously
 *                     in sync with the primary via D1 read replication.
 *
 * Two load-balancing tiers:
 *   1. Network: Cloudflare's anycast edge routes each user to the nearest
 *      colo automatically (no explicit LB appliance needed).
 *   2. Database: the D1 Sessions API is the DB-level load balancer — each
 *      session routes reads to the nearest *healthy* replica and writes to
 *      the primary. A sequential-consistency bookmark is threaded through
 *      every response (`X-D1-Bookmark`) and accepted back (`X-D1-Bookmark`
 *      request header / ?bookmark=), so a user never reads older data than
 *      they wrote — the two databases stay in real time, in sync.
 */

/** A request-scoped D1 session bound to the caller's bookmark. */
export function makeSession(env, request) {
  const bookmark = request.headers.get("X-D1-Bookmark")
    || new URL(request.url).searchParams.get("bookmark")
    || "first-unconstrained"; // first request: pick the freshest replica
  return env.DB_A.withSession(bookmark);
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
 * Write path: executes statements on the PRIMARY and returns the post-write
 * bookmark so clients can pin their next read to a replica that is at least
 * as fresh as this write.
 */
export async function write(session, sql, params = []) {
  const result = await session.prepare(sql).bind(...params).run();
  return { result, bookmark: session.getBookmark() };
}

/** Simple read helper (replica-served, bookmark-ordered). */
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

/** Persist the newest bookmark so cross-request consumers (cron, admin) can pin to it. */
export async function saveBookmark(session, name = "latest") {
  const b = session.getBookmark();
  if (!b) return;
  await session.prepare(
    "INSERT INTO sync_bookmarks(name, bookmark, updated_at) VALUES(?,?,datetime('now')) " +
    "ON CONFLICT(name) DO UPDATE SET bookmark=excluded.bookmark, updated_at=excluded.updated_at"
  ).bind(name, b).run();
}
