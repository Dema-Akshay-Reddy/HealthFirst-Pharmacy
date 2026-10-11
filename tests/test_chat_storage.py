"""Per-user chatbot storage & cross-user isolation (API-level)."""
import pytest


@pytest.fixture()
def two_users(client):
    """Login as admin (default seeded) and a pharmacist; return header dicts.
    User B is created idempotently (session-scoped client)."""
    from pharmacy import db
    if not db.one("SELECT id FROM users WHERE username = 'chat_user_b'"):
        from pharmacy import auth
        auth.create_user("chat_user_b", "pw123456", "pharmacist")

    def login(username, password):
        r = client.post("/api/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        h = client.headers.copy()
        h["X-Session-Token"] = r.json()["token"]
        return h

    admin = login("admin", "admin123")
    ph_b = login("chat_user_b", "pw123456")
    return admin, ph_b


def test_users_have_separate_conversations(client, two_users):
    """Session-scoped app: previous tests may have created conversations for
    the admin — snapshot ids before, then verify isolation of the new ones."""
    admin, ph_b = two_users
    pre_a = {c["id"] for c in client.get("/api/chat/conversations",
                                         headers=admin).json()["items"]}
    pre_b = {c["id"] for c in client.get("/api/chat/conversations",
                                         headers=ph_b).json()["items"]}
    r1 = client.post("/api/chat", json={"message": "stock of Dolo 650", "use_llm": False},
                     headers=admin)
    r2 = client.post("/api/chat", json={"message": "expiry report please", "use_llm": False},
                     headers=ph_b)
    assert r1.status_code == 200 and r2.status_code == 200

    conv_a = client.get("/api/chat/conversations", headers=admin).json()
    conv_b = client.get("/api/chat/conversations", headers=ph_b).json()
    new_a = next(c for c in conv_a["items"] if c["id"] not in pre_a)
    new_b = next(c for c in conv_b["items"] if c["id"] not in pre_b)
    assert new_a["id"] != new_b["id"], "same conversation id owned by two users"

    msgs_a = client.get(f"/api/chat/conversations/{new_a['id']}/messages", headers=admin)
    msgs_b = client.get(f"/api/chat/conversations/{new_b['id']}/messages", headers=ph_b)
    assert msgs_a.status_code == 200 and msgs_b.status_code == 200
    texts_a = [m["content"] for m in msgs_a.json()["items"]]
    texts_b = [m["content"] for m in msgs_b.json()["items"]]
    assert "stock of Dolo 650" in texts_a
    assert "expiry report please" in texts_b
    assert "expiry report please" not in texts_a
    assert "stock of Dolo 650" not in texts_b
    # user B can never open A's new conversation
    assert client.get(f"/api/chat/conversations/{new_a['id']}/messages",
                      headers=ph_b).status_code == 404


def test_cross_user_access_denied(client, two_users):
    admin, ph_b = two_users
    client.post("/api/chat", json={"message": "admin secret question", "use_llm": False},
                headers=admin)
    conv_a = client.get("/api/chat/conversations", headers=admin).json()["items"][0]
    a_id = conv_a["id"]

    # User B cannot read, rename, archive, delete, or list messages of A's convo
    assert client.get(f"/api/chat/conversations/{a_id}/messages",
                      headers=ph_b).status_code == 404
    # rename of someone else's conversation is 404 (or 400 when FastAPI
    # validates the body before the ownership check — both deny access)
    r = client.patch(f"/api/chat/conversations/{a_id}", json={"title": "hijacked"},
                     headers=ph_b)
    assert r.status_code in (404, 400)
    r = client.patch(f"/api/chat/conversations/{a_id}", json={"archived": True},
                     headers=ph_b)
    assert r.status_code in (404, 400)
    assert client.delete(f"/api/chat/conversations/{a_id}",
                         headers=ph_b).status_code == 404
    # the admin conversation is untouched
    assert client.get(f"/api/chat/conversations/{a_id}/messages",
                      headers=admin).status_code == 200
    assert client.get("/api/chat/conversations",
                      headers=admin).json()["items"][0]["title"] != "hijacked"


def test_conversation_reopen_and_persistence(client, two_users):
    admin, _ = two_users
    client.post("/api/chat", json={"message": "waste report", "use_llm": False},
                headers=admin)
    convs = client.get("/api/chat/conversations", headers=admin).json()
    cid = convs["items"][0]["id"]
    # reopen: messages endpoint returns the full exchange in order
    msgs = client.get(f"/api/chat/conversations/{cid}/messages", headers=admin).json()
    roles = [m["role"] for m in msgs["items"]]
    assert roles == ["user", "assistant"]
    assert msgs["items"][0]["content"] == "waste report"

    # rename works, then re-login (new token) still sees it — server-side state
    r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    fresh_h = client.headers.copy()
    fresh_h["X-Session-Token"] = r.json()["token"]
    assert client.patch(f"/api/chat/conversations/{cid}",
                        json={"title": "Waste review"}, headers=fresh_h).status_code == 200
    convs = client.get("/api/chat/conversations", headers=fresh_h).json()
    assert convs["items"][0]["title"] == "Waste review"
    # history persisted across re-login
    hist = client.get("/api/chat/history?limit=10", headers=fresh_h).json()
    assert any(m["message"] == "waste report" for m in hist)


    cid = {c["id"] for c in client.get("/api/chat/conversations",
                                        headers=admin).json()["items"]}
    client.post("/api/chat", json={"message": "hello there", "use_llm": False},
                headers=admin)
    new_id = next(c["id"] for c in client.get("/api/chat/conversations",
                                              headers=admin).json()["items"] if c["id"] not in cid)
    assert client.delete(f"/api/chat/conversations/{new_id}",
                         headers=admin).status_code == 200
    ids_now = {c["id"] for c in client.get("/api/chat/conversations",
                                           headers=admin).json()["items"]}
    assert new_id not in ids_now
    # clear ALL of the admin's chat history, then verify it is empty
    r = client.delete("/api/chat/history", headers=admin)
    assert r.status_code == 200
    assert client.get("/api/chat/conversations", headers=admin).json()["total"] == 0
    assert client.get("/api/chat/history", headers=admin).json() == []


def test_message_validation(client, two_users):
    _, ph_b = two_users
    too_long = "x" * 4001
    r = client.post("/api/chat", json={"message": too_long, "use_llm": False}, headers=ph_b)
    assert r.status_code == 400
    r = client.post("/api/chat", json={"message": "   "}, headers=ph_b)
    assert r.status_code == 400


def test_archived_conversations_hidden_but_recoverable(client, two_users):
    admin, _ = two_users
    before = {c["id"] for c in client.get("/api/chat/conversations",
                                          headers=admin).json()["items"]}
    client.post("/api/chat", json={"message": "to be archived", "use_llm": False},
                headers=admin)
    cid = next(c["id"] for c in client.get("/api/chat/conversations",
                                           headers=admin).json()["items"] if c["id"] not in before)
    # the archived flag rides in the body — a dict body with only 'archived'
    assert client.request("PATCH", f"/api/chat/conversations/{cid}?archived=true",
                          json={"title": None}, headers=admin).status_code == 200
    assert client.get("/api/chat/conversations", headers=admin).json()["total"] == 0
    # messages still reachable by id (not deleted)
    assert client.get(f"/api/chat/conversations/{cid}/messages",
                      headers=admin).status_code == 200



