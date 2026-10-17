"""POST /api/chat (docs/api.md → Chat) against a real Postgres: contract shape, postbacks, triage, rate limit.

Skipped when no Postgres is reachable (see the api fixture in conftest). Network calls are disabled.
"""
import uuid
from datetime import datetime, timedelta

import pytest

from agent import chat, risk, tools
from tests.conftest import login

NOW = datetime.now().astimezone().replace(second=0, microsecond=0)
STATION = 9101


@pytest.fixture()
def seeded(api, monkeypatch):
    """One station near Bang Ban (about 5 km away), rising slowly, 1.5 m below bank; one shelter; risk computed."""
    client, conn = api
    monkeypatch.setattr(tools, "_fill_history", lambda conn, ids: False)
    conn.execute("INSERT INTO stations (id, oldcode, name_th, lat, lon, river, agency) "
                 "VALUES (%s, 'T.1', 'สถานีทดสอบ', 14.41, 100.46, 'แม่น้ำทดสอบ', 'RID')", (STATION,))
    for h in range(4):
        conn.execute("INSERT INTO waterlevel_readings (station_id, ts, level_msl, diff_wl_bank) "
                     "VALUES (%s, %s, %s, %s)", (STATION, NOW - timedelta(hours=3 - h), 4.0 + 0.01 * h, -1.5 + 0.01 * h))
    conn.execute("INSERT INTO shelters (name, lat, lon, accepts_patients) VALUES ('ศูนย์ทดสอบ', 14.40, 100.47, true)")
    risk.compute_all(conn, NOW)
    return client, conn


