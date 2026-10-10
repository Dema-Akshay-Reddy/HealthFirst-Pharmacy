-- Smart Pharmacy Inventory — D1 schema
--
-- Two independent D1 databases are used (they cannot be read-replicated to
-- each other, so the Worker replicates explicitly — see src/replication.js):
--
--   pharmacy-primary  (DB_A)  = authoritative WRITER
--   pharmacy-secondary (DB_B) = synchronized read copy, kept in sync by the
--                               transactional outbox (primary → secondary)
--
-- The outbox/state tables below are created on BOTH databases (shared DDL).
-- sync_bookmarks, replication_outbox, replication_state are NEVER replicated —
-- they are per-database bookkeeping. All application tables are replicated.

PRAGMA foreign_keys = ON;

-- ── Sync bookmarks (per-database) ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sync_bookmarks(
  name    TEXT PRIMARY KEY,
  bookmark TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- ── Transactional outbox (primary capture) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS replication_outbox(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  txn        TEXT,              -- request txn — groups multi-table events
  table_name TEXT NOT NULL,     -- which table the event describes
  op         TEXT NOT NULL,     -- upsert | delete | unknown
  key_json   TEXT,              -- JSON array of primary-key values
  note       TEXT,              -- free text (used for "unknown" flags)
  created_at TEXT DEFAULT (datetime('now')),
  applied_at TEXT               -- NULL until the secondary has applied it
);
CREATE INDEX IF NOT EXISTS idx_replication_outbox_pending
  ON replication_outbox(applied_at, id);

-- ── Replication watermark / status (primary + secondary) ───────────────────
CREATE TABLE IF NOT EXISTS replication_state(
  id                 INTEGER PRIMARY KEY CHECK(id = 1),
  last_applied_seq   INTEGER DEFAULT 0,   -- last outbox event id applied
  last_applied_at    TEXT,
  last_success_at    TEXT,
  last_error         TEXT,
  last_error_at      TEXT,
  consecutive_failures INTEGER DEFAULT 0,
  reconciled_at      TEXT,                 -- set by reconcile() after full sync
  updated_at         TEXT
);

-- ── Monthly archive audit (independent of replication) ─────────────────────
CREATE TABLE IF NOT EXISTS archive_runs(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  rows_affected INTEGER DEFAULT 0,
  row_total  INTEGER DEFAULT 0,
  tables     TEXT,                      -- JSON: per-table row counts
  bookmark   TEXT,                       -- primary bookmark at snapshot time
  error      TEXT,                       -- set on failure
  created_at TEXT DEFAULT (datetime('now'))
);

-- ── Archive retention: delete completed runs older than 24 months ───────────
-- (retained by the scheduled cleanup in src/archive.js)

-- ── Application tables (same DDL on both databases) ─────────────────────────
-- (suppliers, drugs, shelves, batches, sales, purchases, bills, bill_items,
--  sales_daily, users, alerts, reorders, notifications, waste, returns,
--  uploads, quarantined, shelf_tasks, settings — see the original schema.sql)

-- FK-safe replication order (parents before children for upserts; reverses
-- for deletes). Keep this in sync with replication.js TABLE_ORDER.
