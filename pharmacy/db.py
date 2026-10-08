"""SQLite access layer (thread-local connection, schema bootstrap)."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from .config import BACKUP_DIR, DATA_DIR, DB_PATH, DATASET_DIR, UPLOAD_DIR

# intentional re-exports: the package accesses paths via db (seed.py, ingest.py)
__all__ = ["BACKUP_DIR", "DATA_DIR", "DB_PATH", "DATASET_DIR", "UPLOAD_DIR"]

_local = threading.local()

SCHEMA = """
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
  created_at TEXT,
  UNIQUE(drug_id, batch_no, received_date)
);
CREATE TABLE IF NOT EXISTS sales(
  id INTEGER PRIMARY KEY,
  txn_id TEXT,
  date TEXT,  -- nullable: rows with missing sale date are kept for totals
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
CREATE TABLE IF NOT EXISTS reorders(
  id INTEGER PRIMARY KEY,
  drug_id INTEGER NOT NULL REFERENCES drugs(id),
  supplier_id INTEGER,
  qty INTEGER, due_date TEXT, reason TEXT,
  status TEXT DEFAULT 'suggested',
  source TEXT DEFAULT 'ai',
  notes TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY,
  supplier_id INTEGER REFERENCES suppliers(id),
  channel TEXT DEFAULT 'email',
  subject TEXT, body TEXT,
  status TEXT DEFAULT 'draft',
  related_type TEXT, related_id INTEGER,
  created_at TEXT, sent_at TEXT
);
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
CREATE TABLE IF NOT EXISTS forecasts(
  id INTEGER PRIMARY KEY,
  drug_id INTEGER NOT NULL REFERENCES drugs(id),
  created_at TEXT, model TEXT,
  mape REAL, mae REAL, wmape REAL, rmse REAL,
  baseline_model TEXT, baseline_wmape REAL,
  avg_daily REAL, metrics TEXT, payload TEXT,
  is_current INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS chat_log(
  id INTEGER PRIMARY KEY,
  role TEXT, message TEXT, meta TEXT, created_at TEXT
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
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
);
"""

# Query-path indexes beyond the schema's uniques (production hot reads)
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_sales_drug_date ON sales(drug_id, date);
CREATE INDEX IF NOT EXISTS idx_sales_daily_date ON sales_daily(date);
CREATE INDEX IF NOT EXISTS idx_batches_drug ON batches(drug_id);
CREATE INDEX IF NOT EXISTS idx_batches_expiry ON batches(expiry_date);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);
CREATE INDEX IF NOT EXISTS idx_waste_status ON waste(status);
CREATE INDEX IF NOT EXISTS idx_reorders_status ON reorders(status);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status);
CREATE INDEX IF NOT EXISTS idx_quarantined_upload ON quarantined(upload_id);
CREATE INDEX IF NOT EXISTS idx_forecasts_current ON forecasts(drug_id, is_current);
CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchases(date_received);
CREATE INDEX IF NOT EXISTS idx_chat_log_created ON chat_log(created_at);
CREATE INDEX IF NOT EXISTS idx_batches_shelf ON batches(shelf_id);
CREATE INDEX IF NOT EXISTS idx_shelf_tasks_status ON shelf_tasks(status, kind);
"""


DEFAULT_SETTINGS = {
    "pharmacy_name": "HealthFirst Pharmacy",
    "service_level": "0.95",
    "review_period_days": "7",
    "expiry_critical_days": "30",
    "expiry_warning_days": "90",
    "overstock_days": "180",
    "pack_size": "10",
    "forecast_horizon_days": "30",
    "llm_model": "",
}


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = connect()
        _local.conn = c
    return c


def init_db() -> None:
    c = conn()
    c.executescript(SCHEMA)
    # lightweight migration for databases created before the shelf layer existed
    # (must run before INDEXES, which references batches.shelf_id)
    batch_cols = {r[1] for r in c.execute("PRAGMA table_info(batches)")}
    if "shelf_id" not in batch_cols:
        c.execute("ALTER TABLE batches ADD COLUMN shelf_id INTEGER REFERENCES shelves(id)")
    c.executescript(INDEXES)
    now = datetime.now().isoformat(timespec="seconds")
    for k, v in DEFAULT_SETTINGS.items():
        c.execute(
            "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES(?,?,?)",
            (k, v, now),
        )
    c.commit()


def backup_to(dest: Path | None = None) -> Path:
    """Online SQLite backup (safe to run while the server keeps serving)."""
    dest = dest or BACKUP_DIR / f"pharmacy-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(dest)
    try:
        conn().backup(target)
    finally:
        target.close()
    return dest


def query(sql: str, params=()) -> list[dict]:
    rows = conn().execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def one(sql: str, params=()) -> dict | None:
    row = conn().execute(sql, params).fetchone()
    return dict(row) if row else None


_bulk = threading.local()


@contextmanager
def bulk():
    """Defer commits for bulk imports (much faster than commit-per-row)."""
    prev = getattr(_bulk, "on", False)
    _bulk.on = True
    try:
        yield
        conn().commit()
    finally:
        _bulk.on = prev
        if not prev:
            conn().commit()


def execute(sql: str, params=()) -> int:
    c = conn()
    cur = c.execute(sql, params)
    if not getattr(_bulk, "on", False):
        c.commit()
    return cur.lastrowid


def executemany(sql: str, seq) -> None:
    c = conn()
    c.executemany(sql, seq)
    if not getattr(_bulk, "on", False):
        c.commit()


def scalar(sql: str, params=(), default=0):
    row = conn().execute(sql, params).fetchone()
    if row is None or row[0] is None:
        return default
    return row[0]


def settings() -> dict:
    return {r["key"]: r["value"] for r in query("SELECT key, value FROM settings")}


def get_setting(key: str, default=None):
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value) -> None:
    execute(
        "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, str(value), datetime.now().isoformat(timespec="seconds")),
    )


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today() -> date:
    return date.today()


def jdump(obj) -> str:
    return json.dumps(obj, default=str)


def jload(text, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default
