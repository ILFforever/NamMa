"""Shared fixture for web API tests: a fresh Postgres database with every migration applied.

Uses TEST_DATABASE_URL's server (default localhost:5433), a per-process database, recreated per test.
Skipped when no Postgres is reachable. Network calls (MaaS, GISTDA, LINE) are disabled.
"""
import os

import psycopg
import pytest

from common.config import ROOT

_BASE = os.environ.get("TEST_DATABASE_URL", "postgresql://namma:namma@localhost:5433/namma_test").rsplit("/", 1)[0]
# One database per test process, so parallel test runs never drop each other's database.
_DB = f"namma_api_test_{os.getpid()}"
API_TEST_URL = f"{_BASE}/{_DB}"


@pytest.fixture()
def api(monkeypatch):
    """(client, conn): a TestClient for api.main.app and an autocommit connection to seed/inspect the same DB.
    Log in with `token = login(client, "responder")` and send headers={"Authorization": f"Bearer {token}"}."""
    try:
        admin = psycopg.connect(_BASE + "/postgres", autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("no Postgres for API tests")
    admin.execute(f"DROP DATABASE IF EXISTS {_DB} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {_DB}")
    admin.close()
    for k in ("MAAS_API_KEY", "GISTDA_API_KEY", "LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("common.db.DATABASE_URL", API_TEST_URL)
    conn = psycopg.connect(API_TEST_URL, autocommit=True)
    for path in sorted((ROOT / "infra" / "migrations").glob("*.sql")):
        conn.execute(path.read_text())

    from fastapi.testclient import TestClient
    from api.main import app

    with TestClient(app) as client:
        yield client, conn
    conn.close()


def login(client, role: str = "responder") -> str:
    """Demo login (DEMO_MODE=1, the default). Returns the bearer token."""
    r = client.post("/api/auth/line", json={"id_token": f"demo:{role}"})
    assert r.status_code == 200, r.text
    return r.json()["data"]["session_token"]
