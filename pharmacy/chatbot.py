"""Inventory chatbot: intent parsing over live stock / forecast / waste data.

Default engine is deterministic (offline, fast, auditable).  If an
OpenAI-compatible endpoint is configured (settings `llm_model` or env
`OPENAI_BASE_URL`/`OPENAI_API_KEY`), it is used to word the answer while every
number still comes from the database.
"""
import json
import math
import os
import re
import urllib.request
from datetime import date, timedelta

from . import catalog, db
from .alerts import list_alerts, summary as alert_summary
from .forecasting import current_forecasts, forecast_drug
from .shelf import waste_summary
from .suppliers import supplier_stats

RUPEE = "₹"

CATEGORY_WORDS = {
    "antibiotic": "Antibiotic", "antibiotics": "Antibiotic",
    "diabetes": "Antidiabetic", "diabetic": "Antidiabetic", "sugar": "Antidiabetic",
    "heart": "Cardiovascular", "cardio": "Cardiovascular", "blood pressure": "Cardiovascular",
    "bp": "Cardiovascular", "stomach": "Gastrointestinal", "acidity": "Gastrointestinal",
    "gastric": "Gastrointestinal", "pain": "Analgesic & Antipyretic",
    "fever": "Analgesic & Antipyretic", "allergy": "Antiallergic",
    "allergies": "Antiallergic", "cough": "Respiratory", "vitamin": "Vitamins & Supplements",
    "supplement": "Vitamins & Supplements",
}

ACTION_WORDS = ("create", "raise", "place", "generate", "send", "notify", "acknowledge",
                "ack", "show", "list", "give", "make", "draft", "record", "check",
                "what", "how", "when", "which", "tell", "is", "are", "do", "does")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9₹%.\s/-]", " ", str(text).lower()).split())


def find_drug(message: str) -> dict | None:
    n = _norm(message)
    drugs = db.query("SELECT * FROM drugs")
    for d in drugs:
        if d["norm_name"] and d["norm_name"] in n:
            return d
    for d in drugs:
        generic = (d["generic"] or "").lower()
        if generic and len(generic) > 3 and generic in n:
            return d
    # fuzzy: compare alphanumeric tokens ("dolo650" / "dolo")
    compact = n.replace(" ", "")
    for d in drugs:
        key = d["norm_name"].replace(" ", "")
        if key and (key in compact or (len(key) > 4 and key[:5] in compact)):
            return d
    return None


def find_category(message: str) -> str | None:
    n = _norm(message)
    for word, cat in CATEGORY_WORDS.items():
        if word in n:
            return cat
    drugs = db.query("SELECT DISTINCT category FROM drugs")
    for d in drugs:
        if d["category"] and d["category"].lower() in n:
            return d["category"]
    return None


def find_days(message: str, default: int = 30) -> int:
    n = _norm(message)
    m = re.search(r"(\d+)\s*(day|days|d)\b", n)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*(week|weeks|w)\b", n)
    if m:
        return int(m.group(1)) * 7
    m = re.search(r"(\d+)\s*(month|months)\b", n)
    if m:
        return int(m.group(1)) * 30
    if "week" in n:
        return 7
    if "month" in n:
        return 30
    if "today" in n or "tonight" in n:
        return 0
    if "year" in n:
        return 365
    return default


def find_period(message: str) -> tuple[date, date, str]:
    """Return (start, end, label)."""
    today = date.today()
    n = _norm(message)
    if "yesterday" in n:
        return today - timedelta(days=1), today - timedelta(days=1), "yesterday"
    if "last month" in n or "previous month" in n:
        start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        end = today.replace(day=1) - timedelta(days=1)
        return start, end, start.strftime("%b %Y")
    if "this month" in n:
        return today.replace(day=1), today, "this month"
    if "last 7" in n or "past week" in n or "last week" in n:
        return today - timedelta(days=7), today, "last 7 days"
    if "last year" in n or "past year" in n:
        return today - timedelta(days=365), today, "last 12 months"
    days = find_days(n, default=0)
    if days:
        return today - timedelta(days=days), today, f"last {days} days"
    return today - timedelta(days=30), today, "last 30 days"


def _money(v) -> str:
    return f"{RUPEE}{v:,.0f}"


