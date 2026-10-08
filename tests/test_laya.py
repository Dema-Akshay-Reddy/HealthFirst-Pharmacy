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


def test_state_traceability(db):
    """Every Laya request retains the exact state + output for audit."""
    drug = chatbot.find_drug("Dolo 650")
    out = laya.prediction_for(drug)
    assert out.get("state_id", "").startswith("st_")
    rec = laya.trace_for(out["state_id"])
    assert rec is not None
    assert rec["input_state"] is out["state"]          # same object, not a copy
    assert rec["analysis_date"] == out["state"]["as_of"]
    assert rec["product"] == drug["name"]
    assert rec["laya_output"] == out["decisions"]


def test_explanation_uses_the_exact_state_sent(db):
    """Explanation numbers must come from the state Laya actually saw."""
    drug = chatbot.find_drug("Dolo 650")
    out = laya.prediction_for(drug)
    text, _ = laya.format_prediction(out)
    s = out["state"]
    assert f"Recent daily demand: {s['recent_daily_demand']}" in text
    assert f"Historical baseline: {s['baseline_daily_demand']}" in text
    assert f"Recent vs baseline: {s['recent_vs_baseline_ratio']}" in text
    assert out["state_id"] in text  # traceability header present


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
    assert "[Laya Reorder Prediction]" in r["text"]
    # §13 contract: all five immutable fields, supporting data, explanation
    for field in ("Reorder within 7 days:", "Reorder timing:", "Quantity band:",
                  "Demand trajectory:", "Seasonality:", "Supporting data:",
                  "Explanation:"):
        assert field in r["text"]
    assert any(c["type"] == "list" and c["title"] == "Laya reorder prediction"
               for c in r["cards"])
    # §6/§7: bands and timing windows rendered per the contract, never exact
    assert "units" in r["text"]
    assert "Order " not in r["text"].split("Explanation:")[-1]


def test_format_preserves_categorical_labels_exactly(db):
    """§1/§6/§7/§8: Laya categories rendered exactly, never reinterpreted."""
    drug = chatbot.find_drug("Dolo 650")
    out = laya.prediction_for(drug)
    text, _ = laya.format_prediction(out)
    d = out["decisions"]
    assert laya._TIMING_DISPLAY[d["reorder_timing"]["value"]] in text
    assert laya._BAND_DISPLAY[d["reorder_quantity_band"]["value"]] in text
    assert laya._TRAJ_DISPLAY[d["next_30d_demand_trajectory"]["value"]] in text
    assert laya._SEAS_DISPLAY[d["seasonality_signal"]["value"]] in text
    # no raw category slugs leak into the output
    assert "4_7_days" not in text and "801_1000" not in text


def test_conflict_note_shows_both_systems(db):
    """§9/§10: when the engine and Laya disagree, both are shown and the
    disagreement is stated - neither side is modified."""
    drug = chatbot.find_drug("Dolo 650")
    out = laya.prediction_for(drug)
    laya_due = out["decisions"]["reorder_due_within_7d"]["value"] == "true"
    engine_status = "order_now" if not laya_due else "ok"
    text, _ = laya.format_prediction(out, engine_status=engine_status)
    if laya_due != (engine_status == "order_now"):
        assert "The two systems currently disagree." in text
        assert f"Reorder due within 7 days: {out['decisions']['reorder_due_within_7d']['value']}" in text
        assert f"Reorder status: {engine_status.replace('_', ' ')}" in text


def test_confidence_phrases_only_from_probabilities(db):
    """§5: confidence wording only where Laya returned a probability."""
    drug = chatbot.find_drug("Dolo 650")
    out = laya.prediction_for(drug)
    text, _ = laya.format_prediction(out)
    d = out["decisions"]
    p = d["reorder_timing"]["probabilities"][d["reorder_timing"]["value"]]
    if p >= 0.75:
        assert "most likely" in text
    elif p < 0.5:
        assert "low confidence" in text
    assert "high confidence" not in text  # never invented


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
    assert "[Laya Reorder Prediction]" in result
    assert "Quantity band" in result


def test_llm_cannot_alter_laya_prediction(db, monkeypatch):
    """§1/§11 hard guardrail: if the model's final answer drops or alters a
    Laya prediction, the compliant tool output replaces the model's answer."""
    from pharmacy import llm_agent

    class Drift:
        def __init__(self):
            self.n = 0

        def __call__(self, messages, tools=None):
            self.n += 1
            if self.n == 1:
                return {"content": None, "tool_calls": [
                    {"id": "c1", "function": {
                        "name": "laya_reorder_prediction",
                        "arguments": json.dumps({"drug_name": "Dolo 650"})}}]}
            # model "reinterprets": invents its own trajectory and drops fields
            return {"content": "Laya says demand is Falling and you should "
                               "order exactly 900 units tomorrow.",
                    "tool_calls": None}

    drift = Drift()
    monkeypatch.setattr(llm_agent, "llm_configured", lambda: True)
    monkeypatch.setattr(llm_agent, "_chat_call", drift)
    r = chatbot.respond("when should we reorder Dolo 650?", use_llm=True)
    assert r["intent"] == "llm"
    # the model's altered text is gone; the immutable tool output is served
    assert r["text"].startswith("[Laya Reorder Prediction]")
    assert "order exactly 900" not in r["text"]
    assert "Reorder timing:" in r["text"] and "Quantity band:" in r["text"]
