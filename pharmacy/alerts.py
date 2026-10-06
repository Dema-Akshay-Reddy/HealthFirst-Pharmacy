"""Smart alert engine: low stock, expiry, stockout risk, overstock, waste, data quality."""
from datetime import date, timedelta

from . import db
from .forecasting import available_stock

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _expiry_rows(today: str, warning_days: int):
    return db.query(
        "SELECT b.id, b.drug_id, b.batch_no, b.expiry_date, b.qty_remaining, b.unit_cost, "
        "d.name AS drug, s.name AS supplier, "
        "CAST(julianday(b.expiry_date) - julianday(?) AS INTEGER) AS days_left "
        "FROM batches b JOIN drugs d ON d.id=b.drug_id "
        "LEFT JOIN suppliers s ON s.id=b.supplier_id "
        "WHERE b.qty_remaining > 0 AND b.expiry_date IS NOT NULL AND b.expiry_date <= ? "
        "ORDER BY b.expiry_date",
        (today, (date.fromisoformat(today) + timedelta(days=warning_days)).isoformat()),
    )


def refresh() -> list[dict]:
    """Recompute every rule, upsert alerts, resolve the ones that no longer apply."""
    settings = db.settings()
    today_d = date.today()
    today = today_d.isoformat()
    crit_days = int(settings.get("expiry_critical_days", 30))
    warn_days = int(settings.get("expiry_warning_days", 90))
    overstock_days = int(settings.get("overstock_days", 180))

    found: dict[str, dict] = {}

    def add(key, atype, severity, title, message, drug_id=None, batch_id=None, details=None):
        found[key] = dict(key=key, atype=atype, severity=severity, title=title,
                          message=message, drug_id=drug_id, batch_id=batch_id,
                          details=db.jdump(details or {}))

    # ---- expiry -----------------------------------------------------------
    exp_rows = _expiry_rows(today, warn_days)
    by_drug: dict[int, list] = {}
    for r in exp_rows:
        by_drug.setdefault(r["drug_id"], []).append(r)

    for drug_id, rows in by_drug.items():
        expired = [r for r in rows if r["days_left"] is not None and r["days_left"] < 0]
        critical = [r for r in rows if r is not None and r.get("days_left") is not None and 0 <= r["days_left"] <= crit_days]
        soon = [r for r in rows if r.get("days_left") is not None and crit_days < r["days_left"] <= warn_days]
        name = rows[0]["drug"]

        if expired:
            qty = sum(r["qty_remaining"] for r in expired)
            val = sum(r["qty_remaining"] * r["unit_cost"] for r in expired)
            add(f"expired:{drug_id}", "expired_stock", "critical",
                f"{len(expired)} expired batch{'es' if len(expired) > 1 else ''} of {name}",
                f"{qty} units (₹{val:,.0f}) already past expiry — block dispensing and raise a "
                f"return-to-vendor request.",
                drug_id, expired[0]["id"],
                dict(qty=qty, value=round(val, 2), batches=[r["batch_no"] for r in expired][:20]))
        if critical:
            qty = sum(r["qty_remaining"] for r in critical)
            add(f"expiry_crit:{drug_id}", "expiry_soon", "high",
                f"{name}: {len(critical)} batch{'es' if len(critical) > 1 else ''} expire within {crit_days} days",
                f"{qty} units expire by "
                f"{max(r['expiry_date'] for r in critical)} — push FEFO dispensing and "
                f"consider a vendor return.",
                drug_id, critical[0]["id"],
                dict(qty=qty, batches=[(r["batch_no"], r["expiry_date"]) for r in critical][:20]))
        if soon:
            qty = sum(r["qty_remaining"] for r in soon)
            add(f"expiry_warn:{drug_id}", "expiry_soon", "medium",
                f"{name}: {len(soon)} batch{'es' if len(soon) > 1 else ''} expiring within {warn_days} days",
                f"{qty} units expire before "
                f"{(today_d + timedelta(days=warn_days)).isoformat()} — schedule run-down or return.",
                drug_id, soon[0]["id"],
                dict(qty=qty, batches=[(r["batch_no"], r["expiry_date"]) for r in soon][:20]))

    # ---- stock levels / demand ------------------------------------------
    for drug in db.query("SELECT * FROM drugs"):
        plan = reorder_plan_for(drug)
        avail = plan["available"]
        rop = plan["reorder_point"]
        cover = plan["cover_days"]

        if avail < rop:
            add(f"low_stock:{drug['id']}", "low_stock", "high",
                f"Low stock: {drug['name']}",
                f"Only {avail} units on hand vs a reorder point of {rop:.0f} "
                f"({plan['lead_time_days']}d lead time + {plan['safety_stock']:.0f} safety stock). "
                f"Suggested order: {plan['order_qty']} units.",
                drug["id"], None, plan)
        if plan.get("stockout_date") and plan["stockout_date"] <= (
            today_d + timedelta(days=plan["lead_time_days"])
        ).isoformat():
            add(f"stockout:{drug['id']}", "stockout_risk", "critical",
                f"Stockout risk: {drug['name']}",
                f"Projected to run out on {plan['stockout_date']} at current demand "
                f"({plan['avg_daily'] if 'avg_daily' in plan else ''} units/day) — reorder today.",
                drug["id"], None, plan)
        if cover > overstock_days:
            add(f"overstock:{drug['id']}", "overstock", "medium",
                f"Overstock: {drug['name']}",
                f"{avail} units = {cover:.0f} days of cover (threshold {overstock_days}d). "
                f"Excess stock is the main driver of expiry waste — pause replenishment.",
                drug["id"], None, plan)
        if plan["expired_stock"] > 0:
            add(f"expired_units:{drug['id']}", "expired_stock", "medium",
                f"Blocked stock: {drug['name']}",
                f"{plan['expired_stock']} units sit in expired batches and are excluded from "
                f"available stock.",
                drug["id"], None, plan)

        # ---- demand spike: last 7 days of feed vs 28-day baseline ----
        anchor = db.scalar(
            "SELECT MAX(date) FROM sales WHERE drug_id=? "
            "AND COALESCE(source,'')!='pos_sim'", (drug["id"],))
        if anchor:
            recent = db.scalar(
                "SELECT COALESCE(SUM(qty),0) FROM sales_daily "
                "WHERE drug_id=? AND date > date(?, '-7 day') AND date <= ?",
                (drug["id"], anchor, anchor)) or 0
            prior28 = db.scalar(
                "SELECT COALESCE(SUM(qty),0) FROM sales_daily "
                "WHERE drug_id=? AND date > date(?, '-35 day') AND date <= date(?, '-7 day')",
                (drug["id"], anchor, anchor)) or 0
            base = prior28 / 4.0
            if base >= 15 and recent >= 50 and recent >= base * 1.5:
                pct = round((recent / base - 1) * 100)
                order_note = (f"suggested order {plan['order_qty']} units"
                              if plan["order_qty"] > 0 else
                              "no reorder needed — stock is sufficient, keep levels flat")
                add(f"spike:{drug['id']}", "demand_spike", "medium",
                    f"Demand spike: {drug['name']}",
                    f"{recent} units sold in the 7 days to {anchor} — {pct}% above the "
                    f"28-day baseline ({base:.0f}/week). Adjust stock level; {order_note}.",
                    drug["id"], None,
                    dict(recent=recent, baseline_weekly=round(base, 1), anchor=anchor,
                         change_pct=pct, order_qty=plan["order_qty"]))

            # ---- price surge: 90-day avg unit price vs prior 90 days ----
            pr = db.one(
                "SELECT AVG(unit_price) AS p, COUNT(*) AS n FROM sales "
                "WHERE drug_id=? AND date > date(?, '-90 day') AND date <= ? "
                "AND unit_price>0", (drug["id"], anchor, anchor))
            pp = db.one(
                "SELECT AVG(unit_price) AS p, COUNT(*) AS n FROM sales "
                "WHERE drug_id=? AND date <= date(?, '-90 day') "
                "AND date > date(?, '-180 day') AND unit_price>0",
                (drug["id"], anchor, anchor))
            if (pr and pp and pr["n"] >= 15 and pp["n"] >= 15 and pp["p"]
                    and pr["p"] / pp["p"] - 1 >= 0.08):
                change = pr["p"] / pp["p"] - 1
                order_note = (f"suggested order {plan['order_qty']} units"
                              if plan["order_qty"] > 0 else
                              "current stock covers demand — hold quantities flat")
                add(f"price:{drug['id']}", "price_rising", "medium",
                    f"Price rising: {drug['name']}",
                    f"Avg sale price \u20b9{pr['p']:.2f} vs \u20b9{pp['p']:.2f} in the prior "
                    f"90 days (+{change * 100:.0f}%) \u2014 stock up before further hikes; "
                    f"{order_note}.",
                    drug["id"], None,
                    dict(recent_price=round(pr["p"], 2), prior_price=round(pp["p"], 2),
                         change_pct=round(change * 100, 1), order_qty=plan["order_qty"]))

    # ---- pending vendor returns ------------------------------------------
    for sup in db.query(
        "SELECT s.id, s.name, COUNT(w.id) AS n, SUM(w.value) AS v FROM suppliers s "
        "JOIN waste w ON w.supplier_id=s.id WHERE w.status='pending' GROUP BY s.id"
    ):
        add(f"return_due:{sup['id']}", "vendor_return", "medium",
            f"Return pending: {sup['name']}",
            f"{sup['n']} waste lot{'s' if sup['n'] > 1 else ''} worth ₹{sup['v']:,.0f} awaiting "
            f"a return-to-vendor request.",
            details=dict(supplier_id=sup["id"], qty=sup["n"], value=round(sup["v"] or 0, 2)))

    # ---- reorder requests awaiting manager approval ----------------------
    for r in db.query(
        "SELECT r.id, r.qty, r.due_date, d.name AS drug, s.name AS supplier "
        "FROM reorders r JOIN drugs d ON d.id=r.drug_id "
        "LEFT JOIN suppliers s ON s.id=r.supplier_id "
        "WHERE r.status IN ('suggested','ordered')"
    ):
        add(f"po_pending:{r['id']}", "approval_request", "medium",
            f"Low-supply request awaiting approval: {r['drug']}",
            f"Purchase request for {r['qty']} units of {r['drug']}"
            + (f" from {r['supplier']}" if r["supplier"] else "")
            + (f" (due {r['due_date']})" if r["due_date"] else "")
            + " \u2014 manager to review; a supplier draft email is in the outbox.",
            details=dict(reorder_id=r["id"], qty=r["qty"]))

    # ---- data quality -----------------------------------------------------
    for up in db.query(
        "SELECT id, filename, rows_total, rows_rejected, issues, created_at FROM uploads "
        "WHERE rows_rejected > 0 ORDER BY id DESC LIMIT 10"
    ):
        issues = db.jload(up["issues"], {}) or {}
        counts = issues.get("counts", {})
        top = ", ".join(f"{k}: {v}" for k, v in list(counts.items())[:4]) or "see report"
        add(f"dq:{up['id']}", "data_quality", "medium",
            f"Data quality: {up['filename']}",
            f"{up['rows_rejected']} of {up['rows_total']} rows quarantined ({top}). "
            f"They are excluded from stock, forecasts and alerts.",
            details=dict(upload_id=up["id"], counts=counts))

    # ---- upsert ------------------------------------------------------------
    now = db.now_iso()
    conn = db.conn()
    existing = {r["dedup_key"]: r for r in db.query("SELECT * FROM alerts WHERE status != 'resolved'")}
    for key, alert in found.items():
        if key in existing:
            conn.execute(
                "UPDATE alerts SET severity=?, title=?, message=?, details=?, drug_id=?, "
                "batch_id=?, updated_at=? WHERE dedup_key=?",
                (alert["severity"], alert["title"], alert["message"], alert["details"],
                 alert["drug_id"], alert["batch_id"], now, key),
            )
        else:
            conn.execute(
                "INSERT INTO alerts(atype, severity, drug_id, batch_id, title, message, details, "
                "dedup_key, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (alert["atype"], alert["severity"], alert["drug_id"], alert["batch_id"],
                 alert["title"], alert["message"], alert["details"], key, "active", now, now),
            )
    for key in existing:
        if key not in found:
            conn.execute("UPDATE alerts SET status='resolved', updated_at=? WHERE dedup_key=?",
                         (now, key))
    conn.commit()
    return list_alerts()


