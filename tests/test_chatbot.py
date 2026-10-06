"""Deterministic chatbot intents + scope restriction."""
from pharmacy import chatbot


def test_reorder_intent_answers_with_cards():
    r = chatbot.respond("what should I reorder this week?", use_llm=False)
    assert r["text"]
    assert isinstance(r.get("cards"), list)


def test_expiry_intent_returns_numbers():
    r = chatbot.respond("how many Dolo 650 expire next month?", use_llm=False)
    assert "Dolo" in r["text"]
    assert any(c.isdigit() for c in r["text"])


def test_scope_restriction_rejects_off_topic():
    r = chatbot.respond("what's the weather tomorrow?", use_llm=False)
    low = r["text"].lower()
    assert "pharmacy" in low or "inventory" in low  # stays in its lane


def test_history_logs_both_sides():
    chatbot.respond("how much stock is left of Pan 40?", use_llm=False)
    h = chatbot.history(5)
    assert len(h) >= 2
