"""Alert engine: rule families, summary, idempotent dedup."""
from pharmacy import alerts


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