def _chat(client, body: dict, token: str | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/api/chat", json=body, headers=headers)


def _uid(conn, external_id: str) -> int:
    return conn.execute("SELECT id FROM users WHERE line_user_id = %s", (external_id,)).fetchone()[0]


def test_needs_exactly_one_of_message_postback_location(seeded):
    client, _ = seeded
    sid = str(uuid.uuid4())
    bad = [
        {"session_id": sid},
        {"session_id": sid, "message": "น้ำจะถึงไหม", "postback": "triage:cancel"},
        {"session_id": sid, "message": "น้ำจะถึงไหม", "location": {"lat": 14.4, "lon": 100.4}},
        {"session_id": sid, "message": "   "},
        {"session_id": sid, "location": {"lat": 95, "lon": 100.4}},
        {"message": "น้ำจะถึงไหม"},
    ]
    for body in bad:
        r = _chat(client, body)
        assert r.status_code == 400, body
        assert r.json()["error"]["code"] == "bad_request"


def test_unknown_postback_is_400(seeded):
    client, _ = seeded
    r = _chat(client, {"session_id": str(uuid.uuid4()), "postback": "hack:me"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"


def test_question_about_a_station_returns_reply_sources_and_quick_replies(seeded):
    client, _ = seeded
    r = _chat(client, {"session_id": str(uuid.uuid4()), "message": "บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม"})
    assert r.status_code == 200
    body = r.json()
    assert "ingested_at" in body["meta"]
    d = body["data"]
    assert set(d) == {"reply", "sources", "quick_replies", "emergency", "ticket_id"}
    # The web draws HotlineNotice under answers with a station source: no text copy, no emoji (LINE keeps both).
    assert "T.1" in d["reply"] and "1784" not in d["reply"] and not chat._EMOJI.search(d["reply"])
    station = next(s for s in d["sources"] if s["type"] == "station")
    assert station["station_id"] == STATION and station["code"] == "T.1" and station["name_th"] == "สถานีทดสอบ"
    assert 0 < station["distance_km"] < 60 and station["observed_at"]
    assert any(s["type"] == "shelter" and s["name"] == "ศูนย์ทดสอบ" for s in d["sources"])
    assert d["emergency"] is False and d["ticket_id"] is None
    assert {q["type"] for q in d["quick_replies"]} >= {"location", "message"}
    assert all(len(q["label"]) <= 20 for q in d["quick_replies"])


def test_location_message_returns_nearest_station(seeded):
    client, _ = seeded
    r = _chat(client, {"session_id": str(uuid.uuid4()), "location": {"lat": 14.41, "lon": 100.46}})
    d = r.json()["data"]
    assert [s["code"] for s in d["sources"] if s["type"] == "station"] == ["T.1"]
    assert "T.1" in d["reply"]


def test_danger_message_is_emergency_and_creates_a_web_ticket(seeded):
    client, conn = seeded
    sid = str(uuid.uuid4())
    d = _chat(client, {"session_id": sid, "message": "ช่วยด้วย ติดอยู่บนหลังคาที่บางบาล อยุธยา"}).json()["data"]
    assert d["emergency"] is True and d["ticket_id"] is not None
    # EmergencyCard above the reply carries 1784/1669, so the text starts with the ticket itself.
    assert chat.RESCUE_HOTLINE not in d["reply"] and not chat._EMOJI.search(d["reply"])
    assert d["reply"].startswith(f"น้ำมาเตรียมคำขอความช่วยเหลือ #{d['ticket_id']}")
    channel, status, place = conn.execute(
        "SELECT channel, status, place_label FROM rescue_tickets WHERE id = %s", (d["ticket_id"],)).fetchone()
    assert (channel, status) == ("web", "unconfirmed") and "บางบาล" in place
    assert any(q.get("data") == "triage:confirm" for q in d["quick_replies"])


def test_triage_confirm_postback_confirms_the_ticket(seeded):
    client, conn = seeded
    sid = str(uuid.uuid4())
    tid = _chat(client, {"session_id": sid, "message": "ช่วยด้วย ติดอยู่บนหลังคา"}).json()["data"]["ticket_id"]
    d = _chat(client, {"session_id": sid, "postback": "triage:confirm"}).json()["data"]
    assert d["ticket_id"] == tid and d["emergency"] is True
    assert conn.execute("SELECT channel, status FROM rescue_tickets WHERE id = %s", (tid,)).fetchone() == ("web", "open")


def test_triage_cancel_postback_cancels_the_pending_ticket(seeded):
    client, conn = seeded
    sid = str(uuid.uuid4())
    tid = _chat(client, {"session_id": sid, "message": "ช่วยด้วย ติดอยู่บนหลังคา"}).json()["data"]["ticket_id"]
    d = _chat(client, {"session_id": sid, "postback": "triage:cancel"}).json()["data"]
    assert d["ticket_id"] is None and d["emergency"] is False
    assert conn.execute("SELECT status FROM rescue_tickets WHERE id = %s", (tid,)).fetchone()[0] == "cancelled"


def test_confirm_with_nothing_pending_is_not_a_ticket(seeded):
    client, _ = seeded
    d = _chat(client, {"session_id": str(uuid.uuid4()), "postback": "triage:confirm"}).json()["data"]
    assert d["ticket_id"] is None


def test_consent_postback_writes_consent_and_saves_the_fact(seeded):
    client, conn = seeded
    sid = str(uuid.uuid4())
    d = _chat(client, {"session_id": sid, "message": "ยายติดเตียง"}).json()["data"]
    yes = next(q for q in d["quick_replies"] if q.get("data") == "consent:sensitive_memory:yes")
    assert yes["type"] == "postback" and yes["display_text"] == yes["label"]
    assert any(q.get("data") == "consent:sensitive_memory:no" for q in d["quick_replies"])

    assert _chat(client, {"session_id": sid, "postback": "consent:sensitive_memory:yes"}).status_code == 200
    uid = _uid(conn, f"web:{sid}")
    assert conn.execute("SELECT granted FROM consents WHERE user_id = %s ORDER BY ts DESC LIMIT 1",
                        (uid,)).fetchone()[0] is True
    assert conn.execute("SELECT household FROM user_memory WHERE user_id = %s", (uid,)).fetchone()[0] == {"immobile": ["ยาย"]}


def test_consent_no_postback_records_refusal_and_saves_nothing(seeded):
    client, conn = seeded
    sid = str(uuid.uuid4())
    _chat(client, {"session_id": sid, "message": "ยายติดเตียง"})
    assert _chat(client, {"session_id": sid, "postback": "consent:sensitive_memory:no"}).status_code == 200
    uid = _uid(conn, f"web:{sid}")
    assert conn.execute("SELECT granted FROM consents WHERE user_id = %s ORDER BY ts DESC LIMIT 1",
                        (uid,)).fetchone()[0] is False
    assert conn.execute("SELECT household FROM user_memory WHERE user_id = %s", (uid,)).fetchone()[0] == {}


def test_memory_delete_all_postback_forgets(seeded):
    client, conn = seeded
    sid = str(uuid.uuid4())
    _chat(client, {"session_id": sid, "message": "บ้านแม่อยู่บางบาล อยุธยา น้ำจะถึงไหม"})
    uid = _uid(conn, f"web:{sid}")
    assert conn.execute("SELECT count(*) FROM user_locations WHERE user_id = %s", (uid,)).fetchone()[0] == 1
    assert _chat(client, {"session_id": sid, "postback": "memory:delete_all"}).status_code == 200
    assert conn.execute("SELECT count(*) FROM user_locations WHERE user_id = %s", (uid,)).fetchone()[0] == 0


def test_signed_in_user_shares_the_line_identity(seeded):
    client, conn = seeded
    token = login(client, "user")
    sid = str(uuid.uuid4())
    assert _chat(client, {"session_id": sid, "message": "สวัสดี"}, token=token).status_code == 200
    assert conn.execute("SELECT count(*) FROM messages m JOIN users u ON u.id = m.user_id "
                        "WHERE u.line_user_id = 'demo:user' AND m.channel = 'web'").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM users WHERE line_user_id = %s", (f"web:{sid}",)).fetchone()[0] == 0
    r = _chat(client, {"session_id": sid, "message": "สวัสดี"}, token="not-a-token")
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_token"


def test_agent_failure_gives_the_fallback_reply(seeded, monkeypatch):
    client, _ = seeded

    def boom(*args, **kwargs):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(chat, "handle", boom)
    d = _chat(client, {"session_id": str(uuid.uuid4()), "message": "ช่วยด้วย น้ำเข้าบ้าน"}).json()["data"]
    assert d["emergency"] is True and "1784" in d["reply"] and d["ticket_id"] is None


def test_rate_limit_is_20_per_minute_per_session(seeded):
    client, _ = seeded
    sid = str(uuid.uuid4())
    for _ in range(20):
        assert _chat(client, {"session_id": sid, "message": "สวัสดี"}).status_code == 200
    r = _chat(client, {"session_id": sid, "message": "สวัสดี"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    assert _chat(client, {"session_id": str(uuid.uuid4()), "message": "สวัสดี"}).status_code == 200
