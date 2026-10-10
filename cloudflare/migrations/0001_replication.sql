-- Migration: transactional-outbox replication between pharmacy-primary and
-- pharmacy-secondary (two INDEPENDENT D1 databases).
--
-- Apply to BOTH databases:
--   npx wrangler d1 execute pharmacy-primary   --remote --file=./migrations/0001_replication.sql
--   npx wrangler d1 execute pharmacy-secondary --remote --file=./migrations/0001_replication.sql
--
-- All statements are idempotent (IF NOT EXISTS) and additive: no existing
-- table, column, index, or row is modified or dropped. The Worker also runs
-- this DDL lazily on first use per isolate, so a code-before-migration
-- deployment order cannot break writes.

CREATE TABLE IF NOT EXISTS replication_outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  txn TEXT,
  table_name TEXT NOT NULL,
  op TEXT NOT NULL,
  key_json TEXT,
  note TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  applied_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_replication_outbox_pending
ON replication_outbox(applied_at, id);

CREATE TABLE IF NOT EXISTS replication_state(
  id INTEGER PRIMARY KEY CHECK(id = 1),
  last_applied_seq INTEGER DEFAULT 0,
  last_applied_at TEXT,
  last_success_at TEXT,
  last_error TEXT,
  last_error_at TEXT,
  consecutive_failures INTEGER DEFAULT 0,
  reconciled_at TEXT,
  updated_at TEXT
);

-- NOTE: archive_runs.error (failed/partial run auditing) is added by the
-- Worker's lazy DDL (src/replication.js ensureSchemaPrimary) because SQLite
-- has no ADD COLUMN IF NOT EXISTS and this file must stay re-runnable.