def _card_table(title, columns, rows, note=""):
    return dict(type="table", title=title, columns=columns,
                rows=rows, note=note)


def _card_kpis(items, title=""):
    return dict(type="kpis", title=title, items=items)


def _card_list(title, items, note=""):
    return dict(type="list", title=title, items=items, note=note)


# --------------------------------------------------------------------------- #
# intents
# --------------------------------------------------------------------------- #
def _answer_stock(msg: str, drug) -> tuple[str, list]:
    from .forecasting import available_stock
    from .alerts import reorder_plan_for

    if drug:
        avail = available_stock(drug["id"])
        plan = reorder_plan_for(drug)
        cov = plan.get("cover_days", 0)
        text = (
            f"{drug['name']} ({drug['generic'] or '—'}): **{avail['usable']} units** usable "
            f"in stock worth {RUPEE}{avail['usable_value']:,.0f}. "
            f"{avail['expired']} units are locked in expired batches. "
            f"Cover ≈ {cov:.0f} days at current demand; reorder point is "
            f"{plan.get('reorder_point', 0):.0f} units."
        )
        cards = [_card_kpis([
            dict(label="Usable stock", value=f"{avail['usable']}", tone="ok"),
            dict(label="Expired (blocked)", value=f"{avail['expired']}", tone="bad"),
            dict(label="Stock value", value=_money(avail["usable_value"]), tone=""),
            dict(label="Days of cover", value=f"{cov:.0f}", tone="warn" if cov < 30 else ""),
            dict(label="Reorder point", value=f"{plan.get('reorder_point', 0):.0f}", tone=""),
            dict(label="Reorder status", value=str(plan.get("status", "-")).replace("_", " "),
                 tone="bad" if plan.get("status") == "order_now" else "ok"),
        ], title=f"{drug['name']} — stock position")]
        actions = [dict(label=f"Forecast {drug['name']}", intent=f"forecast for {drug['name']}"),
                   dict(label="Reorder plan", intent=f"what should I reorder for {drug['name']}")]
        return text, cards + actions_cards(actions)

    rows = []
    for p in sorted(current_forecasts(), key=lambda x: x.get("reorder", {}).get("cover_days", 0)):
        plan = p.get("reorder", {})
        rows.append([p["drug"], p.get("generic", ""), plan.get("available", 0),
                     plan.get("expired_stock", 0), f"{plan.get('cover_days', 0):.0f}",
                     f"{plan.get('reorder_point', 0):.0f}",
                     str(plan.get("status", "-")).replace("_", " ")])
    total = sum(r[2] for r in rows)
    text = (f"Usable stock across {len(rows)} SKUs is **{total} units**. "
            f"Lowest cover is shown first — SKUs marked *order now* need replenishment.")
    cards = [_card_table("Live stock by SKU",
                         ["Medicine", "Generic", "Usable", "Expired", "Cover (d)",
                          "ROP", "Status"], rows)]
    return text, cards


def actions_cards(actions: list[dict]) -> list[dict]:
    return [dict(type="actions", items=actions)] if actions else []


def _answer_substitute(msg: str, drug) -> tuple[str, list]:
    """Therapeutic substitution: same molecule first, then same category."""
    if not drug:
        return ("Name the medicine you want an alternative for — e.g. "
                "“substitute for Dolo 650”."), []
    name = drug["name"]
    generic = drug.get("generic") or ""
    subs = catalog.substitutes(drug.get("norm_name") or "")
    if generic:
        for r in db.query(
            "SELECT name, category FROM drugs WHERE id!=? AND generic=? LIMIT 5",
            (drug["id"], generic),
        ):
            subs.insert(0, dict(name=r["name"], category=r["category"],
                                generic=generic, match="molecule"))
    if subs:
        text = (f"**Substitution for {name}:** "
                + "; ".join(f"{s['name']} ({s['category']}, {s['match']} match)"
                            for s in subs[:4])
                + ". Confirm with the pharmacist before switching brands.")
        card = _card_table("Substitution suggestions",
                           ["Alternative", "Category", "Match"],
                           [[s["name"], s["category"], s["match"]] for s in subs[:4]])
        return text, [card]
    return (f"No equivalent is stocked for **{name}**"
            + (f" ({generic})" if generic else "") +
            ". No same-molecule or same-category brand exists in this catalogue, so no safe "
            "substitution can be suggested — source the molecule from an alternate "
            "manufacturer instead (pharmacist to confirm)."), []


