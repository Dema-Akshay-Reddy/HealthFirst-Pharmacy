#!/usr/bin/env python3
"""Export the Python app's seeded SQLite DB to D1-compatible SQL chunks.

The Python app (pharmacy/seed.py + ingest) is the single source of truth for
derived state — FEFO qty_remaining, sales_daily, waste ledger. This script
serialises its verified tables into chunked `wrangler d1 execute --file`
inputs so the Cloudflare deployment is reseeded from exactly the same data.

Usage:
  python tools/export_d1_seed.py ../data/pharmacy.db .seed-parts

Then (from cloudflare/, after schema.sql on a wiped DB):
  for f in .seed-parts/dataset-*.sql; do
    npx wrangler d1 execute pharmacy-primary --remote --file="$f" || exit 1
  done

Settings rows are exported except `session_secret` (a local secret; the
Worker generates its own). Users are intentionally NOT exported: the Worker
seeds them from ADMIN_PASSWORD / PHARMACIST_PASSWORD env secrets.
"""
import argparse
import sqlite3
from pathlib import Path

# (table, columns) in dependency order; matches cloudflare/schema.sql.
TABLES = [
    ("suppliers", ["id", "name", "contact_name", "email", "phone",
                   "lead_time_days", "created_at"]),
    ("drugs", ["id", "name", "norm_name", "generic", "category", "form",
               "schedule", "storage", "unit", "mrp", "cost", "lead_time_days",
               "reorder_point", "supplier_id", "created_at"]),
    ("shelves", ["id", "code", "zone", "note"]),
    ("settings", ["key", "value", "updated_at"]),
    ("batches", ["id", "drug_id", "supplier_id", "batch_no", "expiry_date",
                 "qty_received", "qty_remaining", "unit_cost", "received_date",
                 "purchase_ref", "source", "created_at", "shelf_id"]),
    ("purchases", ["id", "purchase_id", "date_received", "drug_id", "batch_id",
                   "qty", "unit_cost", "total", "supplier_id", "source",
                   "created_at"]),
    ("sales", ["id", "txn_id", "date", "drug_id", "batch_id", "qty",
               "unit_price", "total", "source", "created_at"]),
    ("sales_daily", ["drug_id", "date", "qty", "revenue"]),
    ("waste", ["id", "drug_id", "batch_id", "supplier_id", "qty", "reason",
               "unit_cost", "value", "status", "note", "created_at",
               "updated_at"]),
]

MAX_CHUNK_BYTES = 600_000  # keep each --file comfortably under API limits
ROWS_PER_INSERT = 200


def sql_literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value).replace("'", "''")
    return f"'{text}'"


def export(db_path: Path, out_dir: Path) -> list[Path]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("dataset-*.sql"):
        stale.unlink()

    chunks: list[Path] = []
    buf: list[str] = []
    size = 0

    def flush():
        if not buf:
            return
        idx = len(chunks) + 1
        path = out_dir / f"dataset-{idx:02d}.sql"
        path.write_text("\n".join(buf) + "\n", encoding="utf-8")
        chunks.append(path)
        buf.clear()
        nonlocal size
        size = 0

    for table, cols in TABLES:
        where = ""
        if table == "settings":
            where = " WHERE key <> 'session_secret'"
        rows = con.execute(f"SELECT {', '.join(cols)} FROM {table}{where}").fetchall()
        for start in range(0, len(rows), ROWS_PER_INSERT):
            batch = rows[start:start + ROWS_PER_INSERT]
            values = ",\n".join(
                "(" + ", ".join(sql_literal(r[c]) for c in cols) + ")" for r in batch)
            stmt = (f"INSERT OR REPLACE INTO {table} ({', '.join(cols)})\n"
                    f"VALUES\n{values};\n")
            if size and size + len(stmt) > MAX_CHUNK_BYTES:
                flush()
            buf.append(stmt)
            size += len(stmt)
    flush()
    con.close()

    counts = {t: sqlite3.connect(db_path).execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t, _ in TABLES}
    print("exported rows:", counts)
    print("chunks:", [p.name for p in chunks])
    return chunks


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("db", type=Path)
    ap.add_argument("out_dir", type=Path)
    args = ap.parse_args()
    export(args.db, args.out_dir)
