"""Alert engine: rule families, summary, idempotent dedup, newest-first order."""
from pharmacy import alerts, laya


def test_refresh_creates_expected_rule_families(db):
    items = alerts.refresh()
    types = {a["atype"] for a in items}
    assert "expired_stock" in types      # dataset has long-expired stock
    assert "expiry_soon" in types        # 30/90-day expiry windows
    assert types & {"low_stock", "overstock", "demand_spike", "price_rising"}


def test_summary_counts_active_alerts(db):
    s = alerts.summary()
    assert {"critical", "high", "medium", "active"} <= set(s)
    assert s["active"] >= 1


def test_refresh_is_idempotent_via_dedup(db):
    n1 = len(alerts.refresh())
    n2 = len(alerts.refresh())
    assert n1 == n2


def test_newest_alerts_surface_first_regardless_of_severity(db):
    """A freshly raised alert sits above older ones even when the old one is
    critical - 'what just happened' beats 'what has been there all along'."""
    conn = db.conn()
    rows = [
        # (dedup_key, severity, created_at)
        ("order_test:old_critical", "critical", "2000-01-01T00:00:00"),
        ("order_test:new_medium", "medium", db.now_iso()),
    ]
    try:
        for key, sev, created in rows:
            conn.execute(
                "INSERT INTO alerts(atype, severity, drug_id, batch_id, title, message, "
                "details, dedup_key, status, created_at, updated_at) "
                "VALUES('overstock',?,NULL,NULL,?,?,? ,?,'active',?,?)",
                (sev, f"T {key}", f"M {key}", "{}", key, created, created),
            )
        conn.commit()

        items = alerts.list_alerts()
        keys = [a["dedup_key"] for a in items]
        new_i, old_i = keys.index("order_test:new_medium"), keys.index("order_test:old_critical")
        assert new_i < old_i, "new alert must rank above the older critical one"
        stamps = [a["created_at"] for a in items]
        assert stamps == sorted(stamps, reverse=True), \
            "list must be strictly newest-first by created_at"
    finally:
        db.execute("DELETE FROM alerts WHERE dedup_key IN ('order_test:old_critical', 'order_test:new_medium')")
        db.conn().commit()


def test_reopened_alert_surfaces_as_new_again(client, db, monkeypatch):
    """When a resolved condition returns, its row re-opens AND counts as new:
    created_at is refreshed so it climbs back to the top of the list."""
    conn = db.conn()
    synth = [dict(key="laya_spike:777777", atype="laya_spike", severity="high",
                  title="Laya spike: Synthetic", message="spike back",
                  drug_id=None, batch_id=None, details=dict(source="laya"))]
    conn.execute(
        "INSERT INTO alerts(atype, severity, drug_id, batch_id, title, message, "
        "details, dedup_key, status, created_at, updated_at) "
        "VALUES('laya_spike','high',NULL,NULL,'Laya spike: Synthetic','stale',"
        "'{}','laya_spike:777777','resolved','2000-01-01T00:00:00','2000-01-01T00:00:00')")
    conn.commit()
    monkeypatch.setattr(laya, "spike_alerts", lambda: list(synth))
    alerts._laya_scan = None
    try:
        alerts.refresh()
        row = db.one("SELECT status, created_at FROM alerts WHERE dedup_key='laya_spike:777777'")
        assert row["status"] == "active"
        assert row["created_at"] > "2000-01-01", "re-open must refresh created_at"
        items = alerts.list_alerts()
        keys = [a["dedup_key"] for a in items]
        idx = keys.index("laya_spike:777777")
        # nothing older may outrank a re-raised alert (ties on the same
        # second are allowed to share the top slots)
        above = [a for a in items[:idx] if a["created_at"] < row["created_at"]]
        assert not above, f"older alerts ranked above the re-opened one: {above}"
    finally:
        db.execute("DELETE FROM alerts WHERE dedup_key='laya_spike:777777'")
        db.conn().commit()
        alerts._laya_scan = None