def _answer_price(msg: str, drug) -> tuple[str, list]:
    """Unit-price trend: 90-day avg vs the prior 90 days, anchored at feed end."""
    anchor = db.scalar("SELECT MAX(date) FROM sales WHERE source!='pos_sim'")
    if not anchor:
        return "No sales history yet — there is no price trend to report.", []

    def windows(did: int):
        pr = db.one(
            "SELECT AVG(unit_price) AS p, COUNT(*) AS n FROM sales "
            "WHERE drug_id=? AND source!='pos_sim' AND date > date(?, '-90 day') "
            "AND date <= ? AND unit_price>0", (did, anchor, anchor))
        pp = db.one(
            "SELECT AVG(unit_price) AS p, COUNT(*) AS n FROM sales "
            "WHERE drug_id=? AND source!='pos_sim' AND date <= date(?, '-90 day') "
            "AND date > date(?, '-180 day') AND unit_price>0", (did, anchor, anchor))
        if not (pr and pp and pr["n"] >= 15 and pp["n"] >= 15 and pp["p"]):
            return None
        return pr["p"], pp["p"], (pr["p"] / pp["p"] - 1) * 100

    if drug:
        w = windows(drug["id"])
        if not w:
            return (f"Not enough sales history to price-trend {drug['name']} "
                    f"(needs ≥15 sales in each 90-day window)."), []
        now, was, pct = w
        verdict = "rising" if pct >= 2 else ("falling" if pct <= -2 else "stable")
        text = (f"**{drug['name']} price trend** (90 days to {anchor} vs prior 90 days): "
                f"₹{now:.2f} vs ₹{was:.2f} — **{verdict}** ({pct:+.1f}%).")
        if pct >= 8:
            text += (" Rising fast — consider stocking up before further hikes "
                     "(see the price-rising alert for a suggested order quantity).")
        cards = [dict(type="kpis", title="Unit price (avg)", items=[
            dict(label="Last 90d", value=f"₹{now:.2f}"),
            dict(label="Prior 90d", value=f"₹{was:.2f}"),
            dict(label="Change", value=f"{pct:+.1f}%",
                 tone="bad" if pct >= 2 else ("ok" if pct <= -2 else "")),
        ])]
        return text, cards
    rows = []
    for d in db.query("SELECT id, name FROM drugs ORDER BY name"):
        w = windows(d["id"])
        rows.append([d["name"], f"₹{w[0]:.2f}", f"₹{w[1]:.2f}", f"{w[2]:+.1f}%"]
                    if w else [d["name"], "–", "–", "n/a"])
    return (f"Unit-price trend per medicine (90 days to {anchor} vs prior 90 days):"), [
        _card_table("Price trends", ["Medicine", "Now (90d)", "Prior 90d", "Change"], rows)]


def _answer_expiry(msg: str, drug, days: int) -> tuple[str, list]:
    today = date.today()
    params = []
    sql = ("SELECT b.batch_no, d.name AS drug, b.expiry_date, b.qty_remaining, b.unit_cost, "
           "s.name AS supplier, CAST(julianday(b.expiry_date)-julianday(?) AS INTEGER) AS days_left "
           "FROM batches b JOIN drugs d ON d.id=b.drug_id LEFT JOIN suppliers s ON s.id=b.supplier_id "
           "WHERE b.qty_remaining>0 AND b.expiry_date IS NOT NULL")
    params.append(today.isoformat())
    n = _norm(msg)
    wants_future = bool(re.search(r"\b(next|upcoming|coming|within|future)\b", n))
    wants_past = bool(re.search(r"\b(already|past|blocked|lapsed)\b", n))
    if wants_past and not wants_future:
        sql += " AND b.expiry_date < ?"
        params.append(today.isoformat())
    elif wants_future and not wants_past:
        sql += " AND b.expiry_date >= ? AND b.expiry_date <= ?"
        params.append(today.isoformat())
        params.append((today + timedelta(days=max(days, 1))).isoformat())
    else:
        sql += " AND b.expiry_date <= ?"
        params.append((today + timedelta(days=max(days, 1))).isoformat())
    if drug:
        sql += " AND b.drug_id=?"
        params.append(drug["id"])
    sql += " ORDER BY b.expiry_date LIMIT 60"
    rows = db.query(sql, params)
    if not rows:
        return "No batches match that expiry window — nothing needs attention there.", []
    value = sum(r["qty_remaining"] * r["unit_cost"] for r in rows)
    expired = [r for r in rows if r["days_left"] is not None and r["days_left"] < 0]
    text = (f"**{len(rows)} batches** ({len(expired)} already expired) worth **{_money(value)}** "
            f"match the window. Earliest: {rows[0]['drug']} / {rows[0]['batch_no']} on "
            f"{rows[0]['expiry_date']}.")
    table = _card_table(
        "Expiry watchlist (FEFO order)",
        ["Batch", "Medicine", "Expiry", "Days left", "Qty", "Value", "Supplier"],
        [[r["batch_no"], r["drug"], r["expiry_date"],
          ("EXPIRED" if r["days_left"] < 0 else r["days_left"]),
          r["qty_remaining"], _money(r["qty_remaining"] * r["unit_cost"]),
          r["supplier"] or "—"] for r in rows])
    actions = actions_cards([
        dict(label="Draft vendor return", intent="which suppliers should I return expired stock to"),
        dict(label="Waste report", intent="show waste report"),
        dict(label="⬇ Expiry report (CSV)", href="/api/reports/expiry.csv"),
    ])
    return text, [table] + actions


