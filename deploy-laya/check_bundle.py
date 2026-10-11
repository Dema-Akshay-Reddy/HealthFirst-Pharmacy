"""Verify the deploy bundle runs standalone: model loads, predictions work.

Run inside deploy-laya/:  python check_bundle.py
(expects LAYA_SHARED_KEY set in the real service; this check injects one and
calls the endpoint directly via FastAPI TestClient — no network needed).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ["LAYA_SHARED_KEY"] = os.environ.get("LAYA_SHARED_KEY", "bundle-check")


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from fastapi.testclient import TestClient
    from laya_service import app
    client = TestClient(app)
    r = client.get("/health")
    print("health:", r.status_code, r.json())
    assert r.status_code == 200 and r.json()["engine"]["status"] == "ready", r.text

    # minimal realistic state (one row) — the service must handle it
    state = {
        "as_of": "2026-09-30", "drug": "Dolo 650", "current_batch": "B-001",
        "supplier": "Hetero", "days_since_last_receipt": 5,
        "last_receipt_qty": 500,
        "estimated_current_batch_remaining": 1200,
        "estimated_days_of_cover": 30,
        "historical_median_reorder_interval_days": 30,
        "sales_units_7d": 32, "sales_units_14d": 65, "sales_units_28d": 130,
        "sales_units_56d": 245, "transactions_28d": 28,
        "avg_units_per_transaction_28d": 4.6,
        "recent_daily_demand": 4.64, "baseline_daily_demand": 4.38,
        "recent_vs_baseline_ratio": 1.06,
        "same_period_last_year_daily_demand": 4.1,
        "seasonality_index": 1.07,
        "weekly_sales_units_last_8_weeks": [30, 28, 31, 29, 30, 32, 33, 65],
        "prescription_trend_note": "n/a",
    }
    r2 = client.post("/v1/laya/latest",
                     json={"rows": [{"drug_id": 1, "drug": "Dolo 650", "state": state}]},
                     headers={"X-Laya-Key": os.environ["LAYA_SHARED_KEY"]})
    print("predict:", r2.status_code)
    assert r2.status_code == 200, r2.text
    data = r2.json()
    row = data["rows"][0]
    assert row["error"] is None, row
    assert row["card"]["outlook"]["timing"]["label"], row
    print("card ok:", row["card"]["medicine"],
          row["card"]["outlook"]["timing"]["label"],
          "| band:", row["card"]["outlook"]["band"]["label"],
          "| qty:", row["order_qty"])
    # auth negative
    r3 = client.post("/v1/laya/latest",
                     json={"rows": []}, headers={"X-Laya-Key": "wrong"})
    assert r3.status_code == 401, r3.status_code
    print("auth negative OK (401)")
    print("BUNDLE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
