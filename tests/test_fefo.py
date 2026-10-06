"""FEFO dispensing: earliest unexpired batch first; expired stock is blocked."""


def test_dispense_picks_earliest_unexpired_batch(client, db):
    row = db.one(
        """SELECT b.drug_id FROM batches b WHERE b.qty_remaining>0
           AND b.expiry_date >= date('now') GROUP BY b.drug_id
           HAVING COUNT(*) >= 1 ORDER BY COUNT(*) DESC LIMIT 1""")
    assert row, "seeded dataset should have dispensable stock"
    drug_id = row["drug_id"]
    first = db.one(
        """SELECT batch_no FROM batches WHERE drug_id=? AND qty_remaining>0
           AND (expiry_date IS NULL OR expiry_date>=date('now'))
           ORDER BY expiry_date IS NULL, expiry_date LIMIT 1""", (drug_id,))
    r = client.post("/api/dispense", json={"drug_id": drug_id, "qty": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["requested"] == 2 and body["dispensed"] == 2
    assert body["batches"][0]["batch_no"] == first["batch_no"]


def test_dispense_never_takes_expired_stock(client, db):
    did = db.execute(
        "INSERT INTO drugs(name, norm_name, mrp, cost) VALUES(?,?,?,?)",
        ("Testamine 10", "testamine 10", 10.0, 5.0))
    db.execute(
        """INSERT INTO batches(drug_id, batch_no, expiry_date, qty_received,
           qty_remaining, unit_cost, received_date, source)
           VALUES(?,?,?,?,?,?,?,?)""",
        (did, "TEST-EXP-1", "2020-01-01", 100, 100, 1.0, "2024-01-01", "test"))
    r = client.post("/api/dispense", json={"drug_id": did, "qty": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["dispensed"] == 0            # expired batch is never consumed
    assert body["blocked_expired"] == 100    # and reported, not silently lost


def test_dispense_rejects_nonpositive_qty(client):
    assert client.post("/api/dispense", json={"drug_id": 1, "qty": 0}).status_code == 400
