"""Patient billing: every issue at the patient counter writes a bill with its
line items (medicine, batch issued, qty, MRP), so the Invoices view shows the
bills made at the counter — like a real pharmacy receipt ledger."""
from __future__ import annotations

from . import db


def next_bill_no() -> str:
    """Sequential receipt number: BILL-YYYYMMDD-NNN (per-day counter)."""
    day = (db.now_iso() or "")[:10].replace("-", "")
    row = db.one("SELECT COUNT(*) AS n FROM bills WHERE bill_no LIKE ?", (f"BILL-{day}-%",))
    return f"BILL-{day}-{(row['n'] if row else 0) + 1:03d}"


def create_bill(items: list[dict], patient: str = "", doctor: str = "",
                note: str = "", source: str = "counter") -> dict:
    """items: [{drug_id, drug_name, batch_no, qty, unit_price}] — one bill row
    plus one line per (medicine, batch) actually issued. Returns the bill with
    its items. Amount = Σ qty × unit MRP."""
    if not items:
        raise ValueError("bill needs at least one line item")
    now = db.now_iso()
    bill_no = next_bill_no()
    units = sum(int(i["qty"]) for i in items)
    amount = round(sum(float(i["line_total"]) for i in items), 2)
    bill_id = db.execute(
        "INSERT INTO bills(bill_no, date, patient, doctor, note, items_count, units, amount, source, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (bill_no, now[:10], patient, doctor, note, len(items), units, amount, source, now),
    )
    for i in items:
        db.execute(
            "INSERT INTO bill_items(bill_id, drug_id, drug_name, batch_no, qty, unit_price, line_total, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (bill_id, i["drug_id"], i.get("drug_name", ""), i.get("batch_no", ""),
             int(i["qty"]), float(i.get("unit_price", 0)), float(i["line_total"]), now),
        )
    return get_bill(bill_id)


def get_bill(bill_id: int) -> dict | None:
    b = db.one("SELECT * FROM bills WHERE id=?", (bill_id,))
    if not b:
        return None
    b["items"] = db.query(
        "SELECT bi.*, d.category FROM bill_items bi LEFT JOIN drugs d ON d.id=bi.drug_id "
        "WHERE bi.bill_id=? ORDER BY bi.id", (bill_id,))
    return b


def list_bills(limit: int = 200, q: str | None = None,
               date_from: str | None = None, date_to: str | None = None) -> dict:
    """Bill ledger, newest first. `q` matches bill_no, patient or a medicine
    name on any line of the bill."""
    cond, params = [], []
    if q:
        like = f"%{q.lower()}%"
        cond.append("(LOWER(b.bill_no) LIKE ? OR LOWER(b.patient) LIKE ? OR b.id IN "
                    "(SELECT bi.bill_id FROM bill_items bi WHERE LOWER(bi.drug_name) LIKE ?))")
        params += [like, like, like]
    if date_from:
        cond.append("b.date>=?")
        params.append(date_from)
    if date_to:
        cond.append("b.date<=?")
        params.append(date_to)
    where = ("WHERE " + " AND ".join(cond)) if cond else ""
    rows = db.query(
        f"SELECT b.* FROM bills b {where} ORDER BY b.id DESC LIMIT ?", (*params, limit))
    for r in rows:
        r["items"] = db.query(
            "SELECT drug_name, batch_no, qty, unit_price, line_total FROM bill_items "
            "WHERE bill_id=? ORDER BY id", (r["id"],))
    summary = db.one(
        f"SELECT COUNT(*) AS bills, COALESCE(SUM(units),0) AS units, "
        f"COALESCE(SUM(amount),0) AS amount FROM bills b {where}", params)
    return dict(items=rows, summary=dict(
        bills=summary["bills"], units=summary["units"], amount=round(summary["amount"], 2)))
