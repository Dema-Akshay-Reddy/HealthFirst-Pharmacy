"""Shared fixtures: isolated temp data dir + a fully seeded API client."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Isolate ALL mutable state in a temp dir BEFORE any pharmacy import happens.
# The bundled dataset stays at its repo location (read-only inputs).
_TMP = Path(tempfile.mkdtemp(prefix="pharmacy-test-"))
os.environ["PHARMACY_DATA_DIR"] = str(_TMP)
os.environ.pop("PHARMACY_API_KEY", None)  # demo mode: auth off unless a test opts in
os.environ["PYTHONIOENCODING"] = "utf-8"


@pytest.fixture(scope="session")
def client():
    """Full app with lifespan: seeds the dataset, refreshes alerts + forecasts.
    Signs in as the seeded admin so existing tests exercise the full API."""
    from fastapi.testclient import TestClient

    import app as app_module
    with TestClient(app_module.app) as c:
        r = c.post("/api/login", json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200, f"admin login failed: {r.text}"
        c.headers.update({"X-Session-Token": r.json()["token"]})
        yield c


@pytest.fixture(scope="session")
def db(client):
    """DB handle; depends on client so the dataset is seeded before any test."""
    from pharmacy import db as db_module
    db_module.init_db()
    return db_module