def _answer_reorder(msg: str, drug) -> tuple[str, list]:
    plans = []
    for p in current_forecasts():
        if drug and p["drug_id"] != drug["id"]:
            continue
        plan = p.get("reorder", {})
        if plan:
            plans.append((p, plan))
    plans.sort(key=lambda x: (x[1].get("status") != "order_now",
                              x[1].get("due_date") or "9999"))
    rows = [[p["drug"], plan.get("available", 0), f"{plan.get('demand_lead_time', 0):.0f}",
             f"{plan.get('safety_stock', 0):.0f}", f"{plan.get('reorder_point', 0):.0f}",
             plan.get("order_qty", 0), plan.get("due_date") or "—",
             str(plan.get("status")).replace("_", " ")]
            for p, plan in plans]
    due = [r for r in rows if r[7] == "order now" or r[7] == "order_now"]
    text = (f"AI reorder plan for **{len(rows)} SKUs** — {len(due)} need ordering now. "
            f"Quantities = demand over lead time + review period + safety stock "
            f"(95% service level), minus usable stock.")
    table = _card_table("Reorder suggestions",
                        ["Medicine", "On hand", "LT demand", "Safety", "ROP",
                         "Order qty", "Due", "Status"], rows)
    actions = actions_cards([
        dict(label="Draft supplier POs", intent="draft purchase orders for items that need reordering"),
        dict(label="Show low stock only", intent="show low stock items"),
    ])
    return text, [table] + actions


def _answer_sales(msg: str, drug, period) -> tuple[str, list]:
    start, end, label = period
    params = [start.isoformat(), end.isoformat()]
    sql = ("SELECT d.name AS drug, d.category, SUM(sd.qty) AS qty, SUM(sd.revenue) AS revenue "
           "FROM sales_daily sd JOIN drugs d ON d.id=sd.drug_id WHERE sd.date>=? AND sd.date<=?")
    if drug:
        sql += " AND d.id=?"
        params.append(drug["id"])
    sql += " GROUP BY d.id ORDER BY revenue DESC"
    rows = db.query(sql, params)
    note = ""
    if not rows or not rows[0]["qty"]:
        # no activity in that window (feed gap): fall back to the newest data
        latest = db.scalar("SELECT MAX(date) FROM sales")
        if not latest:
            return f"No sales recorded for {label}.", []
        from datetime import date as _date, timedelta as _td
        anchor = _date.fromisoformat(latest)
        start, end, label = anchor - _td(days=30), anchor, "latest 30 days of data"
        note = f"(no sales in {label}; showing window ending {latest})"
        params = [start.isoformat(), end.isoformat()]
        sql = ("SELECT d.name AS drug, d.category, SUM(sd.qty) AS qty, SUM(sd.revenue) AS revenue "
               "FROM sales_daily sd JOIN drugs d ON d.id=sd.drug_id WHERE sd.date>=? AND sd.date<=?")
        if drug:
            sql += " AND d.id=?"
            params.append(drug["id"])
        sql += " GROUP BY d.id ORDER BY revenue DESC"
        rows = db.query(sql, params)
        note = f" — no sales in the requested window, so this covers {start.isoformat()} to {end.isoformat()}."
    total_qty = sum(r["qty"] for r in rows)
    total_rev = sum(r["revenue"] for r in rows)
    top = rows[0]
    text = (f"*{label.capitalize()}*: {total_qty} units sold for {_money(total_rev)}. "
            f"Top mover: {top['drug']} ({top['qty']} units, {_money(top['revenue'])})." + note)
    cards = [
        _card_kpis([dict(label="Units sold", value=f"{total_qty}", tone=""),
                    dict(label="Revenue", value=_money(total_rev), tone=""),
                    dict(label="Top mover", value=top["drug"], tone="ok"),
                    dict(label="SKUs selling", value=str(len(rows)), tone="")],
                   title=f"Sales — {label}"),
        _card_table("Sales by medicine", ["Medicine", "Category", "Units", "Revenue"],
                    [[r["drug"], r["category"], r["qty"], _money(r["revenue"])] for r in rows]),
    ]
    return text, cards


