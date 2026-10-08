"""Dashboard analytics: KPIs + chart bundles served to the SPA."""
from datetime import date, timedelta

from . import db
from .alerts import summary as alert_summary
from .forecasting import current_forecasts


def overview() -> dict:
    today_d = date.today()
    today = today_d.isoformat()
    settings = db.settings()

    stock = db.one(
        "SELECT "
        "COALESCE(SUM(CASE WHEN expiry_date>=? THEN qty_remaining ELSE 0 END),0) AS usable_qty, "
        "COALESCE(SUM(CASE WHEN expiry_date>=? THEN qty_remaining*unit_cost ELSE 0 END),0) AS usable_value, "
        "COALESCE(SUM(CASE WHEN expiry_date<? THEN qty_remaining ELSE 0 END),0) AS expired_qty, "
        "COALESCE(SUM(CASE WHEN expiry_date<? THEN qty_remaining*unit_cost ELSE 0 END),0) AS expired_value, "
        "COUNT(*) AS batches FROM batches",
        (today, today, today, today))
    retail = db.scalar(
        "SELECT COALESCE(SUM(b.qty_remaining * d.mrp),0) FROM batches b JOIN drugs d ON d.id=b.drug_id "
        "WHERE b.expiry_date>=? OR b.expiry_date IS NULL", (today,))

    expiring = db.scalar(
        "SELECT COUNT(*) FROM batches WHERE qty_remaining>0 AND expiry_date IS NOT NULL "
        "AND expiry_date>=? AND expiry_date<=?",
        (today, (today_d + timedelta(days=int(settings.get("expiry_warning_days", 90)))).isoformat()))
    expiring_90d_value = db.scalar(
        "SELECT COALESCE(SUM(qty_remaining*unit_cost),0) FROM batches WHERE qty_remaining>0 "
        "AND expiry_date IS NOT NULL AND expiry_date>=? AND expiry_date<=?",
        (today, (today_d + timedelta(days=int(settings.get("expiry_warning_days", 90)))).isoformat()))

    low_stock = 0
    order_now = 0
    for p in current_forecasts():
        plan = p.get("reorder", {})
        if plan.get("available", 0) < plan.get("reorder_point", 0):
            low_stock += 1
        if plan.get("status") == "order_now":
            order_now += 1

    rev30 = db.one(
        "SELECT COALESCE(SUM(revenue),0) AS rev, COALESCE(SUM(qty),0) AS qty FROM sales_daily "
        "WHERE date >= ?", ((today_d - timedelta(days=30)).isoformat(),))

    # latest day present in the feed (the dataset ends before today, so the
    # rolling sales windows are anchored on data freshness, not the wall clock)
    data_to = db.scalar("SELECT MAX(date) FROM sales") or today
    anchor = date.fromisoformat(data_to)
    rev_last30 = db.one(
        "SELECT COALESCE(SUM(revenue),0) AS rev, COALESCE(SUM(qty),0) AS qty FROM sales_daily "
        "WHERE date > ? AND date <= ?",
        ((anchor - timedelta(days=30)).isoformat(), data_to))
    rev_prev30_data = db.scalar(
        "SELECT COALESCE(SUM(revenue),0) FROM sales_daily WHERE date > ? AND date <= ?",
        ((anchor - timedelta(days=60)).isoformat(), (anchor - timedelta(days=30)).isoformat()))
    waste = db.one("SELECT COALESCE(SUM(value),0) AS v, COALESCE(SUM(qty),0) AS q, COUNT(*) AS n FROM waste")

    forecasts = current_forecasts()
    # headline accuracy is measured on 7-day buckets (the planning bucket);
    # daily wMAPE on bursty POS data is reported alongside it
    accs = [p["metrics"].get("mape_weekly") for p in forecasts if p.get("metrics")]
    accs = [a for a in accs if a is not None]
    accuracy = round(max(0.0, 100 - sum(accs) / len(accs)), 1) if accs else None
    daily_w = [p["metrics"].get("wmape") for p in forecasts if p.get("metrics")]
    daily_w = [a for a in daily_w if a is not None]
    accuracy_daily = round(max(0.0, 100 - sum(daily_w) / len(daily_w)), 1) if daily_w else None

    # rolling 90-day view anchored on the newest data (not the wall clock)
    anchor = date.fromisoformat(data_to)
    series = db.query(
        "SELECT date, SUM(qty) AS qty, SUM(revenue) AS revenue FROM sales_daily "
        "WHERE date > ? AND date <= ? GROUP BY date ORDER BY date",
        ((anchor - timedelta(days=90)).isoformat(), data_to))
    return dict(
        pharmacy=settings.get("pharmacy_name"),
        as_of=today,
        sku_count=db.scalar("SELECT COUNT(*) FROM drugs"),
        batch_count=stock["batches"],
        supplier_count=db.scalar("SELECT COUNT(*) FROM suppliers"),
        usable_qty=stock["usable_qty"], usable_value=round(stock["usable_value"], 2),
        retail_value=round(retail, 2),
        expired_qty=stock["expired_qty"], expired_value=round(stock["expired_value"], 2),
        expiring_90d=expiring, expiring_90d_value=round(expiring_90d_value, 2),
        low_stock=low_stock, order_now=order_now,
        sales_30d_qty=rev_last30["qty"], sales_30d_rev=round(rev_last30["rev"], 2),
        sales_prev30_rev=round(rev_prev30_data, 2),
        sales_growth_pct=round((rev_last30["rev"] - rev_prev30_data) / rev_prev30_data * 100, 1)
        if rev_prev30_data else None,
        sales_window=f"{(anchor - timedelta(days=29)).isoformat()} to {data_to}",
        sales_window_90=f"{(anchor - timedelta(days=89)).isoformat()} to {data_to}",
        data_staleness_days=(today_d - anchor).days,
        calendar_30d_rev=round(rev30["rev"], 2),
        waste_value=round(waste["v"], 2), waste_qty=waste["q"], waste_lots=waste["n"],
        forecast_accuracy=accuracy, forecast_accuracy_daily=accuracy_daily,
        alerts=alert_summary(),
        sales_series=[dict(date=r["date"], qty=r["qty"], revenue=round(r["revenue"], 2)) for r in series],
        total_sales=db.scalar("SELECT COUNT(*) FROM sales"),
        total_purchases=db.scalar("SELECT COUNT(*) FROM purchases"),
        quarantined=db.scalar("SELECT COUNT(*) FROM quarantined"),
        data_from=db.scalar("SELECT MIN(date) FROM sales"),
        data_to=db.scalar("SELECT MAX(date) FROM sales"),
    )


