"""SmartShelf: FEFO allocation views, expiry management, waste & vendor returns."""
from datetime import date, datetime

from . import db


def today() -> date:
    return date.today()


# --------------------------------------------------------------------------- #
# shelf / FEFO
# --------------------------------------------------------------------------- #
def shelf_status(expiry: str | None, critical: int, warning: int) -> tuple[str, int | None]:
    if not expiry:
        return "unknown", None
    d = date.fromisoformat(expiry)
    days = (d - today()).days
    if days < 0:
        return "expired", days
    if days <= critical:
        return "critical", days
    if days <= warning:
        return "warning", days
    return "ok", days


def shelf(drug_id: int | None = None, include_empty: bool = False) -> list[dict]:
    """Batches with FEFO rank per drug (rank 1 = dispense first)."""
    settings = db.settings()
    crit = int(settings.get("expiry_critical_days", 30))
    warn = int(settings.get("expiry_warning_days", 90))
    sql = ("SELECT b.*, d.name AS drug, d.category, s.name AS supplier, "
           "sh.code AS shelf_code, sh.zone AS shelf_zone "
           "FROM batches b JOIN drugs d ON d.id=b.drug_id "
           "LEFT JOIN suppliers s ON s.id=b.supplier_id "
           "LEFT JOIN shelves sh ON sh.id=b.shelf_id")
    params: tuple = ()
    if drug_id:
        sql += " WHERE b.drug_id=?"
        params = (drug_id,)
    if not include_empty:
        sql += (" AND" if drug_id else " WHERE") + " b.qty_remaining > 0"
    sql += " ORDER BY d.name, b.expiry_date IS NULL, b.expiry_date"
    rows = db.query(sql, params)
    out = []
    rank: dict[int, int] = {}
    for r in rows:
        status, days = shelf_status(r["expiry_date"], crit, warn)
        rank[r["drug_id"]] = rank.get(r["drug_id"], 0) + 1
        r["fefo_rank"] = rank[r["drug_id"]]
        r["status"] = status
        r["days_to_expiry"] = days
        r["value"] = round(r["qty_remaining"] * r["unit_cost"], 2)
        out.append(r)
    return out


def fefo_dispense(drug_id: int, qty: int, note: str = "") -> dict:
    """Manual FEFO issue: consumes earliest-expiry, *unexpired* stock first.
    Each consumed batch reports its physical shelf so the pharmacist knows
    exactly where to pick the medicine from."""
    t = today().isoformat()
    batches = db.query(
        "SELECT b.*, s.code AS shelf, s.zone AS shelf_zone FROM batches b "
        "LEFT JOIN shelves s ON s.id=b.shelf_id "
        "WHERE b.drug_id=? AND b.qty_remaining>0 "
        "AND (b.expiry_date IS NULL OR b.expiry_date>=?) "
        "ORDER BY b.expiry_date IS NULL, b.expiry_date",
        (drug_id, t),
    )
    remaining, taken = qty, []
    blocked = db.scalar(
        "SELECT COALESCE(SUM(qty_remaining),0) FROM batches WHERE drug_id=? AND qty_remaining>0 "
        "AND expiry_date IS NOT NULL AND expiry_date<?", (drug_id, t))
    for b in batches:
        if remaining <= 0:
            break
        take = min(remaining, b["qty_remaining"])
        db.execute("UPDATE batches SET qty_remaining=qty_remaining-? WHERE id=?", (take, b["id"]))
        taken.append(dict(batch_no=b["batch_no"], expiry=b["expiry_date"], qty=take,
                          shelf=b["shelf"], shelf_zone=b["shelf_zone"]))
        remaining -= take
    return dict(requested=qty, dispensed=qty - remaining, batches=taken,
                shortfall=remaining, blocked_expired=blocked,
                note=note, at=db.now_iso())


