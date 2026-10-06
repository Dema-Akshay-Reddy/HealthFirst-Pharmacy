"""Ingestion pipeline: daily Excel/CSV/JSON uploads -> validated records -> stock.

Handles the noisy Zenith-2k25-MedTech feed as well as arbitrary daily uploads:
column aliasing, drug-name normalisation, date/qty/price validation, duplicate
detection, batch resolution and FEFO stock allocation.
"""
import io
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta

from . import db, shelfops
from .catalog import classify, display_name

COLUMN_ALIASES = {
    "drug_name": ["drug_name", "drug", "medicine", "medicine_name", "product",
                  "product_name", "item", "item_name", "brand", "brand_name",
                  "generic_name", "sku"],
    "date": ["date", "sale_date", "transaction_date", "date_sold", "txn_date",
             "invoice_date", "sold_on"],
    "date_received": ["date_received", "received_date", "purchase_date",
                      "order_date", "date", "grn_date"],
    "qty_sold": ["qty_sold", "quantity_sold", "quantity", "qty", "units_sold",
                 "units", "sale_qty", "quantity_sold_units"],
    "qty_received": ["qty_received", "quantity_received", "qty", "quantity",
                     "units_received", "order_qty", "received_qty"],
    "batch_number": ["batch_number", "batch", "batch_no", "batchnumber", "lot",
                     "lot_number", "lot_no"],
    "mrp_unit_price": ["mrp_unit_price", "mrp", "unit_price", "price",
                       "selling_price", "mrp_price", "retail_price"],
    "total_amount": ["total_amount", "total", "amount", "line_total", "revenue",
                     "sale_amount"],
    "txn_id": ["transaction_id", "txn_id", "sale_id", "invoice_no",
               "invoice_number", "bill_no", "receipt_no"],
    "purchase_id": ["purchase_id", "po_id", "po_number", "grn", "grn_id",
                    "order_id", "purchase_order"],
    "supplier_name": ["supplier_name", "supplier", "vendor", "vendor_name",
                      "distributor", "company"],
    "unit_cost_price": ["unit_cost_price", "unit_cost", "cost_price", "cost",
                        "purchase_price", "buy_price"],
    "total_purchase_cost": ["total_purchase_cost", "total_cost",
                            "purchase_total", "total_amount"],
    "expiry_date": ["expiry_date", "expiry", "exp_date", "expires_on",
                    "expiration_date", "expiry_dt"],
}

MAX_DATE = date.today() + timedelta(days=1)
MIN_DATE = date(2000, 1, 1)


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def _key(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).strip().lower())


def map_columns(fieldnames) -> dict:
    """canonical field -> original column name."""
    lookup = {_key(f): f for f in fieldnames}
    mapping = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            hit = lookup.get(_key(alias))
            if hit:
                mapping[canonical] = hit
                break
    return mapping


def parse_file(raw: bytes, filename: str) -> tuple[str, list[dict]]:
    """Return (kind, rows). kind is 'sales' | 'purchases' | 'unknown'."""
    name = (filename or "").lower()
    rows: list[dict] = []

    if name.endswith((".xlsx", ".xls")):
        import pandas as pd

        df = pd.read_excel(io.BytesIO(raw))
        rows = json.loads(df.to_json(orient="records"))
    elif name.endswith(".csv"):
        import pandas as pd

        df = pd.read_csv(io.BytesIO(raw))
        rows = json.loads(df.to_json(orient="records"))
    elif name.endswith(".json"):
        rows = _parse_json(raw)
    else:
        # try excel, then csv, then json
        for attempt in ("excel", "csv", "json"):
            try:
                if attempt == "excel":
                    import pandas as pd

                    df = pd.read_excel(io.BytesIO(raw))
                    rows = json.loads(df.to_json(orient="records"))
                elif attempt == "csv":
                    import pandas as pd

                    df = pd.read_csv(io.BytesIO(raw))
                    rows = json.loads(df.to_json(orient="records"))
                else:
                    rows = _parse_json(raw)
                break
            except Exception:
                rows = []
    if not rows:
        raise ValueError("Could not read any rows from the file")
    return detect_kind(list(rows[0].keys())), rows


