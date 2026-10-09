"""Laya spike alerts in the alert engine.

When Laya classifies a SKU's demand as spiking, the alert engine must carry
the two operational answers Laya actually produced: HOW MUCH as a predicted
quantity band (a range - never an exact order) and BY WHEN as an act-by
deadline derived from Laya's reorder-timing window. Entries are upserted by
dedup key (one alert per SKU, refresh after refresh), and a Laya outage
during refresh keeps existing alerts active - an outage is not "the spike
is gone".

The alert scan runs inside refresh() on hot paths (every dispense), so its
result is TTL-cached; every test here resets that cache to stay independent.
The frontend cell that renders these values in "AI reorder suggestions"
runs under Node via tests/forecast_laya.test.js.
"""
from datetime import date

import pytest

from pharmacy import alerts, laya


@pytest.fixture(autouse=True)
def _fresh_laya_scan():
    """The spike scan is TTL-cached globally; tests must not inherit it."""
    alerts._laya_scan = None
    yield
    alerts._laya_scan = None


# --------------------------------------------------------------------------- #
# deadline derivation (pure helper - no data dependency)
# --------------------------------------------------------------------------- #
def test_reorder_by_deadline_uses_the_timing_windows_upper_bound():
    today = date(2026, 10, 9)
    assert laya.reorder_by_date("within_3_days", today) == "2026-10-12"
    assert laya.reorder_by_date("4_7_days", today) == "2026-10-16"
    assert laya.reorder_by_date("8_14_days", today) == "2026-10-23"
    assert laya.reorder_by_date("15_plus_days", today) == "2026-10-24"
    # unmappable/absent timing -> no deadline, never an invented date
    assert laya.reorder_by_date(None, today) is None
    assert laya.reorder_by_date("no_reorder", today) is None


# --------------------------------------------------------------------------- #
# alert payload: how much (band) + by when (deadline), state-driven
# --------------------------------------------------------------------------- #
def test_spike_alerts_answer_how_much_and_by_when(client):
    payload = laya.forecast_page_payload()
    expected = {
        row["drug_id"]: row
        for row in payload["rows"]
        if row.get("card")
        and ((row["card"].get("outlook") or {}).get("trajectory") or {}).get("value")
        == "spiking"
    }
    produced = laya.spike_alerts()
    assert {a["drug_id"] for a in produced} == set(expected)

    for a in produced:
        row = expected[a["drug_id"]]
        card = row["card"]
        outlook = card["outlook"]
        band = (outlook.get("band") or {}).get("label")

        assert a["key"] == f"laya_spike:{row['drug_id']}"
        assert a["atype"] == "laya_spike"
        assert a["severity"] in ("high", "medium")
        assert a["title"].startswith("Laya spike: ")

        # HOW MUCH: the band, stated as a range - never an exact order
        if band:
            assert band in a["message"]
            assert "a model band, not an exact order" in a["message"]
        assert a["details"]["band"] == band

        # BY WHEN: the act-by deadline, or an explicit "unavailable"
        deadline = row["reorder_by"]
        if deadline:
            assert f"Act by {deadline}" in a["message"]
        else:
            assert "review timing manually" in a["message"]
        assert a["details"]["deadline"] == deadline

        d = a["details"]
        assert d["source"] == "laya"
        assert d["trajectory"] == "spiking"
        assert d["analysis_date"] == card.get("analysis_date")
        # no fabricated probability: valid one or absent
        p = d.get("trajectory_prob")
        assert p is None or (isinstance(p, (int, float)) and not isinstance(p, bool)
                             and 0 <= p <= 1)


def test_skus_without_a_valid_prediction_never_become_spike_alerts(client):
    """An alert is either fully backed by a prediction or absent."""
    payload = laya.forecast_page_payload()
    unbacked = {row["drug_id"] for row in payload["rows"]
                if row["error"] or not row.get("card")}
    produced_ids = {a["drug_id"] for a in laya.spike_alerts()}
    assert not (produced_ids & unbacked)


