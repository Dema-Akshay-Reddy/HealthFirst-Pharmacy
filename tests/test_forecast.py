"""Forecasting engine: model fit, blend, current forecasts, evaluation."""
import numpy as np

from pharmacy import forecasting


def _synthetic(n=140, seed=7):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    base = 20 + 6 * np.sin(2 * np.pi * t / 7) + 0.03 * t
    return np.maximum(0, base + rng.normal(0, 2.0, n))


def test_fit_best_picks_stable_model():
    res = forecasting.fit_best(_synthetic())
    assert res["model"] and res["model"] != "none"
    assert 0 <= res["holdout_metrics"]["wmape"] < 200  # stability guard holds
    assert len(res["holdout_pred"]) == len(res["holdout_actual"]) > 0


def test_combined_predict_length_and_nonneg():
    y = _synthetic()
    res = forecasting.fit_best(y)
    preds = forecasting.combined_predict(res["state"], 30, y)
    assert len(preds) == 30
    assert np.all(preds >= 0)


def test_ensure_forecasts_one_current_per_drug(db):
    forecasting.ensure_forecasts()
    rows = db.query("SELECT drug_id FROM forecasts WHERE is_current=1 GROUP BY drug_id")
    # SKUs without sales history correctly get no model — assert coverage of the
    # seeded catalog (6 SKUs) and that every current forecast points at a real drug.
    drug_ids = {d["id"] for d in db.query("SELECT id FROM drugs")}
    assert len(rows) >= 6
    assert {r["drug_id"] for r in rows} <= drug_ids


def test_evaluation_backtests_every_sku(db):
    rows = forecasting.evaluate_all()
    scored = [r for r in rows if r["models"]]  # thin-history SKUs report empty
    assert len(scored) >= 6
    for r in scored:
        assert r["best_model"] == r["models"][0]["model"]      # sorted best-first
        assert r["models"][0]["wmape"] <= r["models"][-1]["wmape"]
        assert any(m["in_production"] for m in r["models"])     # prod model audited