def _answer_forecast(msg: str, drug) -> tuple[str, list]:
    targets = [drug] if drug else []
    if not targets:
        payload = current_forecasts()
    else:
        payload = [forecast_drug(drug["id"])]
    rows, cards = [], []
    for p in (payload if isinstance(payload, list) else []):
        plan = p.get("reorder", {})
        fut = sum(d["qty"] for d in p.get("daily", []))
        rows.append([p["drug"], p.get("model", "-"), p.get("avg_daily", 0),
                     round(fut, 0), p.get("metrics", {}).get("wmape"),
                     p.get("trend_vs_prev", 0), plan.get("status", "-")])
        if drug:
            chart = _card_list(
                f"Next {p.get('horizon_days')} days — {p['drug']}",
                [f"{d['date']}: {d['qty']} units (80% CI {d['lo']}–{d['hi']})"
                 for d in p.get("daily", [])[:14]],
                note=f"model={p.get('model')} · wMAPE={p.get('metrics', {}).get('wmape')}% "
                     f"vs baseline {p.get('baseline_wmape')}% ({p.get('baseline_model')})")
            cards.append(chart)
    rows.sort(key=lambda r: -(r[3] or 0))
    if not rows:
        return "Not enough history to forecast yet — upload a few more days of sales.", []
    text = (f"Forecasting model (damped Holt-Winters with weekly seasonality, selected by "
            f"back-test) predicts **{sum(r[3] or 0 for r in rows):,.0f} units** over the next "
            f"30 days across {len(rows)} SKUs.")
    table = _card_table("30-day demand forecast",
                        ["Medicine", "Model", "Units/day", "30d demand",
                         "wMAPE %", "Trend %", "Reorder"], rows)
    return text, [table] + cards


def _answer_waste(msg: str) -> tuple[str, list]:
    s = waste_summary()
    rows = db.query(
        "SELECT d.name AS drug, w.reason, w.qty, w.value, w.status, b.batch_no, s.name AS supplier "
        "FROM waste w JOIN drugs d ON d.id=w.drug_id LEFT JOIN batches b ON b.id=w.batch_id "
        "LEFT JOIN suppliers s ON s.id=w.supplier_id ORDER BY w.value DESC LIMIT 15")
    text = (f"Total waste: **{s['total_lots']} lots / {s['total_qty']} units / "
            f"{_money(s['total_value'])}** — {s['waste_pct_of_purchases']}% of purchase value."
            + (f" Another {_money(s['value_at_risk'])} of expired stock is still awaiting write-off."
               if s.get("value_at_risk") else " All expired lots are already booked for write-off."))
    cards = [
        _card_kpis([
            dict(label="Waste value", value=_money(s["total_value"]), tone="bad"),
            dict(label="Waste units", value=f"{s['total_qty']}", tone=""),
            dict(label="% of purchases", value=f"{s['waste_pct_of_purchases']}%", tone="warn"),
            dict(label="Value at risk", value=_money(s["value_at_risk"]), tone="warn"),
        ]),
        _card_table("Top waste lots", ["Medicine", "Reason", "Batch", "Qty", "Value", "Status", "Supplier"],
                    [[r["drug"], r["reason"], r["batch_no"] or "—", r["qty"],
                      _money(r["value"]), r["status"], r["supplier"] or "—"] for r in rows]),
    ]
    actions = actions_cards([dict(label="Export waste report", intent="export waste report csv")])
    return text, cards + actions


