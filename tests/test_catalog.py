"""Medicine auto-categorization rules."""
from pharmacy import catalog


def test_classify_known_brand_with_case_and_hyphen_noise():
    r = catalog.classify("dolo--650")
    assert r["known"] is True
    assert r["generic"] == "Paracetamol"


def test_classify_unknown_drug_is_flagged():
    r = catalog.classify("Randomix Ultra 999")
    assert r["known"] is False


def test_substitutes_returns_list_and_refuses_unknown():
    assert isinstance(catalog.substitutes("dolo 650"), list)
    assert catalog.substitutes("not-a-real-drug") == []
