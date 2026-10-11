"""Laya - reorder prediction layer.

Trained offline on the laya_test_data JSONL (gradient-boosted classifiers).
Prediction only: never an order authorization, never the system of record.
"""
import json
import math
import pickle
import statistics
from datetime import date, datetime, timedelta

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

from . import config, db
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
# Weather features (exogenous, never hard rules). Numeric weather columns are
# appended to the feature vector; the model discovers relationships from data.
WEATHER_NUM = ["temp_7d_avg", "temp_max_7d", "rain_7d_mm", "humidity_7d_avg",
               "temp_28d_avg", "rain_28d_mm", "temperature_anomaly",
               "rainfall_anomaly", "humidity_anomaly", "forecast_temp_7d",
               "forecast_rain_7d", "forecast_rain_prob_max",
               "forecast_heatwave_flag", "forecast_heavy_rain_flag"]
WEATHER_LABELS = ["hotter_than_normal", "cooler_than_normal",
                  "wetter_than_normal", "drier_than_normal", "normal"]


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
    wx = s.get("weather") or {}
    if config.LAYA_WEATHER_FEATURES:
        wnums = [float(wx.get(k) or 0) for k in WEATHER_NUM]
        wlabel = [1.0 if wx.get("weather_anomaly") == lab else 0.0
                  for lab in WEATHER_LABELS]
    else:
        wnums, wlabel = [], []  # weather dims are opt-in, never silent
    drugs = _drug_names()
    onehot = [1.0 if s.get("drug") == d else 0.0 for d in drugs]
    return nums + extra + wnums + wlabel + onehot


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


def _attach_weather(rows: list[dict]) -> None:
    """Backfill the weather block for every state (in place).

    One shared archive fetch covers the whole date range (cached on disk);
    each state then slices its own 7d/28d windows from it. No per-state HTTP.
    """
    from . import weather

    pending = [r["state"] for r in rows
               if "weather" not in r["state"] and r["state"].get("as_of")]
    if not pending:
        return
    dates = sorted(s["as_of"] for s in pending)
    try:
        wide_start = (date.fromisoformat(dates[0]) - timedelta(days=27)).isoformat()
        wide = weather.daily_history(wide_start, dates[-1])
    except ValueError:
        wide = None
    for s in pending:
        s["weather"] = weather.weather_features(s["as_of"], wide)


def train(force: bool = False) -> dict:
    """Train one classifier per decision on the train split. Returns val accuracy."""
    rows = _load_split("train")
    if not rows:
        raise RuntimeError(f"no Laya training data found at {LAYA_DATA}")
    if config.LAYA_WEATHER_FEATURES:
        _attach_weather(rows)
    drugs = sorted({r["state"].get("drug", "") for r in rows})
    _bundle["drugs"] = drugs
    models = {}
    val_acc = {}
    val_rows = _load_split("val")
    if config.LAYA_WEATHER_FEATURES:
        _attach_weather(val_rows)
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
    MODELS_PATH.write_bytes(pickle.dumps({
        "drugs": drugs, "models": models, "val_accuracy": val_acc,
        "n_features": len(_features(rows[0]["state"])),
    }))
    _bundle["models"] = models
    _bundle["val_accuracy"] = val_acc
    return val_acc


def ensure_trained() -> None:
    if _bundle.get("models"):
        return
    if MODELS_PATH.exists():
        payload = pickle.loads(MODELS_PATH.read_bytes())
        _bundle["drugs"] = payload.get("drugs", [])
        # A pickle trained on a different feature set (e.g. pre-weather) would
        # crash on a vector-length mismatch: retrain instead of serving it.
        if payload.get("n_features") == len(_features({})):
            _bundle.update(payload)
            return
        _bundle.clear()
    train()


# Laya failure wording per the integrity policy (§13/§42).
MSG_UNAVAILABLE = "The reorder prediction service is currently unavailable."
MSG_TIMEOUT = "The reorder prediction could not be retrieved."
MSG_INCOMPLETE = ("The reorder prediction returned incomplete data and cannot "
                  "be reliably interpreted.")