def next_fefo_batch(drug_id: int) -> dict | None:
    """Where a patient's medicine must come from: the nearest-expiry,
    unexpired batch, with its physical shelf location."""
    t = today().isoformat()
    row = db.one(
        "SELECT b.*, s.code AS shelf_code, s.zone AS shelf_zone FROM batches b "
        "LEFT JOIN shelves s ON s.id=b.shelf_id "
        "WHERE b.drug_id=? AND b.qty_remaining>0 "
        "AND (b.expiry_date IS NULL OR b.expiry_date>=?) "
        "ORDER BY b.expiry_date IS NULL, b.expiry_date LIMIT 1",
        (drug_id, t),
    )
    if row and row.get("expiry_date"):
        try:
            row["days_to_expiry"] = (date.fromisoformat(row["expiry_date"]) - today()).days
        except ValueError:
            row["days_to_expiry"] = None
    return row


def expiry_buckets() -> dict:
    """Counts + value per expiry bucket, plus per-drug breakdown."""
    settings = db.settings()
    crit = int(settings.get("expiry_critical_days", 30))
    warn = int(settings.get("expiry_warning_days", 90))
    rows = db.query(
        "SELECT b.drug_id, d.name AS drug, b.expiry_date, b.qty_remaining, b.unit_cost "
        "FROM batches b JOIN drugs d ON d.id=b.drug_id "
        "WHERE b.qty_remaining > 0 AND b.expiry_date IS NOT NULL"
    )
    t = today()
    buckets = {
        "expired": dict(label="Expired", qty=0, value=0.0, batches=0),
        f"0-{crit}": dict(label=f"Next {crit} days", qty=0, value=0.0, batches=0),
        f"{crit + 1}-{warn}": dict(label=f"{crit + 1}–{warn} days", qty=0, value=0.0, batches=0),
        f"{warn + 1}-180": dict(label=f"{warn + 1}–180 days", qty=0, value=0.0, batches=0),
        "180+": dict(label="Beyond 180 days", qty=0, value=0.0, batches=0),
    }
    per_drug: dict[str, dict] = {}
    for r in rows:
        days = (date.fromisoformat(r["expiry_date"]) - t).days
        if days < 0:
            key = "expired"
        elif days <= crit:
            key = f"0-{crit}"
        elif days <= warn:
            key = f"{crit + 1}-{warn}"
        elif days <= 180:
            key = f"{warn + 1}-180"
        else:
            key = "180+"
        val = r["qty_remaining"] * r["unit_cost"]
        buckets[key]["qty"] += r["qty_remaining"]
        buckets[key]["value"] += val
        buckets[key]["batches"] += 1
        d = per_drug.setdefault(r["drug"], dict(expired=0, critical=0, warning=0, safe=0))
        if key == "expired":
            d["expired"] += r["qty_remaining"]
        elif key in (f"0-{crit}", f"{crit + 1}-{warn}"):
            d["critical" if key == f"0-{crit}" else "warning"] += r["qty_remaining"]
        else:
            d["safe"] += r["qty_remaining"]
    for v in buckets.values():
        v["value"] = round(v["value"], 2)
    return dict(buckets=buckets, per_drug=per_drug)


# --------------------------------------------------------------------------- #
# waste
# --------------------------------------------------------------------------- #
def sync_expired_waste() -> int:
    """Create (idempotent) waste records for expired batches still holding stock."""
    rows = db.query(
        "SELECT b.*, d.name AS drug FROM batches b JOIN drugs d ON d.id=b.drug_id "
        "WHERE b.qty_remaining > 0 AND b.expiry_date IS NOT NULL AND b.expiry_date < ? "
        "AND NOT EXISTS (SELECT 1 FROM waste w WHERE w.batch_id=b.id AND w.reason='expired')",
        (today().isoformat(),),
    )
    now = db.now_iso()
    for r in rows:
        value = round(r["qty_remaining"] * r["unit_cost"], 2)
        db.execute(
            "INSERT INTO waste(drug_id, batch_id, supplier_id, qty, reason, unit_cost, value, "
            "status, note, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (r["drug_id"], r["id"], r["supplier_id"], r["qty_remaining"], "expired",
             r["unit_cost"], value, "pending",
             f"Batch {r['batch_no']} expired on {r['expiry_date']}", now),
        )
    return len(rows)


