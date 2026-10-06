"""Physical shelf layer: layout, putaway of new batches, system-directed shifts.

The website instructs the pharmacist instead of assuming shelf knowledge:
  * putaway — every newly received batch gets a "place on shelf X" task,
  * shift   — the nearest-expiry usable batch of every medicine is directed to
              that medicine's pick shelf (FEFO at the shelf level), and expired
              stock is directed to the quarantine shelf for vendor return.

Every directive is a row in `shelf_tasks` the pharmacist confirms once done.
"""
from . import db

PICK_SHELVES = ["P1", "P2", "P3"]
RESERVE_SHELVES = ["R1", "R2", "R3", "R4"]
QUARANTINE_SHELF = "Q1"
RETURNS_SHELF = "RT1"


def ensure_shelves() -> None:
    plan = ([(c, "pick", "Pick face — dispense patient medicines from here") for c in PICK_SHELVES]
            + [(c, "reserve", "Reserve stock — refill pick faces from here") for c in RESERVE_SHELVES]
            + [(QUARANTINE_SHELF, "quarantine", "Expired / blocked — stage for vendor return")]
            + [(RETURNS_SHELF, "returns", "Picked, awaiting vendor pickup")])
    for code, zone, note in plan:
        db.execute("INSERT OR IGNORE INTO shelves(code, zone, note) VALUES(?,?,?)",
                   (code, zone, note))


def _shelf_id(code: str) -> int | None:
    return db.scalar("SELECT id FROM shelves WHERE code=?", (code,))


def shelf_code(shelf_id: int | None) -> str | None:
    if shelf_id is None:
        return None
    return db.scalar("SELECT code FROM shelves WHERE id=?", (shelf_id,))


def pick_shelf(drug_id: int) -> str:
    """Stable pick face per medicine (FEFO dispensing happens here)."""
    return PICK_SHELVES[(drug_id - 1) % len(PICK_SHELVES)]


def reserve_shelf(drug_id: int) -> str:
    return RESERVE_SHELVES[(drug_id - 1) % len(RESERVE_SHELVES)]


def recommend_shelf(batch_id: int, drug_id: int) -> int | None:
    """Shelf for a freshly received batch: pick face if it is the medicine's
    nearest-expiry usable batch, otherwise that medicine's reserve shelf."""
    earliest = db.scalar(
        "SELECT id FROM batches WHERE drug_id=? AND qty_remaining>0 "
        "AND (expiry_date IS NULL OR expiry_date>=?) "
        "ORDER BY expiry_date IS NULL, expiry_date LIMIT 1",
        (drug_id, db.today().isoformat()))
    return _shelf_id(pick_shelf(drug_id) if earliest == batch_id else reserve_shelf(drug_id))


def on_batch_arrived(batch_id: int, drug_id: int, batch_no: str = "", qty: int = 0) -> None:
    """Purchase ingest hook: place the batch + message the pharmacist where to put it."""
    to_id = recommend_shelf(batch_id, drug_id)
    if to_id is None:
        return
    code = shelf_code(to_id)
    db.execute(
        "INSERT INTO shelf_tasks(kind, drug_id, batch_id, to_shelf_id, reason, "
        "status, dedup_key, created_at) VALUES('putaway',?,?,?,?,'pending',?,?) "
        "ON CONFLICT(dedup_key) DO NOTHING",
        (drug_id, batch_id, to_id,
         f"New batch {batch_no or batch_id} ({qty} units) received — place on shelf {code}",
         f"putaway:{batch_id}", db.now_iso()),
    )


def assign_initial_shelves() -> int:
    """Give every batch without a physical location a shelf (seed / backfill).
    Rank-1 usable stock lands on its pick shelf, the rest on the reserve shelf."""
    n = 0
    for b in db.query("SELECT id, drug_id FROM batches WHERE shelf_id IS NULL"):
        to_id = recommend_shelf(b["id"], b["drug_id"])
        if to_id is not None:
            db.execute("UPDATE batches SET shelf_id=? WHERE id=?", (to_id, b["id"]))
            n += 1
    return n