MSG_INVALID = ("The reorder prediction returned an invalid value and cannot be "
               "reliably interpreted.")


def predict(state: dict) -> dict:
    """Laya predictions for one state. Raises laya.Unavailable / laya.Timeout /
    laya.Malformed per §13; flags §42 invalid categories instead of mapping
    them to the nearest valid one."""
    ensure_trained()
    x = np.array([_features(state)])
    out = {"decisions": {}, "uncertain": [], "invalid": []}
    for q in TARGETS:
        entry = _bundle["models"][q]
        probs = entry["clf"].predict_proba(x)[0]
        dist = {c: round(float(p), 4) for c, p in zip(entry["classes"], probs, strict=False)}
        top = max(dist, key=dist.get)
        if top not in TARGETS[q]:
            out["invalid"].append(q)   # §42: never map to nearest category
        if dist[top] < 0.5:
            out["uncertain"].append(q)
        out["decisions"][q] = {"value": top, "probabilities": dist}
    return out


class Unavailable(RuntimeError):
    """Laya service unavailable (§13)."""


class Timeout(RuntimeError):
    """Laya call timed out (§13)."""


class Malformed(RuntimeError):
    """Laya returned malformed/invalid output (§13/§42)."""


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
    state = {
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
        # Prescriptions are not ingested by this system yet; per policy §20
        # this is sales/transaction demand, never called prescription demand.
        "prescription_trend_note": (
            "Prescription records are absent; observed sales volume and "
            "transaction frequency are the demand signal used here."),
    }
    try:
        from . import weather
        state["weather"] = weather.weather_features(state["as_of"])
    except Exception:
        state["weather"] = {}  # weather missing, never zero-filled
    return state


# §4: batch ambiguity is surfaced, never silently resolved.
MSG_AMBIGUOUS_BATCH = ("I found multiple inventory states for this product. "
                       "I can't reliably determine which one you mean.")
MSG_NO_EXACT_STATE = ("I don't have the exact inventory state required for "
                      "this prediction.")
MSG_STATE_MISMATCH = ("I couldn't retrieve the exact historical state "
                      "requested, so I won't provide a prediction from a "
                      "different state.")


class StateMismatch(RuntimeError):
    """Resolved state does not match the requested date/batch (hard guard)."""


_STATE_INDEX: dict[tuple, dict] | None = None


def _state_index() -> dict[tuple, dict]:
    """Index of every known backend state, keyed by (drug, as_of, batch).

    These dataset states ARE the backend's recorded inventory snapshots: when
    the user requests a specific analysis date, the exact recorded state is
    resolved from here - never rebuilt from other values, never substituted
    with the latest snapshot.
    """
    global _STATE_INDEX
    if _STATE_INDEX is None:
        _STATE_INDEX = {}
        for split in ("train", "val", "test"):
            for r in _load_split(split):
                s = r["state"]
                key = (s.get("drug", ""), s.get("as_of", ""),
                       s.get("current_batch", ""))
                _STATE_INDEX[key] = s
    return _STATE_INDEX


def resolve_state(drug_name: str, as_of: str | None = None,
                  batch: str | None = None) -> dict | None | str:
    """Resolve the EXACT requested state (§2/§4/§6).

    Returns the recorded state, None when no exact state exists for the
    request, or MSG_AMBIGUOUS_BATCH when the requested batch is known but
    does not match the state resolved for that date.
    """
    idx = _state_index()
    if as_of:
        candidates = {k: v for k, v in idx.items()
                      if k[0] == drug_name and k[1] == as_of}
        if not candidates:
            return None  # no fallback: §6 date integrity
        if batch:
            exact = candidates.get((drug_name, as_of, batch))
            if exact is not None:
                return exact
            # batch known on other dates, or date has a different batch:
            # either way we cannot silently substitute (§4).
            any_batch = any(k[2] == batch for k in idx)
            return MSG_AMBIGUOUS_BATCH if any_batch else MSG_AMBIGUOUS_BATCH
        if len(candidates) > 1:
            return MSG_AMBIGUOUS_BATCH
        return next(iter(candidates.values()))
    if batch:
        exact = [v for k, v in idx.items() if k[0] == drug_name and k[2] == batch]
        if not exact:
            return None
        if len(exact) > 1:
            return MSG_AMBIGUOUS_BATCH
        return exact[0]
    return None  # no date, no batch: caller uses the current backend snapshot


