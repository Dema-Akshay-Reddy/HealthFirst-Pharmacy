"""Laya - reorder prediction layer.

Trained offline on the laya_test_data JSONL (gradient-boosted classifiers).
Prediction only: never an order authorization, never the system of record.
"""
import json
import pickle
import statistics
from datetime import date, timedelta

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

from . import db
from .config import BASE_DIR, DATA_DIR

LAYA_DATA = BASE_DIR / "laya_test_data"
MODELS_PATH = DATA_DIR / "models" / "laya.pkl"

FEATURES = [
    "days_since_last_receipt", "last_receipt_qty",
    "estimated_current_batch_remaining", "estimated_days_of_cover",
    "historical_median_reorder_interval_days", "sales_units_7d",
    "sales_units_14d", "sales_units_28d", "sales_units_56d",
    "transactions_28d", "avg_units_per_transaction_28d",
    "recent_daily_demand", "baseline_daily_demand",
    "recent_vs_baseline_ratio", "same_period_last_year_daily_demand",
    "seasonality_index",
]
TARGETS = {
    "reorder_due_within_7d": ["false", "true"],
    "reorder_timing": ["within_3_days", "4_7_days", "8_14_days", "15_plus_days"],
    "reorder_quantity_band": ["1_600", "601_800", "801_1000", "1001_plus"],
    "next_30d_demand_trajectory": ["falling", "stable", "rising", "spiking"],
    "seasonality_signal": ["seasonal_down", "seasonal_normal", "seasonal_up"],
}

# --------------------------------------------------------------------------- #
# features / training
# --------------------------------------------------------------------------- #
def _features(state: dict) -> list[float]:
    s = state or {}
    wk = (s.get("weekly_sales_units_last_8_weeks") or [0] * 8)[:8]
    wk = wk + [0] * (8 - len(wk))
    nums = [float(s.get(k) or 0) for k in FEATURES]
    rem = max(float(s.get("estimated_current_batch_remaining") or 0), 1.0)
    dem = max(float(s.get("recent_daily_demand") or 0), 0.01)
    ly = max(float(s.get("same_period_last_year_daily_demand") or 0), 0.01)
    extra = [
        sum(wk[:2]), sum(wk[-2:]),
        (sum(wk[-2:]) - sum(wk[:2])) / max(sum(wk[:2]), 1),
        float(np.mean(wk)), float(np.std(wk)),
        rem / dem,                                   # days of stock at recent demand
        float(s.get("last_receipt_qty") or 0) / dem,  # last receipt in days of demand
        float(s.get("recent_daily_demand") or 0) / ly,
        float(s.get("sales_units_28d") or 0)
        / max(float(s.get("historical_median_reorder_interval_days") or 1), 1),
    ]
    drugs = _drug_names()
    onehot = [1.0 if s.get("drug") == d else 0.0 for d in drugs]
    return nums + extra + onehot


_bundle: dict = {}


def _drug_names() -> list[str]:
    return _bundle.get("drugs", [])


def _load_split(split: str) -> list[dict]:
    path = LAYA_DATA / f"{split}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _gold_label(gold: dict) -> str:
    probs = gold.get("probabilities") or {}
    return max(probs, key=probs.get) if probs else gold.get("answer", "")


def train(force: bool = False) -> dict:
    """Train one classifier per decision on the train split. Returns val accuracy."""
    rows = _load_split("train")
    if not rows:
        raise RuntimeError(f"no Laya training data found at {LAYA_DATA}")
    drugs = sorted({r["state"].get("drug", "") for r in rows})
    _bundle["drugs"] = drugs
    models = {}
    val_acc = {}
    val_rows = _load_split("val")
    Xva = np.array([_features(r["state"]) for r in val_rows]) if val_rows else None
    for q, classes in TARGETS.items():
        ymap = {c: i for i, c in enumerate(classes)}
        Xtr = np.array([_features(r["state"]) for r in rows])
        ytr = np.array([ymap.get(_gold_label(r["gold"][q]), 0) for r in rows])
        clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.1,
                                             random_state=0)
        clf.fit(Xtr, ytr)
        models[q] = {"clf": clf, "classes": classes}
        if Xva is not None and len(Xva):
            yva = [ymap.get(_gold_label(r["gold"][q]), 0) for r in val_rows]
            val_acc[q] = round(float((clf.predict(Xva) == np.array(yva)).mean()), 3)
    MODELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODELS_PATH.write_bytes(pickle.dumps({"drugs": drugs, "models": models,
                                          "val_accuracy": val_acc}))
    _bundle["models"] = models
    _bundle["val_accuracy"] = val_acc
    return val_acc


