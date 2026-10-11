"""Laya prediction microservice — the REAL pharmacy/laya.py engine over HTTP.

Deployed on Render (free tier). The Cloudflare Worker POSTs live per-SKU
state snapshots built from its D1 database; this service runs the genuine
sklearn pipeline (pharmacy/laya.py: predict -> demand_forecast_card ->
demand_trajectory_card -> reorder_by_date -> band_order_qty) and returns
EXACTLY the payload shape the frontend's Laya outlook already renders.

Security: every request must present `X-Laya-Key` matching the LAYA_SHARED_KEY
env var (set in both Render and `wrangler secret put LAYA_SHARED_KEY`). The
service never touches a database — it is prediction-only, stateless, and
holds no secrets worth stealing beyond the model.

Failure honesty (§13): a state whose prediction fails comes back as an
`error` row with the same error vocabulary the local app uses — never a
fabricated prediction.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

# --- paths: work both from the repo root and as a deployed service ----------
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The engine reads models from DATA_DIR/models/laya.pkl; the deploy bundle
# ships them under ./data/models/. LAYA_WEATHER_FEATURES stays OFF (the
# Worker does not send weather blocks), matching how the model was trained
# for this deployment.
os.environ.setdefault("PHARMACY_DATA_DIR", str(ROOT / "data"))
os.environ.setdefault("PHARMACY_LAYA_WEATHER", "false")

from pharmacy import config  # noqa: E402  (imports after env is set)
config.LAYA_WEATHER_FEATURES = False  # defense in depth: never fetch weather here

from pharmacy import laya  # noqa: E402

SHARED_KEY = os.environ.get("LAYA_SHARED_KEY", "")

app = FastAPI(title="Laya prediction service", version="1.0.0")


class StateRow(BaseModel):
    drug_id: int
    drug: str
    state: dict[str, Any]


class PredictRequest(BaseModel):
    rows: list[StateRow]


def check_key(key: str | None) -> None:
    if not SHARED_KEY:
        raise HTTPException(status_code=503, detail="service not configured with LAYA_SHARED_KEY")
    if key != SHARED_KEY:
        raise HTTPException(status_code=401, detail="invalid key")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "engine": laya.laya_engine_status()}


@app.post("/v1/laya/latest")
def latest(rows_req: PredictRequest, x_laya_key: str | None = Header(default=None)) -> dict:
    """Laya outlook for the pushed latest-state rows.

    Mirrors laya.forecast_page_payload() but takes states from the Worker
    instead of the local DB. Each row is predicted independently; one broken
    SKU must not sink the page.
    """
    check_key(x_laya_key)
    engine = laya.laya_engine_status()
    out_rows: list[dict[str, Any]] = []
    for row in rows_req.rows:
        base = dict(
            drug_id=int(row.drug_id), drug=str(row.drug),
            as_of=None, card=None, trajectory=None,
            reorder_by=None, order_qty=None, order_qty_open=False,
            error=None, error_detail=None,
        )
        if engine["status"] != "ready":
            base["error"] = "engine_unavailable"
            base["error_detail"] = engine["detail"]
            out_rows.append(base)
            continue
        state = dict(row.state)
        try:
            from pharmacy.laya import Malformed, Timeout, Unavailable

            out = laya.predict(state)
            if out.get("invalid"):
                raise Malformed(laya.MSG_INVALID)
            out["state"] = state
            out["state_id"] = laya._register_trace(state, out)
            state_out = state
            s = state_out
            base["as_of"] = str(s.get("as_of") or "") or None
            base["card"] = laya.demand_forecast_card(out)
            base["trajectory"] = laya.demand_trajectory_card(out)
            timing_raw = (out.get("decisions") or {}).get("reorder_timing") or {}
            base["reorder_by"] = laya.reorder_by_date(timing_raw.get("value"))
            if base["card"] is not None:
                band_key = ((out.get("decisions") or {}).get("reorder_quantity_band")
                            or {}).get("value")
                base["order_qty"] = laya.band_order_qty(band_key)
                base["order_qty_open"] = band_key == laya._BAND_OPEN_TOP
            if base["card"] is None:
                base["error"] = "prediction_invalid"
                base["error_detail"] = (
                    "Laya returned a state that cannot be presented "
                    "(invalid or incomplete category).")
        except (Unavailable, Timeout) as exc:
            base["error"] = "engine_unavailable" if isinstance(exc, Unavailable) else "timeout"
            base["error_detail"] = str(exc)
        except Malformed:
            base["error"] = "malformed"
            base["error_detail"] = laya.MSG_INVALID
        except Exception as exc:  # one broken SKU must not sink the page
            base["error"] = "error"
            base["error_detail"] = str(exc)
        out_rows.append(base)
    return dict(engine=engine,
                generated_at=datetime.now().isoformat(timespec="seconds"),
                state="latest", rows=out_rows)
