"""AI demand forecasting + reorder planning.

Pipeline per drug:
  1. daily demand series built from validated sales (missing days = 0)
  2. candidate models fitted on history: damped Holt-Winters (weekly seasonality),
     Holt linear, simple exponential smoothing, seasonal-naive, moving average
  3. rolling back-test on the last 8 weeks picks the model with lowest wMAPE
  4. chosen model refitted on all history -> forecast to today + horizon with
     80% prediction interval, weekday seasonality profile, accuracy metrics
  5. reorder plan: lead-time demand + safety stock vs available (FEFO) stock
"""
import math
from datetime import date, timedelta

import numpy as np

from . import db

MIN_HISTORY_DAYS = 28
SEASON = 7


# --------------------------------------------------------------------------- #
# series
# --------------------------------------------------------------------------- #
def daily_series(drug_id: int, start: date | None = None, end: date | None = None):
    """Return (dates, values) dense daily array."""
    rows = db.query(
        "SELECT date, qty FROM sales_daily WHERE drug_id=? "
        "AND (? IS NULL OR date>=?) AND (? IS NULL OR date<=?) ORDER BY date",
        (drug_id, start, start, end, end),
    )
    if not rows:
        return [], np.zeros(0)
    d0 = date.fromisoformat(rows[0]["date"]) if start is None else start
    d1 = date.fromisoformat(rows[-1]["date"]) if end is None else end
    lookup = {r["date"]: r["qty"] for r in rows}
    dates, values = [], []
    cur = d0
    while cur <= d1:
        dates.append(cur)
        values.append(lookup.get(cur.isoformat(), 0))
        cur += timedelta(days=1)
    return dates, np.array(values, dtype=float)


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
def _holt_winters(y: np.ndarray, m: int, alpha: float, beta: float, gamma: float,
                  phi: float, seasonal: bool = True):
    """Additive Holt-Winters with optional damped trend. Returns (state, resid)."""
    n = len(y)
    if seasonal and n >= 2 * m:
        weeks = max(2, min(8, n // m))
        head = y[: weeks * m]
        level = float(np.mean(head))
        half_len = len(head) // 2
        trend = ((float(np.mean(head[half_len:])) - float(np.mean(head[:half_len])))
                 / max(half_len, 1)) if half_len else 0.0
        seas = []
        for j in range(m):
            vals = [head[t] for t in range(len(head)) if t % m == j]
            seas.append(float(np.mean(vals)) - level if vals else 0.0)
        seasonal = True
    else:
        level = float(y[0])
        trend = float(y[1] - y[0]) if n > 1 else 0.0
        seas = [0.0] * m
        seasonal = False

    resid = np.zeros(n)
    phi_acc = 0.0
    for t in range(n):
        s_prev = seas[t % m] if seasonal else 0.0
        pred = level + phi_acc * trend + s_prev
        resid[t] = y[t] - pred
        y_adj = y[t] - s_prev
        new_level = alpha * y_adj + (1 - alpha) * (level + phi * trend)
        new_trend = beta * (new_level - level) + (1 - beta) * phi * trend
        if seasonal:
            seas[t % m] = gamma * (y[t] - new_level) + (1 - gamma) * s_prev
        level, trend = new_level, new_trend
        phi_acc = phi_acc * phi + phi if phi < 1 else phi_acc + 1.0
    state = dict(level=level, trend=trend, seas=list(seas), m=m, phi=phi,
                 seasonal=seasonal, seasonal_pos=n % m)
    return state, resid


def _predict(state: dict, steps: int) -> np.ndarray:
    level, trend, phi = state["level"], state["trend"], state["phi"]
    m, seas, pos = state["m"], state["seas"], state["seasonal_pos"]
    out = np.zeros(steps)
    psi = 0.0
    for h in range(1, steps + 1):
        psi = psi * phi + phi if phi < 1 else h
        s = seas[(pos + h - 1) % m] if state["seasonal"] else 0.0
        out[h - 1] = level + psi * trend + s
    return out


def _ses(y: np.ndarray, alpha: float):
    level = float(y[0])
    resid = np.zeros(len(y))
    for t in range(len(y)):
        resid[t] = y[t] - level
        level = alpha * y[t] + (1 - alpha) * level
    return dict(level=level, trend=0.0, seas=[0.0] * 7, m=7, phi=1.0,
                seasonal=False, seasonal_pos=len(y)), resid


def _seasonal_naive(y: np.ndarray, m: int = 7):
    """Weekday-mean baseline (level=0, seasonal carries the weekday means)."""
    sums = np.zeros(m)
    counts = np.zeros(m)
    for t in range(max(0, len(y) - 4 * m), len(y)):
        sums[t % m] += y[t]
        counts[t % m] += 1
    seas = list(np.where(counts > 0, sums / np.maximum(counts, 1), 0.0))
    resid = np.zeros(len(y))
    for t in range(m, len(y)):
        resid[t] = y[t] - y[t - m]
    return dict(level=0.0, trend=0.0, seas=seas,
                m=m, phi=1.0, seasonal=True, seasonal_pos=len(y) % m), resid


def _moving_average(y: np.ndarray, window: int = 28):
    level = float(np.mean(y[-window:]))
    resid = np.zeros(len(y))
    for t in range(window, len(y)):
        resid[t] = y[t] - np.mean(y[t - window:t])
    return dict(level=level, trend=0.0, seas=[0.0] * 7, m=7, phi=1.0,
                seasonal=False, seasonal_pos=len(y)), resid


CANDIDATES = [
    # name, builder returning (state, resid), param grid
    ("hw_damped", lambda y, p: _holt_winters(y, 7, *p, seasonal=True),
     [(a, b, g, phi)
      for a in (0.1, 0.2, 0.3, 0.5, 0.7)
      for b in (0.0, 0.05, 0.1, 0.2)
      for g in (0.0, 0.1, 0.2, 0.3)
      for phi in (1.0, 0.92)]),
    ("ses", lambda y, p: _ses(y, p[0]), [(0.1,), (0.2,), (0.3,), (0.5,), (0.7,)]),
    ("seasonal_naive", lambda y, p: _seasonal_naive(y, 7), [(None,)]),
    ("moving_avg_28", lambda y, p: _moving_average(y, 28), [(None,)]),
]


# --------------------------------------------------------------------------- #
# evaluation
# --------------------------------------------------------------------------- #
def metrics(actual: np.ndarray, pred: np.ndarray) -> dict:
    actual = np.asarray(actual, dtype=float)
    pred = np.asarray(pred, dtype=float)
    err = actual - pred
    nz = actual > 0
    mape = float(np.mean(np.abs(err[nz]) / actual[nz]) * 100) if nz.any() else 0.0
    wmape = float(np.sum(np.abs(err)) / max(np.sum(np.abs(actual)), 1e-9) * 100)
    mw, ww = weekly_metrics(actual, pred)
    return dict(
        mape=round(min(mape, 999.0), 2),
        wmape=round(wmape, 2),
        mape_weekly=mw, wmape_weekly=ww,
        mae=round(float(np.mean(np.abs(err))), 3),
        rmse=round(float(np.sqrt(np.mean(err ** 2))), 3),
    )


def weekly_metrics(actual: np.ndarray, pred: np.ndarray) -> tuple[float, float]:
    """MAPE / wMAPE on 7-day totals — the bucket pharmacies actually plan at."""
    n = (len(actual) // 7) * 7
    if n < 14:
        return metrics_no_weekly(actual, pred)
    a = actual[:n].reshape(-1, 7).sum(axis=1)
    p = pred[:n].reshape(-1, 7).sum(axis=1)
    nz = a > 0
    mape = float(np.mean(np.abs(a[nz] - p[nz]) / a[nz]) * 100) if nz.any() else 0.0
    wmape = float(np.sum(np.abs(a - p)) / max(np.sum(np.abs(a)), 1e-9) * 100)
    return round(min(mape, 999.0), 2), round(wmape, 2)


def metrics_no_weekly(actual: np.ndarray, pred: np.ndarray) -> tuple[float, float]:
    err = np.abs(np.asarray(actual, dtype=float) - np.asarray(pred, dtype=float))
    a = np.asarray(actual, dtype=float)
    nz = a > 0
    mape = float(np.mean(err[nz] / a[nz]) * 100) if nz.any() else 0.0
    wmape = float(np.sum(err) / max(np.sum(np.abs(a)), 1e-9) * 100)
    return round(mape, 2), round(wmape, 2)


def weekday_baselines(y: np.ndarray) -> list[float]:
    """Positional weekday means (Mon..Sun) used as the long-horizon anchor."""
    sums, counts = np.zeros(7), np.zeros(7)
    for t in range(max(0, len(y) - 56), len(y)):
        sums[t % 7] += y[t]
        counts[t % 7] += 1
    means = np.where(counts > 0, sums / np.maximum(counts, 1), 0.0)
    overall = float(np.mean(y[-56:])) if len(y) else 0.0
    return [float(m) if counts[i] else overall for i, m in enumerate(means)]


def combined_predict(state: dict, steps: int, y_train: np.ndarray) -> np.ndarray:
    """Model forecast blended with the weekday baseline.

    Weight on the fitted model decays as exp(-h/120) so long-range projections
    converge to the seasonal mean instead of extrapolating a linear trend.
    """
    model_fc = _predict(state, steps)
    base = weekday_baselines(y_train)
    pos = state.get("seasonal_pos", 0)
    out = np.zeros(steps)
    for h in range(1, steps + 1):
        w = math.exp(-h / 120.0)
        anchor = base[(pos + h - 1) % 7]
        out[h - 1] = w * model_fc[h - 1] + (1 - w) * anchor
    return np.clip(out, 0, None)


def fit_best(values: np.ndarray, holdout: int = 56) -> dict:
    """Back-test candidates, return best model fitted on the full series."""
    n = len(values)
    if n == 0:
        return dict(model="none", state=None, metrics=dict(mape=0, wmape=0, mae=0, rmse=0),
                    sigma=0.0, baseline=None)
    if n < MIN_HISTORY_DAYS:
        holdout = max(7, n // 4)
    train = values[: n - holdout] if n > holdout + 14 else values
    test = values[n - holdout:] if len(train) < n else np.zeros(0)

    results = []
    for name, build, grid in CANDIDATES:
        best = None
        for params in grid:
            try:
                state, resid = build(train, params)
                # stability guard: reject configs that cannot even fit the training data
                fit_w = metrics(train, train - resid)["wmape"]
                if fit_w > 200:
                    continue
                if len(test):
                    preds = combined_predict(state, len(test), train)
                    m = metrics(test, preds)
                    score = m["wmape"]
                else:
                    score = fit_w
                if best is None or score < best[0]:
                    best = (score, params, state, resid)
            except Exception:
                continue
        if best:
            results.append((best[0], name, best[1], best[2], best[3]))

    results.sort(key=lambda r: r[0])
    best = results[0]
    _, model, params, state_train, _ = best

    # refit chosen configuration on the complete history
    build = next(b for n2, b, _ in CANDIDATES if n2 == model)
    state, resid = build(values, params)
    fitted_values = values - resid          # honest one-step-ahead in-sample fit
    fit_metrics = metrics(values, fitted_values)

    baseline = results[1] if len(results) > 1 else None
    baseline_name = baseline[1] if baseline else None
    baseline_score = round(baseline[0], 2) if baseline else None

    holdout = {}
    holdout_pred: list[float] = []
    holdout_actual: list[float] = []
    if len(test):
        hp = combined_predict(state_train, len(test), train)
        holdout = metrics(test, hp)
        holdout_pred = [round(float(x), 3) for x in hp]
        holdout_actual = [float(x) for x in test]
    else:
        holdout = dict(fit_metrics)

    sigma = float(np.std(resid[max(0, len(resid) - 120):], ddof=1)) if len(resid) > 2 else 0.0
    return dict(
        model=model, params=list(params), state=state,
        metrics=holdout, fit_metrics=fit_metrics, holdout_metrics=holdout,
        holdout_pred=holdout_pred, holdout_actual=holdout_actual,
        sigma=sigma, baseline=baseline_name, baseline_wmape=baseline_score,
    )


def evaluate_models(drug_id: int, holdout: int = 56) -> dict:
    """Back-test every candidate model on one SKU's history.

    Same protocol as fit_best (last `holdout` days held out, damped multi-step
    path via combined_predict, wMAPE scoring) but reports each candidate
    separately instead of only the winner, so the model choice is auditable.
    """
    drug = db.one("SELECT name FROM drugs WHERE id=?", (drug_id,))
    if not drug:
        raise ValueError("unknown drug")
    _, values = daily_series(drug_id)
    n = len(values)
    if n < MIN_HISTORY_DAYS + 7:
        return dict(drug_id=drug_id, drug=drug["name"], history_days=n,
                    holdout_days=0, models=[], best_model=None)

    eff_holdout = holdout if n > holdout + 14 else max(7, n // 4)
    train = values[: n - eff_holdout]
    test = values[n - eff_holdout:]

    models = []
    for name, build, grid in CANDIDATES:
        best = None
        for params in grid:
            try:
                state, resid = build(train, params)
                fit_w = metrics(train, train - resid)["wmape"]
                if fit_w > 200:
                    continue
                preds = combined_predict(state, len(test), train)
                m = metrics(test, preds)
                if best is None or m["wmape"] < best[0]["wmape"]:
                    best = (m, params, fit_w)
            except Exception:
                continue
        if best:
            m, params, fit_w = best
            models.append(dict(
                model=name, params=list(params),
                mape=m["mape"], wmape=m["wmape"], mae=m["mae"], rmse=m["rmse"],
                accuracy=round(max(0.0, 100 - m["wmape_weekly"]), 2),
                fit_wmape=round(float(fit_w), 2),
            ))
    models.sort(key=lambda r: r["wmape"])
    for rank, m in enumerate(models, 1):
        m["rank"] = rank
    current = current_forecast_model(drug_id)
    for m in models:
        m["in_production"] = bool(current and m["model"] == current)
    return dict(drug_id=drug_id, drug=drug["name"], history_days=n,
                holdout_days=int(len(test)), models=models,
                best_model=models[0]["model"] if models else None)


def current_forecast_model(drug_id: int) -> str | None:
    row = db.one("SELECT model FROM forecasts WHERE drug_id=? AND is_current=1", (drug_id,))
    return row["model"] if row else None


def evaluate_all(holdout: int = 56) -> list[dict]:
    drugs = db.query("SELECT id FROM drugs ORDER BY id")
    return [evaluate_models(d["id"], holdout=holdout) for d in drugs]


def weekday_profile_dated(dates: list[date], values: np.ndarray) -> list[float]:
    """Weekday multiplicative factors (Mon..Sun), mean = 1."""
    if len(values) < 14:
        return [1.0] * 7
    sums = np.zeros(7)
    counts = np.zeros(7)
    for d, v in zip(dates, values, strict=True):
        sums[d.weekday()] += v
        counts[d.weekday()] += 1
    avg = np.where(counts > 0, sums / np.maximum(counts, 1), 0)
    mean = float(np.mean(avg)) or 1.0
    factors = [round(float(x / mean), 2) for x in avg]
    return [f if np.isfinite(f) else 1.0 for f in factors]


# --------------------------------------------------------------------------- #
# forecast + reorder plan
# --------------------------------------------------------------------------- #
def forecast_drug(drug_id: int, horizon: int | None = None, persist: bool = True) -> dict:
    horizon = horizon or int(db.get_setting("forecast_horizon_days", 30))
    drug = db.one("SELECT * FROM drugs WHERE id=?", (drug_id,))
    if not drug:
        raise ValueError("unknown drug")

    dates, values = daily_series(drug_id)
    today = date.today()
    if len(values) == 0:
        payload = dict(drug_id=drug_id, drug=drug["name"], model="insufficient_data",
                       daily=[], horizon_days=horizon, avg_daily=0.0)
        return payload

    fitted = fit_best(values)
    last_date = dates[-1]
    steps = max(1, (today + timedelta(days=horizon) - last_date).days)
    state = fitted["state"]
    preds = combined_predict(state, steps, values) if state else np.zeros(steps)
    preds = np.clip(preds, 0, None)

    sigma = fitted["sigma"]
    z = 1.2816  # 80% interval
    daily = []
    for i in range(steps):
        d = last_date + timedelta(days=i + 1)
        if d < today:
            continue
        width = z * sigma * math.sqrt(min((d - today).days + 1, 28))
        daily.append(dict(
            date=d.isoformat(),
            qty=round(float(preds[i]), 1),
            lo=round(max(0.0, float(preds[i]) - width), 1),
            hi=round(float(preds[i]) + width, 1),
        ))
    if not daily:
        d = today
        daily = [dict(date=d.isoformat(), qty=round(float(preds[-1]), 1), lo=0.0,
                      hi=round(float(preds[-1]) * 1.5, 1))]

    avg_daily = round(float(np.mean([p["qty"] for p in daily])), 2)
    factors = weekday_profile_dated(dates, values)
    m = fitted["metrics"]
    payload = dict(
        drug_id=drug_id, drug=drug["name"], generic=drug["generic"],
        category=drug["category"], model=fitted["model"],
        metrics=m, fit_metrics=fitted.get("fit_metrics"), holdout_metrics=fitted.get("holdout_metrics") or {},
        baseline_model=fitted["baseline"], baseline_wmape=fitted["baseline_wmape"],
        sigma=round(sigma, 3), avg_daily=avg_daily,
        weekday_factors=factors,
        history_from=dates[0].isoformat(), history_to=last_date.isoformat(),
        history_days=len(dates), total_units=int(values.sum()),
        daily=daily, horizon_days=horizon,
        trend_vs_prev=round(_trend_pct(values), 1),
    )
    plan = reorder_plan(drug, daily, avg_daily, sigma)
    payload["reorder"] = plan

    if persist:
        db.execute("UPDATE forecasts SET is_current=0 WHERE drug_id=?", (drug_id,))
        db.execute(
            "INSERT INTO forecasts(drug_id, created_at, model, mape, mae, wmape, rmse, "
            "baseline_model, baseline_wmape, avg_daily, metrics, payload, is_current) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (drug_id, db.now_iso(), payload["model"], m.get("mape"), m.get("mae"),
             m.get("wmape"), m.get("rmse"), payload["baseline_model"],
             payload["baseline_wmape"], avg_daily, db.jdump(payload["metrics"]),
             db.jdump(payload)),
        )
    return payload


def _trend_pct(values: np.ndarray) -> float:
    if len(values) < 60:
        return 0.0
    recent = float(np.mean(values[-28:]))
    prev = float(np.mean(values[-56:-28]))
    if prev <= 0:
        return 0.0
    return (recent - prev) / prev * 100


def z_score(service_level: float) -> float:
    """Approximate inverse normal CDF for common service levels."""
    table = {0.90: 1.2816, 0.92: 1.4051, 0.93: 1.4758, 0.94: 1.5548,
             0.95: 1.6449, 0.96: 1.7507, 0.97: 1.8808, 0.98: 2.0537,
             0.99: 2.3263}
    if service_level in table:
        return table[service_level]
    return 1.6449


def available_stock(drug_id: int) -> dict:
    """Usable (unexpired) + expired stock for a drug, valued at cost."""
    today = date.today().isoformat()
    row = db.one(
        "SELECT "
        "COALESCE(SUM(CASE WHEN expiry_date>=? THEN qty_remaining ELSE 0 END),0) AS usable, "
        "COALESCE(SUM(CASE WHEN expiry_date<? THEN qty_remaining ELSE 0 END),0) AS expired, "
        "COALESCE(SUM(CASE WHEN expiry_date>=? THEN qty_remaining*unit_cost ELSE 0 END),0) AS usable_value, "
        "COALESCE(SUM(CASE WHEN expiry_date<? THEN qty_remaining*unit_cost ELSE 0 END),0) AS expired_value, "
        "COUNT(*) AS batches "
        "FROM batches WHERE drug_id=?",
        (today, today, today, today, drug_id),
    )
    return row


def reorder_plan(drug: dict, daily: list[dict], avg_daily: float, sigma: float) -> dict:
    """Lead-time demand + safety stock vs available FEFO stock."""
    settings = db.settings()
    lead = int(drug.get("lead_time_days") or 7)
    review = int(settings.get("review_period_days", 7))
    service = float(settings.get("service_level", 0.95))
    pack = max(1, int(settings.get("pack_size", 10)))
    z = z_score(service)

    avail = available_stock(drug["id"])
    usable = int(avail["usable"])
    expired = int(avail["expired"])

    horizon_qty = [p["qty"] for p in daily]
    demand_lt = sum(horizon_qty[:lead]) if horizon_qty else avg_daily * lead
    demand_cycle = sum(horizon_qty[: lead + review]) if horizon_qty else avg_daily * (lead + review)
    safety = z * max(sigma, avg_daily * 0.35) * math.sqrt(lead)
    rop = demand_lt + safety

    manual = drug.get("reorder_point")
    rop_used = float(manual) if manual not in (None, "") else rop

    # projection: first day balance falls below ROP
    balance = float(usable)
    stockout_day = None
    rop_day = None
    for p in daily:
        balance -= p["qty"]
        if balance <= 0 and stockout_day is None:
            stockout_day = p["date"]
        if balance < rop_used and rop_day is None:
            rop_day = p["date"]
        if balance <= 0:
            break

    need = demand_cycle + safety - usable
    qty = int(math.ceil(max(0.0, need) / pack) * pack)
    cover_days = round(usable / avg_daily, 1) if avg_daily > 0 else 9999

    if usable < rop_used:
        status = "order_now"
        due = date.today().isoformat()
    elif rop_day:
        status = "scheduled"
        due = rop_day
    else:
        status = "healthy"
        due = None

    return dict(
        available=usable, expired_stock=expired, available_value=round(avail["usable_value"], 2),
        lead_time_days=lead, review_days=review, service_level=service,
        demand_lead_time=round(demand_lt, 1), safety_stock=round(safety, 1),
        reorder_point=round(rop_used, 1), reorder_point_auto=round(rop, 1),
        manual_override=manual not in (None, ""),
        order_qty=qty, status=status, due_date=due,
        stockout_date=stockout_day, cover_days=cover_days,
        projected_balance=round(balance, 1),
    )


def forecast_all(horizon: int | None = None, persist: bool = True) -> list[dict]:
    drugs = db.query("SELECT id FROM drugs ORDER BY name")
    return [forecast_drug(d["id"], horizon, persist) for d in drugs]


def current_forecasts() -> list[dict]:
    rows = db.query(
        "SELECT f.*, d.name AS drug, d.category, d.generic FROM forecasts f "
        "JOIN drugs d ON d.id=f.drug_id WHERE f.is_current=1 ORDER BY d.name"
    )
    out = []
    for r in rows:
        payload = db.jload(r["payload"], {}) or {}
        payload.setdefault("drug", r["drug"])
        out.append(payload)
    return out


def ensure_forecasts() -> None:
    """Recompute forecasts that are missing or older than the latest sale."""
    for d in db.query("SELECT id FROM drugs"):
        row = db.one(
            "SELECT f.created_at, (SELECT MAX(created_at) FROM sales WHERE drug_id=?) AS last_sale "
            "FROM forecasts f WHERE f.drug_id=? AND f.is_current=1",
            (d["id"], d["id"]),
        )
        if row is None or (row["last_sale"] and row["last_sale"] > row["created_at"]):
            forecast_drug(d["id"])