def ensure_trained() -> None:
    if _bundle.get("models"):
        return
    if MODELS_PATH.exists():
        payload = pickle.loads(MODELS_PATH.read_bytes())
        _bundle.update(payload)
        return
    train()


def predict(state: dict) -> dict:
    """Laya predictions for one state: the three reorder decisions plus the
    demand-trajectory and seasonality auxiliary signals.

    Returns probabilities (model probabilities, not guarantees). A decision is
    flagged uncertain when its top probability is below 0.5.
    """
    ensure_trained()
    x = np.array([_features(state)])
    out = {"decisions": {}, "uncertain": []}
    for q in TARGETS:
        entry = _bundle["models"][q]
        probs = entry["clf"].predict_proba(x)[0]
        dist = {c: round(float(p), 4) for c, p in zip(entry["classes"], probs, strict=False)}
        top = max(dist, key=dist.get)
        if dist[top] < 0.5:
            out["uncertain"].append(q)
        out["decisions"][q] = {"value": top, "probabilities": dist}
    return out


# --------------------------------------------------------------------------- #
# live state from the inventory DB
# --------------------------------------------------------------------------- #
def _sales_total(drug_id: int, start: str, end: str) -> int:
    row = db.one("SELECT COALESCE(SUM(qty),0) AS n FROM sales "
                 "WHERE drug_id=? AND date IS NOT NULL AND date>=? AND date<=?",
                 (drug_id, start, end))
    return int(row["n"] or 0)


def _weekly_sales(drug_id: int, end: date) -> list[int]:
    weeks = []
    for w in range(8):
        e = end - timedelta(days=7 * w)
        s = e - timedelta(days=6)
        weeks.append(_sales_total(drug_id, s.isoformat(), e.isoformat()))
    return weeks[::-1]  # oldest first, matching the training data


def _latest_sales_date() -> date | None:
    row = db.one("SELECT MAX(date) AS d FROM sales WHERE date IS NOT NULL")
    if not row or not row["d"]:
        return None
    try:
        return date.fromisoformat(str(row["d"])[:10])
    except ValueError:
        return None