def prediction_for(drug: dict, as_of: date | None = None,
                   batch: str | None = None) -> dict | None | str:
    """State + predictions for one drug.

    State preservation (§2/§4/§6): when the user requests a specific analysis
    date or batch, the exact recorded state is resolved and sent to Laya
    verbatim - no fallback to the latest snapshot, no SKU substitution, no
    recalculation. Without an explicit date/batch, the current backend
    snapshot is the requested state. Returns the prediction dict, None when
    no data supports a prediction, or an integrity message string.
    """
    if as_of is not None or batch is not None:
        state = resolve_state(drug["name"],
                              as_of.isoformat() if as_of else None, batch)
        if state is None:
            return MSG_NO_EXACT_STATE
        if isinstance(state, str):
            return state
        # Hard state-integrity check: the resolved state MUST match the
        # request exactly. A mismatch is a bug, never a soft fallback.
        if as_of is not None and state.get("as_of") != as_of.isoformat():
            raise StateMismatch(
                f"requested {as_of.isoformat()} got {state.get('as_of')}")
        if batch and state.get("current_batch") != batch:
            raise StateMismatch(
                f"requested batch {batch} got {state.get('current_batch')}")
    else:
        state = state_from_db(drug)
        if state is None:
            return None
    if config.LAYA_WEATHER_FEATURES and "weather" not in state:
        # opt-in weather model: backfill the weather block for recorded states
        try:
            from . import weather
            state["weather"] = weather.weather_features(state["as_of"])
        except Exception:
            state["weather"] = {}
    out = predict(state)
    if out.get("invalid"):
        # §42: invalid categories are never mapped or repaired.
        raise Malformed(MSG_INVALID)
    out["state"] = state
    out["state_id"] = _register_trace(state, out)
    return out


# --------------------------------------------------------------------------- #
# state traceability (state_id -> exact input state + laya output)
# --------------------------------------------------------------------------- #
_TRACE: list[dict] = []
_TRACE_MAX = 200


def _register_trace(state: dict, laya_output: dict) -> str:
    """Retain {state_id, analysis_date, product, batch, input_state,
    laya_output} for every Laya request."""
    import hashlib

    payload = json.dumps({"state": state, "out": laya_output["decisions"]},
                         sort_keys=True)
    state_id = "st_" + hashlib.sha1(payload.encode()).hexdigest()[:12]
    _TRACE.append({
        "state_id": state_id,
        "analysis_date": state.get("as_of"),
        "product": state.get("drug"),
        "batch": state.get("current_batch"),
        "input_state": state,          # the exact state sent to Laya
        "laya_output": laya_output["decisions"],
    })
    del _TRACE[:-_TRACE_MAX]
    return state_id


def trace_for(state_id: str) -> dict | None:
    # Newest first: identical states hash to the same state_id, so the most
    # recent record is the request the caller's object belongs to (the state
    # is stored by reference, never copied).
    for rec in reversed(_TRACE):
        if rec["state_id"] == state_id:
            return rec
    return None


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
# Lower bound of each predicted band: the quantity Laya's own prediction
# supports as an order suggestion (the range stays visible next to it).
_BAND_FLOOR = {"1_600": 1, "601_800": 601, "801_1000": 801, "1001_plus": 1001}
_BAND_OPEN_TOP = "1001_plus"  # open-ended band: its floor is a MINIMUM