def reorder_plan_for(drug: dict) -> dict:
    """Plan from the cached forecast when available (cheap), else a quick fit."""
    row = db.one(
        "SELECT payload FROM forecasts WHERE drug_id=? AND is_current=1 ORDER BY id DESC LIMIT 1",
        (drug["id"],),
    )
    payload = db.jload(row["payload"], {}) if row else None
    if payload and payload.get("reorder"):
        plan = dict(payload["reorder"])
        # stock figures can change between forecast runs -> refresh them
        live = available_stock(drug["id"])
        plan["available"] = int(live["usable"])
        plan["expired_stock"] = int(live["expired"])
        plan["available_value"] = round(live["usable_value"], 2)
        plan["avg_daily"] = payload.get("avg_daily", 0)
        return plan
    live = available_stock(drug["id"])
    return dict(available=int(live["usable"]), expired_stock=int(live["expired"]),
                available_value=round(live["usable_value"], 2),
                reorder_point=float(drug.get("reorder_point") or 0),
                cover_days=9999, lead_time_days=drug.get("lead_time_days") or 7,
                safety_stock=0, order_qty=0, status="unknown", avg_daily=0)


def list_alerts(status: str | None = None, severity: str | None = None,
                limit: int = 200) -> list[dict]:
    sql = ("SELECT a.*, d.name AS drug FROM alerts a LEFT JOIN drugs d ON d.id=a.drug_id "
           "WHERE a.status != 'resolved'")
    params: list = []
    if status:
        sql += " AND a.status=?"
        params.append(status)
    if severity:
        sql += " AND a.severity=?"
        params.append(severity)
    sql += " ORDER BY CASE a.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
    sql += "WHEN 'medium' THEN 2 ELSE 3 END, a.updated_at DESC LIMIT ?"
    params.append(limit)
    rows = db.query(sql, params)
    for r in rows:
        r["details"] = db.jload(r["details"], {})
    return rows


def summary() -> dict:
    rows = db.query("SELECT severity, COUNT(*) AS n FROM alerts WHERE status='active' GROUP BY severity")
    counts = {r["severity"]: r["n"] for r in rows}
    acked = db.scalar("SELECT COUNT(*) FROM alerts WHERE status='acknowledged'")
    return dict(critical=counts.get("critical", 0), high=counts.get("high", 0),
                medium=counts.get("medium", 0), low=counts.get("low", 0),
                active=sum(counts.values()), acknowledged=acked)


def acknowledge(alert_id: int) -> None:
    db.execute("UPDATE alerts SET status='acknowledged', updated_at=? WHERE id=?",
               (db.now_iso(), alert_id))
