"""Demand trajectory insight card: structured payload + rendering contract.

Backend: the card payload is built from the exact Laya state/decisions,
never fabricated, and reaches every chat path (deterministic, LLM tool call,
LLM seed, forecast intent).

Frontend: static/demand-trajectory-card.js is executed under Node to verify
deterministic percentage maths, explicit missing/invalid handling, badge
integrity (metrics never override Laya's trajectory) and HTML escaping.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pharmacy import chatbot, laya

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = dict(
    type="demand_trajectory",
    medicine="Dolo 650",
    analysis_date="2025-11-30",
    trajectory="spiking",
    trajectory_label="Spiking",
    recent_daily_demand=5.57,
    baseline_daily_demand=4.46,
    recent_vs_baseline_ratio=1.249,
    state_id="st_abc123",
)


# --------------------------------------------------------------------------- #
# backend: payload built from the exact state
# --------------------------------------------------------------------------- #
def test_payload_matches_the_exact_state_and_decisions(db):
    drug = chatbot.find_drug("Dolo 650")
    out = laya.prediction_for(drug)
    card = laya.demand_trajectory_card(out)
    assert card is not None
    assert card["type"] == "demand_trajectory"
    s = out["state"]
    assert card["medicine"] == s["drug"]
    assert card["analysis_date"] == s["as_of"]
    traj = out["decisions"]["next_30d_demand_trajectory"]["value"]
    assert card["trajectory"] == traj
    assert card["trajectory_label"] == laya._TRAJ_DISPLAY[traj]
    assert card["recent_daily_demand"] == s["recent_daily_demand"]
    assert card["baseline_daily_demand"] == s["baseline_daily_demand"]
    assert card["recent_vs_baseline_ratio"] == s["recent_vs_baseline_ratio"]
    assert card["state_id"] == out["state_id"]
    # This card explains trajectory only: no reorder timing, no quantity band.
    assert not any(k in card for k in ("reorder_timing", "reorder_quantity_band",
                                       "timing", "band", "due"))


def test_format_prediction_returns_the_card_alongside_the_text(db):
    out = laya.prediction_for(chatbot.find_drug("Dolo 650"))
    text, cards = laya.format_prediction(out)
    assert "[Laya Reorder Prediction]" in text
    assert [c["type"] for c in cards] == ["list", "demand_trajectory"]


def test_invalid_or_missing_fields_omit_the_card_never_fake_it(db):
    out = laya.prediction_for(chatbot.find_drug("Dolo 650"))
    # unknown trajectory category -> no card (§42: never mapped to nearest)
    bad = dict(out, decisions=dict(out["decisions"],
                                   next_30d_demand_trajectory={"value": "exploding"}))
    assert laya.demand_trajectory_card(bad) is None
    # missing state / garbage payload -> no card, no exception
    assert laya.demand_trajectory_card({}) is None
    assert laya.demand_trajectory_card({"state": None, "decisions": {}}) is None
    # unusable core fields (empty medicine/date) -> no card
    assert laya.demand_trajectory_card(
        dict(out, state=dict(out["state"], drug="  "))) is None
    # non-numeric metrics pass through as None - never coerced to 0
    card = laya.demand_trajectory_card(
        dict(out, state=dict(out["state"], recent_daily_demand="n/a",
                             baseline_daily_demand=None)))
    assert card is not None
    assert card["recent_daily_demand"] is None
    assert card["baseline_daily_demand"] is None


# --------------------------------------------------------------------------- #
# backend: the card reaches every chat path exactly once
# --------------------------------------------------------------------------- #
def test_reorder_answer_carries_the_structured_card(db):
    r = chatbot.respond("should I reorder Dolo 650?", use_llm=False)
    cards = [c for c in r["cards"] if c["type"] == "demand_trajectory"]
    assert len(cards) == 1
    assert cards[0]["medicine"] == "Dolo 650"
    # the card's trajectory matches the text Laya was served verbatim
    assert f"Demand trajectory: {cards[0]['trajectory_label']}" in r["text"]


def test_forecast_answer_carries_the_structured_card(db):
    r = chatbot.respond("forecast for Dolo 650", use_llm=False)
    cards = [c for c in r["cards"] if c["type"] == "demand_trajectory"]
    assert len(cards) == 1
    assert cards[0]["medicine"] == "Dolo 650"
    assert cards[0]["analysis_date"]


def test_seed_helper_returns_text_and_cards(db):
    seed = chatbot.laya_answer_for_message("should I reorder Dolo 650?")
    assert seed is not None
    text, cards = seed
    assert text.startswith("[Laya Reorder Prediction]")
    assert any(c["type"] == "demand_trajectory" for c in cards)
    # action requests / unknown products stay unseeded
    assert chatbot.laya_answer_for_message("create reorder for Pan 40") is None
    assert chatbot.laya_answer_for_message("reorder foobar 999") is None


def test_llm_agent_serves_seed_cards_without_tool_call(db, monkeypatch):
    """The model may answer without calling any tool; the seeded structured
    cards must still reach the user alongside the verbatim prediction."""
    from pharmacy import llm_agent

    class FinalOnly:
        def __call__(self, messages, tools=None):
            return {"content": "Sure - reorder Dolo 650 soon to be safe.",
                    "tool_calls": None}

    monkeypatch.setattr(llm_agent, "llm_configured", lambda: True)
    monkeypatch.setattr(llm_agent, "_chat_call", FinalOnly())
    r = chatbot.respond("should I reorder Dolo 650?", use_llm=True)
    assert r["intent"] == "llm"
    assert r["text"].startswith("[Laya Reorder Prediction]")  # guardrail
    assert sum(1 for c in r["cards"] if c["type"] == "demand_trajectory") == 1


def test_llm_agent_tool_call_yields_exactly_one_card(db, monkeypatch):
    """When the model does call the prediction tool, seed cards are not
    duplicated on top of the tool's own cards."""
    from pharmacy import llm_agent

    steps = [
        {"content": None, "tool_calls": [
            {"id": "c1", "function": {
                "name": "laya_reorder_prediction",
                "arguments": json.dumps({"drug_name": "Dolo 650"})}}]},
        {"content": "Here is the prediction.", "tool_calls": None},
    ]

    def script(messages, tools=None):
        return steps.pop(0)

    monkeypatch.setattr(llm_agent, "llm_configured", lambda: True)
    monkeypatch.setattr(llm_agent, "_chat_call", script)
    r = chatbot.respond("should I reorder Dolo 650?", use_llm=True)
    assert r["intent"] == "llm"
    assert sum(1 for c in r["cards"] if c["type"] == "demand_trajectory") == 1


# --------------------------------------------------------------------------- #
# frontend: the component itself, executed under Node
# --------------------------------------------------------------------------- #
def test_js_component_validation_and_rendering():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed - JS component test cannot run")
    script = Path(__file__).with_name("demand_trajectory_card.test.js")
    proc = subprocess.run([node, str(script)], capture_output=True, text=True,
                          timeout=120, cwd=ROOT)
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