# --------------------------------------------------------------------------- #
# refresh(): merge, upsert-by-dedup-key, idempotency
# --------------------------------------------------------------------------- #
def test_refresh_merges_laya_entries_once_and_keeps_them_stable(client, db, monkeypatch):
    synthetic = [dict(
        key="laya_spike:4242", atype="laya_spike", severity="high",
        title="Laya spike: Synthetic",
        message=("Laya classifies Synthetic demand as spiking. Predicted need: "
                 "1001+ units - a model band, not an exact order. "
                 "Act by 2099-01-01 (8\u201314 days reorder window)."),
        drug_id=None, batch_id=None,
        details=dict(source="laya", trajectory="spiking", band="1001+ units",
                     timing="8\u201314 days", deadline="2099-01-01",
                     analysis_date="2025-11-30", status="monitor"))]
    monkeypatch.setattr(laya, "spike_alerts", lambda: list(synthetic))
    alerts._laya_scan = None

    try:
        first = alerts.refresh()
        hits = [a for a in first if a["dedup_key"] == "laya_spike:4242"]
        assert len(hits) == 1, "one alert per spiking SKU"
        assert hits[0]["atype"] == "laya_spike"
        assert hits[0]["severity"] == "high"
        assert "1001+ units" in hits[0]["message"]
        assert hits[0]["details"]["deadline"] == "2099-01-01"
        first_id = hits[0]["id"]

        # refresh again (fresh scan, cache bypassed) -> same row updated,
        # never duplicated
        alerts._laya_scan = None
        second = alerts.refresh()
        again = [a for a in second if a["dedup_key"] == "laya_spike:4242"]
        assert len(again) == 1 and again[0]["id"] == first_id

        keys = [r["dedup_key"] for r in db.query(
            "SELECT dedup_key FROM alerts WHERE atype='laya_spike' "
            "AND status != 'resolved'")]
        assert len(keys) == len(set(keys)), "dedup_key is unique per active alert"
        assert all(k.startswith("laya_spike:") for k in keys)
    finally:
        db.execute("DELETE FROM alerts WHERE dedup_key='laya_spike:4242'")
        db.conn().commit()
        alerts._laya_scan = None


def test_engine_outage_never_resolves_active_laya_alerts(client, db, monkeypatch):
    """Scan failure -> keep existing alerts; an outage is not "spike gone"."""
    conn = db.conn()
    conn.execute(
        "INSERT INTO alerts(atype, severity, drug_id, batch_id, title, message, "
        "details, dedup_key, status, created_at, updated_at) "
        "VALUES('laya_spike','high',NULL,NULL,'Laya spike: Synthetic','keep me',"
        "'{}','laya_spike:999999','active',?,?)",
        (db.now_iso(), db.now_iso()))
    conn.commit()

    def boom():
        raise laya.Unavailable("model offline")

    try:
        monkeypatch.setattr(laya, "spike_alerts", boom)
        alerts._laya_scan = None
        alerts.refresh()
        row = db.one("SELECT status FROM alerts WHERE dedup_key='laya_spike:999999'")
        assert row is not None and row["status"] == "active"
    finally:
        db.execute("DELETE FROM alerts WHERE dedup_key='laya_spike:999999'")
        db.conn().commit()
        alerts._laya_scan = None


def test_resolved_spike_reopens_instead_of_duplicating(client, db, monkeypatch):
    """dedup_key is UNIQUE across statuses: a returning condition RE-OPENS
    its row. An insert here would crash refresh() on the dispense hot path
    and leave the transaction open (database-locked cascade)."""
    synth = [dict(key="laya_spike:5555", atype="laya_spike", severity="medium",
                  title="Laya spike: Synthetic", message="spike back",
                  drug_id=None, batch_id=None, details=dict(source="laya"))]
    monkeypatch.setattr(laya, "spike_alerts", lambda: list(synth))
    alerts._laya_scan = None
    try:
        alerts.refresh()

        # spike gone -> the alert resolves
        monkeypatch.setattr(laya, "spike_alerts", lambda: [])
        alerts._laya_scan = None
        alerts.refresh()
        rows = db.query("SELECT status FROM alerts WHERE dedup_key='laya_spike:5555'")
        assert [r["status"] for r in rows] == ["resolved"]

        # spike returns -> the SAME row re-opens, exactly one row exists
        monkeypatch.setattr(laya, "spike_alerts", lambda: list(synth))
        alerts._laya_scan = None
        alerts.refresh()
        rows = db.query("SELECT status FROM alerts WHERE dedup_key='laya_spike:5555'")
        assert len(rows) == 1 and rows[0]["status"] == "active"
    finally:
        db.execute("DELETE FROM alerts WHERE dedup_key='laya_spike:5555'")
        db.conn().commit()
        alerts._laya_scan = None