def _answer_alerts(msg: str) -> tuple[str, list]:
    s = alert_summary()
    rows = list_alerts(limit=8)
    text = (f"**{s['active']} active alerts**: {s['critical']} critical, {s['high']} high, "
            f"{s['medium']} medium.")
    cards = [_card_kpis([
        dict(label="Critical", value=str(s["critical"]), tone="bad"),
        dict(label="High", value=str(s["high"]), tone="warn"),
        dict(label="Medium", value=str(s["medium"]), tone=""),
        dict(label="Acknowledged", value=str(s["acknowledged"]), tone="ok"),
    ])]
    if rows:
        cards.append(_card_table("Top alerts", ["Severity", "Type", "Title"],
                                 [[r["severity"], r["atype"].replace("_", " "), r["title"]]
                                  for r in rows]))
    actions = actions_cards([dict(label="Acknowledge all", intent="acknowledge all alerts")])
    return text, cards + actions


def _answer_suppliers(msg: str) -> tuple[str, list]:
    stats = supplier_stats()
    text = ("Supplier scorecard (higher = better shelf-life and less expiry waste):")
    cards = [_card_table("Suppliers",
                         ["Supplier", "Orders", "Spend", "Waste", "Waste %",
                          "Avg shelf life (d)", "Score"],
                         [[s["name"], s["orders"], _money(s["spend"]), _money(s["waste_value"]),
                           f"{s['waste_pct']}%", f"{s['avg_shelf_life_days']:.0f}", s["score"]]
                          for s in stats])]
    return text, cards


def _answer_list(msg: str, drug, category) -> tuple[str, list]:
    rows = db.query(
        "SELECT d.name, d.generic, d.category, d.form, d.schedule, d.mrp, "
        "COALESCE(SUM(CASE WHEN b.expiry_date>=date('now') THEN b.qty_remaining ELSE 0 END),0) AS stock "
        "FROM drugs d LEFT JOIN batches b ON b.drug_id=d.id "
        "GROUP BY d.id ORDER BY d.category, d.name")
    if category:
        rows = [r for r in rows if r["category"] == category]
    if drug:
        rows = [r for r in rows if r["name"] == drug["name"]]
    if not rows:
        return "No medicines match that.", []
    text = f"**{len(rows)} medicines**" + (f" in {category}" if category else "") + " in the catalogue."
    cards = [_card_table("Medicine catalogue",
                         ["Medicine", "Generic", "Category", "Form", "Schedule", "MRP", "Stock"],
                         [[r["name"], r["generic"] or "—", r["category"], r["form"],
                           r["schedule"], _money(r["mrp"]), r["stock"]] for r in rows])]
    return text, cards


