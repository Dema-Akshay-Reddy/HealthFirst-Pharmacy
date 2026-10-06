"""Upload pipeline: validation, unknown-drug policy, batch creation."""
import csv
import io


def _csv_bytes(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode()


def test_sales_upload_accepts_noise_and_quarantines_unknown(client, db):
    b = db.one(
        """SELECT b.batch_no FROM batches b JOIN drugs d ON d.id=b.drug_id
           WHERE d.name LIKE 'Dolo%' AND b.qty_remaining > 0 LIMIT 1""")
    assert b, "seeded dataset should hold Dolo stock"
    rows = [
        dict(Transaction_ID="TST-1", Date="2026-09-15", Drug_Name="dolo 650",
             Batch_Number=b["batch_no"], Qty_Sold=1, MRP_Unit_Price=30, Total_Amount=30),
        dict(Transaction_ID="TST-2", Date="2026-09-15", Drug_Name="DOLO--650",
             Batch_Number=b["batch_no"], Qty_Sold=2, MRP_Unit_Price=30, Total_Amount=60),
        dict(Transaction_ID="TST-3", Date="2026-09-15", Drug_Name="Unknowndrug 999",
             Batch_Number="X-1", Qty_Sold=1, MRP_Unit_Price=10, Total_Amount=10),
    ]
    r = client.post("/api/upload", files={"file": ("t.csv", _csv_bytes(rows), "text/csv")},
                    data={"kind": "sales"})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 2                      # case/hyphen noise normalised
    assert body["rejected"] == 1                      # unknown drug quarantined, not added
    assert body["issues"].get("unknown_drug") == 1
    q = db.one("SELECT COUNT(*) AS n FROM quarantined WHERE issues LIKE '%unknown_drug%'")
    assert q["n"] >= 1


def test_purchase_upload_creates_fefo_batch(client, db):
    sup = db.one("SELECT name FROM suppliers LIMIT 1")
    rows = [dict(Purchase_ID="PT-1", Date_Received="2026-09-20", Drug_Name="Pan 40",
                 Supplier_Name=sup["name"], Batch_Number="PAN-TEST-01", Qty_Received=50,
                 Unit_Cost_Price=100, Total_Purchase_Cost=5000, Expiry_Date="2028-06-30")]
    r = client.post("/api/upload", files={"file": ("p.csv", _csv_bytes(rows), "text/csv")},
                    data={"kind": "purchases"})
    assert r.status_code == 200
    assert r.json()["accepted"] == 1
    batch = db.one("SELECT qty_remaining FROM batches WHERE batch_no='PAN-TEST-01'")
    assert batch and batch["qty_remaining"] == 50


def test_upload_template_is_xlsx(client):
    r = client.get("/api/upload/template")
    assert r.status_code == 200
    assert r.content[:2] == b"PK"  # zip magic = valid xlsx


def test_empty_upload_rejected(client):
    r = client.post("/api/upload", files={"file": ("e.csv", b"", "text/csv")},
                    data={"kind": "sales"})
    assert r.status_code == 400