def _parse_json(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    return v
            return [data]
    except Exception:
        pass
    # concatenated pretty-printed objects (Kaggle Zenith feed format)
    dec = json.JSONDecoder()
    items, i, n = [], 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        try:
            obj, i = dec.raw_decode(text, i)
        except Exception:
            break
        if isinstance(obj, dict):
            items.append(obj)
    return items


def detect_kind(columns) -> str:
    keys = {_key(c) for c in columns}
    if keys & {"qtyreceived", "expirydate", "suppliername", "unitcostprice",
               "daterceived", "totalpurchasecost"}:
        return "purchases"
    if keys & {"qtysold", "mrpunitprice", "totalamount", "transactionid"}:
        return "sales"
    return "unknown"


# --------------------------------------------------------------------------- #
# value coercion
# --------------------------------------------------------------------------- #
def parse_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and 20000 < float(value) < 80000:
        # excel serial date
        try:
            from datetime import timedelta as _td

            return date(1899, 12, 30) + _td(days=int(value))
        except Exception:
            return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%d-%m-%Y",
                "%m/%d/%Y", "%d-%b-%Y", "%d %b %Y", "%Y/%m/%d", "%b %d, %Y",
                "%d-%m-%y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19] if "T" in text or " " in text else text, fmt).date()
        except Exception:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "")).date()
    except Exception:
        return None


def to_number(value, default=None):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^0-9.\-]", "", str(value))
    if text in ("", "-", "."):
        return default
    try:
        return float(text)
    except ValueError:
        return default


def date_issues(d: date | None) -> str | None:
    if d is None:
        return "invalid_date"
    if d < MIN_DATE or d > MAX_DATE:
        return "date_out_of_range"
    return None


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
def normalise_rows(rows: list[dict], kind: str) -> tuple[list[dict], list[dict], Counter]:
    """Validate + canonicalise. Returns (accepted, quarantined, issue_counts)."""
    accepted, rejected = [], []
    issues: Counter = Counter()
    seen_txn: set = set()
    i = 0
    for raw in rows:
        i += 1
        row = {str(k): v for k, v in raw.items() if k is not None}
        mapping = map_columns(row.keys())
        rec: dict = {}
        problems: list[str] = []

        def field(name, mapping=mapping, row=row):  # bind this iteration's values
            col = mapping.get(name)
            return row.get(col) if col else None

        raw_name = field("drug_name")
        if not raw_name or not str(raw_name).strip():
            problems.append("missing_drug_name")
        else:
            meta = classify(raw_name)
            rec["norm_name"] = meta["norm_name"]
            rec["display_name"] = meta["name"]
            rec["meta"] = {k: v for k, v in meta.items() if k != "norm_name"}
            if str(raw_name).strip() != meta["name"]:
                issues["name_variants_normalised"] += 1
            # sales for medicines we do not stock are a data-quality problem:
            # quarantine them instead of silently growing the catalogue.
            if kind == "sales" and not meta.get("known"):
                problems.append("unknown_drug")

        if kind == "sales":
            d = parse_date(field("date"))
            bad = date_issues(d)
            if bad:
                problems.append(bad)
            rec["date"] = d.isoformat() if d else None

            qty = to_number(field("qty_sold"))
            if qty is None or qty <= 0:
                problems.append("invalid_qty")
            rec["qty"] = int(qty) if qty is not None else None

            txn = field("txn_id")
            txn = str(txn).strip() if txn not in (None, "") else f"AUTO-{i}"
            rec["txn_id"] = txn
            if txn in seen_txn:
                problems.append("duplicate_txn_id")
            seen_txn.add(txn)

            price = to_number(field("mrp_unit_price"))
            total = to_number(field("total_amount"))
            if price is None:
                price = 0.0
                issues["missing_price"] += 1
            rec["unit_price"] = price
            if total is None and price and rec.get("qty"):
                total = round(price * rec["qty"], 2)
                issues["missing_total_recomputed"] += 1
            if total is not None and price and rec.get("qty"):
                if abs(total - price * rec["qty"]) > 0.01:
                    issues["price_total_mismatch_fixed"] += 1
                    total = round(price * rec["qty"], 2)
            rec["total"] = round(total or 0.0, 2)

            batch = field("batch_number")
            rec["batch_no"] = str(batch).strip() if batch not in (None, "") else ""
            if not rec["batch_no"]:
                issues["missing_batch"] += 1
        else:  # purchases
            d = parse_date(field("date_received")) or parse_date(field("date"))
            bad = date_issues(d)
            if bad:
                problems.append(bad)
            rec["date_received"] = d.isoformat() if d else None

            qty = to_number(field("qty_received"))
            if qty is None or qty <= 0:
                problems.append("invalid_qty")
            rec["qty"] = int(qty) if qty is not None else None

            cost = to_number(field("unit_cost_price")) or 0.0
            total = to_number(field("total_purchase_cost"))
            rec["unit_cost"] = round(cost, 2)
            if total is not None and cost and rec.get("qty"):
                if abs(total - cost * rec["qty"]) > 1.0:
                    issues["cost_total_mismatch_fixed"] += 1
                    total = round(cost * rec["qty"], 2)
            rec["total"] = round(total if total is not None else cost * (rec.get("qty") or 0), 2)

            exp = parse_date(field("expiry_date"))
            if exp is None:
                issues["missing_expiry"] += 1
            rec["expiry_date"] = exp.isoformat() if exp else None

            batch = field("batch_number")
            rec["batch_no"] = str(batch).strip() if batch not in (None, "") else ""
            if not rec["batch_no"]:
                rec["batch_no"] = f"AUTO-{(field('purchase_id') or i)}"
                issues["batch_generated"] += 1

            sup = field("supplier_name")
            rec["supplier"] = str(sup).strip() if sup not in (None, "") else "Unknown Supplier"
            rec["purchase_id"] = str(field("purchase_id") or f"PO-IMP-{i}")

        if problems:
            for p in problems:
                issues[p] += 1
            rejected.append({"record": raw, "issues": problems})
        else:
            accepted.append(rec)
    return accepted, rejected, issues


