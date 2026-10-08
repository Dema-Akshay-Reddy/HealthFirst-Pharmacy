"""Seed the platform from the current pharmacy inventory dataset.

Two JSON files (purchases + sales) under data/zenith/. The feed is already
cleaned but contains intentional nulls (missing Batch_Number, missing sales
Date) that the ingestion pipeline handles: surrogate lot ids from Purchase_ID,
null-batch sales counted for demand but excluded from batch stock deduction,
and null-date sales excluded from forecasting but counted in totals with a
data-quality flag.
"""
import sys
from pathlib import Path

from . import alerts, db, forecasting, ingest, shelfops

DATASETS = [
    ("pharmacy_purchases_current.json", "purchases"),
    ("pharmacy_sales_current.json", "sales"),
]


def dataset_files() -> list[tuple[Path, str]]:
    out = []
    for name, kind in DATASETS:
        p = db.DATASET_DIR / name
        if p.exists():
            out.append((p, kind))
    # accept any other json/csv/xlsx dropped into data/zenith
    for p in sorted(db.DATASET_DIR.glob("*")):
        if p.name in {f.name for f, _ in out} or p.suffix.lower() not in (
                ".json", ".csv", ".xlsx", ".xls"):
            continue
        out.append((p, "sales" if "sale" in p.name.lower() else "purchases"))
    return out


def already_seeded() -> bool:
    return db.scalar("SELECT COUNT(*) FROM sales") > 0 or db.scalar("SELECT COUNT(*) FROM batches") > 0


def run(force: bool = False, quiet: bool = False) -> dict:
    db.init_db()
    if already_seeded() and not force:
        return dict(status="already_seeded", sales=db.scalar("SELECT COUNT(*) FROM sales"))

    if force:
        # children first so foreign keys never bite
        for table in ("sales", "purchases", "waste", "returns", "notifications",
                      "reorders", "forecasts", "alerts", "quarantined", "uploads",
                      "chat_log", "sales_daily", "shelf_tasks", "batches"):
            db.execute(f"DELETE FROM {table}")
        db.execute("DELETE FROM drugs")
        db.execute("DELETE FROM suppliers")

    _bootstrap_suppliers()
    shelfops.ensure_shelves()
    results = []
    for path, kind in dataset_files():
        if not quiet:
            print(f"[seed] ingesting {path.name} ({kind}) ...", flush=True)
        summary = ingest.ingest(path.read_bytes(), path.name, kind_hint=kind)
        results.append(summary)
        if not quiet:
            print(f"        rows={summary['rows']} accepted={summary['accepted']} "
                  f"quarantined={summary['rejected']} issues={summary['issues']}", flush=True)

    _assign_suppliers()
    if not quiet:
        print("[seed] running FEFO shelf sync + expiry write-offs ...", flush=True)
    shelf_waste = _sync_shelf()
    # the seeded historical stock is already physically placed: settle the
    # auto-putaway directives the ingest hook raised for its 420 batches
    db.execute("UPDATE shelf_tasks SET status='done', done_at=? "
               "WHERE kind='putaway' AND status='pending'", (db.now_iso(),))
    shelfops.assign_initial_shelves()
    shelfops.refresh_tasks()
    if not quiet:
        print(f"[seed] forecasting {db.scalar('SELECT COUNT(*) FROM drugs')} SKUs ...", flush=True)
    forecasting.forecast_all()
    alerts.refresh()
    if not quiet:
        print("[seed] done.", flush=True)
    return dict(status="seeded", uploads=results, waste_created=shelf_waste)


def _bootstrap_suppliers():
    for name, email, lead in (
        ("Apollo Supply Chain", "orders@apollosupplychain.com", 5),
        ("Hetero Healthcare", "orders@heterohealthcare.com", 7),
        ("MedPlus Mart", "orders@medplusmart.com", 6),
    ):
        db.execute("INSERT OR IGNORE INTO suppliers(name, email, lead_time_days, created_at) "
                   "VALUES(?,?,?,?)", (name, email, lead, db.now_iso()))


def _assign_suppliers():
    """Attach a default supplier/drug lead time so reorders can be addressed."""
    for d in db.query("SELECT id FROM drugs"):
        top = db.one(
            "SELECT supplier_id, COUNT(*) AS n FROM batches WHERE drug_id=? AND supplier_id "
            "IS NOT NULL GROUP BY supplier_id ORDER BY n DESC LIMIT 1", (d["id"],))
        if top:
            lead = db.one("SELECT lead_time_days FROM suppliers WHERE id=?", (top["supplier_id"],))
            db.execute("UPDATE drugs SET supplier_id=?, lead_time_days=? WHERE id=?",
                       (top["supplier_id"], (lead or {}).get("lead_time_days", 7) or 7, d["id"]))


def _sync_shelf() -> int:
    """Write off expired stock, refresh alerts."""
    created = 0
    from .shelf import sync_expired_waste
    created += sync_expired_waste()
    return created


if __name__ == "__main__":
    force = "--force" in sys.argv
    print(run(force=force))