def state_from_db(drug: dict, as_of: date | None = None) -> dict | None:
    """Build a Laya state for one drug from live inventory data.

    Returns None when there is not enough demand history for a prediction.
    """
    from .forecasting import available_stock

    # Anchor on the backend's own latest data timestamp: if the sales history
    # is historical, predicting "as of today" would see no demand at all.
    as_of = as_of or _latest_sales_date() or date.today()
    drug_id = drug["id"]
    sales_28 = _sales_total(drug_id, (as_of - timedelta(days=27)).isoformat(), as_of.isoformat())
    if sales_28 == 0 and _sales_total(drug_id, (as_of - timedelta(days=365)).isoformat(),
                                      as_of.isoformat()) == 0:
        return None  # no demand signal to learn from

    wk = _weekly_sales(drug_id, as_of)
    s7 = _sales_total(drug_id, (as_of - timedelta(days=6)).isoformat(), as_of.isoformat())
    s14 = _sales_total(drug_id, (as_of - timedelta(days=13)).isoformat(), as_of.isoformat())
    s56 = _sales_total(drug_id, (as_of - timedelta(days=55)).isoformat(), as_of.isoformat())
    recent_daily = s14 / 14.0
    baseline_daily = s56 / 56.0
    if baseline_daily <= 0:
        return None
    ratio = recent_daily / baseline_daily

    receipts = db.query(
        "SELECT date_received, qty FROM purchases WHERE drug_id=? AND "
        "date_received IS NOT NULL ORDER BY date_received DESC LIMIT 60", (drug_id,))
    last_receipt = receipts[0] if receipts else None
    days_since = None
    if last_receipt and last_receipt["date_received"]:
        try:
            days_since = max(0, (as_of - date.fromisoformat(str(last_receipt["date_received"])[:10])).days)
        except ValueError:
            days_since = None
    gaps = []
    dates = sorted({str(r["date_received"])[:10] for r in receipts
                    if r["date_received"]})
    for a, b in zip(dates, dates[1:], strict=False):
        try:
            gaps.append((date.fromisoformat(b) - date.fromisoformat(a)).days)
        except ValueError:
            continue
    median_interval = statistics.median(gaps) if gaps else 30

    txn_row = db.one("SELECT COUNT(DISTINCT txn_id) AS n FROM sales WHERE drug_id=? "
                     "AND date IS NOT NULL AND date>=?",
                     (drug_id, (as_of - timedelta(days=27)).isoformat()))
    txns_28 = int(txn_row["n"] or 0)

    try:
        ly_end_d = as_of.replace(year=as_of.year - 1)
    except ValueError:  # Feb 29 -> use Feb 28 in the prior year
        ly_end_d = as_of.replace(year=as_of.year - 1, day=28)
    ly_start = (ly_end_d - timedelta(days=13)).isoformat()
    ly_daily = _sales_total(drug_id, ly_start, ly_end_d.isoformat()) / 14.0
    seasonality = round(min(baseline_daily / max(ly_daily, 0.01), 5.0), 3) if ly_daily > 0 else 1.0

    avail = available_stock(drug_id)
    batch = db.one(
        "SELECT * FROM batches WHERE drug_id=? AND qty_remaining>0 "
        "ORDER BY expiry_date IS NULL, expiry_date LIMIT 1", (drug_id,))
    return {
        "as_of": as_of.isoformat(),
        "drug": drug["name"],
        "current_batch": (batch or {}).get("batch_no", ""),
        "supplier": (db.one("SELECT name FROM suppliers WHERE id=?",
                            (drug.get("supplier_id"),)) or {}).get("name", ""),
        "days_since_last_receipt": days_since if days_since is not None else 999,
        "last_receipt_qty": int((last_receipt or {}).get("qty") or 0),
        "estimated_current_batch_remaining": (batch or {}).get("qty_remaining", 0) or 0,
        "estimated_days_of_cover": round((avail["usable"]) / max(recent_daily, 0.01), 1),
        "historical_median_reorder_interval_days": median_interval,
        "sales_units_7d": s7,
        "sales_units_14d": s14,
        "sales_units_28d": sales_28,
        "sales_units_56d": s56,
        "transactions_28d": txns_28,
        "avg_units_per_transaction_28d": round(sales_28 / max(txns_28, 1), 1),
        "recent_daily_demand": round(recent_daily, 2),
        "baseline_daily_demand": round(baseline_daily, 2),
        "recent_vs_baseline_ratio": round(ratio, 3),
        "same_period_last_year_daily_demand": round(ly_daily, 2),
        "seasonality_index": seasonality,
        "weekly_sales_units_last_8_weeks": wk,
        # Prescriptions are not ingested by this system yet, so per the Laya
        # contract we describe the signal as observed sales/transaction demand.
        "prescription_trend_note": (
            "Prescription records are absent; transaction frequency and sales "
            "units are used as the observable prescription-demand proxy."),
    }


def prediction_for(drug: dict, as_of: date | None = None) -> dict | None:
    """State + predictions for one drug, or None when data is insufficient."""
    state = state_from_db(drug, as_of)
    if state is None:
        return None
    out = predict(state)
    out["state"] = state
    return out


# Exact display mappings from the output-integrity contract: categorical
# values are preserved and only rendered per these tables - never reworded.
_TIMING_DISPLAY = {
    "within_3_days": "Within 3 days",
    "4_7_days": "4\u20137 days",
    "8_14_days": "8\u201314 days",
    "15_plus_days": "15+ days",
}
_BAND_DISPLAY = {
    "1_600": "1\u2013600 units",
    "601_800": "601\u2013800 units",
    "801_1000": "801\u20131000 units",
    "1001_plus": "1001+ units",
}
_TRAJ_DISPLAY = {
    "falling": "Falling", "stable": "Stable",
    "rising": "Rising", "spiking": "Spiking",
}
_SEAS_DISPLAY = {
    "seasonal_down": "Seasonal down", "seasonal_normal": "Seasonal normal",
    "seasonal_up": "Seasonal up",
}