# --------------------------------------------------------------------------- #
# stock helpers (FEFO)
# --------------------------------------------------------------------------- #
class _Shelf:
    """In-memory batch cache used while ingesting."""

    def __init__(self, drug_id: int):
        self.drug_id = drug_id
        self.batches = db.query(
            "SELECT id, batch_no, expiry_date, qty_remaining, received_date "
            "FROM batches WHERE drug_id=? ORDER BY expiry_date IS NULL, expiry_date",
            (drug_id,),
        )

    def allocate(self, qty: int, batch_no: str, on_date: str):
        """FEFO allocation. Returns batch_id or None."""
        pool = [b for b in self.batches if b["qty_remaining"] > 0]
        if not pool:
            return None
        chosen = None
        if batch_no:
            for b in pool:
                if b["batch_no"] == batch_no:
                    chosen = b
                    break
        if chosen is None:
            eligible = [
                b for b in pool
                if (b["received_date"] or "0000") <= (on_date or "9999")
                and (b["expiry_date"] is None or b["expiry_date"] >= (on_date or "0000"))
            ]
            if not eligible:
                eligible = [
                    b for b in pool if (b["received_date"] or "0000") <= (on_date or "9999")
                ] or pool
            eligible.sort(key=lambda b: (b["expiry_date"] is None, b["expiry_date"] or "9999"))
            chosen = eligible[0]
        take = min(qty, chosen["qty_remaining"])
        chosen["qty_remaining"] -= take
        db.execute("UPDATE batches SET qty_remaining=? WHERE id=?",
                   (chosen["qty_remaining"], chosen["id"]))
        return chosen["id"], qty - take


def get_or_create_drug(norm_name: str, meta: dict) -> int:
    row = db.one("SELECT id FROM drugs WHERE norm_name=?", (norm_name,))
    if row:
        return row["id"]
    return db.execute(
        "INSERT INTO drugs(name, norm_name, generic, category, form, schedule, storage, "
        "mrp, cost, lead_time_days, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (meta.get("name") or display_name(norm_name), norm_name,
         meta.get("generic", ""), meta.get("category", "Other"), meta.get("form", "Tablet"),
         meta.get("schedule", "Schedule H"), meta.get("storage", ""),
         0, 0, 7, db.now_iso()),
    )


def get_or_create_supplier(name: str) -> int:
    row = db.one("SELECT id FROM suppliers WHERE name=?", (name,))
    if row:
        return row["id"]
    return db.execute(
        "INSERT INTO suppliers(name, email, lead_time_days, created_at) VALUES(?,?,?,?)",
        (name, _email_for(name), 7, db.now_iso()),
    )


def _email_for(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", ".", name.strip().lower()).strip(".")
    return f"orders@{slug}.com"


# --------------------------------------------------------------------------- #
# ingestion
# --------------------------------------------------------------------------- #
def ingest(raw: bytes, filename: str, kind_hint: str | None = None,
           source: str = "upload") -> dict:
    kind, rows = parse_file(raw, filename)
    if kind_hint in ("sales", "purchases"):
        kind = kind_hint
    if kind == "unknown":
        kind = "purchases" if any("expiry" in _key(c) for c in rows[0]) else "sales"

    accepted, rejected, issues = normalise_rows(rows, kind)
    with db.bulk():
        if kind == "sales":
            added, no_stock = _ingest_sales(accepted, source)
        else:
            added, no_stock = _ingest_purchases(accepted, source)

    upload_id = db.execute(
        "INSERT INTO uploads(filename, kind, rows_total, rows_accepted, rows_rejected, issues, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (filename, kind, len(rows), len(accepted), len(rejected),
         db.jdump({"counts": dict(issues), "rejected": rejected[:50]}), db.now_iso()),
    )
    for item in rejected[:500]:
        db.execute(
            "INSERT INTO quarantined(upload_id, kind, record, issues, created_at) VALUES(?,?,?,?,?)",
            (upload_id, kind, db.jdump(item["record"]), db.jdump(item["issues"]), db.now_iso()),
        )
    _record_alert(upload_id, filename, kind, len(rows), len(accepted), len(rejected), issues)
    return dict(
        upload_id=upload_id, kind=kind, filename=filename, rows=len(rows),
        accepted=len(accepted), rejected=len(rejected), inserted=added,
        issues=dict(issues), no_stock=no_stock,
    )


