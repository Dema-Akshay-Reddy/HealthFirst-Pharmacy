"""AI demand forecast card: structured payload + delivery contract.

Backend: every calculation (percentage, status badge, interpretation) is
done in laya.demand_forecast_card from the EXACT state Laya saw; missing
data stays missing (never zero), weather absence is never reported as
normal, prescriptions are labelled as a sales proxy, probabilities pass
through only when valid, and bands are ranges - never exact orders.

Frontend: static/demand-forecast-card.js runs under Node to verify the
display contract (format-only, explicit unavailable states, badge only
when supported, escaping).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pharmacy import chatbot, laya

ROOT = Path(__file__).resolve().parent.parent


def _out(drug="Dolo 650"):
    return laya.prediction_for(chatbot.find_drug(drug))


def _with_state(out, **overrides):
    """Same prediction with selected state fields replaced (fresh dicts)."""
    return dict(out, state={**out["state"], **overrides})


def _with_decision(out, key, value, probs):
    return dict(out, decisions={**out["decisions"],
                                key: {"value": value, "probabilities": probs}})


# --------------------------------------------------------------------------- #
# payload: computed backend-side from the exact state and decisions
# --------------------------------------------------------------------------- #
def test_payload_matches_exact_state_and_decisions(db):
    out = _out()
    card = laya.demand_forecast_card(out)
    assert card is not None
    assert card["type"] == "demand_forecast"
    s, d = out["state"], out["decisions"]
    assert card["medicine"] == s["drug"]
    assert card["analysis_date"] == s["as_of"]          # actual analysis date
    assert card["state_id"] == out["state_id"]
    traj = d["next_30d_demand_trajectory"]["value"]
    assert card["outlook"]["timing"]["label"] == laya._TIMING_DISPLAY[d["reorder_timing"]["value"]]
    assert card["outlook"]["band"]["label"] == laya._BAND_DISPLAY[d["reorder_quantity_band"]["value"]]
    assert card["outlook"]["trajectory"]["label"] == laya._TRAJ_DISPLAY[traj]
    # percentage computed HERE, from the state Laya saw
    recent, base = s["recent_daily_demand"], s["baseline_daily_demand"]
    raw = (recent - base) / base * 100
    assert card["drivers"]["baseline_change_pct"] == round(raw)
    expected = "in_line" if abs(raw) < 2 else ("above" if raw > 0 else "below")
    assert card["drivers"]["baseline_change"] == expected
    assert card["drivers"]["recent_daily_demand"] == recent
    assert card["drivers"]["baseline_daily_demand"] == base


def test_interpretation_adapts_and_blames_nothing(db):
    out = _out()
    card = laya.demand_forecast_card(out)
    s = out["state"]
    text = card["interpretation"]
    traj = laya._TRAJ_DISPLAY[out["decisions"]["next_30d_demand_trajectory"]["value"]]
    assert text.startswith(f"{s['drug']} demand is currently classified as {traj.lower()}")
    # status-driven operational tail, deterministic
    assert any(t in text for t in (
        "A reorder may be due within the predicted window",
        "Monitor the trend and review available stock",
        "No immediate reorder is indicated",
        "Review available stock alongside this forecast"))
    low = text.lower()
    assert "weather" not in low                    # never blamed without evidence
    assert "caused" not in low and "because of" not in low
    assert "will " not in low                      # no promised future demand
    assert "order placed" not in low and "auto" not in low


def test_status_badge_only_from_backend_data(db):
    out = _out()
    # due within 7 days -> Reorder soon
    c = laya.demand_forecast_card(
        _with_decision(out, "reorder_due_within_7d", "true", {"true": 0.8, "false": 0.2}))
    assert c["status"] == {"key": "reorder_soon", "label": "Reorder soon"}
    # not due, trajectory rising -> Monitor
    not_due = _with_decision(out, "reorder_due_within_7d", "false",
                             {"false": 0.7, "true": 0.3})
    c = laya.demand_forecast_card(
        _with_decision(not_due, "next_30d_demand_trajectory", "rising",
                       {"rising": 0.6}))
    assert c["status"] == {"key": "monitor", "label": "Monitor"}
    # not due, trajectory stable -> No immediate reorder indicated
    c = laya.demand_forecast_card(
        _with_decision(not_due, "next_30d_demand_trajectory", "stable",
                       {"stable": 0.6}))
    assert c["status"] == {"key": "none", "label": "No immediate reorder indicated"}
    # missing / unsupported due value -> no badge at all
    decisions = {k: v for k, v in out["decisions"].items()
                 if k != "reorder_due_within_7d"}
    assert laya.demand_forecast_card(dict(out, decisions=decisions))["status"] is None
    assert laya.demand_forecast_card(
        _with_decision(out, "reorder_due_within_7d", "maybe", {"maybe": 1.0}))["status"] is None


def test_band_is_a_range_never_an_exact_order(db):
    out = _out()
    card = laya.demand_forecast_card(out)
    band = card["outlook"]["band"]
    assert band["label"] in laya._BAND_DISPLAY.values()   # contract range, not a number of units
    note = card["outlook"]["band_note"].lower()
    assert "not an exact order quantity" in note
    assert "inventory engine" in note
    flat = json.dumps(card).lower()
    assert "order_qty" not in flat and "suggested_order" not in flat


def test_probabilities_only_when_returned_and_valid(db):
    out = _out()
    card = laya.demand_forecast_card(out)
    for cell in ("timing", "band", "trajectory"):
        p = card["outlook"][cell]["prob"]
        assert p is None or (isinstance(p, float) and 0.0 <= p <= 1.0)
    # tampered / non-numeric / out-of-range probabilities are dropped
    c = laya.demand_forecast_card(
        _with_decision(out, "reorder_timing", "4_7_days", {"4_7_days": "high"}))
    assert c["outlook"]["timing"]["prob"] is None
    c = laya.demand_forecast_card(
        _with_decision(out, "reorder_timing", "4_7_days", {"4_7_days": 1.5}))
    assert c["outlook"]["timing"]["prob"] is None
    c = laya.demand_forecast_card(
        _with_decision(out, "reorder_timing", "4_7_days", {}))
    assert c["outlook"]["timing"]["prob"] is None


def test_weather_absence_is_unavailable_never_assumed_normal(db):
    out = _out()
    # missing block -> None (card must show "Weather signal unavailable")
    c = laya.demand_forecast_card(_with_state(out, weather={}))
    assert c["drivers"]["weather"] is None
    c = laya.demand_forecast_card(_with_state(out, weather=None))
    assert c["drivers"]["weather"] is None
    # observed normal classification is allowed (it is measured, not assumed)
    c = laya.demand_forecast_card(
        _with_state(out, weather={"weather_anomaly": "normal", "source": "open-meteo"}))
    assert c["drivers"]["weather"]["label"] == "Normal conditions"
    # observed anomaly surfaces with human wording
    c = laya.demand_forecast_card(_with_state(
        out, weather={"weather_anomaly": "hotter_than_normal", "temp_7d_avg": 41.5,
                      "rain_7d_mm": 0.0, "source": "open-meteo"}))
    assert c["drivers"]["weather"]["label"] == "Elevated temperatures"
    assert "41.5" in c["drivers"]["weather"]["detail"]


def test_prescriptions_absent_labelled_as_sales_proxy(db):
    out = _out()
    drv = laya.demand_forecast_card(out)["drivers"]
    assert drv["prescriptions_available"] is False
    assert drv["prescription_trend"] is None
    assert "demand proxy" in drv["demand_proxy_note"].lower()
    # when actual prescription data exists, the trend is shown instead
    c = laya.demand_forecast_card(
        _with_state(out, prescription_demand_trend=12.5))
    assert c["drivers"]["prescriptions_available"] is True
    assert "12.5" in c["drivers"]["prescription_trend"]
    assert c["drivers"]["demand_proxy_note"] is None


def test_missing_metrics_stay_none_and_zero_stays_zero(db):
    out = _out()
    c = laya.demand_forecast_card(
        _with_state(out, recent_daily_demand="n/a", baseline_daily_demand=None))
    assert c["drivers"]["recent_daily_demand"] is None
    assert c["drivers"]["baseline_daily_demand"] is None
    assert c["drivers"]["baseline_change"] == "unavailable"
    assert c["drivers"]["baseline_change_pct"] is None
    # a real zero is a value, not missing
    c = laya.demand_forecast_card(_with_state(out, recent_daily_demand=0.0))
    assert c["drivers"]["recent_daily_demand"] == 0.0
    assert c["drivers"]["baseline_change"] in ("below", "in_line")


def test_invalid_payload_omits_the_card_never_fakes_it(db):
    out = _out()
    assert laya.demand_forecast_card({}) is None
    assert laya.demand_forecast_card({"state": None, "decisions": {}}) is None
    assert laya.demand_forecast_card(_with_state(out, drug="  ")) is None
    assert laya.demand_forecast_card(_with_state(out, as_of="")) is None
    # unknown trajectory category -> no card (§42: never mapped to nearest)
    assert laya.demand_forecast_card(
        _with_decision(out, "next_30d_demand_trajectory", "exploding", {"exploding": 1.0})) is None
    # invalid categories from Laya -> no card
    assert laya.demand_forecast_card(dict(out, invalid=["reorder_timing"])) is None


# --------------------------------------------------------------------------- #
# delivery: forecast intent + LLM path, exact-state rules
# --------------------------------------------------------------------------- #
def test_forecast_answer_carries_forecast_and_trajectory_cards(db):
    r = chatbot.respond("forecast for Dolo 650", use_llm=False)
    fc = [c for c in r["cards"] if c["type"] == "demand_forecast"]
    tc = [c for c in r["cards"] if c["type"] == "demand_trajectory"]
    assert len(fc) == 1 and len(tc) == 1
    assert fc[0]["medicine"] == "Dolo 650"
    assert fc[0]["analysis_date"]
    # existing forecasting workflow untouched
    assert any(c["type"] == "table" for c in r["cards"])
    assert "Forecasting model" in r["text"]


def test_requested_historical_state_is_never_substituted(db):
    r = chatbot.respond("forecast for Telma 40 on 5 March 2020", use_llm=False)
    # no cards from a substitute state - the integrity message is surfaced
    assert not any(c["type"] in ("demand_forecast", "demand_trajectory")
                   for c in r["cards"])
    assert laya.MSG_NO_EXACT_STATE in r["text"]
    assert any(c["type"] == "table" for c in r["cards"])  # forecast still answered


def test_llm_forecast_cards_reach_user_without_tool_calls(db, monkeypatch):
    """The model may answer a forecast question with plain prose; the
    structured cards must still arrive while its wording stays untouched."""
    from pharmacy import llm_agent

    class FinalOnly:
        def __call__(self, messages, tools=None):
            return {"content": "The 30-day forecast looks steady overall.",
                    "tool_calls": None}

    monkeypatch.setattr(llm_agent, "llm_configured", lambda: True)
    monkeypatch.setattr(llm_agent, "_chat_call", FinalOnly())
    r = chatbot.respond("forecast for Dolo 650", use_llm=True)
    assert r["intent"] == "llm"
    assert r["text"] == "The 30-day forecast looks steady overall."
    assert sum(1 for c in r["cards"] if c["type"] == "demand_forecast") == 1
    assert sum(1 for c in r["cards"] if c["type"] == "demand_trajectory") == 1


def test_llm_tool_call_and_seed_never_duplicate_cards(db, monkeypatch):
    from pharmacy import llm_agent

    steps = [
        {"content": None, "tool_calls": [
            {"id": "c1", "function": {
                "name": "laya_reorder_prediction",
                "arguments": json.dumps({"drug_name": "Dolo 650"})}}]},
        {"content": "Here is the forecast.", "tool_calls": None},
    ]

    def script(messages, tools=None):
        return steps.pop(0)

    monkeypatch.setattr(llm_agent, "llm_configured", lambda: True)
    monkeypatch.setattr(llm_agent, "_chat_call", script)
    r = chatbot.respond("forecast for Dolo 650", use_llm=True)
    assert r["intent"] == "llm"
    for t in ("demand_forecast", "demand_trajectory", "list"):
        assert sum(1 for c in r["cards"] if c["type"] == t) == 1, t


def test_off_topic_and_unknown_product_get_no_forecast_cards(db):
    assert chatbot.laya_forecast_cards_for_message("show waste report") == []
    assert chatbot.laya_forecast_cards_for_message("forecast for Foobar 999") == []
    assert chatbot.laya_forecast_cards_for_message(
        "create reorder for Pan 40") == []   # action request, not a forecast


# --------------------------------------------------------------------------- #
# frontend: the component itself, executed under Node
# --------------------------------------------------------------------------- #
def test_js_component_formatting_and_validation():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed - JS component test cannot run")
    script = Path(__file__).with_name("demand_forecast_card.test.js")
    proc = subprocess.run([node, str(script)], capture_output=True, text=True,
                          timeout=120, cwd=ROOT)
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