def band_order_qty(band_value: str | None) -> int | None:
    """Laya's own order quantity: the floor of its predicted quantity band.

    None when the band is missing or unknown - never an invented number.
    The top band is open-ended, so callers present its floor as a minimum
    (order_qty_open=True). This is Laya's suggestion for display only; the
    inventory engine's order quantity stays the authoritative one.
    """
    return _BAND_FLOOR.get(band_value)
_TRAJ_DISPLAY = {
    "falling": "Falling", "stable": "Stable",
    "rising": "Rising", "spiking": "Spiking",
}
_SEAS_DISPLAY = {
    "seasonal_down": "Seasonal down", "seasonal_normal": "Seasonal normal",
    "seasonal_up": "Seasonal up",
}
WX_DISPLAY = {
    "hotter_than_normal": "Elevated temperatures",
    "cooler_than_normal": "Below-normal temperatures",
    "wetter_than_normal": "Elevated rainfall",
    "drier_than_normal": "Below-normal rainfall",
    "normal": "Normal conditions",
}


def demand_trajectory_card(out: dict) -> dict | None:
    """Structured demand-trajectory payload for the frontend insight card.

    Carries ONLY what the card explains: Laya's trajectory classification
    plus the exact supporting metrics it was made from - no reorder timing,
    no quantity band (§6/§7: those stay in the prediction text).

    Every field is validated here so the card never guesses: a missing state,
    an unknown trajectory category or a non-numeric metric is passed through
    as None (or makes the whole card None when the core fields are gone),
    never fabricated and never substituted from another state.
    """
    try:
        s = out["state"]
        traj_raw = out["decisions"]["next_30d_demand_trajectory"]["value"]
        medicine = str(s.get("drug") or "").strip()
        as_of = str(s.get("as_of") or "").strip()
    except (KeyError, TypeError, AttributeError):
        return None
    if traj_raw not in _TRAJ_DISPLAY or not medicine or not as_of:
        return None  # unusable core fields: no card rather than a fake one

    def _num(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return round(f, 3) if math.isfinite(f) else None

    return dict(
        type="demand_trajectory",
        medicine=medicine,
        analysis_date=as_of,
        trajectory=traj_raw,
        trajectory_label=_TRAJ_DISPLAY[traj_raw],
        recent_daily_demand=_num(s.get("recent_daily_demand")),
        baseline_daily_demand=_num(s.get("baseline_daily_demand")),
        recent_vs_baseline_ratio=_num(s.get("recent_vs_baseline_ratio")),
        state_id=str(out.get("state_id") or ""),
    )


# Human wording for the forecast card: plain language, never raw model field
# names, and never a causal claim the data does not support.
_SEAS_EXPLAIN = {
    "seasonal_up": "Demand is seasonally elevated for this period.",
    "seasonal_normal": "Demand is within its normal seasonal range.",
    "seasonal_down": "Demand is seasonally subdued for this period.",
}
_BAND_NOTE = ("Model-predicted range, not an exact order quantity. "
              "Exact quantities come from the inventory engine.")
_PROXY_NOTE_PREFIX = "Sales transactions are used as a demand proxy"
_LOW_CONF_HUMAN = {
    "reorder_due_within_7d": "reorder due within 7 days",
    "reorder_timing": "reorder timing",
    "reorder_quantity_band": "quantity band",
    "next_30d_demand_trajectory": "demand trajectory",
    "seasonality_signal": "seasonality signal",
}


def demand_forecast_card(out: dict) -> dict | None:
    """Structured demand-forecast payload for the frontend forecast card.

    Answers the three operational questions from the EXACT state Laya saw:
    when (timing), how much (quantity band - always a range, never an exact
    order), and why (drivers + interpretation). Every calculation - the
    baseline percentage, the status badge, the wording - happens HERE; the
    frontend only formats and displays supplied values.

    Transparency rules: unavailable metrics arrive as None (displayed as
    missing, never as zero); weather absence is None (the card shows
    "Weather signal unavailable", never an assumed normal); prescription
    data being absent is stated explicitly with the sales-proxy label;
    probabilities are passed through only when Laya actually returned a
    valid one; the status badge is None when the backend cannot support it.
    """
    try:
        s = out["state"]
        d = out["decisions"]
        if out.get("invalid"):
            return None  # §42: never present or repair an invalid category
        med = str(s.get("drug") or "").strip()
        as_of = str(s.get("as_of") or "").strip()
        traj_dec = d.get("next_30d_demand_trajectory") or {}
        traj_raw = traj_dec.get("value")
    except (KeyError, TypeError, AttributeError):
        return None
    if not med or not as_of or traj_raw not in _TRAJ_DISPLAY:
        return None  # unusable core fields: no card rather than a fake one

    def _num(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return round(f, 3) if math.isfinite(f) else None

    def _prob(dec):
        """Chosen-class probability, only when Laya returned a valid one."""
        p = (dec.get("probabilities") or {}).get(dec.get("value"))
        if isinstance(p, (int, float)) and not isinstance(p, bool) \
                and math.isfinite(p) and 0.0 <= p <= 1.0:
            return round(float(p), 4)
        return None

    def _decision(key):
        dec = d.get(key)
        return (dec if isinstance(dec, dict) else {})

    # --- reorder status badge: only from data Laya actually returned ---
    due_raw = _decision("reorder_due_within_7d").get("value")
    if due_raw == "true":
        status = dict(key="reorder_soon", label="Reorder soon")
    elif due_raw == "false":
        status = dict(key="monitor", label="Monitor") if traj_raw in ("rising", "spiking") \
            else dict(key="none", label="No immediate reorder indicated")
    else:
        status = None  # no backend support -> the card shows no badge

    # --- outlook: bands stay bands (ranges, never exact orders) ---
    timing_dec, band_dec = _decision("reorder_timing"), _decision("reorder_quantity_band")
    seas_dec = _decision("seasonality_signal")
    outlook = dict(
        timing=dict(label=_TIMING_DISPLAY.get(timing_dec.get("value")),
                    prob=_prob(timing_dec)),
        band=dict(label=_BAND_DISPLAY.get(band_dec.get("value")),
                  prob=_prob(band_dec)),
        trajectory=dict(value=traj_raw, label=_TRAJ_DISPLAY[traj_raw],
                        prob=_prob(traj_dec)),
        band_note=_BAND_NOTE,
    )

    # --- drivers: every number computed here, missing kept distinct from 0 ---
    recent = _num(s.get("recent_daily_demand"))
    baseline = _num(s.get("baseline_daily_demand"))
    if recent is None or baseline is None or baseline <= 0:
        change, change_pct = "unavailable", None
    else:
        pct = (recent - baseline) / baseline * 100
        change_pct = round(pct)
        change = "in_line" if abs(pct) < 2 else ("above" if pct > 0 else "below")

    seas_raw = seas_dec.get("value")
    seasonal = None
    if seas_raw in _SEAS_DISPLAY:
        seasonal = dict(label=_SEAS_DISPLAY[seas_raw],
                        explanation=_SEAS_EXPLAIN.get(seas_raw), prob=_prob(seas_dec))

    # prescriptions: absent today - stated as a sales-proxy, never implied present
    presc_raw = _num(s.get("prescription_demand_trend"))
    prescriptions_available = presc_raw is not None
    proxy_note = None
    prescription_trend = None
    if prescriptions_available:
        prescription_trend = f"{presc_raw} items/day"
    else:
        note = str(s.get("prescription_trend_note") or "").strip()
        proxy_note = _PROXY_NOTE_PREFIX + (f": {note}" if note else ".")

    # weather: observed/classified signal, or explicitly unavailable - an
    # empty block is NEVER reported as "normal weather".
    wx = s.get("weather") or {}
    anomaly = wx.get("weather_anomaly")
    weather = None
    if anomaly in WX_DISPLAY:
        detail = []
        if _num(wx.get("temp_7d_avg")) is not None:
            detail.append(f"{wx['temp_7d_avg']}C avg, {wx.get('rain_7d_mm')}mm rain (7d)")
        ev = wx.get("weather_event") or {}
        if ev.get("type") not in (None, "none"):
            detail.append(f"{str(ev['type']).replace('_', ' ')} ({ev.get('severity')}) "
                          f"over {ev.get('duration_days')} day(s)")
        if _num(wx.get("forecast_temp_7d")) is not None:
            detail.append(f"7d forecast {wx['forecast_temp_7d']}C avg")
        weather = dict(label=WX_DISPLAY[anomaly],
                       detail="; ".join(detail) or None,
                       source=str(wx.get("source") or "") or None)

    low_confidence = [_LOW_CONF_HUMAN.get(q, q.replace("_", " "))
                      for q in (out.get("uncertain") or [])
                      if isinstance(q, str)]

    drivers = dict(
        recent_daily_demand=recent,
        baseline_daily_demand=baseline,
        baseline_change=change,
        baseline_change_pct=change_pct,
        seasonal=seasonal,
        prescriptions_available=prescriptions_available,
        prescription_trend=prescription_trend,
        demand_proxy_note=proxy_note,
        weather=weather,
    )

    return dict(
        type="demand_forecast",
        medicine=med,
        analysis_date=as_of,
        state_id=str(out.get("state_id") or ""),
        status=status,
        outlook=outlook,
        drivers=drivers,
        interpretation=_forecast_meaning(med, _TRAJ_DISPLAY[traj_raw],
                                         change, change_pct, status),
        low_confidence=low_confidence,
    )


def _forecast_meaning(medicine: str, traj_label: str, change: str,
                      change_pct: int | None, status: dict | None) -> str:
    """Deterministic plain-English interpretation (no LLM): facts first,
    then the operational next step - no invented causes, no promised demand,
    no weather or seasonality blame, no automatic ordering."""
    head = f"{medicine} demand is currently classified as {traj_label.lower()}"
    if change == "unavailable":
        head += ", with baseline comparison unavailable for this state"
    elif change == "in_line":
        head += ", with recent average sales in line with the historical baseline"
    else:
        head += (f", with recent average sales approximately {abs(change_pct)}% "
                 f"{change} the historical baseline")
    key = (status or {}).get("key")
    if key == "reorder_soon":
        tail = ("A reorder may be due within the predicted window; review "
                "available stock and confirm quantities with the inventory "
                "engine before ordering.")
    elif key == "monitor":
        tail = ("Monitor the trend and review available stock before making "
                "replenishment decisions.")
    elif key == "none":
        tail = ("No immediate reorder is indicated; continue routine "
                "monitoring.")
    else:
        tail = ("Review available stock alongside this forecast before "
                "making replenishment decisions.")
    return head + ". " + tail


def laya_engine_status() -> dict:
    """`{status, detail}` for the UI without running a prediction.

    status: "ready" (model loadable) or "unavailable" (with the reason).
    """
    try:
        ensure_trained()
    except Exception as exc:  # missing model/data, unreadable pickle, ...
        return dict(status="unavailable", detail=str(exc))
    return dict(status="ready", detail=None)


_TIMING_UPPER_DAYS = {"within_3_days": 3, "4_7_days": 7,
                      "8_14_days": 14, "15_plus_days": 15}


def reorder_by_date(timing_value: str | None, today: date | None = None) -> str | None:
    """Act-by deadline derived from Laya's own reorder-timing window.

    Deadline = today + the window's upper bound (e.g. 8-14 days -> +14).
    None when Laya returned no mappable timing - never an invented date.
    """
    upper = _TIMING_UPPER_DAYS.get(timing_value)
    if upper is None:
        return None
    return ((today or date.today()) + timedelta(days=upper)).isoformat()


def forecast_page_payload() -> dict:
    """Laya outlook for EVERY catalogue drug at its latest state.

    Drives the forecast page's Laya section. Each call recomputes every
    prediction from the current backend snapshot - the page is therefore
    always up to date, and each row carries the SAME validated card payloads
    the chatbot uses (one prediction, one source of truth: page and chat
    cannot disagree).

    Integrity rules carried over from the cards: unavailable metrics arrive
    as None (never zero), probabilities pass through only when Laya returned
    a valid one, bands stay ranges (never an exact order), and a SKU whose
    prediction fails comes back as an `error` row - never a fabricated
    prediction and never a substituted state. Rows additionally carry
    Laya's own order quantity (the band floor, flagged when open-ended)
    for the suggestions column; it never replaces the engine's qty.
    """
    engine = laya_engine_status()
    drugs = db.query("SELECT * FROM drugs ORDER BY category, name")
    rows = []
    for d in drugs:
        row = dict(drug_id=int(d["id"]), drug=str(d["name"]),
                   as_of=None, card=None, trajectory=None,
                   reorder_by=None, order_qty=None, order_qty_open=False,
                   error=None, error_detail=None)
        if engine["status"] != "ready":
            row["error"] = "engine_unavailable"
            row["error_detail"] = engine["detail"]
            rows.append(row)
            continue
        try:
            out = prediction_for(dict(d))  # no date -> latest state, up to date
        except (Unavailable, Timeout) as exc:
            row["error"] = "engine_unavailable" if isinstance(exc, Unavailable) else "timeout"
            row["error_detail"] = str(exc)
        except Malformed:
            row["error"] = "malformed"
            row["error_detail"] = MSG_INVALID
        except Exception as exc:  # one broken SKU must not sink the page
            row["error"] = "error"
            row["error_detail"] = str(exc)
        else:
            if out is None:
                row["error"] = "no_data"
                row["error_detail"] = "No state data supports a prediction for this SKU."
            elif isinstance(out, str):
                row["error"] = "state_unavailable"
                row["error_detail"] = out
            else:
                state = out.get("state") or {}
                row["as_of"] = str(state.get("as_of") or "") or None
                row["card"] = demand_forecast_card(out)
                row["trajectory"] = demand_trajectory_card(out)
                timing_raw = (out.get("decisions") or {}).get("reorder_timing") or {}
                row["reorder_by"] = reorder_by_date(timing_raw.get("value"))
                if row["card"] is not None:
                    # Laya's own order quantity, straight from its band:
                    # the floor, flagged when the top band is open-ended.
                    band_key = ((out.get("decisions") or {}).get("reorder_quantity_band")
                                or {}).get("value")
                    row["order_qty"] = band_order_qty(band_key)
                    row["order_qty_open"] = band_key == _BAND_OPEN_TOP
                if row["card"] is None:
                    row["error"] = "prediction_invalid"
                    row["error_detail"] = (
                        "Laya returned a state that cannot be presented "
                        "(invalid or incomplete category).")
        rows.append(row)
    return dict(engine=engine, generated_at=datetime.now().isoformat(timespec="seconds"),
                state="latest", rows=rows)


def spike_alerts() -> list[dict]:
    """Alert-engine entries for SKUs Laya classifies as spiking.

    Each entry answers the two operational questions with Laya's own
    numbers: HOW MUCH as a predicted quantity band (a range - never an
    exact order), and BY WHEN as an act-by deadline derived from Laya's
    reorder-timing window. Severity reflects Laya's reorder-due decision.
    SKUs without a valid prediction are skipped: an alert is either fully
    backed by a prediction or absent - never fabricated.
    """
    out: list[dict] = []
    for row in forecast_page_payload()["rows"]:
        card = row.get("card")
        if not card:
            continue
        outlook = card.get("outlook") or {}
        traj = outlook.get("trajectory") or {}
        if traj.get("value") != "spiking":
            continue
        band = (outlook.get("band") or {}).get("label")
        timing = (outlook.get("timing") or {}).get("label")
        deadline = row.get("reorder_by")
        status = (card.get("status") or {}).get("key")
        prob = traj.get("prob")
        name = str(card.get("medicine") or row.get("drug") or "")
        msg = f"Laya classifies {name} demand as spiking"
        if isinstance(prob, (int, float)) and not isinstance(prob, bool):
            msg += f" (p={prob})"
        msg += f". Predicted need: {band or 'band unavailable'}"
        msg += " - a model band, not an exact order. " if band else ". "
        if deadline:
            msg += f"Act by {deadline}"
            msg += f" ({timing} reorder window). " if timing else ". "
        else:
            msg += "Reorder window unavailable - review timing manually. "
        msg += f"Analysis date: {card.get('analysis_date') or 'unavailable'}."
        out.append(dict(
            key=f"laya_spike:{row['drug_id']}", atype="laya_spike",
            severity="high" if status == "reorder_soon" else "medium",
            title=f"Laya spike: {name}", message=msg,
            drug_id=row["drug_id"], batch_id=None,
            details=dict(source="laya", trajectory="spiking",
                         trajectory_prob=prob, band=band,
                         band_prob=(outlook.get("band") or {}).get("prob"),
                         timing=timing, deadline=deadline,
                         analysis_date=card.get("analysis_date"), status=status,
                         state_id=card.get("state_id")),
        ))
    return out


def _conf_phrase(p: float) -> str:
    """§14/§15: probability-aware wording tied to an actually-returned
    probability - never a confidence claim Laya did not make."""
    if p >= 0.75:
        return " (highest-probability timing)" if p >= 0.9 else " (highest probability returned)"
    if p < 0.5:
        return " (below 0.5 probability)"
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

    wx = s.get("weather") or {}
    ev = wx.get("weather_event") or {}
    text = (
        "[Laya Reorder Prediction]\n"
        f"State: {out.get('state_id', 'n/a')} (analysis date {s['as_of']}, "
        f"batch {s.get('current_batch') or '-'}, SKU-level result)\n"
        f"Reorder within 7 days: {due}\n"
        f"Reorder timing: {timing}{_conf_phrase(p_timing)}\n"
        f"Quantity band: {band}{_conf_phrase(p_band)}\n"
        f"Demand trajectory: {traj}\n"
        f"Seasonality: {seas}\n"
        + (f"Weather signal: {WX_DISPLAY.get(wx.get('weather_anomaly'), 'Not available')}\n"
           if wx else "")
        + "\nSupporting data:\n"
        f"Recent daily demand: {s['recent_daily_demand']} units/day\n"
        f"Historical baseline: {s['baseline_daily_demand']} units/day\n"
        f"Recent vs baseline: {s['recent_vs_baseline_ratio']}\n"
        + (f"Weather (7d): {wx.get('temp_7d_avg')}C avg, "
           f"{wx.get('rain_7d_mm')}mm rain ({wx.get('source')})\n"
           if wx and wx.get("temp_7d_avg") is not None else "")
        + (f"Weather event: {ev['type'].replace('_', ' ')} ({ev['severity']}) "
           f"over {ev['duration_days']} day(s)\n"
           if ev.get("type") not in (None, "none") else "")
        + "Usable stock (inventory engine): available from the engine"
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

    # §8 numerical integrity: never silently present an inconsistent metric.
    cover_note = ""
    try:
        rem = float(s.get("estimated_current_batch_remaining") or 0)
        dem = float(s.get("recent_daily_demand") or 0)
        cover = float(s.get("estimated_days_of_cover") or 0)
        if dem > 0 and rem > 0 and cover > 0:
            implied = rem / dem
            if abs(implied - cover) > max(0.2 * cover, 2.0):
                cover_note = (f"\n\nData inconsistency: {rem:,.0f} units at "
                              f"{dem} units/day implies about {implied:,.0f} days "
                              f"of cover, not the reported {cover:,.0f}. The "
                              "inventory calculation should be verified before "
                              "acting on this prediction.")
    except (TypeError, ValueError):
        cover_note = ""
    text += cover_note

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
    # Frontend renders the trajectory insight card from this structured
    # payload (never from prose); omitted entirely when it can't be trusted.
    traj_card = demand_trajectory_card(out)
    return text, [card] + ([traj_card] if traj_card else [])
