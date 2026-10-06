"""Roles & sessions: admin (full access) vs pharmacist (counter + shelf workflow),
plus the physical-shelf directives (putaway of new batches, FEFO shifts)."""
from datetime import date

from fastapi.testclient import TestClient

import app as app_module

PHARM = {"username": "pharmacist", "password": "pharm123"}


def _pharm_headers(client) -> dict:
    r = client.post("/api/login", json=PHARM)
    assert r.status_code == 200
    return {"X-Session-Token": r.json()["token"]}


def test_login_rejects_bad_credentials(client):
    assert client.post("/api/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"username": "ghost", "password": "x"}).status_code == 401


def test_unauthenticated_requests_are_rejected():
    with TestClient(app_module.app) as c:  # no session header on this client
        assert c.get("/api/drugs").status_code == 401
        assert c.get("/healthz").status_code == 200          # probes stay open
        assert c.get("/").status_code == 200                 # SPA (login screen) stays open
        assert c.post("/api/login", json=PHARM).status_code == 200


def test_pharmacist_scope(client):
    h = _pharm_headers(client)
    assert client.get("/api/me", headers=h).json()["role"] == "pharmacist"
    # allowed: operational read + counter/shelf workflow
    for path in ("/api/drugs", "/api/batches", "/api/expiry", "/api/shelf/tasks",
                 "/api/shelf/tasks?status=done"):
        assert client.get(path, headers=h).status_code == 200, path
    for params in ({"name": "Dolo 650"}, {"name": "Dolo 650", "qty": 1}):
        assert client.get("/api/counter/lookup", params=params, headers=h).status_code == 200
    # blocked: analytics, demand, policy, data tools (admin-only)
    for path in ("/api/reorders", "/api/forecast", "/api/suppliers", "/api/waste",
                 "/api/charts", "/api/users", "/api/settings",
                 "/api/reports/inventory.csv"):
        assert client.get(path, headers=h).status_code == 403, path
    assert client.post("/api/settings", headers=h, json={"values": {}}).status_code == 403
    assert client.patch("/api/drugs/1", headers=h, json={}).status_code == 403


def test_admin_has_full_access(client):
    assert client.get("/api/me").json()["role"] == "admin"
    for path in ("/api/overview", "/api/reorders", "/api/users",
                 "/api/forecast/evaluation", "/api/shelf/tasks"):
        assert client.get(path).status_code == 200, path
    assert client.post("/api/settings", json={"values": {}}).status_code == 200
    assert client.get("/api/counter/lookup", params={"name": "Dolo 650"}).status_code == 200


def test_api_key_acts_as_admin_service_credential(client, monkeypatch):
    monkeypatch.setenv("PHARMACY_API_KEY", "svc-key")
    try:
        h = {"X-API-Key": "svc-key"}
        assert client.get("/api/reorders", headers=h).status_code == 200  # no session needed
    finally:
        monkeypatch.delenv("PHARMACY_API_KEY")


def test_admin_can_create_pharmacist_user(client):
    r = client.post("/api/users", json={"username": "RxTwo", "password": "secret1",
                                        "role": "pharmacist", "name": "Counter 2"})
    assert r.status_code == 200
    lg = client.post("/api/login", json={"username": "rxtwo", "password": "secret1"})
    assert lg.status_code == 200 and lg.json()["role"] == "pharmacist"
    assert client.post("/api/users", headers=_pharm_headers(client),
                       json={"username": "nope", "password": "secret1"}).status_code == 403


def test_counter_lookup_points_at_nearest_expiry_batch(client):
    """Lookup resolves a medicine and names the nearest-expiry batch plus its shelf."""
    r = client.get("/api/counter/lookup", params={"name": "DOLO-650"})
    assert r.status_code == 200
    body = r.json()
    assert body["drug"] == "Dolo 650"
    pick = body["pick"]
    assert pick is not None
    expected_days = (date(2026, 10, 13) - date.today()).days
    assert pick["days_to_expiry"] == expected_days, "nearest-expiry batch shown (2026-10-13)"
    assert pick["qty_remaining"] > 0, "batch must have usable stock"


def test_pharmacist_counter_issue_reports_shelf_and_deducts(client, db):
    """Pharmacy issues a patient's medicine FEFO: issued batches report their
    physical shelf and total usable stock drops by the exact amount.

    The batch is supplied exactly like a real purchase: ingest assigns a shelf
    and raises a 'put new batch on shelf X' directive, which the pharmacist
    confirms before any issue."""
    h = _pharm_headers(client)
    did = db.scalar("SELECT id FROM drugs WHERE norm_name='dolo 650'")
    csv = ("purchase_id,date_received,drug_name,batch_number,expiry_date,"
           "qty_received,unit_cost_price,supplier_name\n"
           "PUT-DEMO-1,2026-01-05,Dolo 650,DEMO-1,2029-01-01,100,1.0,Apollo Supply Chain\n")
    upload = client.post("/api/upload", data={"kind": "purchases"},
                         files={"file": ("demo.csv", csv.encode(), "text/csv")})
    assert upload.status_code == 200 and upload.json()["accepted"] == 1
    # the system directed the pharmacist: find the putaway directive
    pending = db.one(
        "SELECT t.id, t.batch_id, s.code AS to_code FROM shelf_tasks t "
        "JOIN shelves s ON s.id=t.to_shelf_id WHERE t.kind='putaway' AND t.status='pending' "
        "AND t.batch_id IN (SELECT id FROM batches WHERE drug_id=?)", (did,))
    assert pending, "expected a 'place new batch on shelf' directive"
    # confirm the placement -> batch.shelf_id becomes the pick shelf
    done = client.post(f"/api/shelf/tasks/{pending['id']}/done", headers=h)
    assert done.status_code == 200
    # now issue the patient's medicine: the dispensed batch reports its shelf
    r = client.post("/api/prescriptions", headers=h,
                    json={"drug_id": did, "qty": 2, "patient": "OPD-7"})
    assert r.status_code == 200
    body = r.json()
    assert body["dispensed"] == 2
    # the API reports usable stock before/after; drop equals the dispensed units
    assert body["usable_after"] == body["usable_before"] - 2
    # the issued batch reports the shelf it physically sits on (FEFO pick =
    # the seeded nearest-expiry batch, which the backfill placed on its pick shelf)
    assert body["batches"][0]["shelf"], "issued batch must report its shelf"
    row = db.one("SELECT s.code FROM batches b JOIN shelves s ON s.id=b.shelf_id "
                 "WHERE b.batch_no=?", (body["batches"][0]["batch_no"],))
    assert row["code"] == body["batches"][0]["shelf"]



def test_shelf_task_completion_updates_shelf_map(client, db):
    items = client.get("/api/shelf/tasks?status=pending").json()["items"]
    assert items, "system should raise putaway/shift directives"
    batch_task = next((t for t in items if t["kind"] == "shift" and t["batch_id"]), None)
    target = batch_task or items[0]
    r = client.post(f"/api/shelf/tasks/{target['id']}/done")
    assert r.status_code == 200
    if target["batch_id"]:
        row = db.one("SELECT shelf_id FROM batches WHERE id=?", (target["batch_id"],))
        assert row["shelf_id"] == target["to_shelf_id"]
    done = client.get("/api/shelf/tasks?status=done&limit=50").json()["items"]
    assert any(t["id"] == target["id"] for t in done)


def test_new_purchase_upload_raises_putaway_message(client, db):
    """Uploading a purchase makes the pharmacist's shelf inbox show where to
    place the new batch."""
    before = db.scalar("SELECT COUNT(*) FROM shelf_tasks WHERE kind='putaway'")
    csv = ("purchase_id,date_received,drug_name,batch_number,expiry_date,"
           "qty_received,unit_cost_price,supplier_name\n"
           "PUT-ROLETEST-1,2026-01-05,Dolo 650,ROLE-T1,2029-01-01,50,1.0,Apollo Supply Chain\n")
    r = client.post("/api/upload", data={"kind": "purchases"},
                    files={"file": ("purchases_role.csv", csv.encode(), "text/csv")})
    assert r.status_code == 200
    assert r.json()["accepted"] == 1
    after = db.scalar("SELECT COUNT(*) FROM shelf_tasks WHERE kind='putaway'")
    assert after == before + 1
    task = db.one("SELECT * FROM shelf_tasks WHERE kind='putaway' ORDER BY id DESC LIMIT 1")
    assert task["status"] == "pending"
    assert "shelf" in task["reason"] and task["to_shelf_id"]
