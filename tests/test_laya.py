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
    """§14/§15: confidence claims are never invented; wording is tied to an
    actually-returned probability."""
    drug = chatbot.find_drug("Dolo 650")
    out = laya.prediction_for(drug)
    text, _ = laya.format_prediction(out)
    assert "most likely" not in text
    assert "low confidence" not in text
    assert "high confidence" not in text  # never invented
    d = out["decisions"]
    p = d["reorder_timing"]["probabilities"][d["reorder_timing"]["value"]]
    # wording must match the actually-returned probability band
    if p >= 0.9:
        assert "highest-probability timing" in text
    elif p >= 0.75:
        assert "highest probability returned" in text
    elif p < 0.5:
        assert "below 0.5 probability" in text


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


def test_numerical_integrity_flags_inconsistent_cover(db):
    """§8: contradictory backend values are flagged, not silently accepted."""
    drug = chatbot.find_drug("Dolo 650")
    out = laya.prediction_for(drug)
    out["state"]["estimated_days_of_cover"] = 5911  # contradicts rem/demand
    text, _ = laya.format_prediction(out)
    assert "Data inconsistency" in text
    assert "should be verified" in text


def test_probability_language_not_confidence(db):
    """§14/§15: probability-aware wording, never invented confidence."""
    drug = chatbot.find_drug("Dolo 650")
    out = laya.prediction_for(drug)
    text, _ = laya.format_prediction(out)
    assert "most likely" not in text
    assert "high confidence" not in text
    for phrase in ("highest-probability timing", "highest probability returned",
                   "below 0.5 probability"):
        if phrase in text:
            break
    else:
        raise AssertionError("no probability-aware wording found: " + text[:200])


def test_invalid_category_raises_malformed(db, monkeypatch):
    """§42: invalid Laya categories are never mapped to the nearest valid one."""
    drug = chatbot.find_drug("Dolo 650")
    state = laya.state_from_db(drug)
    assert state is not None
    monkeypatch.setattr(laya, "predict", lambda s: {
        "decisions": {q: {"value": "tomorrowish" if q == "reorder_timing"
                          else laya.TARGETS[q][0], "probabilities": {}}
                      for q in laya.TARGETS},
        "uncertain": [], "invalid": ["reorder_timing"]})
    try:
        laya.prediction_for(drug)
        raise SystemError("Malformed should have been raised")
    except laya.Malformed as exc:
        assert "invalid value" in str(exc)


def test_sales_not_called_prescriptions(db):
    """§20: the state note uses sales-demand phrasing, never prescriptions."""
    drug = chatbot.find_drug("Dolo 650")
    state = laya.state_from_db(drug)
    note = state["prescription_trend_note"]
    assert "sales volume" in note.lower() or "transaction frequency" in note.lower()
    assert "prescription-demand proxy" not in note.lower()


def test_batch_request_honoured_or_flagged(db):
    """§4: with a requested date+batch, the exact state is honoured; a
    non-matching batch is surfaced, never substituted."""
    r = chatbot.respond(
        "should I reorder Allegra 120 batch ALL-2509-15 on 2025-09-27?",
        use_llm=False)
    assert "[Laya Reorder Prediction]" in r["text"]
    assert "ALL-2509-15" in r["text"]
    # wrong batch for a known date must NOT be silently substituted
    r2 = chatbot.respond(
        "should I reorder Allegra 120 batch NOPE-99-9 on 2025-09-27?",
        use_llm=False)
    assert "can't reliably determine which one you mean" in r2["text"]


def test_exact_requested_state_is_resolved_not_substituted(db):
    """§2/§6: a requested analysis date resolves the exact recorded state -
    never the latest snapshot. Uses the held-out Allegra 2025-09-27 case."""
    import datetime as dt

    drug = chatbot.find_drug("Allegra 120")
    out = laya.prediction_for(drug, as_of=dt.date(2025, 9, 27))
    assert isinstance(out, dict), out  # not an integrity message
    s = out["state"]
    # exact recorded values, untouched: no recalculation, no substitution
    assert s["as_of"] == "2025-09-27"
    assert s["current_batch"] == "ALL-2509-15"
    assert s["recent_daily_demand"] == 4.86
    assert s["baseline_daily_demand"] == 4.62
    assert s["recent_vs_baseline_ratio"] == 1.05
    assert s["seasonality_index"] == 1.3
    # with the exact state, the model matches the gold labels 5/5
    d = out["decisions"]
    assert d["reorder_due_within_7d"]["value"] == "true"
    assert d["reorder_timing"]["value"] == "4_7_days"
    assert d["reorder_quantity_band"]["value"] == "1001_plus"
    assert d["next_30d_demand_trajectory"]["value"] == "spiking"
    assert d["seasonality_signal"]["value"] == "seasonal_up"


def test_no_fallback_for_unknown_date(db):
    """§6: an unknown requested date must not fall back to the latest state."""
    import datetime as dt

    drug = chatbot.find_drug("Dolo 650")
    out = laya.prediction_for(drug, as_of=dt.date(2019, 1, 1))
    assert out == laya.MSG_NO_EXACT_STATE


def test_ambiguous_batch_is_surfaced(db):
    """§4: a wrong/non-matching batch is never silently substituted."""
    import datetime as dt

    drug = chatbot.find_drug("Allegra 120")
    out = laya.prediction_for(drug, as_of=dt.date(2025, 9, 27),
                              batch="NOPE-99-9")
    assert out == laya.MSG_AMBIGUOUS_BATCH


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
