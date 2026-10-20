"""common.db.connect against a real Postgres (TEST_DATABASE_URL's server). Skipped when none is reachable."""
import os
from datetime import timedelta

import psycopg
import pytest

from common import db

SERVER = os.environ.get("TEST_DATABASE_URL", "postgresql://namma:namma@localhost:5433/namma_test").rsplit("/", 1)[0]


def test_every_connection_is_on_bangkok_time(monkeypatch):
    url = SERVER + "/postgres"
    try:
        psycopg.connect(url, connect_timeout=3).close()
    except psycopg.OperationalError:
        pytest.skip("no Postgres for DB tests")
    monkeypatch.setattr(db, "DATABASE_URL", url)
    with db.connect() as conn:
        assert conn.execute("SHOW TimeZone").fetchone()[0] == "Asia/Bangkok"
        assert conn.execute("SELECT now()").fetchone()[0].utcoffset() == timedelta(hours=7)
