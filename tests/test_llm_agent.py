"""LLM agent: tool routing, role-based write gating and graceful fallback.

The LLM itself is mocked at the transport boundary (`llm_agent._chat_call`) so
these tests run fully offline and never touch a real provider.
"""
import json

from pharmacy import chatbot, llm_agent


# --------------------------------------------------------------------------- #
# fake transport
# --------------------------------------------------------------------------- #
class _Script:
    """Queue of fake assistant replies; records every tool result fed back."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.tool_results = []

    def __call__(self, messages, tools=None):
        self.tool_results += [m["content"] for m in messages if m.get("role") == "tool"]
        return self.steps.pop(0)


def _call(name, args_json, cid="c1"):
    return {"id": cid, "function": {"name": name, "arguments": args_json}}


def _final(text="Done — here is your answer."):
    return {"content": text, "tool_calls": None}


def _patch(monkeypatch, script):
    monkeypatch.setattr(llm_agent, "llm_configured", lambda: True)
    monkeypatch.setattr(llm_agent, "_chat_call", script)


def _script_one_call(name, args):
    """One fake assistant turn: a single tool call, then a final answer."""
    step_call = {"content": None, "tool_calls": [_call(name, json.dumps(args))]}
    return _Script([step_call, _final("Here is your answer.")])


# --------------------------------------------------------------------------- #
# read tools
# --------------------------------------------------------------------------- #
def test_read_tool_routes_to_stock(db, monkeypatch):
    s = _script_one_call("get_stock", {"drug_name": "Dolo 650"})
    _patch(monkeypatch, s)
    r = chatbot.respond("how much stock of Dolo 650?", use_llm=True)
    assert r["intent"] == "llm"
    assert "answer" in r["text"].lower()
    assert any(c["type"] == "kpis" for c in r["cards"])  # card built from DB data
    assert s.tool_results and any(ch.isdigit() for ch in s.tool_results[0])


def test_product_info_tool_lists_batches(db, monkeypatch):
    s = _script_one_call("get_product_info", {"drug_name": "Dolo 650"})
    _patch(monkeypatch, s)
    r = chatbot.respond("tell me about Dolo 650", use_llm=True)
    assert r["intent"] == "llm"
    assert any(c["type"] == "table" for c in r["cards"])


def test_unknown_tool_is_refused(db, monkeypatch):
    s = _script_one_call("drop_all_tables", {})
    _patch(monkeypatch, s)
    r = chatbot.respond("please drop all tables", use_llm=True)
    assert json.loads(s.tool_results[0])["text"].startswith("Unknown tool")
    assert r["intent"] == "llm"


# --------------------------------------------------------------------------- #
# write tools + role gating
# --------------------------------------------------------------------------- #
def test_create_reorders_denied_for_pharmacist(db, monkeypatch):
    before = db.scalar("SELECT COUNT(*) FROM reorders")
    s = _script_one_call("create_reorders", {"drug_name": "Dolo 650"})
    _patch(monkeypatch, s)
    r = chatbot.respond("create reorder for Dolo 650", use_llm=True, role="pharmacist")
    assert "denied" in json.loads(s.tool_results[0])["text"].lower()
    assert db.scalar("SELECT COUNT(*) FROM reorders") == before  # nothing written


def test_create_reorders_allowed_for_admin(db, monkeypatch):
    before = db.scalar("SELECT COUNT(*) FROM reorders")
    s = _script_one_call("create_reorders", {"drug_name": "Dolo 650"})
    _patch(monkeypatch, s)
    chatbot.respond("create reorder for Dolo 650", use_llm=True, role="admin")
    assert db.scalar("SELECT COUNT(*) FROM reorders") == before + 1


def test_add_waste_denied_for_pharmacist(db, monkeypatch):
    drug = chatbot.find_drug("Dolo 650")
    before = db.scalar("SELECT COUNT(*) FROM waste")
    s = _script_one_call("add_waste", {"drug_name": "Dolo 650", "qty": 2, "reason": "damaged"})
    _patch(monkeypatch, s)
    chatbot.respond("record 2 units of Dolo 650 as waste", use_llm=True, role="pharmacist")
    assert db.scalar("SELECT COUNT(*) FROM waste") == before
    assert drug is not None  # sanity: the drug exists, denial is role-based


def test_add_waste_records_for_admin(db, monkeypatch):
    before = db.scalar("SELECT COUNT(*) FROM waste")
    s = _script_one_call("add_waste", {"drug_name": "Dolo 650", "qty": 2, "reason": "damaged"})
    _patch(monkeypatch, s)
    r = chatbot.respond("record 2 units of Dolo 650 as waste", use_llm=True, role="admin")
    assert db.scalar("SELECT COUNT(*) FROM waste") == before + 1
    assert r["intent"] == "llm"  # agent reported the tool's outcome in its reply
    assert "damaged" in json.loads(s.tool_results[0])["text"].lower()


def test_acknowledge_alerts_allowed_for_pharmacist(db, monkeypatch):
    s = _script_one_call("acknowledge_alerts", {})
    _patch(monkeypatch, s)
    r = chatbot.respond("acknowledge all alerts", use_llm=True, role="pharmacist")
    assert "denied" not in json.loads(s.tool_results[0])["text"].lower()
    assert r["intent"] == "llm"


# --------------------------------------------------------------------------- #
# resilience / fallback
# --------------------------------------------------------------------------- #
def test_agent_returns_none_when_not_configured(monkeypatch):
    monkeypatch.setattr(llm_agent, "llm_configured", lambda: False)
    assert llm_agent.agent_respond("how much stock of Dolo 650?") is None


def test_llm_failure_falls_back_to_deterministic(db, monkeypatch):
    def boom(messages, tools=None):
        raise OSError("network down")

    monkeypatch.setattr(llm_agent, "llm_configured", lambda: True)
    monkeypatch.setattr(llm_agent, "_chat_call", boom)
    r = chatbot.respond("what should I reorder this week?", use_llm=True)
    assert r["text"] and r["intent"] != "llm"  # deterministic engine answered


def test_malformed_tool_args_do_not_crash_agent(db, monkeypatch):
    bad = _Script([{"content": None,
                    "tool_calls": [_call("get_stock", "not-json{{")]},
                   _final("Sorry, that lookup failed.")])
    _patch(monkeypatch, bad)
    r = chatbot.respond("stock of Dolo 650", use_llm=True)
    assert r["intent"] == "llm"  # malformed args tolerated, loop continued


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #
def test_chat_endpoint_requires_admin(client):
    from pharmacy import auth
    auth.create_user("pharm_llm", "pw123456", "pharmacist")
    r = client.post("/api/login", json={"username": "pharm_llm", "password": "pw123456"})
    assert r.status_code == 200
    ph = client.headers.copy()
    ph["X-Session-Token"] = r.json()["token"]
    resp = client.post("/api/chat", json={"message": "stock of Dolo 650"}, headers=ph)
    assert resp.status_code == 403
    # admin still works (LLM unconfigured -> deterministic engine)
    resp = client.post("/api/chat", json={"message": "stock of Dolo 650", "use_llm": False})
    assert resp.status_code == 200
    assert resp.json()["text"]