def add_waste(drug_id: int, batch_no: str | None, qty: int, reason: str, note: str = "") -> dict:
    """Manual write-off: damaged / recalled / expired adjustment. Deducts stock FEFO."""
    if reason not in ("expired", "damaged", "recalled", "theft", "other"):
        raise ValueError("invalid reason")
    batch = None
    if batch_no:
        batch = db.one("SELECT * FROM batches WHERE drug_id=? AND batch_no=? ORDER BY id LIMIT 1",
                       (drug_id, batch_no))
    if batch is None:
        batch = db.one(
            "SELECT * FROM batches WHERE drug_id=? AND qty_remaining>0 "
            "ORDER BY expiry_date IS NULL, expiry_date LIMIT 1",
            (drug_id,),
        )
    if batch is None:
        raise ValueError("no stock available for this drug")
    take = min(int(qty), batch["qty_remaining"])
    db.execute("UPDATE batches SET qty_remaining=qty_remaining-? WHERE id=?", (take, batch["id"]))
    value = round(take * batch["unit_cost"], 2)
    now = db.now_iso()
    wid = db.execute(
        "INSERT INTO waste(drug_id, batch_id, supplier_id, qty, reason, unit_cost, value, "
        "status, note, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (drug_id, batch["id"], batch["supplier_id"], take, reason, batch["unit_cost"],
         value, "pending", note, now, now),
    )
    return dict(id=wid, qty=take, value=value, batch=batch["batch_no"])


def waste_list() -> list[dict]:
    return db.query(
        "SELECT w.*, d.name AS drug, b.batch_no, b.expiry_date, s.name AS supplier "
        "FROM waste w JOIN drugs d ON d.id=w.drug_id "
        "LEFT JOIN batches b ON b.id=w.batch_id LEFT JOIN suppliers s ON s.id=w.supplier_id "
        "ORDER BY w.id DESC"
    )


def waste_summary() -> dict:
    total = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(value),0) AS v, COALESCE(SUM(qty),0) AS q FROM waste")
    by_reason = db.query("SELECT reason, COUNT(*) AS n, SUM(value) AS v, SUM(qty) AS q "
                         "FROM waste GROUP BY reason")
    by_drug = db.query("SELECT d.name AS drug, SUM(w.value) AS v, SUM(w.qty) AS q "
                       "FROM waste w JOIN drugs d ON d.id=w.drug_id GROUP BY d.name ORDER BY v DESC")
    by_supplier = db.query("SELECT s.name AS supplier, SUM(w.value) AS v, SUM(w.qty) AS q "
                           "FROM waste w LEFT JOIN suppliers s ON s.id=w.supplier_id "
                           "GROUP BY s.name ORDER BY v DESC")
    by_month = db.query("SELECT substr(w.created_at,1,7) AS month, SUM(w.value) AS v, SUM(w.qty) AS q "
                        "FROM waste w GROUP BY month ORDER BY month")
    by_status = db.query("SELECT status, COUNT(*) AS n, SUM(value) AS v FROM waste GROUP BY status")
    purchases_total = db.scalar("SELECT COALESCE(SUM(total),0) FROM purchases")
    sales_total = db.scalar("SELECT COALESCE(SUM(total),0) FROM sales")
    value_at_risk = db.scalar(
        "SELECT COALESCE(SUM(qty_remaining*unit_cost),0) FROM batches "
        "WHERE expiry_date IS NOT NULL AND expiry_date < ? "
        "AND NOT EXISTS (SELECT 1 FROM waste w WHERE w.batch_id=batches.id)",
        (today().isoformat(),))
    return dict(
        total_qty=total["q"], total_value=round(total["v"], 2), total_lots=total["n"],
        by_reason=by_reason, by_drug=by_drug, by_supplier=by_supplier,
        by_month=by_month, by_status=by_status,
        purchase_value=round(purchases_total, 2), sales_value=round(sales_total, 2),
        waste_pct_of_purchases=round(purchases_total and total["v"] / purchases_total * 100, 2),
        value_at_risk=round(value_at_risk, 2),
    )


