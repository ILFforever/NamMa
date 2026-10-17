"""End-to-end flow against a real Postgres: risk → chat → consent → alerts → rescue → delete.

Uses TEST_DATABASE_URL (default: the local dev server, database namma_test, recreated each run).
Skipped when no Postgres is reachable. Network calls (ThaiWater, GISTDA, MaaS, LINE) are disabled.
"""
import os
from datetime import datetime, timedelta

import psycopg
import pytest

from agent import alerts, answer, chat, risk, tools
from common.config import ROOT

TEST_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://namma:namma@localhost:5433/namma_test")
ADMIN_URL = TEST_URL.rsplit("/", 1)[0] + "/postgres"
DB_NAME = TEST_URL.rsplit("/", 1)[1]


@pytest.fixture()
def conn(monkeypatch):
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("no Postgres for integration tests")
    admin.execute(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {DB_NAME}")
    admin.close()
    for k in ("MAAS_API_KEY", "GISTDA_API_KEY", "LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(tools, "_fill_history", lambda conn, ids: False)
    c = psycopg.connect(TEST_URL)
    for path in sorted((ROOT / "infra" / "migrations").glob("*.sql")):
        c.execute(path.read_text())
    _seed(c)
    c.commit()
    yield c
    c.close()


NOW = datetime.now().astimezone().replace(second=0, microsecond=0)
DOWN, UP = 9001, 9002            # fake ids, near Bang Ban (Ayutthaya)


def _seed(c):
    for sid, code, lat in ((DOWN, "T.1", 14.41), (UP, "T.2", 14.60)):
        c.execute("INSERT INTO stations (id, oldcode, name_th, lat, lon, river, agency) VALUES (%s, %s, %s, %s, 100.46, "
                  "'แม่น้ำทดสอบ', 'RID')", (sid, code, f"สถานี{code}", lat))
    c.execute("INSERT INTO station_edges VALUES (%s, %s, 10)", (UP, DOWN))
    c.execute("INSERT INTO shelters (name, lat, lon, accepts_patients) VALUES ('ศูนย์ทดสอบ', 14.40, 100.47, true)")
    for h in range(4):
        ts = NOW - timedelta(hours=3 - h)
        c.execute("INSERT INTO waterlevel_readings (station_id, ts, level_msl, diff_wl_bank) VALUES (%s, %s, %s, %s)",
                  (DOWN, ts, 4.0 + 0.01 * h, -1.5 + 0.01 * h))         # rising 1 cm/h, 1.47 m below bank
        c.execute("INSERT INTO waterlevel_readings (station_id, ts, level_msl, diff_wl_bank) VALUES (%s, %s, %s, %s)",
                  (UP, ts, 9.0, -0.3))                                # upstream steady, below bank


def _set_over_bank(c, sid, diff):
    c.execute("INSERT INTO waterlevel_readings (station_id, ts, level_msl, diff_wl_bank) VALUES (%s, %s, 9.5, %s) "
              "ON CONFLICT (station_id, ts) DO UPDATE SET diff_wl_bank = EXCLUDED.diff_wl_bank",
              (sid, NOW + timedelta(minutes=1), diff))
    risk.compute_all(c, NOW + timedelta(minutes=2))
    c.commit()


def test_full_flow(conn):
    res = {r.station_id: r.risk_tier for r in risk.compute_all(conn, NOW)}
    assert res == {DOWN: "normal", UP: "normal"}

    r = chat.handle(conn, "U-test", "line", text="บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม")
    assert "T.1" in r.text and "1784" in r.text and r.text.endswith(answer.FOOTER)
    assert "บ้านแม่" in r.text and "ผู้สูงอายุ" in r.text              # saved note + household question
    prof = tools.get_user_profile(conn, tools.get_or_create_user(conn, "U-test"))
    assert [l["label"] for l in prof["locations"]] == ["บ้านแม่"]

    r = chat.handle(conn, "U-test", "line", text="ยายติดเตียง")
    assert [q.text for q in r.quick] == [chat.CONSENT_YES, chat.CONSENT_NO]
    uid = tools.get_or_create_user(conn, "U-test")
    assert tools.get_user_profile(conn, uid)["memory"]["household"] == {}   # nothing stored before consent
    chat.handle(conn, "U-test", "line", text=chat.CONSENT_YES)
    assert tools.get_user_profile(conn, uid)["memory"]["household"] == {"immobile": ["ยาย"]}

    # Normal tier → no alert. Upstream over bank → warning → one personalised push, then dedupe.
    assert alerts.run(conn, NOW) == []
    _set_over_bank(conn, UP, 0.2)
    sent = alerts.run(conn, NOW + timedelta(minutes=3))
    assert [s["tier"] for s in sent] == ["warning"]
    msg = conn.execute("SELECT content FROM messages WHERE channel = 'alert' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "เดินไม่ได้" in msg and "ศูนย์ทดสอบ" in msg and "1784" in msg
    assert alerts.run(conn, NOW + timedelta(minutes=4)) == []

    _set_over_bank(conn, DOWN, 0.1)                                         # escalation to danger pushes again
    assert [s["tier"] for s in alerts.run(conn, NOW + timedelta(minutes=5))] == ["danger"]

    r = chat.handle(conn, "U-test", "line", text="น้ำเข้าบ้านแล้ว ยายเดินไม่ได้ อยู่กัน 3 คน")
    assert r.text.startswith("🚨") and "1784" in r.text.splitlines()[0]
    t = conn.execute("SELECT people_count, urgency, status, lat FROM rescue_tickets WHERE id = %s", (r.ticket_id,)).fetchone()
    assert t[:3] == (3, "critical", "unconfirmed") and t[3] is not None   # location pre-filled from บ้านแม่
    chat.handle(conn, "U-test", "line", text=f"ยืนยันขอความช่วยเหลือ #{r.ticket_id}")
    assert conn.execute("SELECT status FROM rescue_tickets WHERE id = %s", (r.ticket_id,)).fetchone()[0] == "open"

    chat.handle(conn, "U-test", "line", text="ลบข้อมูลทั้งหมด")
    prof = tools.get_user_profile(conn, uid)
    assert prof["locations"] == [] and prof["memory"]["household"] == {}


def test_shared_location_attaches_to_ticket(conn):
    risk.compute_all(conn, NOW)
    r = chat.handle(conn, "U-loc", "web", text="ช่วยด้วย ติดอยู่บนหลังคา")
    assert conn.execute("SELECT lat FROM rescue_tickets WHERE id = %s", (r.ticket_id,)).fetchone()[0] is None
    r2 = chat.handle(conn, "U-loc", "web", location=(14.41, 100.46))
    assert f"#{r.ticket_id}" in r2.text
    assert conn.execute("SELECT status, lat FROM rescue_tickets WHERE id = %s", (r.ticket_id,)).fetchone() == ("open", 14.41)


def test_responder_endpoints_need_token(api, monkeypatch):
    """/api/tickets uses session auth (401 without a session, 403 for a user); /api/demo/simulate still uses RESPONDER_TOKEN."""
    from tests.conftest import login

    monkeypatch.setenv("RESPONDER_TOKEN", "t0k")
    c, _ = api
    assert c.get("/api/tickets").status_code == 401
    assert c.get("/api/tickets", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/tickets", headers={"Authorization": f"Bearer {login(c, 'user')}"}).status_code == 403
    assert c.get("/api/tickets", headers={"Authorization": f"Bearer {login(c, 'responder')}"}).status_code == 200
    assert c.post("/api/demo/simulate", json={"station_id": 1}).status_code == 401
    assert c.post("/api/demo/simulate", json={"station_id": 1}, headers={"Authorization": "Bearer wrong"}).status_code == 401