def _ingest_sales(rows: list[dict], source: str = "upload") -> tuple[int, int]:
    now = db.now_iso()
    shelves: dict[int, _Shelf] = {}
    inserted = 0
    no_stock = 0
    for rec in sorted(rows, key=lambda r: r["date"]):
        drug_id = get_or_create_drug(rec["norm_name"], rec["meta"])
        shelf = shelves.setdefault(drug_id, _Shelf(drug_id))
        allocation = shelf.allocate(rec["qty"], rec["batch_no"], rec["date"])
        batch_id = None
        if allocation:
            batch_id, shortfall = allocation
            if shortfall:
                no_stock += 1
        else:
            no_stock += 1
        try:
            db.execute(
                "INSERT INTO sales(txn_id, date, drug_id, batch_id, qty, unit_price, total, source, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (rec["txn_id"], rec["date"], drug_id, batch_id, rec["qty"],
                 rec["unit_price"], rec["total"], source, now),
            )
            inserted += 1
        except Exception:
            continue  # duplicate txn
        db.execute(
            "INSERT INTO sales_daily(drug_id, date, qty, revenue) VALUES(?,?,?,?) "
            "ON CONFLICT(drug_id, date) DO UPDATE SET qty=qty+excluded.qty, revenue=revenue+excluded.revenue",
            (drug_id, rec["date"], rec["qty"], rec["total"]),
        )
        _touch_drug_price(drug_id, rec["unit_price"])
    return inserted, no_stock


def _ingest_purchases(rows: list[dict], source: str = "upload") -> tuple[int, int]:
    now = db.now_iso()
    inserted = 0
    no_stock = 0
    for rec in rows:
        drug_id = get_or_create_drug(rec["norm_name"], rec["meta"])
        supplier_id = get_or_create_supplier(rec["supplier"])
        existing = db.one(
            "SELECT id FROM batches WHERE drug_id=? AND batch_no=? AND received_date IS ?",
            (drug_id, rec["batch_no"], rec["date_received"]),
        )
        if existing:
            batch_id = existing["id"]
            db.execute("UPDATE batches SET qty_received=qty_received+?, qty_remaining=qty_remaining+? "
                       "WHERE id=?", (rec["qty"], rec["qty"], batch_id))
        else:
            batch_id = db.execute(
                "INSERT INTO batches(drug_id, supplier_id, batch_no, expiry_date, qty_received, "
                "qty_remaining, unit_cost, received_date, purchase_ref, source, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (drug_id, supplier_id, rec["batch_no"], rec["expiry_date"], rec["qty"],
                 rec["qty"], rec["unit_cost"], rec["date_received"], rec["purchase_id"],
                 source, now),
            )
        db.execute(
            "INSERT INTO purchases(purchase_id, date_received, drug_id, batch_id, qty, unit_cost, total, "
            "supplier_id, source, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (rec["purchase_id"], rec["date_received"], drug_id, batch_id, rec["qty"],
             rec["unit_cost"], rec["total"], supplier_id, source, now),
        )
        _touch_drug_price(drug_id, cost=rec["unit_cost"])
        shelfops.on_batch_arrived(batch_id, drug_id, rec["batch_no"], rec["qty"])
        inserted += 1
    return inserted, no_stock


def _touch_drug_price(drug_id: int, price: float | None = None, cost: float | None = None):
    if price:
        db.execute("UPDATE drugs SET mrp=CASE WHEN ?>0 THEN ? ELSE mrp END WHERE id=?",
                   (price, price, drug_id))
    if cost:
        db.execute("UPDATE drugs SET cost=CASE WHEN ?>0 THEN ? ELSE cost END WHERE id=?",
                   (cost, cost, drug_id))


def _record_alert(upload_id, filename, kind, total, accepted, rejected, issues):
    if rejected or issues:
        top = ", ".join(f"{k}:{v}" for k, v in issues.most_common(4))
        db.execute(
            "INSERT INTO alerts(atype, severity, drug_id, title, message, details, dedup_key, "
            "status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(dedup_key) DO UPDATE SET updated_at=excluded.updated_at, status='active'",
            ("data_quality", "medium", None,
             f"Data quality issues in {filename}",
             f"{rejected} of {total} rows quarantined ({top}). "
             f"Quarantined rows are kept for review and excluded from stock & forecasts.",
             db.jdump({"upload_id": upload_id, "issues": dict(issues)}),
             f"dq:{filename}:{upload_id}", "active", db.now_iso(), db.now_iso()),
        )
