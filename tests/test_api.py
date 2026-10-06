"""API surface: health, core reads, security headers, auth gate, backup."""


def test_healthz(client):
    assert client.get("/healthz").json()["status"] == "ok"


def test_ready(client):
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["checks"]["database"] == "ok"


def test_meta_and_overview(client):
    assert client.get("/api/meta").status_code == 200
    assert client.get("/api/overview").status_code == 200


def test_drugs_seeded(client):
    drugs = client.get("/api/drugs").json()
    assert len(drugs) >= 6
    assert all("name" in d and "usable" in d for d in drugs)


def test_alerts_shape(client):
    r = client.get("/api/alerts").json()
    assert "items" in r and "summary" in r


def test_forecast_suggestions(client):
    rows = client.get("/api/forecast").json()
    assert isinstance(rows, list) and len(rows) >= 6


def test_reports_are_csv(client):
    for path in ("/api/reports/waste.csv", "/api/reports/inventory.csv",
                 "/api/reports/expiry.csv", "/api/reports/forecast.csv"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")


def test_security_headers_and_request_id(client):
    r = client.get("/api/meta")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Cache-Control"] == "no-store"
    assert "X-Request-ID" in r.headers


def test_settings_roundtrip(client):
    old = client.get("/api/meta").json()["settings"]["review_period_days"]
    client.post("/api/settings", json={"values": {"review_period_days": "9"}})
    assert client.get("/api/meta").json()["settings"]["review_period_days"] == "9"
    client.post("/api/settings", json={"values": {"review_period_days": old}})
    assert client.get("/api/meta").json()["settings"]["review_period_days"] == old


def test_online_backup(client):
    r = client.post("/api/admin/backup")
    assert r.status_code == 200
    assert r.json()["size_bytes"] > 0


def test_chat_endpoint(client):
    r = client.post("/api/chat", json={"message": "how much stock is left of Dolo 650?",
                                       "use_llm": False})
    assert r.status_code == 200
    assert r.json()["text"]


def test_api_key_gate(client, monkeypatch):
    monkeypatch.setenv("PHARMACY_API_KEY", "secret-key-123")
    try:
        assert client.get("/api/meta").status_code == 401
        assert client.get("/api/meta", headers={"X-API-Key": "secret-key-123"}).status_code == 200
        assert client.get("/api/meta", headers={"X-API-Key": "wrong"}).status_code == 401
        assert client.get("/api/meta?api_key=secret-key-123").status_code == 200
        assert client.get("/healthz").status_code == 200      # probes stay open
        assert client.get("/").status_code == 200             # SPA stays open
    finally:
        monkeypatch.delenv("PHARMACY_API_KEY")
    assert client.get("/api/meta").status_code == 200         # demo mode restored


def test_docs_disabled_in_production(monkeypatch):
    import importlib

    from pharmacy import config

    monkeypatch.setenv("PHARMACY_ENV", "production")
    importlib.reload(config)                 # ENV is read at import time
    import app as app_module
    importlib.reload(app_module)
    try:
        assert app_module.app.docs_url is None
        assert app_module.app.openapi_url is None
    finally:
        monkeypatch.delenv("PHARMACY_ENV")
        importlib.reload(config)             # restore development defaults
        importlib.reload(app_module)
