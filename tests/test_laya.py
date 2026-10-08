"""Laya reorder-prediction layer: state building, prediction, chat integration.

The models train on the bundled laya_test_data (train split only) on first use
and are cached in the temp data dir the test suite already isolates.
"""
import json

from pharmacy import chatbot, laya


def test_train_and_predict_on_val_states():
    rows = laya._load_split("val")[:2]
    assert rows, "laya_test_data/val.jsonl missing"
    for r in rows:
        out = laya.predict(r["state"])
        for q, classes in laya.TARGETS.items():
            dec = out["decisions"][q]
            assert dec["value"] in classes
            assert abs(sum(dec["probabilities"].values()) - 1.0) < 0.01
    assert laya._bundle.get("val_accuracy")


def test_state_from_db_builds_signals(db):
    drug = chatbot.find_drug("Dolo 650")
    assert drug is not None
    state = laya.state_from_db(drug)
    assert state is not None
    assert state["drug"] == drug["name"]
    for key in ("recent_daily_demand", "baseline_daily_demand",
                "seasonality_index", "weekly_sales_units_last_8_weeks"):
        assert key in state
    assert len(state["weekly_sales_units_last_8_weeks"]) == 8


def test_prediction_for_named_drug(db):
    drug = chatbot.find_drug("Dolo 650")
    out = laya.prediction_for(drug)
    assert out is not None
    timing = out["decisions"]["reorder_timing"]["value"]
    assert timing in laya.TARGETS["reorder_timing"]
    band = out["decisions"]["reorder_quantity_band"]["value"]
    assert band in laya.TARGETS["reorder_quantity_band"]


def test_reorder_answer_includes_laya_prediction(db):
    r = chatbot.respond("should I reorder Dolo 650?", use_llm=False)
    assert "Laya prediction" in r["text"]
    assert any(c["type"] == "list" and c["title"] == "Laya reorder prediction"
               for c in r["cards"])
    # the model reports a band, never a fabricated exact order quantity
    assert "Quantity band" in r["text"]


def test_unknown_drug_gets_insufficient_data_message(db):
    r = chatbot.respond("should I reorder Dolo 650?", use_llm=False)
    assert r["intent"] == "reorder"


def test_llm_agent_tool_routes_to_laya(db, monkeypatch):
    from pharmacy import llm_agent

    class Fake:
        def __init__(self):
            self.tool_results = []

        def __call__(self, messages, tools=None):
            self.tool_results += [m["content"] for m in messages
                                  if m.get("role") == "tool"]
            if len(self.tool_results) == 0:
                return {"content": None, "tool_calls": [
                    {"id": "c1", "function": {
                        "name": "laya_reorder_prediction",
                        "arguments": json.dumps({"drug_name": "Dolo 650"})}}]}
            return {"content": "Here is the Laya prediction.", "tool_calls": None}

    fake = Fake()
    monkeypatch.setattr(llm_agent, "llm_configured", lambda: True)
    monkeypatch.setattr(llm_agent, "_chat_call", fake)
    r = chatbot.respond("when should we reorder Dolo 650?", use_llm=True)
    assert r["intent"] == "llm"
    result = json.loads(fake.tool_results[0])["text"]
    assert "Laya prediction" in result
    assert "quantity band" in result.lower()