def refresh_tasks() -> dict:
    """Recompute system-directed shifts and void stale pending ones.

    Directives generated:
      * nearest-expiry usable batch of each medicine -> its pick shelf,
      * expired stock -> quarantine (aggregated per medicine).
    """
    today = db.today().isoformat()
    needed: dict[str, tuple] = {}
    rows = db.query(
        "SELECT b.id AS batch_id, b.drug_id, b.batch_no, b.expiry_date, b.shelf_id, "
        "d.name AS drug FROM batches b JOIN drugs d ON d.id=b.drug_id "
        "WHERE b.qty_remaining>0")  # noqa: E501 — shelf_id read for shift detection
    first: dict[int, dict] = {}
    drug_names: dict[int, str] = {}
    for r in rows:
        drug_names[r["drug_id"]] = r["drug"]
        if r["expiry_date"] and r["expiry_date"] < today:
            continue
        cur = first.get(r["drug_id"])
        if cur is None or (r["expiry_date"] or "9999") < (cur["expiry_date"] or "9999"):
            first[r["drug_id"]] = r
    for drug_id, r in first.items():
        code = pick_shelf(drug_id)
        want = _shelf_id(code)
        if want is not None and r["shelf_id"] != want:
            needed[f"shift:{r['batch_id']}:{want}"] = (
                r["drug_id"], r["batch_id"], r["shelf_id"], want,
                f"{r['drug']}: batch {r['batch_no']} is nearest to expiry — shift to pick shelf {code}")
    expired: dict[int, int] = {}
    for r in rows:
        if r["expiry_date"] and r["expiry_date"] < today:
            expired[r["drug_id"]] = expired.get(r["drug_id"], 0) + 1
    for drug_id, lots in expired.items():
        want = _shelf_id(QUARANTINE_SHELF)
        if want is not None:
            needed[f"shift-expired:{drug_id}:{want}"] = (
                drug_id, None, None, want,
                f"{lots} expired batch lot(s) of {drug_names.get(drug_id, drug_id)}"
                f" — shift to quarantine shelf {QUARANTINE_SHELF} for vendor return")
    now = db.now_iso()
    for key, (drug_id, batch_id, from_id, to_id, reason) in needed.items():
        db.execute(
            "INSERT INTO shelf_tasks(kind, drug_id, batch_id, from_shelf_id, to_shelf_id, "
            "reason, status, dedup_key, created_at) VALUES('shift',?,?,?,?,?,'pending',?,?) "
            "ON CONFLICT(dedup_key) DO NOTHING",
            (drug_id, batch_id, from_id, to_id, reason, key, now),
        )
    for p in db.query("SELECT id, dedup_key FROM shelf_tasks WHERE kind='shift' AND status='pending'"):
        if p["dedup_key"] not in needed:
            db.execute("UPDATE shelf_tasks SET status='void' WHERE id=?", (p["id"],))
    counts = task_counts()
    return dict(created=len(needed), **counts)


def complete_task(task_id: int) -> dict | None:
    """Mark a directive done: confirms the physical move and updates the shelf map."""
    t = db.one("SELECT * FROM shelf_tasks WHERE id=?", (task_id,))
    if not t:
        return None
    db.execute("UPDATE shelf_tasks SET status='done', done_at=? WHERE id=?",
               (db.now_iso(), task_id))
    if t["to_shelf_id"]:
        if t["batch_id"]:
            db.execute("UPDATE batches SET shelf_id=? WHERE id=?", (t["to_shelf_id"], t["batch_id"]))
        elif t["kind"] == "shift" and t["drug_id"]:  # aggregated expired-lots directive
            db.execute(
                "UPDATE batches SET shelf_id=? WHERE drug_id=? AND qty_remaining>0 "
                "AND expiry_date IS NOT NULL AND expiry_date<?",
                (t["to_shelf_id"], t["drug_id"], db.today().isoformat()))
    return db.one("SELECT * FROM shelf_tasks WHERE id=?", (task_id,))


def list_tasks(status: str | None = "pending", limit: int = 200) -> list[dict]:
    sql = ("SELECT t.*, d.name AS drug, b.batch_no, b.qty_remaining, b.expiry_date, "
           "fs.code AS from_code, ts.code AS to_code "
           "FROM shelf_tasks t "
           "LEFT JOIN drugs d ON d.id=t.drug_id "
           "LEFT JOIN batches b ON b.id=t.batch_id "
           "LEFT JOIN shelves fs ON fs.id=t.from_shelf_id "
           "LEFT JOIN shelves ts ON ts.id=t.to_shelf_id")
    params: tuple = ()
    if status:
        sql += " WHERE t.status=?"
        params = (status,)
    sql += " ORDER BY t.created_at DESC, t.id DESC LIMIT ?"
    params = params + (limit,)
    return db.query(sql, params)


def task_counts() -> dict:
    rows = db.query(
        "SELECT kind, COUNT(*) AS n FROM shelf_tasks WHERE status='pending' GROUP BY kind")
    out = {r["kind"]: r["n"] for r in rows}
    return dict(putaway=out.get("putaway", 0), shift=out.get("shift", 0),
                pending=out.get("putaway", 0) + out.get("shift", 0))