def sales_monthly(months: int = 24) -> list[dict]:
    rows = db.query(
        "SELECT substr(date,1,7) AS month, SUM(qty) AS qty, SUM(revenue) AS revenue "
        "FROM sales_daily GROUP BY month ORDER BY month DESC LIMIT ?", (months,))
    return list(reversed(rows))


def category_breakdown() -> list[dict]:
    return db.query(
        "SELECT d.category, COUNT(DISTINCT d.id) AS skus, "
        "COALESCE(SUM(b.qty_remaining * b.unit_cost),0) AS value "
        "FROM drugs d LEFT JOIN batches b ON b.drug_id=d.id "
        "GROUP BY d.category ORDER BY value DESC")


def top_drugs(limit: int = 8, anchor: str | None = None) -> list[dict]:
    data_to = anchor or db.scalar("SELECT MAX(date) FROM sales")
    cutoff = ((date.fromisoformat(data_to) - timedelta(days=90)).isoformat()
              if data_to else (date.today() - timedelta(days=90)).isoformat())
    rows = db.query(
        "SELECT d.name, d.category, SUM(sd.qty) AS qty, SUM(sd.revenue) AS revenue "
        "FROM sales_daily sd JOIN drugs d ON d.id=sd.drug_id WHERE sd.date>=? "
        "GROUP BY d.id ORDER BY revenue DESC LIMIT ?", (cutoff, limit))
    if not rows:
        rows = db.query(
            "SELECT d.name, d.category, SUM(sd.qty) AS qty, SUM(sd.revenue) AS revenue "
            "FROM sales_daily sd JOIN drugs d ON d.id=sd.drug_id "
            "GROUP BY d.id ORDER BY revenue DESC LIMIT ?", (limit,))
    return rows


def expiry_timeline() -> dict:
    rows = db.query(
        "SELECT b.expiry_date, b.qty_remaining, b.unit_cost FROM batches b "
        "WHERE b.qty_remaining>0 AND b.expiry_date IS NOT NULL ORDER BY b.expiry_date")
    buckets: dict[str, dict] = {}
    for r in rows:
        d = date.fromisoformat(r["expiry_date"])
        key = f"{d.year}-{(d.month - 1) // 3 * 3 + 1:02d}"
        b = buckets.setdefault(key, dict(bucket=key, qty=0, value=0.0))
        b["qty"] += r["qty_remaining"]
        b["value"] += r["qty_remaining"] * r["unit_cost"]
    out = [dict(bucket=k, qty=v["qty"], value=round(v["value"], 2))
           for k, v in sorted(buckets.items())]
    return dict(buckets=out)


def supplier_spend() -> list[dict]:
    return db.query(
        "SELECT s.name AS supplier, COALESCE(SUM(p.total),0) AS spend, "
        "(SELECT COALESCE(SUM(w.value),0) FROM waste w WHERE w.supplier_id=s.id) AS waste "
        "FROM suppliers s LEFT JOIN purchases p ON p.supplier_id=s.id GROUP BY s.id ORDER BY spend DESC")


def forecast_vs_actual(drug_id: int, days: int = 56) -> dict:
    """Back-test view: model forecast for the holdout window vs what actually sold."""
    from .forecasting import daily_series, fit_best

    dates, values = daily_series(drug_id)
    if len(values) < 40:
        return dict(points=[], metrics={})
    fitted = fit_best(values)
    pred, actual = fitted.get("holdout_pred") or [], fitted.get("holdout_actual") or []
    if not pred:
        return dict(points=[], metrics=fitted.get("metrics", {}), model=fitted["model"])
    h = len(pred)
    points = [dict(date=dates[len(values) - h + i].isoformat(),
                   actual=actual[i], predicted=pred[i]) for i in range(h)]
    return dict(points=points, metrics=fitted.get("metrics", {}), model=fitted["model"])


def category_of_sales() -> list[dict]:
    return db.query(
        "SELECT d.category, SUM(sd.revenue) AS revenue, SUM(sd.qty) AS qty "
        "FROM sales_daily sd JOIN drugs d ON d.id=sd.drug_id GROUP BY d.category ORDER BY revenue DESC")
