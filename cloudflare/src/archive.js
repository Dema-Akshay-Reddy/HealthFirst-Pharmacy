/**
 * Monthly archive → R2.
 *
 * Runs on the cron trigger `5 0 1 * *` (00:05 UTC on the 1st of every month),
 * or on demand via POST /api/archive/run.
 *
 * Strategy: read every table from the PRIMARY database (bounded batches, so a
 * big `sales` table never blows the Worker memory limit or D1's row-read
 * window) and write a single JSON snapshot per month into the ARCHIVE R2
 * bucket:
 *
 *   archives/pharmacy-YYYY-MM.json
 *
 * Snapshot consistency: reads run in one D1 session, which provides
 * sequential consistency — later table reads are never older than earlier
 * ones. Concurrent writes during the run can still land between table reads,
 * so the snapshot records `started_at`/`finished_at` plus the session
 * bookmark of the cut; it is a point-in-time-ish image, not a transactional
 * export. This limitation is inherent to D1 (no cross-query snapshots) and
 * is documented rather than hidden. Archive progress is tracked separately
 * from replication progress (sync_bookmarks name 'archive' + archive_runs).
 *
 * Retention: snapshots older than 24 months are deleted on each successful
 * run (list() is paginated — R2 pages at 1000 objects).
 */
import { write } from "./db.js";

const TABLES = ["suppliers", "drugs", "shelves", "users", "batches", "sales",
  "purchases", "sales_daily", "alerts", "reorders", "notifications", "waste",
  "returns", "uploads", "quarantined", "shelf_tasks", "settings", "bills", "bill_items"];
const RETENTION_MONTHS = 24;
const CHUNK = 5_000; // rows per query batch

export async function handleArchive(env) {
  // R2 must be provisioned; skip gracefully (with a clear reason) until it is.
  // One-time activation: Cloudflare Dashboard → R2 → “Purchase R2” (free tier
  // includes 10 GB). Until then the rest of the platform runs unaffected.
  if (!env.ARCHIVE) {
    return { ok: false, skipped: true, reason: "R2 ARCHIVE binding not available — enable R2 on the account and redeploy." };
  }
  const session = env.DB_A.withSession("first-primary");
  const period = new Date().toISOString().slice(0, 7); // YYYY-MM
  const key = `archives/pharmacy-${period}.json`;
  const startedAt = new Date().toISOString();
  const snapshot = {
    period, generated_at: startedAt, started_at: startedAt, finished_at: null,
    consistency: "sequential session cut (D1 has no transactional snapshots)",
    bookmark: null, tables: {}, row_total: 0,
  };

  // tables without a numeric `id` are small config/rollup tables — single read
  const ID_LESS = new Set(["settings", "sales_daily"]);
  let ok = true;
  for (const table of TABLES) {
    try {
      const rows = [];
      if (ID_LESS.has(table)) {
        const { results } = await session.prepare(`SELECT * FROM ${table}`).all();
        rows.push(...(results || []));
      } else {
        let lastId = 0;
        for (;;) {
          const { results } = await session.prepare(
            `SELECT * FROM ${table} WHERE id > ? ORDER BY id LIMIT ?`
          ).bind(lastId, CHUNK).all();
          // the bookmark is only populated once a query has executed — capture
          // it here so the snapshot records the exact sync cut it read from
          if (!snapshot.bookmark) snapshot.bookmark = session.getBookmark();
          rows.push(...(results || []));
          if (!results || results.length < CHUNK) break;
          lastId = results[results.length - 1].id;
        }
      }
      snapshot.tables[table] = { count: rows.length, rows };
      snapshot.row_total += rows.length;
    } catch (err) {
      snapshot.tables[table] = { error: String(err) };
      ok = false;
    }
  }
  snapshot.finished_at = new Date().toISOString();

  try {
    await env.ARCHIVE.put(key, JSON.stringify(snapshot), {
      httpMetadata: { contentType: "application/json" },
      customMetadata: { retentionMonths: String(RETENTION_MONTHS) },
    });
  } catch (err) {
    // upload failed → record the FAILED run too (audit trail must show
    // attempts, not only successes), then surface the error
    await recordAudit(session, {
      period, key, tables: Object.keys(snapshot.tables).length,
      rows: snapshot.row_total, bytes: 0, status: "failed", error: String(err),
    }).catch(() => {});
    return { ok: false, period, key, error: String(err) };
  }

  // audit row inside D1 — via write() so it replicates to the secondary
  await recordAudit(session, {
    period, key, tables: Object.keys(snapshot.tables).length,
    rows: snapshot.row_total, bytes: JSON.stringify(snapshot).length,
    status: ok ? "ok" : "partial", error: ok ? null
      : Object.entries(snapshot.tables).filter(([, v]) => v.error).map(([t]) => t).join(","),
  });
  // archive watermark stays primary-local bookkeeping (sync_bookmarks is
  // excluded from replication on purpose)
  await session.prepare(
    "INSERT INTO sync_bookmarks(name, bookmark, updated_at) VALUES('archive',?,datetime('now')) " +
    "ON CONFLICT(name) DO UPDATE SET bookmark=excluded.bookmark, updated_at=excluded.updated_at"
  ).bind(snapshot.bookmark || "").run();

  await enforceRetention(env, period);
  return {
    ok, period, key, tables: Object.keys(snapshot.tables).length,
    rows: snapshot.row_total, bytes: JSON.stringify(snapshot).length,
    bookmark: snapshot.bookmark, started_at: startedAt, finished_at: snapshot.finished_at,
    retention_months: RETENTION_MONTHS,
  };
}

/** Audit the run in D1 through the replicated write path. */
async function recordAudit(session, { period, key, tables, rows, bytes, status, error }) {
  await write(session,
    "INSERT INTO archive_runs(period, key, tables_count, rows_count, size_bytes, status, error, created_at) VALUES(?,?,?,?,?,?,?,datetime('now'))",
    [period, key, tables, rows, bytes, status, error || null]);
}

/** Delete snapshots older than the retention window (archive updates monthly). */
async function enforceRetention(env, currentPeriod) {
  const cutoff = new Date();
  cutoff.setMonth(cutoff.getMonth() - RETENTION_MONTHS);
  const cutoffPeriod = cutoff.toISOString().slice(0, 7);
  let cursor, deleted = 0;
  do {
    const listed = await env.ARCHIVE.list({ prefix: "archives/", cursor });
    for (const obj of listed.objects) {
      const m = obj.key.match(/pharmacy-(\d{4}-\d{2})\.json$/);
      if (m && m[1] < cutoffPeriod) { await env.ARCHIVE.delete(obj.key); deleted++; }
    }
    cursor = listed.truncated ? listed.cursor : undefined;
  } while (cursor);
  return { cutoff: cutoffPeriod, current: currentPeriod, deleted };
}