# --------------------------------------------------------------------------- #
# actions (chatbot can drive the workflow)
# --------------------------------------------------------------------------- #
def _do_action(msg: str, drug, n: str) -> tuple[str, list]:
    from .suppliers import reorder_email, create_notification

    if "acknowledge" in n or re.search(r"\back\b", n):
        ids = [r["id"] for r in list_alerts(status="active", limit=500)]
        for i in ids:
            db.execute("UPDATE alerts SET status='acknowledged', updated_at=? WHERE id=?",
                       (db.now_iso(), i))
        return f"Acknowledged {len(ids)} active alerts.", []

    if "return" in n and ("vendor" in n or "supplier" in n or "return" in n):
        rows = db.query(
            "SELECT s.id, s.name, COUNT(*) AS lots, SUM(w.value) AS v FROM waste w "
            "JOIN suppliers s ON s.id=w.supplier_id WHERE w.status='pending' GROUP BY s.id")
        if not rows:
            return "No pending vendor returns — all expired waste is already filed.", []
        items = [f"{r['name']}: {r['lots']} lots, {_money(r['v'])}" for r in rows]
        return ("Return-to-vendor lots still pending, grouped by supplier. "
                "Open *Waste & Returns* to file the RTV request."), [
            _card_list("Return to vendor", items)]

    if "reorder" in n or "purchase order" in n or re.search(r"\bpo\b", n):
        from .alerts import reorder_plan_for

        targets = []
        for d in db.query("SELECT * FROM drugs"):
            if drug and d["id"] != drug["id"]:
                continue
            plan = reorder_plan_for(d)
            if plan.get("status") == "order_now" or ("reorder for" in n and drug):
                targets.append((d, plan))
        if not targets:
            return "Nothing needs reordering right now — every SKU is above its reorder point.", []
        created = []
        now = db.now_iso()
        pack = max(1, int(db.get_setting("pack_size", 10)))
        for d, plan in targets:
            qty = plan.get("order_qty") or 0
            if qty <= 0:
                # user asked explicitly for a drug that is above its reorder point:
                # order up to one cycle of demand + safety stock
                qty = int(math.ceil((plan.get("avg_daily", 0) * (
                    plan.get("lead_time_days", 7) + plan.get("review_days", 7))
                    + plan.get("safety_stock", 0)) / pack) * pack) or pack * 10
            db.execute(
                "INSERT INTO reorders(drug_id, supplier_id, qty, due_date, reason, status, source, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (d["id"], d["supplier_id"], qty, plan.get("due_date"),
                 f"AI: {plan['demand_lead_time']}d demand + {plan['safety_stock']} safety - "
                 f"{plan['available']} on hand", "ordered", "chatbot", now, now))
            supplier = db.one("SELECT * FROM suppliers WHERE id=?", (d["supplier_id"],)) or \
                db.one("SELECT * FROM suppliers ORDER BY id LIMIT 1")
            if supplier:
                subject, body = reorder_email(d, plan, supplier)
                create_notification(supplier["id"], subject, body, "reorder", d["id"], status="draft")
            created.append(f"{d['name']} × {qty}")
        text = f"Created {len(created)} reorder(s) and drafted supplier emails: " + ", ".join(created)
        return text, [_card_list("Reorders created", created)]

    if "return" in n and ("vendor" in n or "supplier" in n):
        rows = db.query(
            "SELECT s.id, s.name, COUNT(*) AS lots, SUM(w.value) AS v FROM waste w "
            "JOIN suppliers s ON s.id=w.supplier_id WHERE w.status='pending' GROUP BY s.id")
        if not rows:
            return "No pending vendor returns — all expired waste is already filed.", []
        items = [f"{r['name']}: {r['lots']} lots, {_money(r['v'])}" for r in rows]
        return "Pending return-to-vendor lots by supplier:", [
            _card_list("Return to vendor", items)]

    if "waste report" in n or "report" in n:
        return _answer_waste(msg)

    if "alert" in n:
        return _answer_alerts(msg)

    return ""


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
HELP_TEXT = (
    "I can answer from live inventory data. Try:\n"
    "• “stock of Dolo 650”\n"
    "• “what expires in the next 30 days”\n"
    "• “what should I reorder”\n"
    "• “sales last month”\n"
    "• “forecast for Telma 40”\n"
    "• “show waste report”\n"
    "• “create reorder for Pan 40”\n"
    "• “substitute for Dolo 650”\n"
    "• “price trend of Telma 40”\n"
    "• “which suppliers should I return expired stock to”"
)