def _conf_phrase(p: float) -> str:
    """Confidence wording only when Laya actually returned a probability."""
    if p >= 0.75:
        return " (most likely)"
    if p < 0.5:
        return " (low confidence)"
    return ""


def format_prediction(out: dict, engine_status: str | None = None) -> tuple[str, list[dict]]:
    """Format a Laya prediction under the OUTPUT INTEGRITY contract.

    Hard rules: Laya's categorical predictions are immutable - they are
    displayed exactly as returned, never reinterpreted from raw data, and the
    explanation never contradicts them. Bands stay bands. Confidence phrases
    appear only when Laya returned a probability. If the inventory engine
    disagrees, both results are shown and the disagreement is stated.
    """
    d = out["decisions"]
    s = out["state"]
    due = d["reorder_due_within_7d"]["value"]
    timing_raw = d["reorder_timing"]["value"]
    band_raw = d["reorder_quantity_band"]["value"]
    traj_raw = d["next_30d_demand_trajectory"]["value"]
    seas_raw = d["seasonality_signal"]["value"]
    timing = _TIMING_DISPLAY[timing_raw]
    band = _BAND_DISPLAY[band_raw]
    traj = _TRAJ_DISPLAY[traj_raw]
    seas = _SEAS_DISPLAY[seas_raw]
    p_timing = d["reorder_timing"]["probabilities"][timing_raw]
    p_band = d["reorder_quantity_band"]["probabilities"][band_raw]

    text = (
        "[Laya Reorder Prediction]\n"
        f"Reorder within 7 days: {due}\n"
        f"Reorder timing: {timing}{_conf_phrase(p_timing)}\n"
        f"Quantity band: {band}{_conf_phrase(p_band)}\n"
        f"Demand trajectory: {traj}\n"
        f"Seasonality: {seas}\n"
        "\nSupporting data:\n"
        f"Recent daily demand: {s['recent_daily_demand']} units/day\n"
        f"Historical baseline: {s['baseline_daily_demand']} units/day\n"
        f"Recent vs baseline: {s['recent_vs_baseline_ratio']}\n"
        f"Usable stock (inventory engine): available from the engine"
    )
    if out.get("uncertain"):
        text += ("\n\nNote: the model returned low confidence for "
                 + ", ".join(q.replace("_", " ") for q in out["uncertain"])
                 + "; treat this as a probabilistic signal, not a guarantee.")
    text += ("\n\nExplanation:\n"
             f"Laya's demand trajectory is {traj}. Recent demand is "
             f"{s['recent_daily_demand']} units/day against a historical "
             f"baseline of {s['baseline_daily_demand']} units/day "
             f"(ratio {s['recent_vs_baseline_ratio']}); the model predicts "
             f"replenishment {timing.lower()} with a target quantity band of "
             f"{band.lower()}. Bands are ranges, not exact orders - the exact "
             "quantity comes from the inventory engine, and nothing is created "
             "without explicit authorization.")

    # Conflict handling: when Laya and the inventory engine disagree, show both
    # results exactly and state the disagreement - never pick a winner.
    if engine_status is not None:
        laya_due = due == "true"
        engine_now = engine_status == "order_now"
        if laya_due != engine_now:
            text += (
                "\n\nLaya prediction:\n"
                f"Reorder due within 7 days: {due}\n"
                "Inventory engine:\n"
                f"Reorder status: {str(engine_status).replace('_', ' ')}\n"
                "Note:\n"
                "The two systems currently disagree.")

    card = dict(type="list", title="Laya reorder prediction", items=[
        f"Reorder timing: {timing}",
        f"Quantity band: {band}",
        f"Demand trajectory: {traj}",
        f"Seasonality: {seas}",
    ])
    return text, [card]
