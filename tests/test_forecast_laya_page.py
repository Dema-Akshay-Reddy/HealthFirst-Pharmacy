"""Forecast page Laya outlook: GET /api/forecast/laya contract.

Every catalogue SKU gets a prediction recomputed from the LATEST state on
each call (the page is always up to date), each row carries the same
validated card payloads the chatbot renders (one prediction -> page and
chat cannot disagree), failures degrade to explicit error rows (never a
fabricated prediction, never a substituted state), and the route is gated
like the rest of /api/forecast.

The frontend display contract runs under Node via
tests/forecast_laya.test.js (test_js_component_formatting_and_validation).
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from pharmacy import chatbot, laya

ROOT = Path(__file__).resolve().parent.parent
ENDPOINT = "/api/forecast/laya"


def test_endpoint_requires_auth(client):
    assert client.get(ENDPOINT, headers={"X-Session-Token": "bogus"}).status_code == 401


def test_every_sku_has_an_up_to_date_prediction(client, db):
    r = client.get(ENDPOINT)
    assert r.status_code == 200
    payload = r.json()
    assert payload["state"] == "latest"
    assert payload["generated_at"]
    assert payload["engine"]["status"] == "ready"
    drugs = db.query("SELECT * FROM drugs ORDER BY category, name")
    rows = payload["rows"]
    assert [row["drug_id"] for row in rows] == [int(d["id"]) for d in drugs]
    for row, d in zip(rows, drugs, strict=True):
        latest = laya.state_from_db(dict(d))
        if latest is None:
            # no demand signal left for this SKU (earlier suite tests can move
            # the data anchor): the honest outcome is an explicit no_data row,
            # never a fabricated prediction and never a zeroed one.
            assert row["card"] is None
            assert row["error"] == "no_data"
            assert row["error_detail"]
            continue
        assert row["error"] is None, f"{row['drug']}: {row['error_detail']}"
        card = row["card"]
        assert card["type"] == "demand_forecast"
        # up to date: the row's analysis date IS the latest state's date
        assert row["as_of"] == latest["as_of"] == card["analysis_date"]
        assert row["trajectory"] is None or row["trajectory"]["type"] == "demand_trajectory"


def test_page_and_chat_share_one_prediction(client):
    """The forecast page and the chatbot render the SAME validated payload."""
    chat_card = laya.demand_forecast_card(
        laya.prediction_for(chatbot.find_drug("Dolo 650")))
    assert chat_card is not None
    row = next(r for r in client.get(ENDPOINT).json()["rows"]
               if r["drug"] == "Dolo 650")
    page, chat = dict(row["card"]), dict(chat_card)
    page.pop("state_id", None)  # trace id is per-call; payload must still match
    chat.pop("state_id", None)
    assert page == chat


def test_bands_stay_ranges_and_probabilities_are_valid(client):
    for row in client.get(ENDPOINT).json()["rows"]:
        card = row["card"]
        if card is None:
            assert row["error"] in ("no_data", "state_unavailable", "engine_unavailable",
                                    "timeout", "malformed", "prediction_invalid", "error")
            continue  # failure is explicit, never a fabricated prediction
        if card["status"] is not None:
            assert card["status"]["key"] in ("reorder_soon", "monitor", "none")
        outlook = card["outlook"]
        assert outlook["band"]["label"]  # a range label, never an order number
        for cell in (outlook["timing"], outlook["band"], outlook["trajectory"]):
            p = cell.get("prob")
            assert p is None or (isinstance(p, (int, float))
                                 and not isinstance(p, bool) and 0 <= p <= 1)


def test_rows_carry_laya_order_qty_from_the_band(client):
    """Every valid row carries Laya's own order quantity: the floor of its
    predicted band, flagged when the top band is open-ended. Failed
    predictions and missing bands arrive as None - never an invented number."""
    for row in client.get(ENDPOINT).json()["rows"]:
        card = row["card"]
        if card is None:
            assert row["order_qty"] is None
            assert row["order_qty_open"] is False
            continue
        label = (card["outlook"].get("band") or {}).get("label")
        if label is None:
            assert row["order_qty"] is None  # no band -> no quantity
            continue
        floor = re.match(r"(\d+)", label)
        assert floor, f"band label must lead with its floor: {label}"
        assert isinstance(row["order_qty"], int) and row["order_qty"] > 0
        assert row["order_qty"] == int(floor.group(1))
        # the open-ended top band must be presented as a minimum
        assert row["order_qty_open"] is label.startswith("1001+")


def test_engine_failure_degrades_to_error_rows(client, monkeypatch):
    def boom(state):
        raise laya.Unavailable("model offline")
    monkeypatch.setattr(laya, "prediction_for", boom)
    payload = client.get(ENDPOINT).json()
    assert payload["rows"], "SKUs stay listed even when the engine is down"
    for row in payload["rows"]:
        assert row["card"] is None
        assert row["error"] == "engine_unavailable"
        assert "model offline" in (row["error_detail"] or "")


def test_one_broken_sku_does_not_sink_the_page(client, monkeypatch):
    real = laya.prediction_for

    def selective(drug, *args, **kwargs):
        if str(drug.get("name")) == "Pan 40":
            raise RuntimeError("boom")
        return real(drug, *args, **kwargs)

    monkeypatch.setattr(laya, "prediction_for", selective)
    rows = client.get(ENDPOINT).json()["rows"]
    broken = next(r for r in rows if r["drug"] == "Pan 40")
    assert broken["error"] == "error" and broken["card"] is None
    assert "boom" in (broken["error_detail"] or "")
    others = [r for r in rows if r["drug"] != "Pan 40"]
    # only the injected SKU gets the generic failure; every other row is
    # either a valid prediction or a legitimate no-data row
    assert all(r["error"] in (None, "no_data") for r in others)
    assert all(r["card"] is not None for r in others if r["error"] is None)


# --------------------------------------------------------------------------- #
# frontend: the component itself, executed under Node
# --------------------------------------------------------------------------- #
def test_js_component_formatting_and_validation():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed - JS component test cannot run")
    script = Path(__file__).with_name("forecast_laya.test.js")
    proc = subprocess.run([node, str(script)], capture_output=True, text=True,
                          timeout=120, cwd=ROOT)
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