def respond(message: str, use_llm: bool = True) -> dict:
    n = _norm(message)
    drug = find_drug(message)
    category = find_category(message)
    days = find_days(message, default=0)
    period = find_period(message)

    intent = "fallback"
    text, cards = "", []

    if re.search(r"\b(hi|hello|hey|namaste|good morning|good evening)\b", n) and len(n) < 30:
        intent, text = "greeting", (
            f"Hello! I'm the {db.get_setting('pharmacy_name')} inventory assistant. " + HELP_TEXT)
    elif re.search(r"\b(help|what can you do|commands|usage)\b", n):
        intent, text = "help", HELP_TEXT
    elif re.search(r"\b(acknowledge|ack)\b.*\balert", n) or re.search(r"\balert.*(acknowledge|ack)\b", n):
        intent = "action_ack_alerts"
        text, cards = _do_action(message, drug, n)
    elif re.search(r"\b(create|raise|place|draft|send)\b.*\b(reorder|purchase|po|order)\b", n) or \
            re.search(r"\b(draft|send|notify)\b.*\b(supplier|email|po)\b", n):
        intent = "action_reorder"
        text, cards = _do_action(message, drug, n)
    elif "return" in n and ("vendor" in n or "supplier" in n or "which supplier" in n):
        intent = "vendor_returns"
        text, cards = _do_action(message, drug, n)
    elif "waste" in n or "damaged" in n or "recalled" in n or "write-off" in n or "write off" in n:
        intent, text, cards = "waste", *_answer_waste(message)
    elif re.search(r"\b(alert|alerts|trigger|notification)s?\b", n) and "supplier" not in n:
        intent, text, cards = "alerts", *_answer_alerts(message)
    elif re.search(r"\b(supplier|vendor|distributor)s?\b", n) and "return" not in n and "reorder" not in n:
        intent, text, cards = "suppliers", *_answer_suppliers(message)
    elif re.search(r"\b(forecast|predict|prediction|project|projection|demand)\b", n):
        intent, text, cards = "forecast", *_answer_forecast(message, drug)
    elif re.search(r"\b(substitut\w*|alternatives?|equivalent)s?\b|instead of|"
                   r"\b(other|different|another) (brand|company|manufacturer)\b", n):
        intent, text, cards = "substitute", *_answer_substitute(message, drug)
    elif re.search(r"\b(prices?|pricing|mrp|expensive|hike)\b", n):
        intent, text, cards = "price", *_answer_price(message, drug)
    elif re.search(r"\b(reorder|re-order|restock|replenish|order now|low stock|running out|stockout|purchase)\b", n):
        intent, text, cards = "reorder", *_answer_reorder(message, drug)
    elif re.search(r"\b(expiry|expire|expires|expiring|expired|fefo|near expiry|shelf life)\b", n):
        intent, text, cards = "expiry", *_answer_expiry(message, drug, days or 30)
    elif re.search(r"\b(sale|sales|sold|selling|revenue|turnover|business|moved|mover|top)\b", n):
        intent, text, cards = "sales", *_answer_sales(message, drug, period)
    elif re.search(r"\b(stock|stocks|inventory|on hand|available|quantity|units|how many)\b", n) or drug:
        intent, text, cards = "stock", *_answer_stock(message, drug)
    elif re.search(r"\b(list|show|categor|medicines|drugs|catalog|products)\b", n):
        intent, text, cards = "catalogue", *_answer_list(message, drug, category)
    elif category:
        intent, text, cards = "catalogue", *_answer_list(message, drug, category)
    else:
        intent = "fallback"
        text = ("I’m the pharmacy inventory assistant — I can only answer pharmacy-related "
                "questions (stock, expiry, batches, suppliers, forecasts, waste, orders). "
                "I can’t help with unrelated topics. ") + HELP_TEXT

    if not text:
        intent = "fallback"
        text = "Nothing to report there. " + HELP_TEXT

    if use_llm:
        text = _llm_polish(message, text) or text

    db.execute("INSERT INTO chat_log(role, message, meta, created_at) VALUES(?,?,?,?)",
               ("user", message, db.jdump(dict(intent=intent)), db.now_iso()))
    db.execute("INSERT INTO chat_log(role, message, meta, created_at) VALUES(?,?,?,?)",
               ("assistant", text, db.jdump(dict(intent=intent, cards=len(cards))), db.now_iso()))
    return dict(text=text, cards=cards, intent=intent)


def history(limit: int = 40) -> list[dict]:
    return db.query("SELECT role, message, meta, created_at FROM chat_log ORDER BY id DESC LIMIT ?",
                    (limit,))[::-1]


# --------------------------------------------------------------------------- #
# optional LLM polish
# --------------------------------------------------------------------------- #
def _llm_polish(question: str, drafted: str) -> str | None:
    base = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
    key = os.environ.get("OPENAI_API_KEY", "")
    model = db.get_setting("llm_model", "") or os.environ.get("LLM_MODEL", "")
    if not (base and key and model):
        return None
    prompt = (
        "You are a pharmacy inventory assistant. Numbers are already computed; rewrite the "
        "draft into one short, friendly sentence without changing any figure.\n\n"
        f"Question: {question}\nDraft: {drafted}"
    )
    try:
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                             "temperature": 0.2}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None
