"""Supplier performance + outbound notification (reorder / return) inbox."""
from datetime import date

from . import db


def supplier_stats() -> list[dict]:
    today = date.today().isoformat()
    rows = db.query(
        "SELECT s.*, COUNT(DISTINCT b.id) AS batches, COALESCE(SUM(b.qty_received),0) AS units "
        "FROM suppliers s LEFT JOIN batches b ON b.supplier_id=s.id GROUP BY s.id ORDER BY s.name"
    )
    out = []
    for r in rows:
        spend = db.scalar("SELECT COALESCE(SUM(total),0) FROM purchases WHERE supplier_id=?", (r["id"],))
        waste = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(value),0) AS v, COALESCE(SUM(qty),0) AS q "
                       "FROM waste WHERE supplier_id=?", (r["id"],))
        pos = db.scalar("SELECT COUNT(DISTINCT purchase_id) FROM purchases WHERE supplier_id=?", (r["id"],))
        shelf_life = db.scalar(
            "SELECT AVG(julianday(b.expiry_date) - julianday(b.received_date)) FROM batches b "
            "WHERE b.supplier_id=? AND b.expiry_date IS NOT NULL AND b.received_date IS NOT NULL",
            (r["id"],), default=0) or 0
        expiring = db.scalar(
            "SELECT COUNT(*) FROM batches WHERE supplier_id=? AND qty_remaining>0 "
            "AND expiry_date < ? AND expiry_date IS NOT NULL", (r["id"], today))
        waste_pct = (waste["v"] / spend * 100) if spend else 0
        score = round(max(0, min(100, 100 - waste_pct * 1.6 - expiring * 1.5
                                 + min(shelf_life, 730) / 60)), 1)
        out.append(dict(
            id=r["id"], name=r["name"], email=r["email"], lead_time_days=r["lead_time_days"],
            orders=pos, batches=r["batches"], units=r["units"],
            spend=round(spend, 2), waste_qty=waste["q"], waste_value=round(waste["v"], 2),
            waste_pct=round(waste_pct, 2), expired_batches=expiring,
            avg_shelf_life_days=round(shelf_life, 1), score=score,
        ))
    return out


def create_notification(supplier_id: int, subject: str, body: str,
                        related_type: str | None = None, related_id: int | None = None,
                        status: str = "draft") -> dict:
    nid = db.execute(
        "INSERT INTO notifications(supplier_id, subject, body, status, related_type, related_id, "
        "created_at) VALUES(?,?,?,?,?,?,?)",
        (supplier_id, subject, body, status, related_type, related_id, db.now_iso()),
    )
    return get(nid)


def get(nid: int) -> dict:
    return db.one(
        "SELECT n.*, s.name AS supplier, s.email FROM notifications n "
        "LEFT JOIN suppliers s ON s.id=n.supplier_id WHERE n.id=?", (nid,))


def list_notifications(status: str | None = None, limit: int = 100) -> list[dict]:
    sql = ("SELECT n.*, s.name AS supplier, s.email FROM notifications n "
           "LEFT JOIN suppliers s ON s.id=n.supplier_id")
    params = []
    if status:
        sql += " WHERE n.status=?"
        params.append(status)
    sql += " ORDER BY n.id DESC LIMIT ?"
    params.append(limit)
    return db.query(sql, params)


def mark_sent(nid: int) -> dict:
    db.execute("UPDATE notifications SET status='sent', sent_at=? WHERE id=?",
               (db.now_iso(), nid))
    return get(nid)


def reorder_email(drug: dict, plan: dict, supplier: dict | None) -> tuple[str, str]:
    subject = (f"Purchase order request: {drug['name']} x {plan['order_qty']} "
               f"({db.get_setting('pharmacy_name')})")
    body = (
        f"Dear {supplier['name'] if supplier else 'Supplier'},\n\n"
        f"As per our AI demand forecast ({plan.get('avg_daily', 0)} units/day, "
        f"{plan['lead_time_days']}-day lead time, {int(plan['service_level'] * 100)}% service level), "
        f"we need to replenish {drug['name']}.\n\n"
        f"  • Available stock : {plan['available']} units\n"
        f"  • Reorder point   : {plan['reorder_point']:.0f} units\n"
        f"  • Suggested order : {plan['order_qty']} units\n"
        f"  • Required by     : {plan.get('due_date') or 'immediately'}\n"
        f"  • Preferred batch shelf life: >= 18 months\n\n"
        f"Please confirm availability, price and delivery schedule.\n\n"
        f"Regards,\n{db.get_setting('pharmacy_name')}"
    )
    return subject, body


def build_reorder_notifications() -> list[dict]:
    """Draft supplier emails for every AI 'order now' suggestion not yet notified."""
    created = []
    for drug in db.query("SELECT * FROM drugs"):
        row = db.one("SELECT payload FROM forecasts WHERE drug_id=? AND is_current=1 ORDER BY id DESC LIMIT 1",
                     (drug["id"],))
        payload = db.jload(row["payload"], {}) if row else None
        plan = (payload or {}).get("reorder")
        if not plan or plan["status"] != "order_now":
            continue
        exists = db.one(
            "SELECT id FROM notifications WHERE related_type='reorder' AND related_id=? AND status!='cancelled'",
            (drug["id"],))
        if exists:
            continue
        supplier = db.one("SELECT * FROM suppliers WHERE id=?", (drug["supplier_id"],)) or \
            db.one("SELECT * FROM suppliers ORDER BY id LIMIT 1")
        subject, body = reorder_email(drug, plan, supplier)
        created.append(create_notification(
            supplier["id"] if supplier else None, subject, body,
            related_type="reorder", related_id=drug["id"]))
    return created