# --------------------------------------------------------------------------- #
# returns to vendor
# --------------------------------------------------------------------------- #
def create_return(supplier_id: int, waste_ids: list[int] | None = None, note: str = "") -> dict:
    if waste_ids:
        marks = ",".join("?" * len(waste_ids))
        rows = db.query(
            f"SELECT w.*, b.batch_no FROM waste w LEFT JOIN batches b ON b.id=w.batch_id "
            f"WHERE w.supplier_id=? AND w.id IN ({marks}) AND w.status='pending'",
            (supplier_id, *waste_ids))
    else:
        rows = db.query(
            "SELECT w.*, b.batch_no FROM waste w LEFT JOIN batches b ON b.id=w.batch_id "
            "WHERE w.supplier_id=? AND w.status='pending'", (supplier_id,))
    if not rows:
        raise ValueError("no pending waste lots for this supplier")
    qty = sum(r["qty"] for r in rows)
    value = round(sum(r["value"] for r in rows), 2)
    ids = [r["id"] for r in rows]
    now = db.now_iso()
    ref = f"RTV-{datetime.now().strftime('%Y%m%d')}-{supplier_id:02d}"
    rid = db.execute(
        "INSERT INTO returns(supplier_id, reference, qty, value, waste_ids, status, note, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (supplier_id, ref, qty, value, db.jdump(ids), "requested", note, now),
    )
    db.execute(f"UPDATE waste SET status='return_requested', updated_at=? WHERE id IN ({','.join('?' * len(ids))})",
               (now, *ids))
    supplier = db.one("SELECT * FROM suppliers WHERE id=?", (supplier_id,))
    body = (
        f"Dear {supplier['name']},\n\n"
        f"Please accept our return request {ref} covering {len(ids)} lot(s), {qty} units "
        f"(value ₹{value:,.2f}) of expired stock as per FEFO/Expiry policy.\n\n"
        f"Lots:\n" + "\n".join(
            f"  - {r['batch_no']}: {r['qty']} units ({r['reason']})" for r in rows
        ) +
        f"\n\nKindly confirm pick-up and credit note.\n{db.get_setting('pharmacy_name')}"
    )
    db.execute(
        "INSERT INTO notifications(supplier_id, subject, body, status, related_type, related_id, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (supplier_id, f"Return request {ref} – {qty} units (₹{value:,.0f})", body,
         "draft", "return", rid, now),
    )
    return dict(id=rid, reference=ref, qty=qty, value=value, lots=len(ids))


RETURN_STATUSES = ["requested", "approved", "picked_up", "credited", "rejected"]


def update_return_status(return_id: int, status: str) -> dict:
    if status not in RETURN_STATUSES:
        raise ValueError(f"status must be one of {RETURN_STATUSES}")
    row = db.one("SELECT * FROM returns WHERE id=?", (return_id,))
    if not row:
        raise ValueError("return not found")
    now = db.now_iso()
    db.execute("UPDATE returns SET status=?, updated_at=? WHERE id=?", (status, now, return_id))
    ids = db.jload(row["waste_ids"], []) or []
    if ids:
        marks = ",".join("?" * len(ids))
        if status == "credited":
            db.execute(f"UPDATE waste SET status='returned_to_vendor', updated_at=? WHERE id IN ({marks})",
                       (now, *ids))
        elif status == "rejected":
            db.execute(f"UPDATE waste SET status='pending', updated_at=? WHERE id IN ({marks})",
                       (now, *ids))
        else:
            db.execute(f"UPDATE waste SET status='return_requested', updated_at=? WHERE id IN ({marks})",
                       (now, *ids))
    if status == "credited":
        # credit note bookkeeping: recover value back (stock already written off)
        db.execute("UPDATE returns SET value=value WHERE id=?", (return_id,))
    return dict(id=return_id, status=status)


def list_returns() -> list[dict]:
    rows = db.query(
        "SELECT r.*, s.name AS supplier FROM returns r LEFT JOIN suppliers s ON s.id=r.supplier_id "
        "ORDER BY r.id DESC"
    )
    for r in rows:
        ids = db.jload(r["waste_ids"], []) or []
        lots = []
        if ids:
            marks = ",".join("?" * len(ids))
            lots = db.query(
                f"SELECT w.qty, w.value, w.reason, d.name AS drug, b.batch_no, b.expiry_date "
                f"FROM waste w JOIN drugs d ON d.id=w.drug_id LEFT JOIN batches b ON b.id=w.batch_id "
                f"WHERE w.id IN ({marks})", ids)
        r["lots"] = lots
    return rows
