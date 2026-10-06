/**
 * Monthly archive → R2.
 *
 * Runs on the cron trigger `5 0 1 * *` (00:05 UTC on the 1st of every month),
 * or on demand via POST /api/archive/run.
 *
 * Strategy: read every table from the PRIMARY database (bounded batches, so a
 * big `sales` table never blows the Worker memory limit) and write a single
 * JSON snapshot per month into the ARCHIVE R2 bucket:
 *
 *   archives/pharmacy-YYYY-MM.json
 *
 * Each snapshot embeds row counts per table + the D1 bookmark it captured, so
 * the archive is provably a consistent cut of the two synced databases. Old
 * snapshots are retained for 24 months (configurable via RETENTION_MONTHS).
 */
const TABLES = ["suppliers", "drugs", "shelves", "users", "batches", "sales",
  "purchases", "sales_daily", "alerts", "reorders", "notifications", "waste",
  "returns", "uploads", "quarantined", "shelf_tasks", "settings"];
const RETENTION_MONTHS = 24;
const CHUNK = 5_000; // rows per query batch

export async function handleArchive(env) {
  // R2 must be provisioned; skip gracefully (with a clear reason) until it is.
  // One-time activation: Cloudflare Dashboard → R2 → “Purchase R2” (free tier
  // includes 10 GB). Until then the rest of the platform runs unaffected.
  if (!env.ARCHIVE) {
    return { ok: false, skipped: true, reason: "R2 ARCHIVE binding not available — enable R2 on the account and redeploy." };
  }
  const session = env.DB_A.withSession("first-unconstrained");
  const period = new Date().toISOString().slice(0, 7); // YYYY-MM
  const key = `archives/pharmacy-${period}.json`;
  const snapshot = {
    period, generated_at: new Date().toISOString(),
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

  try {
    await env.ARCHIVE.put(key, JSON.stringify(snapshot), {
      httpMetadata: { contentType: "application/json" },
      customMetadata: { retentionMonths: String(RETENTION_MONTHS) },
    });
  } catch (err) {
    return { ok: false, period, key, error: String(err) };
  }

  // audit row inside D1 (replicates to both databases)
  await session.prepare(
    "INSERT INTO archive_runs(period, key, tables_count, rows_count, size_bytes, status, created_at) VALUES(?,?,?,?,?,?,datetime('now'))"
  ).bind(period, key, Object.keys(snapshot.tables).length, snapshot.row_total,
    JSON.stringify(snapshot).length, ok ? "ok" : "partial").run();
  await session.prepare(
    "INSERT INTO sync_bookmarks(name, bookmark, updated_at) VALUES('archive',?,datetime('now')) " +
    "ON CONFLICT(name) DO UPDATE SET bookmark=excluded.bookmark, updated_at=excluded.updated_at"
  ).bind(snapshot.bookmark || "").run();

  await enforceRetention(env, period);
  return {
    ok, period, key, tables: Object.keys(snapshot.tables).length,
    rows: snapshot.row_total, bytes: JSON.stringify(snapshot).length,
    bookmark: snapshot.bookmark, retention_months: RETENTION_MONTHS,
  };
}

/** Delete snapshots older than the retention window (archive updates monthly). */
async function enforceRetention(env, currentPeriod) {
  const cutoff = new Date();
  cutoff.setMonth(cutoff.getMonth() - RETENTION_MONTHS);
  const cutoffPeriod = cutoff.toISOString().slice(0, 7);
  const listed = await env.ARCHIVE.list({ prefix: "archives/" });
  for (const obj of listed.objects) {
    const m = obj.key.match(/pharmacy-(\d{4}-\d{2})\.json$/);
    if (m && m[1] < cutoffPeriod) await env.ARCHIVE.delete(obj.key);
  }
  return { cutoff: cutoffPeriod, current: currentPeriod };
}
