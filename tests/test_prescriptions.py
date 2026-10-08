"""POST /api/prescriptions — "Add Medicine (doctor prescribed)".

A prescription resolves the medicine exactly as written on the slip
(brand or generic molecule, case/hyphen-insensitive) and FEFO-issues it:
usable stock decreases and expired batches are never consumed.
"""
from datetime import date


def _usable(db, drug_id: int) -> int:
    return db.scalar(
        "SELECT COALESCE(SUM(qty_remaining),0) FROM batches "
        "WHERE drug_id=? AND (expiry_date IS NULL OR expiry_date>=date('now'))",
        (drug_id,)) or 0


def test_prescription_by_brand_name_deducts_stock(client, db):
    d = db.one(
        """SELECT d.* FROM drugs d JOIN batches b ON b.drug_id=d.id
           WHERE b.qty_remaining>0 AND (b.expiry_date IS NULL OR b.expiry_date>=date('now'))
           GROUP BY d.id ORDER BY d.id LIMIT 1""")
    before = _usable(db, d["id"])
    scribble = d["name"].upper().replace(" ", "-")  # as a doctor writes it: "DOLO-650"
    r = client.post("/api/prescriptions",
                    json={"name": scribble, "qty": 2, "doctor": "Dr. Rao", "patient": "OPD-42"})
    assert r.status_code == 200
    body = r.json()
    assert body["drug_id"] == d["id"]
    assert body["requested"] == 2 and body["dispensed"] == 2
    assert body["usable_before"] == before
    assert body["usable_after"] == before - 2
    assert _usable(db, d["id"]) == before - 2  # stock really went down
    assert body["batches"], "FEFO batches consumed should be reported"


def test_prescription_by_generic_molecule(client, db):
    d = db.one("SELECT * FROM drugs WHERE COALESCE(generic,'')!='' ORDER BY id LIMIT 1")
    r = client.post("/api/prescriptions", json={"name": d["generic"].upper(), "qty": 1})
    assert r.status_code == 200
    assert r.json()["drug_id"] == d["id"]


def test_prescription_unknown_medicine_404(client):
    r = client.post("/api/prescriptions", json={"name": "NotAVaccine 999", "qty": 1})
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_prescription_rejects_nonpositive_qty(client):
    assert client.post("/api/prescriptions", json={"name": "Dolo 650", "qty": 0}).status_code == 400


def test_prescription_partial_fill_and_expired_blocked(client, db):
    db.execute(
        "INSERT INTO drugs(name, norm_name, generic, category, unit, created_at) "
        "VALUES(?,?,?,?,?,?)",
        ("RxTest 10", "rxtest 10", "Rxmol", "Other", "unit", date.today().isoformat()))
    did = db.scalar("SELECT id FROM drugs WHERE norm_name='rxtest 10'")
    db.execute(
        "INSERT INTO batches(drug_id, batch_no, expiry_date, qty_received, "
        "qty_remaining, unit_cost, received_date, source) VALUES(?,?,?,?,?,?,?,?)",
        (did, "RX-FRESH", "2099-01-01", 5, 5, 2.0, date.today().isoformat(), "test"))
    db.execute(
        "INSERT INTO batches(drug_id, batch_no, expiry_date, qty_received, "
        "qty_remaining, unit_cost, received_date, source) VALUES(?,?,?,?,?,?,?,?)",
        (did, "RX-EXPIRED", "2020-01-01", 100, 100, 1.0, date.today().isoformat(), "test"))

    r = client.post("/api/prescriptions", json={"name": "RxTest-10", "qty": 8})
    assert r.status_code == 200
    body = r.json()
    assert body["drug_id"] == did
    assert body["dispensed"] == 5            # only the fresh, unexpired batch is usable
    assert body["shortfall"] == 3
    assert body["blocked_expired"] == 100    # expired stock is reported, never issued
    assert body["batches"][0]["batch_no"] == "RX-FRESH"
    assert body["usable_before"] == 5 and body["usable_after"] == 0
