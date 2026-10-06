-- Smart Pharmacy Inventory — Cloudflare D1 schema
-- Mirrors pharmacy/db.py (SQLite) so the Worker and the Python app share one
-- data shape. D1 is SQLite-compatible; WAL/threading pragmas don't apply.

CREATE TABLE IF NOT EXISTS suppliers(
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  contact_name TEXT, email TEXT, phone TEXT,
  lead_time_days INTEGER DEFAULT 7,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS drugs(
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  norm_name TEXT UNIQUE NOT NULL,
  generic TEXT, category TEXT, form TEXT, schedule TEXT, storage TEXT,
  unit TEXT DEFAULT 'unit',
  mrp REAL DEFAULT 0, cost REAL DEFAULT 0,
  lead_time_days INTEGER DEFAULT 7,
  reorder_point REAL,
  supplier_id INTEGER,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS shelves(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  zone TEXT NOT NULL DEFAULT 'reserve',
  note TEXT
);

CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'pharmacist',
  name TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS batches(
  id INTEGER PRIMARY KEY,
  drug_id INTEGER NOT NULL REFERENCES drugs(id),
  supplier_id INTEGER REFERENCES suppliers(id),
  batch_no TEXT NOT NULL,
  expiry_date TEXT,
  qty_received INTEGER NOT NULL DEFAULT 0,
  qty_remaining INTEGER NOT NULL DEFAULT 0,
  unit_cost REAL DEFAULT 0,
  received_date TEXT,
  purchase_ref TEXT,
  source TEXT,
  shelf_id INTEGER REFERENCES shelves(id),
  created_at TEXT,
  UNIQUE(drug_id, batch_no, received_date)
);
CREATE INDEX IF NOT EXISTS idx_batches_drug ON batches(drug_id);
CREATE INDEX IF NOT EXISTS idx_batches_expiry ON batches(expiry_date);
CREATE INDEX IF NOT EXISTS idx_batches_shelf ON batches(shelf_id);

CREATE TABLE IF NOT EXISTS sales(
  id INTEGER PRIMARY KEY,
  txn_id TEXT,
  date TEXT NOT NULL,
  drug_id INTEGER NOT NULL REFERENCES drugs(id),
  batch_id INTEGER REFERENCES batches(id),
  qty INTEGER NOT NULL,
  unit_price REAL DEFAULT 0,
  total REAL DEFAULT 0,
  source TEXT,
  created_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_uniq ON sales(txn_id, date, drug_id);
CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(date);
CREATE INDEX IF NOT EXISTS idx_sales_drug ON sales(drug_id);
CREATE INDEX IF NOT EXISTS idx_sales_drug_date ON sales(drug_id, date);

CREATE TABLE IF NOT EXISTS purchases(
  id INTEGER PRIMARY KEY,
  purchase_id TEXT,
  date_received TEXT,
  drug_id INTEGER NOT NULL REFERENCES drugs(id),
  batch_id INTEGER REFERENCES batches(id),
  qty INTEGER, unit_cost REAL, total REAL,
  supplier_id INTEGER REFERENCES suppliers(id),
  source TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchases(date_received);

CREATE TABLE IF NOT EXISTS sales_daily(
  drug_id INTEGER NOT NULL,
  date TEXT NOT NULL,
  qty INTEGER NOT NULL DEFAULT 0,
  revenue REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(drug_id, date)
);

CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY,
  atype TEXT NOT NULL,
  severity TEXT NOT NULL,
  drug_id INTEGER, batch_id INTEGER,
  title TEXT NOT NULL, message TEXT,
  details TEXT,
  dedup_key TEXT UNIQUE,
  status TEXT DEFAULT 'active',
  created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);

CREATE TABLE IF NOT EXISTS reorders(
  id INTEGER PRIMARY KEY,
  drug_id INTEGER NOT NULL REFERENCES drugs(id),
  supplier_id INTEGER,
  qty INTEGER, due_date TEXT, reason TEXT,
  status TEXT DEFAULT 'suggested',
  source TEXT DEFAULT 'ai',
  notes TEXT, created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reorders_status ON reorders(status);

CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY,
  supplier_id INTEGER REFERENCES suppliers(id),
  channel TEXT DEFAULT 'email',
  subject TEXT, body TEXT,
  status TEXT DEFAULT 'draft',
  related_type TEXT, related_id INTEGER,
  created_at TEXT, sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status);

CREATE TABLE IF NOT EXISTS waste(
  id INTEGER PRIMARY KEY,
  drug_id INTEGER NOT NULL REFERENCES drugs(id),
  batch_id INTEGER REFERENCES batches(id),
  supplier_id INTEGER,
  qty INTEGER NOT NULL,
  reason TEXT NOT NULL,
  unit_cost REAL DEFAULT 0,
  value REAL DEFAULT 0,
  status TEXT DEFAULT 'pending',
  note TEXT, created_at TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS returns(
  id INTEGER PRIMARY KEY,
  supplier_id INTEGER REFERENCES suppliers(id),
  reference TEXT, qty INTEGER DEFAULT 0, value REAL DEFAULT 0,
  waste_ids TEXT, status TEXT DEFAULT 'requested',
  note TEXT, created_at TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS uploads(
  id INTEGER PRIMARY KEY,
  filename TEXT, kind TEXT,
  rows_total INTEGER DEFAULT 0, rows_accepted INTEGER DEFAULT 0,
  rows_rejected INTEGER DEFAULT 0, issues TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS quarantined(
  id INTEGER PRIMARY KEY,
  upload_id INTEGER, kind TEXT,
  record TEXT, issues TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS shelf_tasks(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  drug_id INTEGER REFERENCES drugs(id),
  batch_id INTEGER REFERENCES batches(id),
  from_shelf_id INTEGER REFERENCES shelves(id),
  to_shelf_id INTEGER REFERENCES shelves(id),
  reason TEXT, status TEXT DEFAULT 'pending',
  dedup_key TEXT UNIQUE,
  created_at TEXT, done_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_shelf_tasks_status ON shelf_tasks(status, kind);

CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
);

-- sync bookkeeping: the replica's watermark + the archive's audit trail
CREATE TABLE IF NOT EXISTS sync_bookmarks(
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  bookmark TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS archive_runs(
  id INTEGER PRIMARY KEY,
  period TEXT,
  key TEXT,
  tables_count INTEGER,
  rows_count INTEGER,
  size_bytes INTEGER,
  status TEXT DEFAULT 'ok',
  created_at TEXT
);
